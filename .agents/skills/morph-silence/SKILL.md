---
name: morph-silence
description: 授業中、文の途中の無音に短い有音判定が挟まっても、後続発話を待たずに保留中の文が英中字幕になるかを有界検証する。Silero の発話開始・無音確定、morph の TurnBoundary 処理を変えたときや、長く黙った前後の説明が一つの字幕に結合されるときに使う。
---

# 短い音を挟んだ後の無音で字幕を確定する

先生が「温度が高いので」と言葉を切った後、32〜64 ms だけ音声と判定されても、それから1秒以上無音が続けば、次の発話を待たずに先生へ確定原文、英中の生徒へその訳が1件ずつ届く。ここで確定し損なうと、次の説明まで字幕が止まり、別々の説明が一緒に翻訳される。

[出荷設定](../../../config.yaml) の morph は、96 ms の連続有音で発話を開始し、500 ms の無音で音声を認識に回し、1,000 ms の無音で言いさしの文も確定する。[silero-lesson](../silero-lesson/SKILL.md) の短音は授業の冒頭に置く。この検査では、いったん認識を終えた文が確定を待つ間に短音を置く。

## 実行する

リポジトリ直下で Python 3.12.10 と [固定依存](../../../scripts/formal/requirements.txt) を使う。次の全探索はモデルの重みを必要としない。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/morph_silence.py --mutation-check --json .audit/morph-silence.json
```

同じ依存が入った開発機では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/morph_silence.py --mutation-check` でも実行できる。終了コードと今回の実行で生成された JSON を合わせて読む。

- **0 / PASS** は全入力で成立。破壊検査を指定したときは `mutations` の全件が `detected: true`、`restored_matches: true` であることも確認する。
- **1 / FAIL** は実コードが期待値と異なる。`failed_cases` の2数は短音の直前の無音枠数と短音の枠数。`first_counterexample` に各観測時点の認識・受信内容と差分がある。
- **2 / ERROR**、JSON が出ない場合、または起動時の停止は検査環境・検証器を調べる。Windows の asyncio の初期化には内部ソケットを利用できる環境が要る。出荷設定や故障の置換箇所が変わった場合は該当コードと入力条件を照合する。

最小反例と、短音だけを除いた正常対照は次の2列で再現できる。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/morph_silence.py --case 16 1
.venv/Scripts/python.exe -X utf8 -B scripts/formal/morph_silence.py --case 16 0
```

## 何を全列挙するか

1枠は 16 kHz の PCM16 を512サンプル、32 ms。英語と中国語の生徒各1人が接続した授業で、最初に3枠の「温度が高いので」を入力する。無音を15〜32枠、その直後の有音を0〜3枠とする **18 × 4 = 72通り**をすべて実行する。その後は必ず新たに32枠（1,024 ms）の連続無音を与え、最後に独立した「次は実験です。」と32枠の無音を入力する。

| 入力 | 期待する振る舞い |
|---|---|
| 直前の無音が15枠 / 16〜31枠 / 32枠 | 順に、まだ認識に回らない、認識済みで確定を待つ、すでに字幕が届いている。 |
| 短音が0〜2枠 | 短音は新しい語を作らない。続く32枠の無音が終わるまでに最初の字幕が届く。短音の扱いで無音を数え直す場合も、この時点で判定できる。 |
| 有音が3枠 | 発話開始に足りる「体積が増えます。」を入力する。先行無音が15枠なら同じ音声区間、16〜31枠なら別々の認識結果が同じ文になる。32枠なら先の文はすでに独立している。 |
| 次の独立した発話 | 先生と両言語の生徒に、次の番号で1件ずつ届く。 |

Python で入力を列挙し、製品の `Pipeline.feed_audio` に直接渡す。Silero の ONNX 出力と認識・翻訳の計算は識別音に応じた決定的な代役を使う。音声を区切り、認識結果を保留・確定し、翻訳を言語別に配信する処理には実コードを使う。短音の前、後続の無音の末尾、独立した次の発話の後で、キューと送信の排出を待つ。認識済みで、短音を含めても経過が31枠以下の列では、通算31枠の時点でも字幕が出ていないことを調べる。一時停止や強制的な発話確定は行わない。認識に渡った各区間から得た原文と、各接続が受け取った発話番号・原文・言語・訳文を、入力から組んだ期待値と比較する。

## 同期時に見つかった反例

72通り中40通りが成立し、直前の無音16〜31枠と短音1〜2枠の全32通りが失敗した。`--case 16 1` では認識結果に「温度が高いので」がある。新たな無音1,024 ms が終わった時点で、先生・英語・中国語の受信はすべて0件。その後は「温度が高いので次は実験です。」が発話1として1件だけ届く。

反例は `before_burst`、対象列での `before_deadline`、`after_silence`、`following` の順に読む。同期時の `VoiceSegmenter._process_idle_frame` は1枠の有音で無音カウンターを解除し、発話開始に足りない場合はそこで戻る。次の無音で `_tick_post_silence` が保留中の文に境界を通知できず、文は次の認識結果まで残った。

破壊検査は正常対照の一時コピーで、認識を1枠遅らせる、無音確定を1枠早める、境界通知を無視する、後続発話の中国語字幕だけを欠配させる、の4変更を個別に試す。指定した観測時点・受信者で差分を検出し、一時変更の復元後に全72ケースの結果が一致することを確認する。製品で反例を修正した後の期待値は72件すべて成功。

## 教材音声と出荷モデルで照合する

通常のアプリ依存と出荷モデルを用意し、教材 `tests/fixtures/ja/01.wav` と `03.wav` を使う。音源がない開発機では `./scripts/make_fixture_audio.ps1 -BaseOnly` で生成する。確認時のライブラリは faster-whisper 1.2.1、onnxruntime 1.31.0、llama-cpp-python 0.3.33。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/morph_silence_real.py --json .audit/morph-silence-real.json
```

モデルの保存場所は `--models-dir` で変更できる。教材の「光合成について」で終わる箇所の後に704 ms の無音を置き、教材の32 ms を挿入した場合と、その箇所も無音とした対照を比較する。両条件とも、さらに2.016秒の無音を入れてから次の「教科書の42ページを開いてください」を入力する。シミュレーションの有音ラベルを使わず、実 Silero の判定を記録し、Haas と Hy-MT2 の出力を先生・英中生徒の受信口で観測する。翻訳キャッシュは無効。

同期時は、対照では各受信者に順に1件、累計2件の字幕が届いた。短音ありでは Silero が1枠だけ有音を追加し、次の発話前は0件、その後は結合された1件だった。`control` と `interrupted` の `before_next`、`after_next` を比較する。モデル更新や音源差で前提の判定列を作れなくなったときは、入力ハッシュと実判定を照合する。これは固定音源での再現であり、教室での発生率、訳の品質、物理マイク、LAN、画面描画、実時間50分の性能は測っていない。

## 実装との同期

照合した製品コミットは `db2a1bdf83cbe521ea376205b341f0269f2624e4`。

| 対象 | 照合する処理 |
|---|---|
| `config.yaml`、`server/config.py`、`server/audio/vad.py` | 出荷時の閾値、`SileroVAD.is_speech`、`VoiceSegmenter.feed`、`_close`、`_process_idle_frame`、`_tick_post_silence` |
| `server/pipeline.py`、`server/asr_backlog.py`、`server/turn/` | `feed_audio`、ASRワーカー、`_close_pending_turn`、`TurnAssembler.add_segment/on_boundary`、`drain`。保留した原文がどの入力で確定するか |
| `server/caption_history.py`、`server/caption_delivery.py`、`server/roster.py`、`server/delivery.py` | 発話番号の採番、翻訳ジョブ、言語別の宛先、各接続の送信キュー |
| `server/main.py`、`server/inference_process.py`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py` | 教材音声の照合で使うモデル構築・推論・停止 |
| `scripts/formal/lesson_audio_boundary{,_real}.py`、`silero_contract.py`、`lesson_liveness{,_real}.py` | 判定代役、教材の読み込み、受信口、翻訳代役 |

```powershell
git diff db2a1bdf83cbe521ea376205b341f0269f2624e4 -- config.yaml server scripts/formal
```

対象の変更後は最小反例と対照を比較し、入力条件・観測時点・代役が製品の契約に合うかを確認する。意図した仕様変更なら根拠とともに期待値を更新する。全探索、各破壊の検出、復元、教材音声の照合を終えてから、同期コミットを基のブランチ上の検証済みの版へ進める。未照合の変更が重なったときは `/maintain-formal-verification-skill` を使う。
