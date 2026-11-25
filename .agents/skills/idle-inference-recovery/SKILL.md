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

修正前の `75d2c42` では正常対照4件が成立し、子を終了させた12件すべてで欠落を検出した。最後の D は全条件で両言語に届いていた。次の表は修正前の受信列を示す。

| 発話 A の後に終了した子 | 次が新しい B のときに観測した受信列 |
|---|---|
| なし | 先生・英・中すべて A、B、C、D |
| 音声認識 | 先生・英・中すべて A、C、D。B は採番前に失われる |
| 翻訳 | 先生と中国語は A、B、C、D。英語だけ A、C、D |
| 両方 | 先生と中国語は A、C、D。英語は A、D |

修正前に翻訳だけを終了させて A を繰り返すと、キャッシュ有効時は2回目の A も届き、次に初めて翻訳する C の英語が抜けた。このためキャッシュを使った直後も C・D まで照合する。

`ProcessInferenceRunner._get_generation` は準備済みの子を再利用する前に生存を確認する。終了済みなら回収を待ち、新しい子を準備してからその要求を送る。修正後は全16条件で、先生・英語・中国語に発話1〜4が順に一度ずつ届いた。

## 教材と出荷モデルで確認する

開発機の通常のアプリ依存、出荷モデル、`tests/fixtures/ja/{03,02,07,05}.wav` を用意する。音源は `./scripts/make_fixture_audio.ps1 -BaseOnly` で生成できる。同期時の推論ライブラリは onnxruntime 1.31.0、faster-whisper 1.2.1、llama-cpp-python 0.3.33。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/idle_inference_recovery.py --real --json .audit/idle-inference-native.json
```

`--models-dir` でモデル保存先を指定できる。「教科書」「光合成」「質問」「実験」の4発話を、出荷設定の Silero・Haas ONNX・morph・Hy-MT2 で順に処理する。キャッシュ有効・次の発話 B に固定し、終了対象は4通りとも試す。先生の確定原文に教材の識別語があり、生徒が受け取った字幕の日本語と番号が先生のものと一致し、訳文がその原文・言語に対する実モデルの応答に一致することを調べる。`frames` には実際の原文と訳文が残る。修正後は4条件とも3人に4発話が届いた。

対象は発話間の1箇所で確認済みのプロセス終了。推論中のクラッシュ、全OSスケジュール、物理マイク・LAN・ブラウザー描画、自然発生頻度、訳の意味精度、実時間50分の性能はこの結果からは判断できない。推論中の期限切れとモデル再準備の停止は [lesson-endurance](../lesson-endurance/SKILL.md)・[inference-startup](../inference-startup/SKILL.md) を参照する。

## 検出力と実装の同期

`--mutation-check` は製品コードの一時コピーに、2文目の先生の番号をずらす、中国語だけ2文目を送信しない、音声認識の代替プロセスを起動できなくする、の3変更を個別に加える。それぞれ対照では成立した `next.teacher`、`next.zh`、`following_bilingual` が破壊時に失敗し、復元で同じ受信列に戻ることを確認する。`mutations` の3件と、元の実装で再び全16条件の結果が一致したことを示す `restored_matches: true` を確認する。製品の反例が残っている場合、破壊検査が成功しても全体は終了コード1を返す。

下のコマンドは検証済みの `server/inference_process.py` が入ったコミットを現在の履歴から探す。squash マージ後も同じファイル内容を基準に照合できる。

| 対象 | 変更時に照合する箇所 |
|---|---|
| `server/inference_process.py` | `_serve`、`_get_generation`、`_exchange`、`run`、`_retire`、`_dispose` の準備・要求・通信失敗・世代回収 |
| `server/main.py`、`server/config.py`、`config.yaml` | `create_app` の認識・翻訳プロセスの構築、出荷モデル、morph、キャッシュ設定 |
| `server/connection.py`、`server/lesson.py`、`server/pipeline.py` | `receive`、`join`、`control`、`feed_audio`、`_process_segment`、`_publish_turn`、`drain` の順序 |
| `server/caption_delivery.py`、`server/mt/cache.py`、`server/caption_history.py` | `confirm`、`publish`、`_translate`、`_process_mt_job` の採番、言語順、再利用、失敗後の次ジョブ |
| `server/roster.py`、`server/delivery.py`、`server/ws_protocol.py` | 送信対象、接続別キューの排出、確定原文と字幕の受信形式 |
| `scripts/formal/` | `idle_inference_recovery.py`、`idle_inference_mutations.py` と、再利用する `classroom_language_demand.start_lesson`、`lesson_liveness` の計算・受信代役、`lesson_audio_boundary_real.read_fixture` |

```powershell
$idleBaseline = git log --reverse --format=%H --find-object=6c34074545f1b6ef6654b970289570e851b6c1f0 -- server/inference_process.py | Select-Object -First 1
git diff $idleBaseline -- server config.yaml scripts/formal
```

変更後は障害なしの対照と1列の反例を再現し、終了が次のPCM受理より前であること、失われた入力または言語を製品コードと照合する。仕様を意図して変えた場合は、その根拠に照らして期待値と入力を更新する。全探索、3破壊と復元、教材での照合を終え、検証が新しい実装を表していると確認したコミットへ同期点を進める。未検証の変更が重なったら `/maintain-formal-verification-skill` を使う。
