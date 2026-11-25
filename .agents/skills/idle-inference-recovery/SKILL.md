---
name: idle-inference-recovery
description: 発話間に待機中の音声認識・翻訳の子プロセスが終了したとき、次の発話から英中の字幕が続くかを実プロセスで有界検証する。ProcessInferenceRunner の世代の再利用・回収を変えたときや、先生が話し直すと字幕が戻る、一方の言語だけ一文抜けるときに使う。
---

# 推論プロセスの終了後、次の一文が届くかを調べる

前の一文を訳し終えた推論プロセスが発話間で終了しても、次に受け付けた音声から先生に確定原文、英語と中国語の生徒に対応する字幕が順番に一度ずつ届くべきである。復旧に最初の要求を使い切ると、授業は進んでいるのに、生徒は説明を一文聞き逃す。

## 実行

リポジトリ直下で Python 3.12.10 と[固定依存](../../../scripts/formal/requirements.txt)を使う。Python で有限の入力を列挙し、製品が起動した子プロセスとの通信、終了の検出、次世代の起動、各受信者までの配信を直接観測する。通常の全探索にはモデルの重みは不要。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/idle_inference_recovery.py --mutation-check --json .audit/idle-inference-recovery.json
```

同じ依存が入った開発機では次のコマンドで、音声認識だけを終了させた1列を再現できる。引数は終了させる子、次の発話、翻訳キャッシュの順。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/idle_inference_recovery.py --case asr B on
```

## 16条件の境界

1. 先生と英中の生徒各1人が参加し、先生が配信を開始する。最初の発話 A を先生の `Connection.receive` に100 ms単位の16 kHz PCM16で送り、認識・翻訳・接続別の送信を排出する。3人全員に発話1が届いたことを確認する。
2. 終了させる子を `none`、`asr`、`mt`、`both` の4通りから選ぶ。指定した子が生きていて、推論要求が完了していることを確認する。この授業が生成した子のプロセスハンドルを使って終了させ、OSが終了を確認してから次の音声を入れる。
3. 先生が A を繰り返す場合と、新しい B を話す場合を調べる。ここで一度排出し、続く C・D は間で排出を待たずに入力する。翻訳キャッシュは出荷設定（512件）と無効の2通り。終了対象4 × 次の発話2 × キャッシュ2の全列を、毎回新しい授業で実行する。
4. 入力列の位置から正しい発話番号を決め、各観測点で3人の受信列を照合する。生徒の字幕では番号・日本語・言語・訳文を検査する。最後の D が3人に同じ番号で届いたかも別に記録し、途中の欠落と停止の継続を区別する。

発話の識別には energy VAD、有音サンプルの値を読む認識器、原文と言語に対応する固定訳を返す翻訳器を使う。認識器と翻訳器を別々の実プロセスで起動し、プロセス間通信、morph の確定、翻訳キャッシュ、字幕配信を製品コードで通す。

## 結果の読み方

- **0 / PASS** は指定した全列が成立したことを示す。修正後の全探索の期待値は16件成功。**1 / FAIL** は受信列の違反。JSON の `cases[].pcm` は投入済みの発話、`exited` は終了を確認した子、`violations` は観測点・受信者別の期待値と実測値を示す。
- `before_exit` に差があれば、障害注入前から経路が成立していない。その列の障害注入は行わない。`next` は次の発話の排出直後、`end` は C・D の排出後。`following_bilingual` は D の受信自体が途絶えた場合にも残る。
- **2 / ERROR** や JSON を出す前の停止は判定未完了。Windows のローカルソケット・プロセス生成、モデル準備、排出待ち、破壊箇所の一致を調べる。実モデルで語が `unknown(...)` と出る場合は、障害なしの同じ教材で原文と識別語を照合する。

同期時点では正常対照4件が成立し、子が終了した12件すべてで欠落を検出した。すべての列で最後の D は両言語に届いた。

| 発話 A の後に終了した子 | 次が新しい B のときに観測した受信列 |
|---|---|
| なし | 先生・英・中すべて A、B、C、D |
| 音声認識 | 先生・英・中すべて A、C、D。B は採番前に失われる |
| 翻訳 | 先生と中国語は A、B、C、D。英語だけ A、C、D |
| 両方 | 先生と中国語は A、C、D。英語は A、D |

翻訳だけを終了させて A を繰り返すと、キャッシュ有効時は2回目の A も届き、次に初めて翻訳する C の英語が抜ける。障害後に同じ文を一度読むだけでは、この欠落を見落とす。

`ProcessInferenceRunner._get_generation` は回収が予約されていない世代を再利用する。待機中に子が終了していると、次の要求で初めて通信失敗を検出し、`run` は世代の回収後にその要求へ `InferenceFailed` を返す。認識ならその音声区間、翻訳ならその言語のジョブがここで終わる。出荷設定のライブ翻訳は英語を先に処理するため、翻訳だけが終了した対照では英語に欠落が現れる。

## 教材と出荷モデルで確認する

開発機の通常のアプリ依存、出荷モデル、`tests/fixtures/ja/{03,02,07,05}.wav` を用意する。音源は `./scripts/make_fixture_audio.ps1 -BaseOnly` で生成できる。同期時の推論ライブラリは onnxruntime 1.31.0、faster-whisper 1.2.1、llama-cpp-python 0.3.33。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/idle_inference_recovery.py --real --json .audit/idle-inference-native.json
```

`--models-dir` でモデル保存先を指定できる。「教科書」「光合成」「質問」「実験」の4発話を、出荷設定の Silero・Haas ONNX・morph・Hy-MT2 で順に処理する。キャッシュ有効・次の発話 B に固定し、終了対象は4通りとも試す。先生の確定原文に教材の識別語があり、生徒が受け取った字幕の日本語と番号が先生のものと一致し、訳文がその原文・言語に対する実モデルの応答に一致することを調べる。`frames` には実際の原文と訳文が残る。同期時の4列で上表と同じ結果を観測した。

対象は発話間の1箇所で確認済みのプロセス終了。推論中のクラッシュ、全OSスケジュール、物理マイク・LAN・ブラウザー描画、自然発生頻度、訳の意味精度、実時間50分の性能はこの結果からは判断できない。推論中の期限切れとモデル再準備の停止は [lesson-endurance](../lesson-endurance/SKILL.md)・[inference-startup](../inference-startup/SKILL.md) を参照する。

## 検出力と実装の同期

`--mutation-check` は製品コードの一時コピーに、2文目の先生の番号をずらす、中国語だけ2文目を送信しない、音声認識の代替プロセスを起動できなくする、の3変更を個別に加える。それぞれ対照では成立した `next.teacher`、`next.zh`、`following_bilingual` が破壊時に失敗し、復元で同じ受信列に戻ることを確認する。`mutations` の3件と、元の実装で再び全16条件の結果が一致したことを示す `restored_matches: true` を確認する。製品の反例が残っている場合、破壊検査が成功しても全体は終了コード1を返す。

同期コミットは `c04eb6546b722dd6aff567b54e3134ba217ed9b0`。

| 対象 | 変更時に照合する箇所 |
|---|---|
| `server/inference_process.py` | `_serve`、`_get_generation`、`_exchange`、`run`、`_retire`、`_dispose` の準備・要求・通信失敗・世代回収 |
| `server/main.py`、`server/config.py`、`config.yaml` | `create_app` の認識・翻訳プロセスの構築、出荷モデル、morph、キャッシュ設定 |
| `server/connection.py`、`server/lesson.py`、`server/pipeline.py` | `receive`、`join`、`control`、`feed_audio`、`_process_segment`、`_publish_turn`、`drain` の順序 |
| `server/caption_delivery.py`、`server/mt/cache.py`、`server/caption_history.py` | `confirm`、`publish`、`_translate`、`_process_mt_job` の採番、言語順、再利用、失敗後の次ジョブ |
| `server/roster.py`、`server/delivery.py`、`server/ws_protocol.py` | 送信対象、接続別キューの排出、確定原文と字幕の受信形式 |
| `scripts/formal/` | `idle_inference_recovery.py`、`idle_inference_mutations.py` と、再利用する `classroom_language_demand.start_lesson`、`lesson_liveness` の計算・受信代役、`lesson_audio_boundary_real.read_fixture` |

```powershell
git diff c04eb6546b722dd6aff567b54e3134ba217ed9b0 -- server config.yaml scripts/formal
```

変更後は障害なしの対照と1列の反例を再現し、終了が次のPCM受理より前であること、失われた入力または言語を製品コードと照合する。仕様を意図して変えた場合は、その根拠に照らして期待値と入力を更新する。全探索、3破壊と復元、教材での照合を終え、検証が新しい実装を表していると確認したコミットへ同期点を進める。未検証の変更が重なったら `/maintain-formal-verification-skill` を使う。
