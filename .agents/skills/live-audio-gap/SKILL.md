---
name: live-audio-gap
description: 配信中のまま先生の音声パケットが途絶えたとき、無音警告と発話中表示が復帰し、途絶前後に受信した音声と次の発話が英中字幕に届くかを有界検証する。Pipeline のマイク監視、SpeakingSignal、Silero の発話状態、morph の保留、または入力復帰時の字幕欠落を調べるときに使う。
---

# 配信中の音声途絶から字幕が続くかを調べる

同じ先生の接続が配信中のまま音声の送信を止め、同じ発話の残りを送り始める。停止前に届いた音と認識済みの節を保ち、続きと次の発話を英語・中国語の生徒へ一度ずつ届ける。これが壊れると、発話中表示が復帰しても、途中までの説明が訳文から抜ける。

[既存の入力途絶試験](../../../docs/qa/2026-10-24-issues-55-54.md) は発話中表示の復帰を確認する。無音の PCM が届き続ける場合は [morph-silence](../morph-silence/SKILL.md)、先生が一時停止・終了を操作した場合は [lesson-audio-boundary](../lesson-audio-boundary/SKILL.md) にそれぞれ検証手順がある。

## 実行

リポジトリ直下で Python 3.12.10 と[固定依存](../../../scripts/formal/requirements.txt)を使う。Python で有限個のパケット到着時刻を列挙し、製品の受信処理、2秒周期の監視、字幕ワーカーにそのまま入力する。監視の時刻と実際に各生徒へ送られたメッセージを同じ実行で比較できる。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/live_audio_gap.py --mutation-check --json .audit/live-audio-gap.json
```

同じ依存を持つ Windows の開発環境では次のコマンドで実行できる。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/live_audio_gap.py --mutation-check --json .audit/live-audio-gap.json
```

同期時点では短い384通りすべてが成功した。先生への警告が0回の列が210通り、1回が148通り、2回が26通り。50分の固定列で英中各850字幕、警告8回を受信した。`mutations` の7件がすべて `detected: true` となり、元に戻した後の全探索と50分列も初回と一致した。

## 入力と判定

1. 先生1人と英語・中国語の生徒各1人が参加する。先生は開始後、100 ms単位で16 kHzのPCM16を送る。「温度が高いので」の有音0.8秒、無音0.8秒、「体積が増えます。」の有音0.8秒、無音1.6秒、その次の「次は実験です。」の有音0.8秒、無音1.6秒を順に送る。最初の2節が1枚、次の文が2枚目の字幕になる。
2. 1回目は第1〜24パケットの各直後、2回目は次の文の途中の第44パケット直後に送信を止める。各回の追加待ちを0、29.9、30、34秒から独立に選び、24 × 4 × 4 通りを全列挙する。待ち時間中は無音データも送らず、復帰したら入力列の次のパケットを渡す。途絶中に録音されなかった言葉の復元は扱わない。
3. 実際の監視処理を2仮想秒ごとに起こす。各監視時刻から直前30秒の有音到着を入力台帳で調べ、警告の送信先・時刻・回数と照合する。しきい値を超えると発話中表示が消え、次の有音で戻る。同時刻の操作はPCM受信を先に処理し、各時刻でイベントループを16周進める。
4. ASR に入った有音サンプルを入力台帳と、無音を含む全サンプルを途絶なしの対照とハッシュで照合する。次の文を送る前とその文末の2か所で、先生の確定原文と生徒が受信した字幕の番号・原文・言語・訳文を比較する。
5. 同じ授業・接続で6.4秒の入力列を425回繰り返す。50回目から50回おきに計8回、35秒の無入力を入れる。発話途中と、認識済みの節を morph が保留している時点を交互に使う。音声27,200パケットと無入力280秒で計50仮想分になる。各文の後にワーカーの排出を待つ操作は行わない。

有界探索では Silero の確率、ASR、翻訳計算、接続先を決定的な代役にする。製品の音声分割、Turn 確定、過去の訳の再利用、字幕送信は実処理を通る。50分列は同じ2文を繰り返すため翻訳キャッシュが働く。推論の負荷、任意のスケジュール、学校のマイク・LAN・ブラウザーや実時間50分の性能は保証範囲外。

## 出荷モデルで照合

開発環境のアプリ依存と出荷モデルを用意する。音源がなければ Windows で `./scripts/make_fixture_audio.ps1 -BaseOnly` を実行して日本語の合成音源を生成する。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/live_audio_gap_real.py --json .audit/live-audio-gap-real.json
```

モデルの場所は `config.yaml` の `models.dir` を使う。`--models-dir` で別の場所を指定できる。同期時は faster-whisper 1.2.1、ONNX Runtime 1.31.0、llama-cpp-python 0.3.33。Silero、Haas ONNX、morph、Hy-MT2、製品の推論子プロセスを使う。先生・生徒のメッセージは `Connection.receive` と送信先ソケットの代役で観測する。

`02.wav` の先頭1秒で発話が開いたことを確認し、実時間で30秒以上PCMを送らず、先生に無音警告が届いてから残りを送る。配信を続けたまま `03.wav` も送る。途絶あり・なしでASR入力のハッシュと確定原文が一致し、各言語でその回の翻訳モデルが返した文字列が字幕に届くことを確認する。同期時は「光合成には日光と水と二酸化炭素が必要です」と「教科書の42ページを開いてください」の順に英中各2件が届いた。音源ごとに処理を排出して観測する固定例で、訳文の意味精度は採点しない。

## 失敗の読み方と保守

- `PASS`・終了0。全探索の件数、50分列の `lesson`、7件の破壊検出、`restored_matches` を読む。
- `FAIL`・終了1。`first_counterexamples` の `case` を `--case` に渡して再現する。例えば `--case 16 340 340` は、第1節を認識した後と次の文の途中で34秒ずつ途絶させる。一時コピーで警告時に morph の保留を消すと、最初の字幕が「体積が増えます。」だけになり、`teacher_finals` が失敗する。`voiced_pcm` はASR前の有音の欠落、`asr_pcm` は無音を含めた波形の相違、`captions_zh` は中国語の受信内容、`warnings` は先生への通知、`speaking_en` は英語の生徒の発話中表示を示す。対照の失敗は `control_failure`、長い列の失敗は `lesson_counterexample` を読む。
- `ERROR`・終了2。依存、モデル、音源、起動、破壊用コードの置換箇所を調べる。全探索の子プロセスには実時間90秒の期限がある。受信処理や終了処理で停止した場合は `--case` で絞り込む。Windows で asyncio の開始時に停止する環境ではローカルソケットが使える状態で再実行する。入力の有音・無音が意図した位置にあり、製品に同じ順序で届いていることを確認したうえで再現した反例は保持する。

同期コミットは `6a02562738895e6cb2665a25d5cda1b7cd8de949`。

| 対象 | 照合する処理 |
|---|---|
| `config.yaml`、`server/config.py` | 30秒の警告、2秒の監視周期、Silero と morph の開始・確定条件 |
| `server/connection.py`、`server/lesson.py` | `receive`、`join`、`control` による同じ先生からの配信 |
| `server/pipeline.py`、`server/speaking.py` | `feed_audio`、`_stats_worker`、`_check_mic_silence`、`_refresh_speaking`、`SpeakingSignal` の点灯・消灯と警告の再通知 |
| `server/audio/`、`server/asr_backlog.py`、`server/turn/` | PCMの端数・プリロール、有音と長い無音、`add_segment` と `on_boundary` が扱う保留中の節 |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/mt/cache.py`、`server/{roster,delivery,ws_protocol}.py` | 確定順、言語別の翻訳・配信、ソケットで受けるメッセージ |
| `server/main.py`、`server/inference_process.py`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py` | 実音源照合のモデル構築、ASR に渡す波形、言語と訳文の対応 |
| `scripts/formal/live_audio_gap*.py` と同ディレクトリの `lesson_audio_boundary{,_real}.py`、`lesson_endurance.py`、`lesson_liveness.py`、`morph_silence.py`、`silero_contract.py` | 入力台帳、時計、VAD の代役、モデル入力の観測、破壊と復元 |

```powershell
git diff 6a02562738895e6cb2665a25d5cda1b7cd8de949 -- config.yaml server scripts/formal scripts/make_fixture_audio.ps1
```

判定周期、警告の意味、発話分割が意図して変わった場合は、入力台帳、期待する字幕、対応する破壊例を根拠とともに更新する。対象実装と照合し、全探索、破壊と復元、実音源の対照を再確認してから同期コミットを進める。未確認の変更が重なった場合は `/maintain-formal-verification-skill` を使う。
