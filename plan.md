# LinguaBridge — 授業リアルタイム翻訳システム 実装計画（plan.md）

作成日: 2026-07-07 / 対象ブランチ: `localServer2` / ステータス: 計画（コード未着手）

---

## 1. Project Overview / Goals

### 概要
先生が話す日本語の授業音声を、同一LAN上のWindows 11機（ローカルサーバー）でリアルタイムに音声認識・機械翻訳し、生徒それぞれが自分の端末（Chromebook / iPhone 等）のブラウザで選択した言語（英語・中国語ほか）の字幕として閲覧できるシステム。

### ゴール
- 日本語が不得手な生徒が授業内容を自分の言語で追え、授業についていけるようにする
- **完全ローカル動作**: APIキー不要・クラウド不要・無料・高価なGPU不要（CPU推論のみ）
- 生徒端末は「表示するだけ」の軽量クライアントとし、低スペック端末（4GB Chromebook / iPhone）でも確実に動く
- 2026年9月（2学期）の授業投入を目標とする

### 非ゴール（今回やらないこと）
- 文化祭デロリアン企画（別プロジェクト・別planとする）
- 生徒の発話の認識・翻訳（先生の音声のみを扱う）
- 複数教室での同時利用（サーバーCPU性能上、1授業のみ）
- 翻訳音声の読み上げ（テキスト表示のみ。TTSは将来検討）

---

## 2. Requirements

### Functional Requirements（機能要件）

| ID | 要件 |
|----|------|
| F-01 | 先生は任意端末のブラウザから「先生ページ」を開き、マイク音声をサーバーへストリーミング送信できる |
| F-02 | サーバーは日本語音声を発話単位で文字起こしする（ASR） |
| F-03 | サーバーは文字起こし結果を、接続中の生徒が選択している全言語へ翻訳する（言語ごとに1回、生徒ごとではない） |
| F-04 | 生徒はQRコードまたはURL＋**4桁参加コード**でセッションに参加し、言語（英語・中国語簡体字。設定で拡張可）を選択できる |
| F-05 | 生徒画面には確定発話単位で翻訳がカード形式で追加表示される（自動スクロール、手動スクロールで追従一時停止、履歴遡り可） |
| F-06 | 生徒画面で日本語原文の併記をON/OFFでき、文字サイズを大/中/小で変更できる |
| F-07 | 生徒画面のUI文言は選択言語に追従する（ja/en/zh のUIリソース） |
| F-08 | 先生ページには、QRコード・参加URL・参加コード・接続生徒数・言語内訳・日本語文字起こしのライブ表示・遅延/キュー状況を表示する |
| F-09 | 先生は配信の開始/一時停止/終了を操作できる |
| F-10 | 先生がトグルをONにした場合のみ（**既定OFF**）、セッション終了時に文字起こし＋全訳文をローカルへ保存する（JSONL＋Markdown） |
| F-11 | 生徒の再接続時、直近の発話履歴（直近K件）が再送され表示が復元される |
| F-12 | 対応言語リスト・ASRモデル・翻訳エンジンは `config.yaml` で変更できる |

### Non-functional Requirements（非機能要件）

| ID | 要件 |
|----|------|
| N-01 | **遅延（正式基準）**: 発話終了 → 生徒画面表示まで **中央値5秒以内 / 最大8秒**。努力目標: 中央値3秒 / 最大5秒 |
| N-02 | 同時接続: 生徒10名＋先生1名。サーバーは15接続まで受け付ける |
| N-03 | 完全ローカル: インターネット接続なしで授業運用可能（モデルは事前ダウンロード） |
| N-04 | 費用ゼロ: 全構成要素がOSS・無料モデル。APIキー・課金・GPU不要 |
| N-05 | サーバー要件: Windows 11 / Intel Core i5 / メモリ8〜16GB。常駐メモリ合計 ≤ 5GB を目安とする |
| N-06 | 生徒端末要件: モダンブラウザのみ（Chrome on ChromeOS / Safari on iOS）。追加インストールなし |
| N-07 | プライバシー: 収音は先生の音声のみ。データはサーバーのローカルに留まりクラウド送信しない。記録保存は既定OFF |
| N-08 | 連続90分の授業でクラッシュ・遅延累積の破綻がないこと |
| N-09 | 先生の操作は「start.bat ダブルクリック → ブラウザでマイク許可」まで、で完結すること |

#### 遅延バジェット（N-01の内訳目安）

| 区間 | 目安 |
|------|------|
| VAD発話終了判定 | 〜0.5s |
| ASR（発話5秒分, kotoba-whisper int8） | 1〜2s |
| 翻訳 × 2言語（NLLB: 〜1s / hy-mt2: 3〜6s） | 1〜6s |
| WebSocket配信・描画 | 〜0.1s |
| **合計** | **2.6〜8.6s** → エンジン選定ベンチ（Phase 0）で確定 |

---

## 3. Architecture / Tech Stack Decisions

### 全体構成（フルサーバー集中型）

```
[先生端末: 任意のブラウザ]                 [Windows 11 サーバー (i5/8-16GB)]
  getUserMedia → AudioWorklet              FastAPI + uvicorn (asyncio)
  16kHz mono PCM16 100msチャンク   ──WSS──▶  ① 音声受信リングバッファ
                                             ② Silero VAD で発話区切り
  ◀─ASR結果/統計をライブ表示──               ③ faster-whisper (kotoba-whisper-v2.0) ASR
                                             ④ TranslationEngine（言語ごとに1回翻訳）
[生徒端末×10: Chromebook/iPhone]              │  hy-mt2 1.8b (llama.cpp) ⇄ NLLB-200 600M (CTranslate2)
  QR/URL＋4桁コードで参加        ◀──WS───   ⑤ 言語別ブロードキャスト
  言語選択 → 字幕カード表示                  ⑥ (任意) セッション記録 JSONL/Markdown
```

### 決定事項と根拠

| 決定 | 内容 | 根拠 |
|------|------|------|
| 処理配置 | ASR・翻訳ともサーバー集中。生徒端末は表示専用 | 4GB Chromebook / iPhone のブラウザで1.8Bモデルは動作不安定。10台×約1GBのモデル配布も校内Wi-Fiで非現実的。サーバーなら発話ごとに ASR×1＋翻訳×言語数 で済む |
| 音声入力 | 先生ブラウザからのWSストリーミングが主経路。Windows機直接収音（localhostでブラウザを開く）がフォールバック | 「先生が自分の端末で話す」要件を満たしつつ、Wi-Fi/証明書トラブル時の保険を確保 |
| ASR | faster-whisper（CTranslate2）ランタイム。既定 **kotoba-whisper-v2.0**（日本語特化蒸留・int8）、`config.yaml` で openai whisper small/base に切替可 | 日本語精度と速度の両立。同一ランタイムなのでモデル切替の実装コストゼロ |
| VAD | Silero VAD（ONNX, CPU） | 軽量・無料・実績。発話区切り検出でASRをチャンク処理化 |
| 翻訳 | `TranslationEngine` 抽象の下に **hy-mt2 1.8b（llama-cpp-python, GGUF int4）** と **NLLB-200-distilled-600M（CTranslate2 int8）** の両実装。暫定既定は品質重視で hy-mt2、Phase 0 実機ベンチで N-01 を満たさなければ NLLB に切替 | 品質（LLM系MT）と速度（専用MTモデル）のトレードオフは机上で確定できないため、差し替え可能にして実機で判定 |
| 対象言語 | 英語・中国語（簡体字）を一次対応、`config.yaml` の言語リストで拡張 | 実需要に合わせ検証コストを集中。NLLBは200言語対応で拡張余地大 |
| バックエンド | Python 3.12+ / FastAPI / uvicorn 単一プロセス。ASR/翻訳は ThreadPoolExecutor（CTranslate2・llama.cpp はGIL解放） | faster-whisper がPython前提。WS・静的配信・非同期を1プロセスで完結 |
| フロントエンド | 素のHTML/CSS/JS（ビルド工程なし）。QR生成はローカル同梱の qrcode ライブラリ（CDN不可＝オフライン要件） | 画面数が少なく、npm/Vite等の保守負担に見合わない |
| ネットワーク | 校内Wi-Fi直結が主構成、**Windowsモバイルホットスポット**が副構成。Phase 0 で実地疎通確認 | 校内Wi-FiのAP分離（クライアントアイソレーション）が未確認のため、両構成を用意 |
| HTTPS | サーバーは **HTTPS(8443, 自己署名)** と **HTTP(8000)** を同時リッスン。先生ページはHTTPS（マイクにセキュアコンテキスト必須、初回のみ警告を手動承認）、生徒ページはHTTP（マイク不要、10台での証明書警告を回避） | `getUserMedia` は HTTPS/localhost 限定。生徒側に警告承認を強いない |
| セッション | 単一ルーム＋4桁参加コード（QRに埋込み、手入力も可）。名前入力・アカウントなし | CPU性能上1授業しか処理できないため複数ルームは無意味。コードで部外者の閲覧を防止 |
| 記録 | 先生トグル・既定OFF・ローカル保存のみ | 学校側合意なしに記録が残る事態を避ける |
| 配布 | `setup.ps1`（1回・管理者権限）＋ `start.bat` | 運用先は実質1台。PyInstaller化はネイティブ依存とモデル同梱で巨大化・破損しやすく不採用 |
| GAS | **不使用** | クラウド依存が「完全ローカル・無料・APIキー不要」要件と矛盾。必要性もない |

---

## 4. File / Directory Structure

```
LinguaBridge/
├── plan.md                       # 本ファイル
├── README.md                     # セットアップ・運用手順（先生向け）
├── config.yaml                   # 全設定（モデル・言語・ポート・TLS・記録既定）
├── requirements.txt
├── setup.ps1                     # 初回セットアップ（要管理者権限）
├── start.bat                     # 日常起動（ダブルクリック）
├── .gitignore                    # models/ sessions/ certs/ venv/ を除外
├── scripts/
│   ├── download_models.py        # ASR/翻訳モデルの事前ダウンロード
│   ├── make_cert.ps1             # 自己署名証明書生成
│   ├── bench.py                  # Phase 0 実機ベンチ（ASR RTF・翻訳遅延計測）
│   └── replay_client.py          # 録音済み音声の再生＋擬似生徒10名の負荷試験
├── server/
│   ├── main.py                   # FastAPIエントリ。HTTP/HTTPS二重リッスン、静的配信
│   ├── config.py                 # config.yaml のロード・検証（pydantic）
│   ├── session.py                # 単一ルーム・参加コード・クライアント管理・履歴再送
│   ├── ws_protocol.py            # WSメッセージのpydanticスキーマ（§6参照）
│   ├── pipeline.py               # オーケストレーター（キュー・ワーカー・配信）
│   ├── audio/
│   │   ├── ingest.py             # 先生WSからのPCMデコード（バッファリングはvad.py側）
│   │   └── vad.py                # Silero VAD による発話セグメンテーション
│   ├── asr/
│   │   ├── base.py               # ASREngine 抽象
│   │   └── fw_engine.py          # faster-whisper 実装（kotoba/whisper切替）
│   ├── mt/
│   │   ├── base.py               # TranslationEngine 抽象
│   │   ├── hymt_engine.py        # hy-mt2 1.8b（llama-cpp-python / GGUF）
│   │   └── nllb_engine.py        # NLLB-200 600M（CTranslate2）
│   └── recorder.py               # セッション記録（JSONL + Markdown 書き出し）
├── web/
│   ├── index.html                # 生徒ページ（参加→言語選択→字幕）
│   ├── student.js / student.css
│   ├── teacher.html              # 先生ページ（マイク・QR・統計・記録トグル）
│   ├── teacher.js / teacher.css
│   ├── audio-worklet.js          # マイク→16kHz PCM16 変換
│   ├── i18n.js                   # UI文言 ja/en/zh
│   └── vendor/qrcode.min.js      # ローカル同梱QR生成
├── models/                       # setup.ps1 がDL（gitignore・OneDrive同期除外）
├── certs/                        # 自己署名証明書（gitignore）
├── sessions/                     # 記録保存先（gitignore）
└── tests/
    ├── unit/                     # vad / protocol / recorder / engineアダプタ
    ├── integration/              # 小型モデルでのパイプライン結合試験
    ├── fixtures/                 # 授業想定の日本語音声WAV・期待テキスト
    └── e2e/                      # replay_client による遅延・負荷計測
```

> **注意**: リポジトリが OneDrive 配下にあるため、`models/`（2〜3GB）と `sessions/` は
> `.gitignore` に加え **OneDrive同期除外**（またはリポジトリ外 `%LOCALAPPDATA%` への配置を
> `config.yaml` で指定）とする。§10 リスク R-08 参照。

---

## 5. Data Models / Schema

すべてインメモリ（単一セッション・揮発）。永続化は記録ON時のJSONLのみ。

```python
# session.py
class Session:
    join_code: str            # 4桁数字。start.bat 起動ごとにランダム生成
    started_at: datetime
    state: Literal["idle", "live", "paused", "ended"]
    recording: bool           # F-10 トグル（既定 False）
    clients: dict[str, Client]
    history: deque[Utterance] # 直近K=50件（再接続時の再送用 F-11）

class Client:
    id: str                   # 接続ごとのUUID
    role: Literal["teacher", "student"]
    lang: str | None          # 生徒のみ。"en" | "zh" | ...（BCP47ベース）
    ws: WebSocket
    joined_at: datetime

# pipeline.py
class Utterance:
    seq: int                  # セッション内連番（生徒側の順序整合・再送キー）
    t_start: float            # 音声先頭からの秒
    t_end: float
    text_ja: str              # ASR結果
    asr_ms: int               # 計測用
    translations: dict[str, Translation]   # lang -> Translation

class Translation:
    lang: str
    text: str
    engine: str               # "hy-mt2" | "nllb"
    mt_ms: int
```

### config.yaml スキーマ（骨子）

```yaml
server:
  http_port: 8000            # 生徒用（平文）
  https_port: 8443           # 先生用（自己署名TLS）
  cert_dir: certs/
asr:
  engine: faster-whisper
  model: kotoba-tech/kotoba-whisper-v2.0-faster   # 切替: "small" 等
  compute_type: int8
  language: ja
vad:
  threshold: 0.5
  min_silence_ms: 500        # 発話終了判定
  max_utterance_s: 30        # 強制分割（§8 E-03）
mt:
  engine: hy-mt2             # "hy-mt2" | "nllb"（Phase 0ベンチで既定確定）
  hy_mt2: { gguf_path: models/hy-mt2-1.8b-q4.gguf, threads: 4 }
  nllb:   { model_dir: models/nllb-200-600m-ct2,  beam_size: 1 }
languages:                   # 生徒が選択可能な言語（F-12）
  - { code: en, label: English }
  - { code: zh, label: 中文（简体） }
recording:
  default_on: false
  out_dir: sessions/
history_resend: 50
```

### 記録ファイル（recording ON時 / F-10）

- `sessions/2026-09-01_1030/transcript.jsonl` — 1行1 Utterance（上記モデルのJSON）
- `sessions/2026-09-01_1030/transcript.ja.md`, `transcript.en.md`, `transcript.zh.md` — 人が読む用

---

## 6. API / Component Design（詳細）

### 6.1 HTTPエンドポイント

| メソッド/パス | 用途 |
|---------------|------|
| `GET /` | 生徒ページ（`?code=1234` 付きQR経由なら参加コード自動入力） |
| `GET /teacher` | 先生ページ（HTTPS側でのみ案内） |
| `GET /api/config` | 言語リスト・UI文言など公開設定のJSON |
| `GET /api/teacher-info` | 参加コード・参加URL（QR用）。ループバック接続のみ応答（先生ページ用。別端末HTTPS運用時は #16 でトークン方式に変更） |
| `GET /healthz` | 死活監視（モデルロード完了で200） |
| 静的配信 `/static/*` | web/ 以下 |

### 6.2 WebSocketプロトコル

エンドポイント: `WS(S) /ws`。テキストフレーム=JSON、バイナリフレーム=音声PCM。

**生徒 → サーバー**

```jsonc
{ "type": "join",       "role": "student", "code": "4831", "lang": "zh",
  "last_seq": 12 }      // 再接続時のみ。以降の履歴が再送される (F-11)
{ "type": "set_lang",   "lang": "en" }     // 授業中の言語変更
```

**先生 → サーバー**

```jsonc
{ "type": "join",    "role": "teacher", "code": "4831" }
{ "type": "control", "action": "start" | "pause" | "end" }
{ "type": "recording", "on": true }
// ＋ バイナリフレーム: 16kHz mono PCM16, 100ms(3200byte)ごと
```

**サーバー → 生徒**

```jsonc
{ "type": "joined",  "seq_head": 12, "languages": [...], "session_state": "live" }
{ "type": "caption", "seq": 13, "ja": "光合成には日光が必要です。",
  "text": "光合作用需要阳光。", "lang": "zh", "delay_ms": 4200 }
{ "type": "session", "state": "paused" | "live" | "ended" }   // バナー表示用
```

**サーバー → 先生**

```jsonc
{ "type": "asr_final", "seq": 13, "ja": "...", "asr_ms": 1400 }
{ "type": "stats", "students": 9, "langs": {"zh": 5, "en": 4},
  "queue_depth": 1, "median_delay_ms": 4100 }   // 2秒ごと
{ "type": "error", "code": "mic_silent" | "queue_overload", "message": "..." }
```

エラー応答: 参加コード不一致は `{"type":"join_rejected", "reason":"bad_code"}` → クライアントは再入力UI。5回連続失敗で当該IPを60秒拒否（総当たり対策）。

### 6.3 サーバー内部パイプライン（pipeline.py）

```
teacher WS ─▶ ingest(リングバッファ)
                │ 100msごと
                ▼
             vad.py ── 発話確定（無音500ms or 30s強制分割）──▶ asr_queue (maxsize=4)
                                                                │
                                        ThreadPoolExecutor(1) ◀─┘  ASRワーカー
                                                                │ Utterance(text_ja)
                                                                ▼
                                                     mt_queue (言語ごとにジョブ展開)
                                                                │
                                        ThreadPoolExecutor(1) ◀─┘  MTワーカー（直列）
                                                                │ Translation
                                                                ▼
                                              broadcast: 該当langの生徒へ caption 送信
                                              teacherへ asr_final / stats 送信
                                              recorder: recording ON なら追記
```

- ASRとMTのワーカーは各1スレッド（i5でのコンテキストスイッチ・メモリ競合を防ぐ）。CTranslate2 / llama.cpp が内部でマルチスレッド推論する
- **アクティブ言語のみ翻訳**: その言語を選択中の生徒が0名なら翻訳ジョブを生成しない
- キュー滞留ポリシー: **発話はスキップしない**（授業内容の欠落は許容しない）。`queue_depth ≥ 3` で先生ページに「処理が追いついていません。ゆっくり話すか一呼吸置いてください」を警告表示（§8 E-05）

### 6.4 フロントエンド構成

**先生ページ (`teacher.html` / `teacher.js`)**
- 参加情報パネル: QRコード（`http://<IP>:8000/?code=XXXX` を符号化）・URL・4桁コードを大きく表示（プロジェクター投影を想定）
- マイク制御: `getUserMedia` → `AudioWorkletNode`（`audio-worklet.js` で 48kHz→16kHz ダウンサンプル・PCM16化）→ WS送信。開始/一時停止/終了ボタン。入力レベルメーター（無音検知 E-01 の一次防衛）
- モニター: 日本語文字起こしのライブ表示、接続数・言語内訳・中央値遅延・キュー深度
- 記録トグル（既定OFF、ON時は録画中インジケーター常時表示）

**生徒ページ (`index.html` / `student.js`)**
- 参加フロー: コード入力（QR経由なら自動）→ 言語選択 → 字幕画面
- 字幕画面: 確定発話ごとのカード（訳文大・日本語原文小[トグル]・タイムスタンプ）。自動スクロール、上方向スクロールで追従停止＋「最新へ」ボタン、文字サイズ大/中/小
- 再接続: WS切断時は指数バックオフで自動再接続し `last_seq` を送って差分復元。切断中はバナー表示
- UI文言は `i18n.js`（ja/en/zh）で選択言語に追従

### 6.5 主要抽象インターフェース

```python
class ASREngine(ABC):
    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult: ...
    # ASRResult: text, avg_logprob, no_speech_prob, compression_ratio（E-04の幻覚フィルタ用）

class TranslationEngine(ABC):
    def translate(self, text_ja: str, target_lang: str) -> str: ...
    def supported_languages(self) -> list[str]: ...
    def warmup(self) -> None: ...   # 起動時ロード＆ダミー推論（初回遅延対策）
```

---

## 7. Step-by-step Implementation Plan

### Phase 0 — 環境検証・スパイク（判断ゲート）

1. リポジトリ骨格作成（ディレクトリ、`.gitignore`、`requirements.txt`、`config.yaml` 雛形、README雛形）
2. `scripts/download_models.py` 実装: kotoba-whisper-v2.0-faster / whisper-small(ct2) / NLLB-200-600M(ct2) / hy-mt2 1.8b GGUF を `models/` へ取得。**hy-mt2 の正確なHF配布元・GGUF有無・ライセンスをここで確認**（→ §11 Q-01）
3. `scripts/bench.py` 実装・実行: 実機i5で (a) ASRのRTF（実時間比）を kotoba / small で計測、(b) hy-mt2 / NLLB の1発話×2言語の翻訳遅延を計測、(c) ASR+MT同時実行時のメモリと遅延を計測
4. **判断ゲート①**: ベンチ結果を遅延バジェット（§2）に当てはめ、既定の ASRモデルと MTエンジンを決定して `config.yaml` に反映。N-01（中央値5s/最大8s）を満たす組合せが存在しない場合はここで構成再検討（リスク R-01）
5. ネットワーク実地確認: 校内Wi-Fiで Chromebook→Windows のHTTP疎通を確認（AP分離チェック）。不可なら**Windowsモバイルホットスポット構成を主に昇格**（接続8台上限の運用策=ペア閲覧等をREADMEに記載）
6. **判断ゲート②**: 学校管理Chromebookで自己署名HTTPSの警告承認が可能か実機確認。不可なら先生はWindows機localhost配信を主経路に変更（設計は両対応なのでコード変更なし、README手順のみ変更）

### Phase 1 — MVP（音声→字幕の一気通貫）

7. `server/config.py`＋`config.yaml` ロード（pydantic検証）
8. `server/main.py`: FastAPI起動、HTTP(8000)/HTTPS(8443)二重リッスン、静的配信、`/healthz`。`scripts/make_cert.ps1` で証明書生成
9. `server/ws_protocol.py`＋`server/session.py`: WSスキーマ、参加コード検証、クライアント管理、履歴re-send
10. `web/audio-worklet.js`＋先生ページのマイク取得〜PCM送信（まずローカルループバックで波形確認）
11. `server/audio/ingest.py`＋`server/audio/vad.py`: PCM受信→Silero VADで発話セグメント化（fixture WAVでユニットテスト）
12. `server/asr/fw_engine.py`: faster-whisperラッパー（モデル切替・幻覚フィルタ・warmup）
13. `server/mt/nllb_engine.py`＋`server/mt/hymt_engine.py`: TranslationEngine 2実装（言語別プロンプト/言語コードマッピング含む）
14. `server/pipeline.py`: キュー・ワーカー・ブロードキャスト結線。統計（遅延・キュー深度）計測込み
15. 生徒ページMVP: 参加→言語選択→字幕カード表示（自動スクロールのみ）。先生ページMVP: QR・コード・開始/停止・文字起こしライブ表示
16. **一気通貫E2E**: 実機2〜3台（Windows＋Chromebook＋iPhone）で10分連続配信し、動作と遅延を確認

### Phase 2 — 授業投入版（現場の壊れ方に耐える）

17. 再接続処理: 生徒の自動再接続＋`last_seq`差分復元、先生切断時の生徒側バナー＋自動一時停止、先生の重複接続は「後勝ち（旧接続を切断）」
18. 過負荷制御: キュー深度監視→先生への警告、`max_utterance_s` 強制分割、遅延の `delay_ms` 表示
19. 表示UX完成: 原文併記トグル・文字サイズ・追従一時停止＋「最新へ」・UI多言語化（i18n.js）
20. 運用スクリプト完成: `setup.ps1`（Python導入→venv→pip→モデルDL→証明書→ファイアウォール規則→電源スリープ無効化 `powercfg`）、`start.bat`（起動→コード生成→既定ブラウザで先生ページを開く）
21. `scripts/replay_client.py` による負荷試験: 録音済み授業音声45分をリプレイ＋擬似生徒10クライアントで、N-01/N-08 の受け入れ判定（§9）
22. 実地検証: 実教室・実Wi-Fi（またはホットスポット）・実端末での通し授業リハーサル。チェックリスト消化（§9）
23. README（先生向け運用手順書）完成: セットアップ、毎授業の起動手順、トラブルシューティング（マイク不許可・Wi-Fi断・遅延警告時の対処）

### Phase 3 — 改善版（授業投入後）

24. `server/recorder.py`: 記録トグル・JSONL/Markdown書き出し（F-10）
25. 用語辞書: 教科固有名詞の対訳辞書（CSV）を hy-mt2 はプロンプト注入、NLLB は後置換で適用
26. 追加言語の有効化検証（ベトナム語等、需要に応じて）と言語別品質スポットチェック
27. 運用フィードバック反映（実授業での誤認識・誤訳サンプル収集→ASR/MT設定チューニング）

---

## 8. Edge Cases & Error Handling

| ID | ケース | 対処 |
|----|--------|------|
| E-01 | マイク無音・ミュートのまま授業開始 | 先生ページに入力レベルメーター常設。30秒無音でサーバーから `mic_silent` 警告 |
| E-02 | 環境雑音・チャイム・BGMで誤認識 | VAD閾値を config 化。`no_speech_prob` 高スコアのセグメントは破棄 |
| E-03 | 先生が30秒以上区切らず話し続ける | `max_utterance_s=30` で強制分割してASRへ（遅延の際限ない増大を防止） |
| E-04 | Whisperの幻覚（無音時の「ご視聴ありがとうございました」等の定型反復） | `compression_ratio > 2.4` / `avg_logprob` 低値 / 既知幻覚フレーズ辞書 でフィルタし配信しない |
| E-05 | 処理がキュー滞留（早口・長文連続） | 発話は落とさず順次処理。深度≥3で先生に警告表示、caption に `delay_ms` を付し生徒側で「遅延中」表示 |
| E-06 | 生徒のWi-Fi瞬断・スリープ復帰 | 指数バックオフ再接続＋`last_seq` 差分再送（F-11）。切断中バナー |
| E-07 | 先生端末の切断 | 生徒全員に `session: paused` バナー。先生再接続で自動再開 |
| E-08 | 先生ページの二重接続（タブ複製等） | 後勝ち: 新接続を有効化し旧接続へ切断通知 |
| E-09 | 参加コード誤入力・総当たり | 再入力UI。同一IP5連続失敗で60秒拒否 |
| E-10 | サーバーIPがDHCPで変わる | start.bat 起動時に現IPでQR再生成（毎授業QR読み直し運用）。README にIP固定の推奨手順も記載 |
| E-11 | Windows機のスリープ・画面ロック | setup.ps1 で電源プラン変更（AC接続時スリープ無効）。README に授業中はAC接続と明記 |
| E-12 | iPhone Safari特有の挙動（バックグラウンドでWS切断） | E-06の再接続で吸収。画面ロック中の受信は諦め、復帰時に履歴復元 |
| E-13 | モデルファイル欠損・破損での起動失敗 | 起動時チェックでファイル存在＋サイズ検証。欠損時はコンソールに `download_models.py` 再実行を案内し、`/healthz` は503 |
| E-14 | 対応外言語コードでの join / 全員退出した言語 | join時に config の言語リストで検証。選択者0名になった言語は翻訳ジョブ停止（§6.3） |
| E-15 | HTTPS証明書の期限切れ | 証明書は有効期間825日で生成し、起動時に残存期間チェック→30日未満で警告＋再生成案内 |

---

## 9. Testing Strategy

### ユニットテスト（pytest / CI可）
- `vad.py`: fixture WAV（発話・無音・雑音・30s超）に対するセグメント境界の検証
- `ws_protocol.py`: 全メッセージ型のバリデーション（不正コード・不正言語含む）
- `session.py`: 参加コード検証、履歴再送ロジック（`last_seq` 差分）、後勝ち接続
- `recorder.py`: JSONL/Markdown出力の形式
- MT/ASRエンジンアダプタ: モデルをモックし、言語コードマッピング・幻覚フィルタ・エラー伝播を検証

### 統合テスト（実モデル・ローカル実行）
- whisper-tiny＋NLLBの最小構成でパイプライン一気通貫（WAV入力→caption出力）を自動検証
- エンジン切替（config変更のみで hy-mt2⇄NLLB / kotoba⇄small が動く）

### 性能・負荷テスト（`scripts/replay_client.py`）
- 45分の授業録音リプレイ＋擬似生徒10接続（en:5, zh:5）で遅延分布を計測
- **受け入れ基準（Phase 2完了条件）**:
  - 発話終了→caption受信の遅延: **中央値 ≤ 5s、p100 ≤ 8s**（N-01）
  - 45分間でクラッシュ・切断復元失敗・メモリ増加傾向（リーク）なし（N-08）
  - サーバー常駐メモリ ≤ 5GB（N-05）

### 品質スポットチェック（人手）
- 授業ドメインの日本語30文（理科・数学・行事連絡等）でASR文字誤り・訳文の意味保持を確認。中国語はネイティブ/教員による5段階評価で平均3.5以上を目安
- 固有名詞・教科用語の誤りは Phase 3 の用語辞書の入力にする

### 実地チェックリスト（教室リハーサル / タスク22）
- [ ] 実Wi-Fi（またはホットスポット）で全端末が接続できる
- [ ] 教室後方のマイク距離・雑音条件で認識が実用レベル
- [ ] プロジェクター投影のQRから生徒が90秒以内に全員参加完了
- [ ] 授業45分の通しで遅延警告が常態化しない
- [ ] Wi-Fi瞬断・端末スリープからの復帰を故意に発生させ復元を確認

---

## 10. Potential Risks & Mitigations

| ID | リスク | 影響 | 緩和策 |
|----|--------|------|--------|
| R-01 | i5 CPUで hy-mt2 1.8b が遅延基準を満たさない | 高 | TranslationEngine抽象で NLLB に即切替（Phase 0 判断ゲート①で早期確定）。両方ダメなら Opus-MT 追加検討 |
| R-02 | 校内Wi-FiのAP分離で端末間通信不可 | 高 | Windowsモバイルホットスポット構成へ切替（Phase 0 で実地確認）。8台上限は「2人1台のペア閲覧」「先生はlocalhost配信でスロット節約」で回避。必要なら安価なトラベルルーター追加を学校に提案（無料要件の例外として明示合意を取る） |
| R-03 | 管理ChromebookでHTTPS警告承認・マイク許可がポリシー制限 | 中 | 先生はWindows機のlocalhostで配信（設計済みフォールバック）。生徒ページはHTTPなので影響なし |
| R-04 | hy-mt2 1.8b の配布元・GGUF・ライセンスが想定と異なる | 中 | Phase 0 タスク2で最初に確認。不可なら NLLB を既定に、LLM枠は別の小型翻訳特化モデルを再調査 |
| R-05 | NLLB-200 のライセンスは CC-BY-NC 4.0（非商用） | 低 | 学校の授業利用は非商用に該当する想定。plan/READMEに利用条件を明記 |
| R-06 | Whisper幻覚による誤情報の字幕配信 | 中 | E-04 の多重フィルタ。先生ページの文字起こしライブ表示で先生自身が監視できる |
| R-07 | 教室のマイク環境（距離・雑音）で認識精度が実験室より大幅劣化 | 中 | 実地リハーサル（タスク22）で早期検出。改善策: 先生端末を胸元に・安価なピンマイク・VAD/ゲイン調整 |
| R-08 | OneDrive配下のリポジトリで models/（GB級）が同期されPC・回線を圧迫 | 中 | models/sessions/ を OneDrive 同期除外 or `%LOCALAPPDATA%\LinguaBridge` へ配置（config で パス指定可にする） |
| R-09 | 授業中のWindows Update・スリープでサーバー停止 | 中 | setup.ps1 で電源設定変更、README に「授業時間帯の再起動延期設定」手順を記載 |
| R-10 | 生徒がURLを校外へ共有し部外者が閲覧 | 低 | LAN限定＋4桁コード＋コードは起動ごとに変更、で実用上十分 |
| R-11 | 9月投入に間に合わない | 中 | Phase 1（MVP）完了時点で「最低限使える」状態を担保する段階設計。Phase 2 のうち再接続（17）と運用スクリプト（20）を投入必須、他は授業と並行改善可 |

---

## 11. Assumptions & Open Questions

### Assumptions（前提）
- A-01: GASは使用しない（完全ローカル要件と矛盾するため）。クラウド・外部APIへの依存ゼロ
- A-02: 中国語は簡体字を既定とする（繁体字需要が判明したら languages 設定に `zh-Hant` を追加）
- A-03: Windows 11 機は先生の管理下にあり管理者権限が使える（確認済み）
- A-04: 記録するのは先生の発話のみで、生徒の音声・個人情報は一切取得しない
- A-05: 学校の授業利用は非商用であり、CC-BY-NC等の非商用ライセンスモデルを利用できる
- A-06: 同時利用は1教室のみ。サーバーWindows機は授業中AC電源・教室内設置
- A-07: 生徒端末のブラウザは Chrome（ChromeOS）/ Safari（iOS15+）相当の現行版

### Open Questions（未解決・Phase 0 で解消）
- Q-01: **hy-mt2 1.8b の正確な配布元（HFリポジトリID）・GGUF提供有無・ライセンス**は？ → タスク2で確認。取得不能なら R-04 の代替へ
- Q-02: 校内Wi-FiのAP分離の有無 → タスク5の実地確認で判明
- Q-03: 学校管理Chromebookで自己署名証明書の警告承認が可能か → タスク6（判断ゲート②）
- Q-04: 教室での実マイク環境（内蔵で足りるか、ピンマイクが要るか） → タスク22
- Q-05: 記録保存を実運用でONにする場合の学校側の合意手続き → Phase 3 開始前に確認
- Q-06: 英・中以外の実需要（在籍生徒の言語構成） → Phase 3 タスク26の対象言語決定に使用
