# Parapper-ASR / whisper-flow 調査と改善方針（2026-08-23）

対象コミット: `c6f70f6` / ブランチ `localServer2` / テスト基準線 **127 passed**

本書は「実装前に必ず出力するもの」に対応する。数値は実コードと
`docs/bench/2026-07-07-bench.json`（開発機 Ryzen 7 8840U）から導出したもので、
未計測のものには明示的に「未計測」と書く。

---

## 1. Current Architecture

単一プロセス Python / FastAPI / uvicorn。HTTP(8000, 生徒) と HTTPS(8443, 先生) を
同時リッスン。ルームは**単一固定**（`Session` 1個、4桁 join code）。

| 層 | 実体 | ファイル |
|----|------|---------|
| 音声取込 | AudioWorklet → 16k mono PCM16 → WS binary | `web/audio-worklet.js`, `server/audio/ingest.py` |
| 発話分割 | Silero VAD(ONNX 直叩き, 32ms) + 状態機械 | `server/audio/vad.py` |
| ASR | faster-whisper small int8, beam=1 | `server/asr/fw_engine.py` |
| 幻覚除去 | 4指標 + 既知フレーズ辞書 | `server/asr/hallucination.py` |
| 翻訳 | Hy-MT2-1.8B Q4_K_M / llama.cpp | `server/mt/hymt_engine.py` |
| 配信 | 言語別 broadcast + 先生 asr_final | `server/pipeline.py` |
| 状態 | seq / history deque(50) / clients | `server/session.py` |
| 契約 | pydantic discriminated union | `server/ws_protocol.py` |

ワーカーは `asr-worker` / `mt-worker` / `stats-worker` の 3 asyncio タスク。
実推論は各 `ThreadPoolExecutor(1)` へ逃がす（推論中の GIL 解放が前提）。

---

## 2. Current Data Flow

```
Teacher browser
  getUserMedia{EC,NS,AGC} → AudioContext(48k) → AudioWorklet
  線形補間で16kへリサンプル → int16 → 1600 sample(100ms/3200B) ごとに postMessage
  ↓ ws.send(ArrayBuffer)   ※ sessionState==="live" のときだけ送る
FastAPI /ws (binary frame)
  ↓ pipeline.feed_audio()      ← ここで await queue.put する（問題 A-2）
np.frombuffer(int16) → VoiceSegmenter.feed()
  32ms=512sample フレーム化 → SileroVAD.is_speech()
  ├ 発話開始: 音声フレーム 1枚 で開始（+pre_roll 240ms=7枚を前置）
  ├ 発話終了: 無音 500ms(16枚) 継続
  └ 強制分割: 30s
  ↓ Segment(pcm, t_start, t_end, closed_at)
asyncio.Queue(maxsize=4)                      ← 有界 OK
  ↓ asr-worker → ThreadPoolExecutor(1)
faster-whisper small int8 / beam=1 / vad_filter=False / condition_on_previous_text=False
  ↓ hallucination_reason() で破棄判定
Utterance(seq=next_seq())
  ├ session.add_history(deque maxlen=50)
  ├ recorder.add()（記録ON時のみ）
  └ send_to_teacher(asr_final)
  ↓ for lang in sorted(session.active_langs()):     ← 言語グルーピング OK
asyncio.PriorityQueue()   ← maxsize 指定なし＝無制限（問題 A-1）
  ↓ mt-worker → ThreadPoolExecutor(1)
Hy-MT2 create_chat_completion(temp=0.7, top_p=0.6, top_k=20, max_tokens=512, n_threads=4)
  ↓ Caption(seq, ja, text, lang, delay_ms)
broadcast_caption(): for student in students: await ws.send_json()   ← 直列（問題 A-3）
  ↓
Student browser: seq昇順挿入 / 重複排除 / 連続確定watermark / 指数バックオフ再接続
```

**partial 字幕は一切存在しない。** 先生画面の `asr_final` も生徒カードも確定発話のみ。

---

## 3. Current Problems（実コードから確認できたもの）

### P0

| # | 問題 | 根拠 |
|---|------|------|
| A-1 | **MTキューが無制限** | `pipeline.py:97` `asyncio.PriorityQueue()` に `maxsize` なし。言語数×発話数で単調増加しうる |
| A-2 | **ASRキュー満杯が先生WSの受信ループを止める** | `feed_audio` の `await self._asr_queue.put(segment)` は `ws_endpoint` の受信ループ内。満杯時は音声だけでなく `control`(pause/end) も読めなくなる |
| A-3 | **遅い生徒1人が全員を止める** | `broadcast_caption` / `_broadcast_all` が `await ws.send_json` を直列実行。1接続のTCP輻輳が mt-worker を止め、他生徒・翻訳キューまで停滞させる |
| A-4 | WS の Origin 検証・受信サイズ上限・送信タイムアウトが無い | `main.py` `ws_endpoint` にいずれも無し |
| A-5 | `audio_queue_seconds` を測っていない | `queue_depth` は `qsize()` の和のみ。1発話は 0.3〜30s と幅があるため件数では過負荷を表現できない |

### P1

| # | 問題 | 根拠 |
|---|------|------|
| B-1 | **partial字幕が無い** | 体感遅延＝発話長＋ASR＋MT。6秒の文なら発話開始から約8秒無反応 |
| B-2 | **区切りが無音のみ** | `min_silence_ms=500` 一本。「例えばですね……これは……」が2枚に割れる。文法境界判定なし |
| B-3 | **発話開始が1フレーム(32ms)** | VAD誤検知1枚で発話が立ち上がる。Parapper は 96ms(3枚)を要求している |
| B-4 | **翻訳キャッシュ・重複排除が無い** | 同一文の再翻訳が毎回 Hy-MT2 を叩く（1回あたり中央値 668〜846ms） |
| B-5 | **Hy-MT2 がサンプリング推論** | `temperature=0.7` で非決定的。翻訳では貪欲法が同等以上のことが多く、決定的ならキャッシュとテストが素直になる（要実測） |
| B-6 | **CPUスレッドの二重確保** | llama.cpp `n_threads=4` は明示、CTranslate2 側は `cpu_threads` 未指定＝全コア使用。競合時 ASR デコード中央値が 1.67s→2.52s（+51%）に悪化しているのはこれが原因の可能性が高い |
| B-7 | `max_tokens=512` | ASR が壊れた入力を出したとき暴走生成で数秒使う余地がある |

### 数値的に最も重要な発見: Whisper の固定コスト

fixture 10本の `audio_s` と `decode_s` を直線で当てると

```
small  : decode ≈ 1.29 + 0.074 × audio_s    → 固定費 1.29s
kotoba : decode ≈ 5.80 + 0.077 × audio_s    → 固定費 5.80s
```

音声1秒あたりの限界コストはわずか **74ms**、残りは**発話長に依存しない固定費**。
Whisper が常に 30 秒へパディングしてエンコードする仕様の直接の帰結である。
（small の実測: 3.08秒の音声で 1.540s、8.24秒の音声で 1.912s。音声が 2.7倍でも
デコードは 1.24倍にしかならない。）

ここから3つの重い含意が出る。

1. **kotoba が判断ゲート①で落ちたのはモデルが悪いからではなく、encoder の固定費が
   5.8s だから。** 長尺の事後書き起こしなら kotoba が有利、という bench の所見と整合する。
2. **partial を Whisper の再実行で作ると 1回あたり 1.3s の CPU を食う。**
   窓を短くしても安くならない。whisper-flow 方式（窓を伸ばしながら毎周期再認識）は
   CPU ノートPCでは成立しない。
3. **発話を細かく割るほど総CPUが増える。** 「はい」1秒でも 1.36s かかる。
   つまり文法境界で不自然な分割を減らす改善は、**品質と CPU の両方**に効く。

### P2 / P3

- 先生UIに**マイク選択が無い**（`getUserMedia` はデバイス指定なし）。ミッションの先生UI要件に不足。
- `queue_depth` は表示されるが、ASR遅延と翻訳遅延の分離表示が無い。
- `get_lan_ip()` が `8.8.8.8:80` へ UDP connect する。実パケットは出ないが、
  「外部通信ゼロ」を機械検証する際に必ず引っかかる書き方。
- 冷キャッシュ時に `transformers` の import が pytest の 20s タイムアウトを超えることがある
  （再実行で緑。実測: 1回目タイムアウト → 2回目 127 passed / 37.8s）。

---

## 4. Parapper Findings

MIT（本体）。ASRモデルは Apache-2.0(ReazonSpeech K2 v2) / CC-BY-4.0(Parakeet) 等で個別。
Silero VAD は MIT、Namo Turn Detector v1 は Apache-2.0。

### DIRECTLY REUSABLE（設計をそのまま持ち込める）

**R-1: turn / segment の二層モデル**
`Segment` = ASR に投げる音声1単位。`Turn` = 複数 Segment を束ねた発話。
`TurnDraft`(可変) と `TurnConfirmed`(不変) を分ける。
→ 現在の `Utterance` は Segment と Turn が同一視されている。分離すれば
「無音で切れたが文法的には続いている」を Turn 側で吸収できる。

**R-2: 短無音による interim 発火（実際の既定値）**

```
vad_interval_ms           = 32     （当アプリと同じ）
vad_threshold             = 0.5    （当アプリと同じ）
segment_start_speech_ms   = 96     （当アプリは 32 相当）
interim_result_enabled    = true
interim_result_silence_ms = 96     ← 96ms の無音で interim ASR を発火
check_silence_ms          = 320    ← 320ms の無音で segment を閉じ turn 判定へ
namo_confidence_threshold = 0.8
namo_context_max_tokens   = 256
rerecognize_full_on_complete = false
asr num_threads           = 4
```

タイマー駆動ではなく**微小な息継ぎを引き金にする**ので、partial の回数が
発話のリズムに比例し、CPU 予算が読める。当アプリの 500ms より短い 320ms を
採れるのは、その後段に文法境界判定があって過分割を回収できるから。

**R-3: 文法境界の5分類**（`documents/developer/japanese_separate_rule.md`）

| クラス | 内容 | 判定 |
|-------|------|------|
| StrongEnd | 終助詞・句点/！/？ | Namo を呼ばず即確定 |
| PredicateEnd | 動詞・形容詞・助動詞の終止形 | 即確定 |
| NormalEnd | 名詞・代名詞・接尾辞・感動詞 | Morph戦略なら**継続**、Namo戦略なら Namo に委ねる |
| ClauseWeak | 読点、「ので/けど/から/し」 | 継続 |
| Reject | 助詞・条件形・連体形 | 継続 |

**末尾候補のみが確定判定の対象**で、文中の境界では絶対に分割しない、という不変条件が要。
実装は Vibrato(Rust) + Unidic で、素性を4桁ASCIIに符号化して照合している。

**R-4: 3段階の戦略スイッチ**（`TurnDetector::{Simple, Morph, Namo}`、既定 `Simple`）
同じパイプラインで VAD only / VAD+文法 / VAD+文法+Namo を切り替えられる。
ミッションが求める A/B/C 比較構造がそのまま実現できる。

**R-5: ストリーミングプロトコルの turn イベント**
（`documents/developer/streaming-recognition-protocol-v1.md`）

```
turn.partial : { turn_session_id, revision:N, ... }   revision は turn 内で単調増加
turn.final   : { turn_session_id, revision:N, audio_duration_ms:M, ... }  不変
speech.started
error        : { code, message, fatal:true }
```

固定エラーコードに `model_unavailable` がある＝**fail closed が仕様化されている**。
音声フレームは pcm_s16le / 16000Hz / mono 固定、最大 3200B(100ms)、**推奨 1024B(32ms)**。

**R-6: 音声キューの秒数上限と「黙って捨てない」方針**

> Application queue limit: ~2 seconds; overflow is **fatal (no silent drop)**

キュー上限を**件数でなく秒数**で定義し、溢れたら黙って捨てずセッションを落とす。
ミッションの `audio_queue_seconds` 要求とバックプレッシャ方針にそのまま合致する。

**R-7: 単一 in-flight 制約と epoch による stale 検出**
ASR リクエストは常に高々1本。結果を適用する前に request identity を照合し、
古い結果は下流へ流さない。partial/final の不整合を構造で防いでいる。

### DESIGN INSPIRATION（考え方だけ借りる）

- **I-1: 診断バイナリ群** `replay_mock_recognition.rs` / `verify_jvs_asr.rs` /
  `mock_ync_server.rs`。JVS コーパスで ASR を機械検証し、モックで遅延応答を注入する。
  当アプリの `scripts/replay_client.py` は既に近いが、**ASR 精度(CER)の回帰検証**が無い。
- **I-2: delivery の sink 分離**（`delivery/sinks/*`）。同一イベントを UI / HTTP / OSC へ
  扇形配信する。当アプリの言語別 fan-out に構造は流用できるが、現状で足りているため優先度は低い。
- **I-3: `translation/local/cache.rs` の存在自体**（翻訳キャッシュを持っている）。

### NOT SUITABLE

- **N-1: Rust / Tauri 構成そのもの。** Parapper は単一ユーザーのデスクトップ常駐アプリで、
  ルーム・生徒 fan-out・再接続・履歴復元・join code といった当アプリの中核が**存在しない**。
  移植して得るものより失うものが大きい。
- **N-2: `recognition_busy`（同時セッション1本）**。当アプリは先生1本なので実害は無いが、
  設計思想として持ち込む必要はない。
- **N-3: OSC / VRChat / YNC 連携、TTS(synthesis)**。用途外。
- **N-4: Vibrato + Unidic 形態素解析器そのもの**。Rust 実装。Python 等価物は
  fugashi/MeCab か SudachiPy になるが、**当アプリの ASR は句読点付きテキストを出す**ため、
  StrongEnd 判定は形態素解析なしで大半が取れる。まず解析器なしで実装し、
  効果不足が実測で示されたときだけ導入する。

---

## 5. whisper-flow Findings

MIT (c) 2024 Dima Statz。約 250 行の小さなプロジェクト。

### 実際のアルゴリズム（`whisperflow/streaming.py` 実物）

```python
while not should_stop[0]:
    await asyncio.sleep(0.01)
    window.extend(get_all(queue))
    window = trim_window(window, config.MAX_WINDOW_CHUNKS)   # 既定 1000 chunk
    data = await safe_transcribe(transcriber, window)        # 窓全体を毎周期再認識
    ...
    if should_close_segment(result, prev_result, cycles):    # 同一テキストが2周連続
        window, prev_result, cycles = [], {}, 0
        result["is_partial"] = False
```

**tumbling window ではなく growing window。** 窓を伸ばしながら毎周期まるごと再認識し、
**テキストが変化しなくなったこと**をもって確定する（VAD を使っていない）。
既定値は `SAMPLE_RATE=16000` / `CHUNK_SIZE=1024` / `TRANSCRIBE_TIMEOUT=30.0` /
`MAX_WINDOW_CHUNKS=1000` / `MAX_SESSIONS=128` / `MAX_UPLOAD_BYTES=25MB`。

### DIRECTLY REUSABLE

- **W-1: `safe_transcribe` の設計** — 推論を `asyncio.wait_for(timeout)` で包み、
  タイムアウトと例外を吸収して `None` を返し、ループを止めない。当アプリの
  `_process_segment` は例外は握るが**タイムアウトが無い**。llama.cpp / CTranslate2 が
  病的入力で長時間返らない場合、現状はワーカーが無限に固まる。
- **W-2: `/health`(liveness) と `/ready`(readiness) の分離** — 当アプリは `/healthz` 一本で、
  モデル未ロード時に 503。liveness と readiness が同一なので「生きているが未準備」を
  区別できない。分離は安価で有益。
- **W-3: `lifespan` での `stop_all_sessions()`** と `finally` での確実な `session.stop()`。
  当アプリは概ね同等だが、`stop()` が `executor.shutdown(wait=False)` で
  **実行中の推論スレッドを待たない**。
- **W-4: `MAX_SESSIONS` / `MAX_UPLOAD_BYTES` の明示上限**。当アプリは生徒数・
  メッセージサイズの上限が無い。

### DESIGN INSPIRATION

- **W-5: partial/final を同一メッセージの `is_partial` フラグで表す**素直な契約。
  ただし Parapper の `turn.partial`/`turn.final` + `revision` の方が
  再接続・順序保証の点で優れているので、採るなら Parapper 側。
- **W-6: `tests/benchmark/`** — LibriSpeech ベース。**日本語評価には使えない**が、
  「ベンチをテストとして CI に置く」構えは参考になる。

### NOT SUITABLE

- **NW-1: growing window の再認識ループ。** 上記の固定コスト分析により、
  Whisper では窓長に関係なく 1 パス ≈ 1.3s。10ms ループで回せば CPU を焼き切る。
  **CPU ノートPCでは採用不可。**
- **NW-2: テキスト安定性による確定判定。** VAD を持たないので確定が最短でも
  2 周期かかり、無音でも回り続ける。当アプリの VAD 起点の方が明確に優れている。
- **NW-3: 無制限の `queue.Queue`。** 当アプリの要件に反する。
- **NW-4: `whisperflow/models/tiny.en.pt` の同梱**（英語専用）。

---

## 6. Parapper vs whisper-flow 比較表

| 項目 | Parapper-ASR | whisper-flow | 現アプリ |
|------|-------------|--------------|---------|
| streaming方式 | VAD起点の segment + turn 二層 | growing window の毎周期再認識 | VAD起点の発話単位のみ |
| partial字幕 | 96ms無音で interim 発火 | 毎周期 `is_partial=True` | **無し** |
| final字幕 | 320ms無音→文法/Namo判定→確定 | テキスト安定で確定 | 500ms無音で確定 |
| VAD | Silero (32ms, 0.5) | 無し | Silero (32ms, 0.5) |
| Turn Detection | Simple/Morph/Namo の3段切替 | 無し | 無し |
| 日本語適性 | 高（ReazonSpeech K2 既定・文法境界・Namo日本語版） | 低（tiny.en 同梱） | 中（whisper small は教科用語を誤る） |
| CPU適性 | 高（transducer は O(音声長)） | 低 | 中（固定費 1.3s/発話） |
| Python統合 | 不可（Rust）。ただし engine の sherpa-onnx には公式 Python wheel あり | そのまま | — |
| FastAPI統合 | 別プロセス+WS protocol なら可 | 直接流用可 | — |
| WebSocket設計 | version付き・状態機械・固定エラーコード・fatal明示 | 最小限 | 中（pydantic契約は良い、上限/Origin検証なし） |
| queue設計 | **秒数**上限・溢れたら fatal | 無制限 | ASR有界(4件) / MT無制限 |
| session lifecycle | AwaitingStart→Active→Draining→Done | ctor でタスク起動・`stop()` で join | lifespan + 明示 control |
| reconnect | 無し（デスクトップ単一） | 無し | **有り（seq/watermark/差分復元）— 現アプリが最良** |
| test | 単体/統合/システム/pipeline_tests の多層 | 単体+benchmark | 単体+統合 127件 |
| benchmark | JVS 検証バイナリ | LibriSpeech | bench.py + replay_client.py（速度中心、**CERなし**） |
| deployment | Tauri インストーラ | Docker | start.bat + setup.ps1（学校向けに最良） |
| 保守複雑度 | 高（281ファイル・Rust） | 低 | 低（約2,900行） |
| license | MIT（モデルは個別） | MIT | — |

**結論: 全面採用すべき側は無い。** 当アプリが優れている領域（room / reconnect / history /
言語グルーピング / 学校向け配布）は維持し、Parapper からは**設計と数値**を、
whisper-flow からは**堅牢化の作法**を取る。

---

## 7. Gap Analysis と ASR エンジンの論点

| 成功条件 | 現状 | 差分の性質 |
|---------|------|-----------|
| 完全ローカル/APIキー不要/GPU不要/cloud送信ゼロ | 達成 | 機械検証の仕組みだけ不足 |
| Hy-MT2-1.8B 維持 | 達成 | 使い方の最適化余地（B-4〜B-7） |
| 生徒人数で ASR 回数が増えない | 構造的に保証 | 自動テストが無い |
| 同一言語人数で MT 回数が増えない | `active_langs()` で保証 | 自動テストが無い |
| reconnect で復元 | 実装済み | — |
| queue が無制限に増えない | **未達（MTキュー）** | P0 |
| 日本語ASRが高品質 | **教科用語を誤る** | 要エンジン再検討 |
| 発話区切りが自然 | **未達（無音のみ）** | P1 |
| partial字幕が低遅延 | **存在しない** | P1 |
| CPU overload しにくい | スレッド二重確保 | P1 |
| 長時間安定 | 未検証（45分試験は未実行、`docs/accept/` が空） | 要実施 |

### ASR エンジンの論点（最重要）

現行 whisper-small の日本語誤りは、授業内容として看過しにくい水準にある。

| 原文 | small の出力 |
|------|------------|
| 光合成 | **構合性 / 交合性** |
| 葉緑体 | **用力体** |
| でんぷん | **電分** |
| 器官 | **機関** |

Parapper が日本語既定に据えている **ReazonSpeech K2 v2**（Apache-2.0）は、
日本語大規模コーパスで学習された transducer 系モデルで、`sherpa-onnx` 経由で使う。
sherpa-onnx には **Windows CPU 向けの Python wheel** があり、Rust を持ち込まずに
Parapper と同じ engine + 同じモデルを使える見込みがある。理論上の利点は3つ:

1. **コストが音声長に比例する**（30秒パディングが無い）。partial が現実的な価格になる。
2. **日本語特化**なので教科用語の誤りが減る見込み。
3. **transducer は無音で幻覚しにくい**。幻覚フィルタへの依存が下がる。

ただしこれは**すべて仮説**であり、ミッションの要求どおり**日本語実測（CER/RTF）で
判定する**。判定に落ちたら whisper-small のまま partial 設計を組み直す。

未検証の重要な前提: **ReazonSpeech K2 v2 の出力に句読点が付くか。**
付かない場合、文法境界の StrongEnd 判定が使えず形態素解析器が必要になり、判断が変わる。
ベンチの最初に確認する。

---

## 8. Architecture Options

| | 構成 | latency | CPU | RAM | Windows配布 | offline | 開発複雑度 | 保守 | debug |
|--|------|---------|-----|-----|------------|---------|-----------|------|-------|
| **A** | 現状維持(Python+FastAPI+faster-whisper) | 固定費1.3s/発話、partial不可 | 中 | 2.6GB | 既存で完成 | 可 | 最小 | 良 | 良 |
| **B** | Python+FastAPI+**sherpa-onnx**(ReazonSpeech K2) | O(音声長)、partial 実現可 | 低（見込み） | +0.3GB程度 | pip wheel | 可 | 小（engine差し替え） | 良 | 良 |
| **C** | Python web + Parapper を local subprocess | 低 | 低 | 二重モデル | Tauri配布物を同梱 | 可 | 中〜大 | 二重運用で悪 | 悪 |
| **D** | Rust backend + browser frontend | 低 | 低 | 低 | 要ビルド基盤 | 可 | **極大**（room/reconnect/history 全書き直し） | 悪 | 悪 |
| **E** | ASR だけ Rust 拡張 | B と同等 | B と同等 | — | ビルド環境要 | 可 | 大 | 悪 | 悪 |

**A と B は同一アーキテクチャで engine を差し替えるだけ**なので、B は「アーキテクチャ変更」
ではなく「既存の `ASREngine` 抽象への実装追加」に収まる。`server/asr/base.py` の
シームが既にあることが決定的に効く。

C/D/E は、Parapper が持たない機能（room・生徒 fan-out・reconnect・履歴・join code・
学校向け start.bat 配布）を全部書き直す代償に見合う実測上の利点が**現時点で存在しない**。
YAGNI の原則どおり却下する。

---

## 9. Recommended Architecture

**Python + FastAPI + single process + browser frontend を維持する（Option A → B）。**

理由:

- 重い計算は既に native（CTranslate2 / ONNX Runtime / llama.cpp）にあり、
  Python は接着剤にすぎない。推論中は GIL が解放されるので Rust 化の利得が小さい。
- Parapper の価値は Rust ではなく**アルゴリズムと数値**にある。それは Python で再実装できる。
- 学校配布（`start.bat` ダブルクリック）という最重要の非機能要件を、現構成が最もよく満たしている。

目標データフロー（ミッションの仮説を実測に照らして修正したもの）:

```
Teacher Browser → AudioWorklet(16k PCM16) → WS
  → Audio ingress (bounded by SECONDS, not count)
  → Silero VAD (32ms, 0.5)
  → Segment builder (start=96ms speech / close=320ms silence)
      ├→ [interim] 96ms 無音で interim ASR  ※engine が O(音声長) の場合のみ既定ON
      └→ [close]  Segment 確定
  → Turn state machine (turn_id, revision)
      ├ StrongEnd/PredicateEnd → Turn 確定
      ├ NormalEnd/ClauseWeak/Reject → Turn 継続（次 Segment を連結）
      └ timeout = check_silence_ms × 2 で強制確定
  → Final Utterance (seq)
  → Translation cache (LRU, bounded)  ← miss のみ下へ
  → Hy-MT2 (active language groups only, 1言語1回)
  → Room Broker (per-client 送信キュー / 遅い生徒を隔離)
  → History (deque) / seq
  → Student WebSockets
```

ミッションの仮説図から変えた点は3つ:

1. **「Streaming ASR / Partial」→「Final ASR」の二段構成を無条件には採らない。**
   Parapper が interim と completion に**同一モデル**を使っている
   （`AsrModelCapability::CompletionAndInterim`）ことが確認できた。モデルを2つ載せる必要はない。
2. **Translation cache を Hy-MT2 の前に置く**（仮説図では後ろだった）。後ろでは意味がない。
3. **Room Broker に per-client 送信キューを入れる**（A-3 の修正）。

---

## 10. Implementation Plan

巨大な rewrite を避け、**1改善＝1コミット、各段で bench と 127 テストの緑を確認**する。

### Step 0 — Baseline（コード変更なし）

学校配布前提の数値を取り直す。`scripts/bench.py` に CER/WER を追加し、
現行 whisper-small の日本語 CER を確定させる。`replay_client.py --minutes 2` を実走させ、
`audio_queue_seconds`・CPU・RSS の基準線を取る。

### Step 1 — P0 の是正（機能追加なし・低リスク）

1. MTキューを有界化し、溢れ時は**確定発話を捨てず**先生へ過負荷を通知
2. 音声取込を「秒数」で有界化し、`feed_audio` が WS 受信ループを止めないようにする
3. `broadcast` を per-client 送信キュー + 送信タイムアウト化（遅い生徒の隔離）
4. WS の Origin 検証・受信サイズ上限
5. 推論呼び出しを `asyncio.wait_for` で包む（whisper-flow W-1）
6. `audio_queue_seconds` を stats に載せる

あわせて**不変条件の自動テスト**（ASR回数・MT回数・録音OFF=0バイト・外部通信0件）を追加する。

### Step 2 — Hy-MT2 の使い方の最適化（モデルは不変）

1. bounded LRU 翻訳キャッシュ（key: text_ja, lang, engine, model_version）
2. 貪欲デコード（temperature=0）と現行サンプリングを実測比較
3. CTranslate2 `cpu_threads` と llama.cpp `n_threads` の組合せをベンチし、
   oversubscription を解消
4. `max_tokens` を入力長基準に

### Step 3 — 発話区切り（VAD only → VAD + 文法境界）

`TurnDetector` 相当の設定を入れ、`simple`(現行) / `morph` を切替可能にする。
`simple` を既定のまま出荷し、実測で morph が勝ったら既定を変える。
`segment_start_speech_ms=96` の導入もここ。

### Step 4 — ASR エンジンの判定（判断ゲート②）

sherpa-onnx + ReazonSpeech K2 v2 を `ASREngine` 実装として追加し、
**同一 fixture で CER / RTF / 固定費 / RSS / 句読点有無**を whisper-small と比較。
勝った方を既定にする。負けたら実装は残して既定は変えない。

### Step 5 — partial 字幕（Step 4 の結果に依存）

engine が O(音声長) なら interim を実装。Whisper のままなら
「1発話につき1回だけ、320ms 無音の時点で interim」など回数を絞った設計に落とす。

### Step 6 — 長時間試験と最終レビュー

45〜60分連続。memory / queue / latency drift / dropped chunks を確認。

---

## 11. Files To Change（見込み）

| ファイル | 変更内容 | Step |
|---------|---------|------|
| `server/pipeline.py` | キュー有界化・per-client送信・timeout・stats拡張 | 1 |
| `server/main.py` | Origin検証・サイズ上限・/ready 分離 | 1 |
| `server/session.py` | per-client 送信キュー | 1 |
| `server/ws_protocol.py` | stats フィールド追加、（Step5で partial メッセージ） | 1,5 |
| `server/mt/cache.py`（新） | bounded LRU | 2 |
| `server/mt/hymt_engine.py` | デコード設定・max_tokens | 2 |
| `server/asr/fw_engine.py` | cpu_threads | 2 |
| `server/config.py`, `config.yaml` | 新設定項目 | 1-5 |
| `server/audio/vad.py` | segment_start_speech_ms | 3 |
| `server/turn/`（新） | 文法境界・turn 状態機械 | 3 |
| `server/asr/sherpa_engine.py`（新） | sherpa-onnx 実装 | 4 |
| `scripts/bench.py` | CER/WER、ASRエンジン比較、区切りベンチ | 0,3,4 |
| `scripts/download_models.py` | ReazonSpeech K2 取得（setup時のみ） | 4 |
| `tests/unit/`, `tests/integration/` | 不変条件テスト群 | 1- |
| `web/teacher.js`, `web/teacher.html` | マイク選択・遅延内訳表示・partial | 1,5 |

---

## 12. Verification Plan

### 不変条件（自動テスト化する）

- 生徒 1/10/20/40 人でも `asr_engine.calls` が発話数と等しい
- 英語15人でも 1発話あたり Hy-MT2 の en 呼び出しは 1回
- 記録OFF で書き出しバイト数 0
- runtime 中の外部 socket 接続 0件（`socket.socket` を monkeypatch して検出）
- reconnect で欠落 final が復元される（履歴内のとき）
- MT/ASR キューが上限を超えない

### ベンチ（before/after を必ず残す）

- ASR: CER, WER, RTF, 固定費, model load time, peak RSS
- 区切り: 字幕カード数, 平均文字数, 不自然分割数, endpoint latency, Hy-MT2 呼び出し回数
  （A: VAD only / B: VAD+文法 / C: VAD+文法+Namo）
- E2E: first partial latency, final latency, audio_queue_seconds, CPU, RSS

### プライバシー

`runtime` プロファイルでの外部接続 0 をテストで機械判定する。
`get_lan_ip()` の `8.8.8.8` UDP connect は誤検知源なので、
検証に引っかからない実装（インターフェース列挙）へ書き換える。

---

## 13. 未決事項（実装前に確認が必要）

1. **ASRエンジン差し替えの可否**（sherpa-onnx + ReazonSpeech K2 を候補に入れるか）。
   setup に新規モデル取得と pip 依存が1つ増える。
2. **partial 字幕を生徒に出すか**。翻訳は final のみという方針なので、生徒に出せるのは
   **未翻訳の日本語**になる。英語選択の生徒には無意味な可能性が高い。
3. **日本語ベンチの正解データ**。現 fixture は SAPI 合成音声10文。実教室録音があれば
   CER の信頼度が段違いに上がる。

---

## 14. OSS ライセンス整理

| 対象 | ライセンス | 本件での扱い |
|------|-----------|------------|
| Parapper-ASR 本体 | MIT | **コードは複製しない**。設計・数値のみ参照（出典を明記） |
| whisper-flow 本体 | MIT (c) 2024 Dima Statz | 同上。作法のみ参照 |
| ReazonSpeech K2 v2 | Apache-2.0 | 採用可（要判定） |
| Silero VAD | MIT | 使用中（faster-whisper 同梱） |
| Namo Turn Detector v1 | Apache-2.0 | 採用可（優先度低） |
| Hy-MT2-1.8B | Apache-2.0 | 継続使用 |
| NLLB-200 | CC-BY-NC 4.0 | 非商用限定。既定ではない |
