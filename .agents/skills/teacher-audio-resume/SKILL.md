---
name: teacher-audio-resume
description: 先生ページの一時停止・授業終了をまたいで、AudioWorklet内の未完成音声や配送待ちパケットが再開後の英中字幕に混ざるかを検証する。teacher.js の授業状態と音声送信、audio-worklet.js のパケット化、または再開直後の誤字幕を調べるときに使う。
---

# 先生のマイクを止めて再開した直後の字幕を確かめる

停止中は先生ページから音声を送らない。再び配信中になったら、新しく取り込んだ発話が欠けずに届き、最初の発話と次の発話がそれぞれ一度ずつ英中字幕になる。停止前の未送信音声や停止中の会話が混ざると、生徒に別の意味の説明が届く。

[teacher-audio](../teacher-audio/SKILL.md) は操作後に200 msずつ音声を入れる。[lesson-audio-boundary](../lesson-audio-boundary/SKILL.md) は完成した100 msパケットから検査を始める。ここでは実際のWorkletがパケットを作っている途中と、完成したパケットが先生ページに届く前に授業を止める。

## 全探索と破壊検出

リポジトリ直下で Node.js 24.22.3、Python 3.12.10、[固定依存](../../../scripts/formal/requirements.txt)を使う。次のコマンドはモデルの重みを必要としない。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/teacher_audio_resume.py --mutation-check --json .audit/teacher-audio-resume.json
```

同じ依存が入った開発機では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/teacher_audio_resume.py --mutation-check` でも実行できる。終了0は全性質が成立、1は反例あり、2は実行エラー。Windowsの制限環境で `asyncio` の起動が `socketpair` で停止するときは、ループバックを利用できる環境で再実行する。

- **入力**　`pause` と `end`、旧音を入れる区間（停止直前または停止中）、48 kHzで128サンプルの処理単位を0〜75個、Workletのメッセージを即時配送または再開通知の後までFIFOで保留する場合を全列挙する。75単位で100 msパケット2個分になり、その間の全位相を通る。各列で旧音を入れる区間は1つ。未完成音声の持ち越しは即時配送でも観測できる。
- **観測**　再開後に異なる識別音を400 msずつ、その直後に無音を1.2秒ずつ入れる。先生の実送信バイトを `Connection.receive` に渡す。各発話で新しい音が6,400サンプルあり、旧音が0であること、ASRに届いた音の順序、先生の確定原文、英中双方の字幕の番号・原文・言語・訳を入力計画と突き合わせる。
- **代役**　Node VMで実JSを動かし、画面の操作と配信状態の応答順序を固定する。32 msごとのSileroのモデル出力、ASR、MTは識別音から決定的な値を返す。VADの区間化、morph、推論キュー、翻訳ジョブ、配信は製品コードを通る。探索結果の件数はこの入力集合での内訳で、授業中の発生頻度は測定していない。

### 混入を検出したケースを再検査する

Issue #215 の修正前（`a9e1cde`）には608件中596件で旧音が混入した。修正後は608件すべてで、最初と次の発話が旧音を含まず英中に1件ずつ届いた。5種類の破壊検出と復元後の再探索も通った。停止中に未完成だったパケットを個別に調べるには次を使う。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/teacher_audio_resume.py --case pause stopped 36 immediate
```

一時停止中に96 msの音を入力する。この間、先生ページからの送信は0件。修正前は停止中の1,536サンプルが再開後の先頭パケットに入った。修正後の混入は0サンプルで、先生と英中の生徒が受け取る原文は「再開直後です。」になった。授業終了後の次の授業を調べるには `pause` を `end` に替える。配送待ちだけを切り分けるには `--case pause stopped 75 queued` を使い、同じ入力の `immediate` と比較する。

`first_counterexamples` は性質ごとに最初の違反と再現引数を示す。`first_old_pcm` または `following_old_pcm` があれば送信条件とWorkletの保持音声を照合する。その後に `first_asr`、`first_teacher`、`first_en`、`first_zh` を追う。`following_ok` は次の独立した発話が全検査を通ったケース数。`--mutation-check` は正常対照の一時コピーで停止中の送信、旧パケット再送、後続音声の欠落、パケット先頭の消音、中国語だけの欠配を別々に注入する。指定の性質で失敗し、一時変更を破棄して全探索が元の結果に戻ったら `restored_matches` が真になる。既知の反例も終了1で報告する。

## 日本語音声と出荷モデルで照合する

照合時の推論ライブラリは faster-whisper 1.2.1、onnxruntime 1.31.0、llama-cpp-python 0.3.33。出荷モデルをセットアップした開発機で `tests/fixtures/ja/01.wav` 〜 `03.wav` を使う。音源がなければ `./scripts/make_fixture_audio.ps1 -BaseOnly` で生成する。モデルの置き場所が既定と異なる場合は `--models-dir` で指定する。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/teacher_audio_resume_real.py --action pause --json .audit/teacher-audio-resume-real/pause.json
.venv/Scripts/python.exe -X utf8 -B scripts/formal/teacher_audio_resume_real.py --action end --json .audit/teacher-audio-resume-real/end.json
```

停止中の64 msを日本語音声にした場合と、同じ長さの無音にした対照を比べる。両方の条件で、再開後に同じ教材2発話を入力する。送信PCMを再生するときは各音源の後に無音を追加し、キャッシュを無効にして言語ごとの推論を確認する。`wire` の `inactive_sample_offset` と `lessons` の `old_frame_offsets` は停止中の音が見つかったサンプル位置（見つからなければ −1）。`properties` は配信の成否と、対照に対する音声・原文・各言語の字幕の一致を個別に返す。

修正前は一時停止・終了の両方で停止音1,024サンプルがHaas入力の先頭に残り、「光合成」に「動物」が加わった。修正後は両方で12項目すべてが成立した。停止音は送信PCMにもHaas入力にも見つからず、光合成と教科書の2発話は原文・英訳・中訳とも無音対照と一致した。音源やモデルが異なると文面は変わりうる。64 msの混入は入力PCMと対照で判断する。

## 実装との同期

照合した製品コミットは `abb6cef97fdfe2eeead6baca99cecb2402ab668a`。

| 照合する箇所 | 対応する観測 |
|---|---|
| `web/teacher.js` の `ensureMic`、`applySessionState`、`replaceAudioWorklet`、開始・停止ボタンと `port.onmessage`、`web/audio-worklet.js` の `PCM16Downsampler.process` | 有効なクリック、メッセージの配送時点、端数、送信された各100 msの中身 |
| `server/connection.py` の `receive`、`server/lesson.py` の `control`、`_transition`、`config.yaml`、`server/audio/vad.py`、`server/pipeline.py` の `feed_audio`、`flush_audio`、`_process_segment` | 先生の入力受理、停止時の排出、再開後のASR入力とTurn確定 |
| `server/caption_delivery.py`、`server/roster.py`、`server/delivery.py` | 受信者ごとの確定字幕と順序 |
| `server/main.py` の `create_app`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py` | 日本語音声を使う照合でのモデル選択、ASR入力、実際の訳文 |
| `scripts/formal/teacher_audio.cjs` の `page`、`lesson_audio_boundary{,_real}.py` の `FrameScores` と `replay` | 先生ページの代役、既定VADへの入力、出荷モデルへの受け渡し |

```powershell
git diff abb6cef97fdfe2eeead6baca99cecb2402ab668a -- web/teacher.js web/audio-worklet.js config.yaml server scripts/formal
```

端数の破棄やWorklet制御メッセージの導入時は、代役のMessagePortと実ブラウザーの実装を照合する。反例が変わったら入力、送信PCM、ASR入力、字幕の順に調べる。契約を意図して変更するときは入力計画と期待値・破壊検出を合わせる。対応するコードと全探索・破壊検出・実音声の結果を確認してから同期コミットを進める。未照合の変更には `/maintain-formal-verification-skill` を使う。

検証範囲は同じ先生・同じマイクでの停止1回と再開1回。物理マイク、ネットワーク遅延の分布、全ブラウザーのスケジュール、授業50分間の性能はこの探索の対象外。
