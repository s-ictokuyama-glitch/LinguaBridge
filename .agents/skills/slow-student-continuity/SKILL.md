---
name: slow-student-continuity
description: 遅い生徒の送信停止・失敗・期限超過があっても、授業のPCMから正常な英中生徒への字幕が続くことを実コードの有界探索と50分仮想入力で確認する。Use when changing ClientSender fault isolation, Roster per-client senders, CaptionDelivery live/replay delivery, or sustained Pipeline delivery under slow student connections.
---

# 通信の遅い生徒がいる授業を検査する

1台の生徒端末への送信が止まっても、先生の音声から接続を維持する正常な英中生徒への翻訳字幕を順番どおり一度ずつ届け続ける。
違反すると、1台の通信不調で授業全体の字幕が止まるか、送信待ちが増え続ける。

[client-delivery](../client-delivery/SKILL.md) の送信処理単体と、送信正常を仮定する [student-continuity](../student-continuity/SKILL.md) の間を担当する。

## 検証を実行する

リポジトリ直下でPython 3.12.10と固定依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/slow_student_continuity.py --mutation-check --json .audit/slow-student-continuity.json
```

同じ依存がある `.venv/Scripts/python.exe` でも実行できる。Windowsの制限環境でasyncioの初期化が停止した場合は実行権限を確認し、環境の停止を製品の反例と数えない。

終了コードとJSONの `status` を両方読む。0と `PASS` は範囲内の成功、1と `FAIL` は性質の違反、2と `ERROR` は環境または検証器の失敗を示す。終了コード1を期待するテストで包んで成功扱いにしない。

[基準結果](../../../scripts/formal/slow_student_continuity_baseline.json) と [選定理由・範囲](../../../scripts/formal/slow_student_continuity.md) を読む。探索の合法列、固定ケース、3000秒の連続入力、破壊の検出、元コードへの復元をそれぞれ確認する。短縮実行は最終検証の代用にしない。

基準は3,110候補、`legal_passed: 180`、`failed: 0`、固定4ケースの `PASS`。`sustained` は30,000フレーム、英中各600字幕、送信満杯・例外・期限超過が各50回である。13件の `mutations` すべてに `healthy: PASS` と `detected: true` があり、`restored: PASS`、`restored_matches: true` であることを確認する。

切分けには `--witness overflow|error|timeout|recovery` の名前を1つ指定する。`--bounded-only` は連続入力を省く。`--sustained-only --seconds 60` は短い連続入力だけを行う。

## 反例と検証器の失敗を分ける

`violations` の `property`、`trace`、`expected`、`actual` を読む。音声入力の台帳から得た発話と訳文を、正常生徒の制御Socketが `send_json` 完了時に保存した字幕へ対応させる。`Roster.send` の成功はキュー受付であり、送信完了とは異なる。障害生徒だけの切断と、正常生徒への後続字幕を分けて確認する。

有限列の `TimeoutError` 注入と、正の期限を実 `asyncio.wait_for` で発火させる固定ケースの両方を確認する。再参加の履歴復元は固定ケースの条件だけを保証する。ライブと復元が交錯する場合の画面上の整列・重複排除は生徒側が担当し、この検証で一般的な受信順序を保証しない。

各操作後のイベントループの進行、送信Future、ASR・MT完了、再参加時の `last_seq` を製品のawait境界と照合する。製品で成立する反例なら記録する。代役が実装の境界を表していなければ検証器を修正する。性質を弱めず、製品コードは修正依頼がある場合に変更する。

故障注入は製品ソースの一時コピーで行う。指定した性質の違反と終了コード1の組合せだけを破壊検出と認める。Traceback、子プロセスの時間切れ、結果なし、置換箇所の不一致を検出成功に数えない。元コードを再実行して、基準と同じ結果へ戻ることも確認する。

## 実装との同期を確認する

同期コミットは `f53aaf2e30b5fab249015e52a67b38b76dd330ec`。

| 対象 | 照合する契約 |
|---|---|
| `server/delivery.py` | `ClientSender.send`、`_send_one`、`_kill`、`aclose`、接続別の容量と期限 |
| `server/roster.py` | `add`、`send`、`broadcast`、`remove`、`aclose`、送信口の所有者 |
| `server/caption_delivery.py`、`server/caption_history.py` | ライブ翻訳、再参加の履歴、受信記録と保持量 |
| `server/pipeline.py` | `feed_audio`、ASR worker、morph確定、翻訳投入、drain、stop |
| `server/audio/ingest.py`、`server/audio/vad.py`、`server/asr_backlog.py`、`server/turn` | PCM、energy判定、morphの分割・確定、音声の順序と容量 |
| `server/lesson.py`、`server/ws_protocol.py`、`server/config.py` | 生徒の再参加、`last_seq`、字幕形式、送信・音声・翻訳の制限 |
| `server/inference.py`、`server/mt/cache.py`、`server/asr/base.py`、`server/mt/base.py` | 制御Futureの完了、計算結果、キャッシュ |
| `scripts/formal/morph_lesson.py`、`scripts/formal/lesson_liveness.py`、`scripts/formal/lesson_endurance.py` | 再利用する音声台帳、決定的計算、Executor、仮想時計、callback処理 |

```powershell
git diff f53aaf2e30b5fab249015e52a67b38b76dd330ec -- server config.yaml scripts/formal/morph_lesson.py scripts/formal/lesson_liveness.py scripts/formal/lesson_endurance.py
```

容量、await、送信停止条件、字幕形式、時計の使い方が変わったら検証器との対応を見直す。実コードとの一致、全探索、50分入力、破壊検出と復元を確認した後だけ、この同期コミット、検証器の `SYNC_COMMIT`、基準結果を更新する。同期先には、対象ファイルと同じ内容を持つ基のブランチのコミットを使う。新規cloneでSHAが解決し、差分照合できることを確認する。

未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証範囲を守る

Python実コードへの有限入力検査であり、無制限の形式証明や全asyncio順序の網羅ではない。全列挙と固定ケースと50分入力の保証を区別する。

送信容量2・3件、6操作の長さ0〜4を全列挙する。正常な英中生徒2人と障害生徒1人を置き、最初の字幕送信を保留して始める。存在しないFutureの完了などは違法列とする。各操作後は16回イベントループを進め、末尾ではASR・MTを正常完了させて新しいPCMの字幕も確認する。最初の反例は容量ごとに短い列から保存するため、容量をまたいだ最短とは限らない。

50分では100ms単位の入力を止めず、20秒ごとに障害生徒を交換する。ASRと言語ごとのMTは各0.8仮想秒、字幕期限は入力計画の確定時点から8仮想秒、末尾の排出は10仮想秒とする。統計の定期配信は間隔3600秒で抑制する。送信待ちの期限は通常3600秒、期限超過の固定ケースは0.5秒、連続入力の期限超過ケースは1秒とし、製品既定5秒の実時間性能を保証しない。

50分は仮想時間である。ASR・MTの計算はIDを返す代役、VADはenergy、Turnはmorphを使う。実モデルの速度・意味精度、Silero、物理マイク、実WebSocket・LAN、ブラウザー、先生交代、記録保存、推論の永久停止は対象外。

ソケットcloseは完了すると仮定する。`ClientSender.aclose` は既に始まったcloseを期限なしで待つため、closeが永久停止した場合の終了時間を保証しない。保持量は観測したコンテナーとタスク数だけを扱う。実時間の50分性能は [lesson-endurance](../lesson-endurance/SKILL.md) の実モデルコマンドで別に確認する。
