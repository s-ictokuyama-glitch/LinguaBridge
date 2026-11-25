---
name: translation-backpressure
description: 英中の翻訳待ちが満杯になり、音声認識の容量も使い切った授業で、先生の音声入力、破棄通知、受理済み発話と回復後の翻訳字幕を有界検証する。CaptionDelivery.publish / _enqueue_mt、Pipeline の ASR ワーカー、AsrBacklog の秒数会計を変更するときに使う。
---

# 翻訳待ちが音声認識まで詰まった授業を検証する

翻訳待ちが満杯でも先生の音声受信は進み、容量に入った発話は順番どおりに英中の字幕になる。入りきらない音声を捨てたときは先生に通知し、容量が空けば次の発話を受け取る。ここが壊れると、先生に確定原文が見えていても片方の言語に字幕が届かない、または音声受信そのものが止まる。

[lesson-liveness](../lesson-liveness/SKILL.md) は翻訳待ちが満杯のときも音声破棄が0件の条件を、[asr-overload-recovery](../asr-overload-recovery/SKILL.md) は翻訳待ちに余裕がある条件を扱う。このスキルは、翻訳の遅れで ASR ワーカーが次の音声を取れなくなる場合を扱う。

## 一度のコマンドで確認する

リポジトリ直下で Python 3.12.10 と[固定依存](../../../scripts/formal/requirements.txt)を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/translation_backpressure.py --mutation-check --json .audit/translation-backpressure.json
```

同じ依存が入っている Windows の開発機では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/translation_backpressure.py --mutation-check` でも実行できる。

- **PASS・終了0**。同期時点で短い入力の324通りと50分相当の1通りが成功。233通りで音声を実際に破棄した。6種類の破壊に指定した反例があり、復元後に全325通りを再実行して `restored_matches: true` になった。
- **FAIL・終了1**。`first_counterexamples` の `capacity`、`boundary`、`gaps` を `--case` に渡す。例は `--case 1 terminal 0000`。50分列は `--shipping` で再現する。`trace` の最後の操作で、投入した音声と、期待値・実際値が最初に食い違う場所を読む。
- **ERROR・終了2**。依存、モデル、起動失敗、監視期限、破壊用コードの置換不一致を調べる。Windows で asyncio が起動しない場合はローカルソケットを利用できる環境で再実行する。

## 何を全探索するか

Python の有限列挙で、100 ms ごとの 16 kHz PCM16 を実際の `Pipeline.feed_audio` に渡す。キュー投入を待つ `await`、morph の確定、ASR の容量解放、翻訳ジョブの取り出し、英中それぞれの送信キューを一緒に動かせる。英中の生徒が各1人、翻訳は1件ずつ完了し、別々の文にはキャッシュヒットがない条件を使う。

1. 区別できる発話を6件続け、音声認識の上限を2秒、各 Segment を1秒とする。翻訳待ちの上限は1件または2件。2番目の発話は「です。」で即確定する場合と、「ので」で終わり長い無音で確定する場合を試す。
2. 最初の2発話は翻訳結果を保留する。残る4発話の各直前で、翻訳を0、1、2件のいずれかだけ完了させる。2容量 × 2語尾 × 3の4乗で324通り。各列の最後に排出し、7番目の新しい発話まで両言語に届くことを調べる。
3. 実装が返した破棄件数とは独立に、投入番号と翻訳完了数から受理・破棄を計算する。英中1組に2枠が必要で、使える翻訳枠の累計は「待ち上限＋実行中1件＋完了件数」。即確定では両言語の投入が済むまで処理中の Segment が ASR 容量を占める。無音確定では Segment は返却済みで、サンプルを持たない TurnBoundary が翻訳の空きを待つ。
4. 各入力後、先生の受信処理が翻訳完了を待たずに戻ること、ASR の占有・破棄秒数、翻訳待ち件数、先生への通知を照合する。翻訳を1件解放するたびに原文と言語を確認し、排出後に先生の確定原文、生徒が実際に受信した発話番号・原文・言語・訳文の列を比較する。

全探索の VAD は energy。認識は音声の識別値を日本語に戻し、翻訳計算には原文と言語を返す代役を使う。外部操作の間にイベントループを16周進める。この検査で扱うスケジュールと音声は有限で、推論期限、任意のイベント順、物理マイク、LAN、生徒画面や実時間の処理性能は対象外。

## 出荷容量で50分の入力を流す

同じコマンドで、翻訳待ち256件、ASR 30秒の固定列も実行する。5秒ごとに2.3秒の有音を含む1発話を入れ、1発話がプリロールと末尾無音を含めて3秒の ASR 容量を使う。600発話、計30,000パケット。前半25分は次の発話までに翻訳を最大1件、後半は最大4件完了させる。発話間に全体の排出は待たない。

277〜299番のうち奇数の12発話だけが破棄される。滞留中も278、280番などは容量が空いた時点で受理される。50分の入力では英中各588字幕、排出後の601番を含めて各589字幕になる。50分は投入した PCM の長さであり、サーバーPCの速度はこの結果から推定できない。

## 実モデルと日本語音声で照合する

通常のアプリ依存、出荷モデル、合成音源 `tests/fixtures/ja/{03,02,01,07,05}.wav` を用意する。音源がなければ Windows で `./scripts/make_fixture_audio.ps1 -BaseOnly` を使う。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/translation_backpressure_real.py --json .audit/translation-backpressure-real.json
```

モデルの場所は `--models-dir` で指定できる。同期時は ONNX Runtime 1.31.0、faster-whisper 1.2.1、llama-cpp-python 0.3.33。Silero・Haas ONNX・morph・Hy-MT2 と製品の推論子プロセスを使う。短い音源で境界に達するよう両ケースの翻訳待ちを1件、ASR を9秒に設定し、翻訳キャッシュを無効にする。

対照では5本を1本ずつ排出する。滞留側では先頭の実翻訳結果をワーカーに返す直前で保留したまま4本目まで入力する。2本目の4.192秒と3本目の3.776秒が処理中・待ちに残り、4本目の2.784秒だけが容量超過で破棄された。先生に翻訳遅延と音声破棄の通知が届き、翻訳を解放すると3本目、さらに新しい5本目まで英中両方に届いた。対照5件、滞留側4件の発話番号と原文を照合し、各言語で Hy-MT2 が実際に返した非空の訳文と受信字幕の一致を確認する。

音源を更新して対照が1本1発話にならない場合は、`by_file` と WAV を調べる。実際の認識精度は別の評価軸で、今回は `01.wav` の「今日は植物」を両ケースとも「共和植物」と認識した。

## 反例と同期記録を更新する

`--case 1 terminal 0000` では、発話1の英訳が実行中、中国語訳が待機中になる。発話2は翻訳枠を待ちながら音声1秒を占め、発話3を待ちに入れると ASR は満杯になる。処理中の1秒を容量判定から外す破壊では、破棄すべき発話4が入ってしまう。中国語の再開だけを落とす破壊では、排出後の発話7が先生と英語の生徒に届いても `captions_zh` が失敗する。

一時コピーの6破壊は、音声受信で翻訳を待つ、満杯時に翻訳ジョブを捨てる、処理中の音声を容量から外す、音声破棄の通知を消す、翻訳遅延の通知を消す、回復後の中国語字幕を落とす、の6点。失敗時は `mutations` または `first_counterexamples` の性質名を次の実装と照合する。

同期コミットは `32f740a64d642bcac635a519b90edb7981f70a44`。

| 対象 | 照合する処理 |
|---|---|
| `server/pipeline.py`、`server/asr_backlog.py` | `feed_audio`、`_asr_worker`、`_process_segment`、`_close_pending_turn`、`_publish_turn`、`_notify_overload`、`drain` と `offer` / `next` の容量解放点 |
| `server/caption_delivery.py` | `publish` の言語順、`_enqueue_mt` の満杯待ち、`_mt_worker`、`_translate`、`_process_mt_job` の起床・翻訳・配信 |
| `server/audio/`、`server/turn/`、`server/config.py`、`config.yaml` | PCM16、プリロール、文末・無音による確定、出荷時の容量と使用モデル |
| `server/{inference,inference_process,main}.py`、`server/asr/haas_onnx_engine.py`、`server/mt/hymt_engine.py` | 実モデルの起動と入力・完了の観測 |
| `server/{roster,delivery,caption_history,ws_protocol}.py`、`server/mt/cache.py` | 採番、翻訳キャッシュ、言語別送信と受信フレーム |
| `scripts/formal/{translation_backpressure,translation_backpressure_real,lesson_liveness,lesson_audio_boundary_real}.py` | 入力生成、翻訳完了の制御、受信 Socket、実音源と対照 |

```powershell
git diff 32f740a64d642bcac635a519b90edb7981f70a44 -- config.yaml server scripts/formal scripts/make_fixture_audio.ps1
```

容量解放点や語尾の確定条件が変わったら、まず反例1列の入力・期待値・実装の待機箇所を比較する。意図した契約変更なら根拠とともに期待値を更新し、製品の不具合なら反例を残す。全探索、指定した破壊検出、復元、実モデルの対照を確認してから同期コミットを進める。未確認の変更が重なった場合は `/maintain-formal-verification-skill` を使う。
