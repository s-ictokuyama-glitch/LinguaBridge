---
name: microphone-sample-rates
description: 先生のマイクのサンプルレートや音声ブロックの境界を変え、16 kHzのPCMと英中字幕の継続を有界検証する。AudioWorkletの補間・端数の持ち越し・100 ms送信を変更したときや、機器によって字幕が遅れたり欠けたりするときに使う。
---

# マイクのサンプルレートから英中字幕までを検証する

先生の端末が選んだサンプルレートで連続して話すと、先生ページは入力時刻に対応する16 kHzのPCMを100 msずつ送り、生徒は後続の発話も選択言語で受け取れる。音声ブロックごとに変換位置を丸めると、パケット数が合っていても、認識器へ届く音の一部が抜けたり繰り返されたりする。

既存の単体テストは44.1 kHzなどの波形をWorkletの出口で比較し、既存の授業検証は48 kHzを先生ページから字幕まで通す。ここでは分数比の入力が100 msに達する前後の送信時刻と波形を調べ、同じ先生ページが44.1 kHzで送った音声を実際の授業処理に渡す。[単体テスト](../../../tests/audio_worklet.cjs)・[teacher-audio](../teacher-audio/SKILL.md)

## 実行する

リポジトリ直下で Node.js 24.22.3、Python 3.12.10、[固定依存](../../../scripts/formal/requirements.txt)を使う。開発機の標準環境では次の1コマンドで全探索、50分相当の入力、一時コピーでの破壊、元の実装での再検証を行う。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/microphone_sample_rates.py --mutation-check
```

依存を別途用意する場合は `uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/microphone_sample_rates.py --mutation-check`。短い切り分けには `--case 44100 -1 1`（分割位置を指定した1列）または `--short-lesson`（10秒・2発話）を使う。

| 検査 | 入力と判定 |
|---|---|
| レートと分割の全探索 | 8,000・16,000・22,050・32,000・44,100・48,000・88,200・96,000 Hz。1・2個目のパケットそれぞれについて、補間に必要な最後の入力が揃う点の前後2サンプルから分割位置を選ぶ。8 × 5 × 5 = 200列。残りは最大128サンプルずつ与え、3個目のパケットまで追う。 |
| 送信時刻と波形 | 各ブロックの直後に、それまでの入力から完成するパケット数と実際の送信数を比較する。各パケットは3,200バイト。出力サンプル番号と入力レートの整数積から補間位置を独立に求め、変化する正負の信号とクリッピングを含む全4,800値を各列で照合する。浮動小数点演算と整数化による1 PCM16単位の差を許す。 |
| 50分相当の授業 | 44.1 kHz・128サンプル単位。4秒の有音と1秒の無音を600回繰り返し、発話ごとに異なる振幅を使う。先生ページが送った30,000パケットを接続の `receive` に渡す。受信PCMの全値、先生の確定原文、英中それぞれの発話番号・原文・訳文を照合する。発話の間で処理完了を待たず、入力終了後に10仮想秒進める。 |

Node.jsで製品のJavaScriptを読み込み、先生の開始操作からソケットに送られたバイト列を観測する。変換位置を写した状態機械を別に作る必要がなく、補間に必要な入力が揃った時刻を整数で計算できる。50分検証はPythonで製品の接続、ワーカー、字幕配信を通す。energy VAD、simple Turn、振幅から原文を返す認識器、原文と言語から訳を返す翻訳器を使い、ASR完了を0.1仮想秒後、MT完了を言語ごとに0.2仮想秒後に進める。

## 出荷モデルで音声を照合する

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/microphone_sample_rates.py --real
```

通常のアプリ依存と `config.yaml` のモデルを使う。別の配置は `--models-dir` で指定する。日本語WAVがなければ `./scripts/make_fixture_audio.ps1 -BaseOnly` で生成する。同期時のモデル依存は onnxruntime 1.31.0、faster-whisper 1.2.1、llama-cpp-python 0.3.33。

音源03・02・07に各1.6秒の無音を加えて1本の44.1 kHz信号に変換し、先生ページに入力する。音源間でマイクを作り直したり推論完了を待ったりしない。出荷設定のSilero・Haas ONNX・morph・Hy-MT2で「教科書」「光合成」「質問」を順に認識し、英中それぞれの受信字幕が、発話番号・原文・言語・履歴に保存した訳文と一致することを確認する。同期時の実測は157パケット、認識3回、翻訳6回、英中各3字幕。

全探索は表のレート・分割・信号に限る。50分入力と実モデルの日本語3発話はそれぞれ固定の入力例である。物理マイク、ブラウザーの音声ドライバー、無線LAN、生徒画面の描画、任意の発話の翻訳精度、サーバーPCでの50分の実時間性能はこの結果から判定できない。

## 反例を読む

`PASS`（終了コード0）の基準は200列の違反0、50分で認識600回・翻訳1,200回・英中各600字幕、5件の破壊を検出し `restored_matches: true`。破壊の詳細は `mutations` に出る。変換比の整数丸め、補間の無効化、次ブロックへ渡す読み出し位置の消失、パケットの先頭2バイトの欠落、中国語の2番目の訳だけの取り違えをそれぞれ独立に検査する。

`FAIL`（1）は元の実装に対する反例。全探索の場合は `firstViolation` の `rate` と2個の `offsets` を `--case` に渡し、`inputSamples`（受信した入力数）と `outputSample`（0始まり）から最初の違いを再現する。例として44.1 kHz、分割位置 −1、+1 で読み出し位置の持ち越しを消すと、47番のサンプルが期待値 −8106 に対して 32767 になる。50分入力は `lesson.violation` の時刻・期待値・実際値を読む。PCMが一致し中国語だけ異なる場合は同じ発話番号の原文、翻訳結果、宛先を照合する。実モデルで語句が違えば `finals` と元のWAVを確認する。製品の違反は入力列とともに記録する。

`ERROR`（2）は検査を完走できなかったことを示す。依存、モデルの準備、子プロセスの出力、破壊位置の一致と正常対照を調べる。Windowsでイベントループの作成が停止する場合は、asyncioが使う内部ループバック接続を利用できる環境で再実行する。

## コードとの対応を更新する

同期コミットは `c54869f005478e8dc4f2766d5da32baa2402efea`。

| 対象 | 検査と照合する処理 |
|---|---|
| `web/audio-worklet.js`、`web/teacher.js` | `PCM16Downsampler.process` の補間、端数の持ち越し、量子化と送信。先生ページの `ensureMic`、WorkletからWebSocketへの転送。 |
| `server/connection.py`、`server/lesson.py`、`server/pipeline.py`、`server/audio/`、`server/turn/` | `Connection.receive`、授業の開始、`feed_audio`、区間分割・認識・発話の確定、排出。 |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/roster.py`、`server/delivery.py` | 翻訳結果を発話と言語に結び付け、各接続が受信するまで。 |
| `config.yaml`、`server/main.py`、`server/inference*.py`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py` | 出荷モデルの生成、ASR・MTの推論と結果の受け渡し。 |
| `scripts/formal/`、`scripts/make_fixture_audio.ps1` | `teacher_audio.cjs` のページとマイク代役、`teacher_audio.py` の授業再生、`lesson_liveness` と `lesson_endurance` の期待字幕・仮想時計、`classroom_language_demand.start_lesson` と `lesson_audio_boundary_real.read_fixture`。 |

```powershell
git diff c54869f005478e8dc4f2766d5da32baa2402efea -- web/audio-worklet.js web/teacher.js server config.yaml scripts/formal scripts/make_fixture_audio.ps1
```

機器レート、補間の定義やパケット長が意図して変わった場合は、実装と入力時刻の契約を読んでから、整数の期待式、境界、許容差、破壊例を合わせる。全探索・破壊・元実装の再実行と出荷モデルでの照合を確認した後にだけ同期コミットと基準値を進める。未照合の変更が重なった場合は `/maintain-formal-verification-skill` を使う。
