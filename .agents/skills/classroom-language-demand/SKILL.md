---
name: classroom-language-demand
description: 授業中に生徒が参加・退出したとき、先生の音声を一度認識し、必要な言語ごとに一度翻訳して各生徒へ届けることを有界検証する。CaptionDelivery.publish の宛先確定、翻訳中の参加・退出、同一言語の人数による推論回数の増加、満席時の字幕欠落を調べるときに使う。
---

# 生徒の出入りをまたぐライブ字幕を検証する

生徒が0人の間も先生の発話を認識する。確定時に選ばれている各言語への訳は生徒全員で共有し、その時点から送信まで接続を保った対象の生徒に、その言語の字幕が一度ずつ届く。翻訳中に新規参加した生徒は、次に確定する発話から受け取る。宛先を誤ると、先生の画面では授業が進んでいても、同じ言語を選んだ一部の生徒だけ字幕を読めなくなる。

既存の規模試験は固定人数での推論回数を測る。ここでは、最後の一人が抜ける時点と最初の一人が加わる時点を動かし、各接続が実際に受信した発話番号・原文・言語・訳文の列まで比較する。[規模試験](../../../scripts/scale_bench.py)・[再接続と履歴復元の検証](../student-continuity/SKILL.md)

## 実行方法

リポジトリ直下で Python 3.12.10 と[固定依存](../../../scripts/formal/requirements.txt)を使う。次のコマンドで全探索、50分相当の入力、一時コピーでの破壊検査、元の実装での再実行を行う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/classroom_language_demand.py --mutation-check
```

同じ依存がある開発機では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/classroom_language_demand.py --mutation-check` でもよい。JSON の `status` と終了コードを確認する。同期時点で `PASS`（0）、290ケース成功、7件すべて `detected: true`、`restored_matches: true`。

## 探索する出席と音声

3席に英・英・中または中・中・英を割り当てる。入れ替え前と後の出席をそれぞれ8通り取り、次の時点でまとめて退出・新規参加を行う。同じ席で接続を保つ生徒も、後で同じ席に戻る新しい接続も識別する。

| 入れ替える時点 | 成立する条件 | 3席の列数 |
|---|---|---:|
| `asr` 認識結果がワーカーへ戻る直前 | 全出席パターン | 128 |
| `mt0` 最初の訳を配信する直前 | 確定時に1言語以上 | 112 |
| `mt1` 一方の言語で送信が済み、もう一方の訳を配信する直前 | 確定時に英中の両方 | 48 |

1列につき異なる4発話を16 kHzのPCM16で入力する。最初の発話の途中で出席を変え、2番目は変更後の出席のまま送る。元の出席に戻して、3・4番目は間で排出を待たずに送る。期待する翻訳先は入力した出席から計算する。確定後に退出しても、既に投入された言語の翻訳は完了し得る。

288列に、同時64人の固定2列を加える。一つは最初の認識中に0人から英中各32人になり、後に0人へ戻る。もう一つは満席で英語の32人が退出し、英語の別の32人が入る。中国語の32人は接続を保つ。言語別人数が同じでも宛先の取り違えを検出できる。

続く50分相当の入力では、100 msのPCMを30,000パケット送り、5秒ごとに異なる発話を1件入れる。5分ごとに、無人・英のみ・英中・中のみ・英中・英のみ・無人・中のみ・英中・英中と出席を変える。各言語は32人。仮想500 msごとに待機中のASRとMTを各1件まで完了させ、各区間の末尾で全接続の受信列を照合する。発話ごとの排出や授業の一時停止は挟まない。基準は認識600回、翻訳720回、受信字幕のべ23,040件。`--lesson` でこの入力だけを実行できる。

Python で有限列を全列挙し、実装の `Connection.receive` に音声を渡す。関数の写しに対する証明では追いにくい、認識結果の受け渡し・実際の参加者名簿・接続別の送信完了を一緒に観測するためである。有限列と50分入力ではenergy VAD、音声の振幅から日本語を返す認識器、言語と原文を返す翻訳器を使う。morphの確定、ワーカー、送信キューは製品コードを通る。

## 出荷モデルで満席の授業を照合する

通常のアプリ依存とモデルがある開発機では次も実行する。音源がない場合は `./scripts/make_fixture_audio.ps1 -BaseOnly` で生成する。モデルは `config.yaml` の `models.dir` を使い、別の場所を使う場合は `--models-dir` で指定できる。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/classroom_language_demand.py --real
```

Silero・Haas・morph・Hy-MT2に日本語音源03、02、07、05をこの順に渡す。「教科書」「光合成」「質問」「実験」で音源と認識の対応を確かめ、各接続の受信内容を、その原文・言語に対して推論プロセスが実際に返した訳と照合する。同期時点の2列はともに認識4回。0人からの参加では翻訳4回・字幕128件、満席での入れ替えでは翻訳8回・字幕256件。確認環境は onnxruntime 1.31.0、faster-whisper 1.2.1、llama-cpp-python 0.3.33。

この探索の境界は新規参加（履歴要求なし）、固定言語、正常な送信先である。マイクからの収録、LAN、ブラウザーの描画、任意のスケジュール、訳の意味精度、サーバーPCが実時間50分で処理できる速度は対象外。実時間の授業性能は [lesson-endurance](../lesson-endurance/SKILL.md) で扱う。

## 反例の再現と保守

`FAIL`（1）では `first_counterexamples` または `lesson_counterexample` の手順・期待値・実測値を読む。例えば `--case EEZ 101 011 mt0` は、英語の1席目と中国語の3席目が出席し、翻訳中に英語の1席目が抜けて2席目が加わる列。訳が戻った時点の名簿で宛先を作り直す破壊では、2席目に参加前の発話1が届く。もう一つの例は言語ごとの先頭にしか送らない破壊で、翻訳回数は正常でも英語の2人目が発話1を受信できない。

破壊検査ではこの2例に加えて、無人時にASRを止める、同じ音声を二度認識する、不在の言語も翻訳する、先生の発話番号をずらす、50件を超えた後の中国語配信を止める変更を個別に検出する。各破壊の前に同じ入力が元の実装で通ること、破壊後は期待した性質で終了コード1になることを確認する。

`ERROR`（2）は検査未完了を示す。モデルの準備、依存、停止したワーカー、破壊箇所の置換が現在の実装に合うかを確認する。実モデルの語が変わった場合は該当WAVの認識結果、送信先の違反なら宛先を確定した時点と接続の生存期間を先に照合する。意図した契約変更では入力と期待値を一緒に更新し、製品の反例は再現手順とともに残す。

同期コミットは `734b4a867f0391ad9bd0a835606a72f7bb92f26f`。

| 変更箇所 | 照合する処理 |
|---|---|
| `server/connection.py`、`server/lesson.py` | 先生の `receive`、`join`、`leave`、`control` と新規参加時の履歴要求 |
| `server/pipeline.py`、`server/audio/`、`server/turn/`、`server/asr_backlog.py` | `feed_audio` から認識、Turnの確定、`_publish_turn`、排出まで |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/mt/cache.py` | `confirm`、`publish` の言語別宛先、`_translate` の再利用、`_process_mt_job` の送信先 |
| `server/roster.py`、`server/delivery.py`、`server/ws_protocol.py` | 接続の追加と退出、IDの照合、接続別キューの送信と字幕形式 |
| `server/main.py`、`server/inference*.py`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py`、`config.yaml` | モデルの生成、推論結果をワーカーへ戻す位置、言語・人数・Turnの出荷設定 |
| `scripts/formal/lesson_liveness.py`、`scripts/formal/lesson_audio_boundary_real.py`、`scripts/make_fixture_audio.ps1` | モデル代役、Future完了、受信Socket、日本語音源 |

```powershell
git diff 734b4a867f0391ad9bd0a835606a72f7bb92f26f -- config.yaml server scripts/formal/lesson_liveness.py scripts/formal/lesson_audio_boundary_real.py scripts/make_fixture_audio.ps1
```

変更箇所の契約と観測点を読み直し、全探索・破壊・復元を再実行する。モデルや音声経路を変えたときは出荷モデルの2列も再確認する。照合後にだけ同期コミットと実測値を進める。再検証を伴わない変更が重なった場合は `/maintain-formal-verification-skill` を使う。
