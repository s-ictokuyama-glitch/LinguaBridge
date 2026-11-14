---
name: student-continuity
description: 生徒の再接続・言語切替と履歴復元が重なっても、授業のPCMから英中字幕まで継続することを実コードの有界探索と50分仮想入力で検査する。Use when changing CaptionDelivery live/replay priority, queue backpressure, history refresh, Lesson student join/set_lang/leave, or Pipeline progress during student recovery.
---

# 生徒の復元中も授業の字幕を届ける

再接続や言語切替による履歴復元中も、新しく受理した音声を、接続し続ける英中生徒へ順序どおり一度ずつ届ける。違反すると、ある生徒の復元処理が授業全体の字幕を止めるか欠落させる。

既存の [caption-recovery](../caption-recovery/SKILL.md) は復元対象の回収と宛先、[morph-lesson](../morph-lesson/SKILL.md) は生徒の再接続なしの音声継続を扱う。このスキルはその接続部分を検査する。

## 全検証を実行する

リポジトリ直下でPython 3.12.10と固定依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/student_continuity.py --mutation-check --json .audit/student-continuity.json
```

同じ依存がある既存環境では次も使える。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/student_continuity.py --mutation-check --json .audit/student-continuity.json
```

Windowsの制限環境でasyncioのsocketpair生成が停止した場合は、ローカル通信の実行権限を確認する。環境の停止を製品の反例に数えない。

終了コード0と `status: PASS`、`failed: 0`、空の `violations` を確認する。`attempted` は2,801、`legal_passed` は1,083、`illegal` は1,718である。固定4ケースと50分入力が成功し、破壊14件の全件が `healthy: PASS` と `detected: true`、`restored: PASS` と `restored_matches: true` であることを確認する。

[基準結果](../../../scripts/formal/student_continuity_baseline.json) と [選定理由・検証範囲](../../../scripts/formal/student_continuity.md) を読む。`--bounded-only` は50分入力を省く。`--sustained-only --seconds 300` は短い連続入力、`--witness priority` は新着優先の固定ケースだけを実行する。短縮実行を最終確認の代用にしない。

## 反例を判断する

終了コード1と `FAIL` は性質の違反、2と `ERROR` は検証器または環境の失敗を示す。元コードがFAILなら反例を保持し、破壊検出は実行しない。FAILを成功扱いの期待値に変更しない。

`violations` の `property`、`trace`、`expected`、`actual` を読み、入力PCM、ASR完了、生徒操作、MT完了の順を実コードと照合する。

- `morph_turns` と `bilingual_delivery` は確定発話と固定英中生徒への新着字幕の内容・順序・欠落・重複を比較する。
- `live_priority` は投入済みの復元とライブが競合したときのライブ優先を調べる。すでに実行中の復元MT1件は完了を待つ。
- `recovery_retry` と `recovery_delivery` は混雑時の1013切断と、その後の空き時の再参加で字幕を回収できることを調べる。
- `selected_language` は新しく字幕を送信キューへ入れる時点の言語を比較する。以前に投入した字幕の遅着は許す。
- `drain_completion`、`worker_liveness`、`stop_completion` は排出・稼働継続・停止後の回収を調べる。
- `sustained_deadline`、`audio_admission`、`retained_state_bound`、`stats_continuation` は連続入力中の進捗、音声破棄、指定コンテナーの保持量、統計配信を調べる。

`*_reached` は競合状態を作れたかを確認する前提条件である。失敗時は入力計画と容量設定を先に確認する。破壊の置換不一致、子プロセスの例外・時間切れ・JSONなしは検出成功に数えない。

言語切替の履歴更新は混雑時に打ち切る契約であり、全履歴の到達を要求しない。再接続先の字幕は生徒側で重複排除・整列するため、固定生徒の新着と同じ順序保証を課さない。観測地点や合法性が誤っていれば検証器を直す。前提が一致する反例は残し、製品修正は依頼された場合に行う。

## 実装との同期を確認する

同期コミットは `b86592318a919b0d1b934a56002e3fd457b8b0f7`。

| 対象 | 照合する処理 |
|---|---|
| `server/lesson.py` | 生徒の `join`、`set_lang`、`leave` と授業開始 |
| `server/caption_delivery.py` | `publish`、`_enqueue_mt`、`_mt_worker`、`_process_mt_job`、`replay_history`、`set_language`、`_refresh_one_caption`、取消しと排出 |
| `server/pipeline.py` | PCM受理、ASRワーカー、morph確定、MT投入待ち、統計配信、開始と停止 |
| `server/audio/ingest.py`、`server/audio/vad.py`、`server/asr_backlog.py`、`server/turn/` | PCM変換、energy判定、音声と境界の順序、確定条件 |
| `server/roster.py`、`server/delivery.py` | 生徒登録・除去、送信先と送信キュー、1013切断、停止 |
| `server/caption_history.py`、`server/mt/cache.py` | 履歴・キャッシュ・受信控えの保持 |
| `server/inference.py`、`server/asr/base.py`、`server/asr/hallucination.py`、`server/mt/base.py` | 標準Futureの完了と結果の扱い |
| `server/config.py`、`config.yaml`、`server/ws_protocol.py` | 出荷設定、容量、言語とメッセージ形式 |
| `scripts/formal/morph_lesson.py`、`scripts/formal/lesson_liveness.py`、`scripts/formal/lesson_endurance.py` | 入力台帳、比較、PCM認識、Executor、callback処理、仮想時計 |

```powershell
git diff b86592318a919b0d1b934a56002e3fd457b8b0f7 -- server config.yaml scripts/formal/morph_lesson.py scripts/formal/lesson_liveness.py scripts/formal/lesson_endurance.py
```

待機境界・復元条件・容量・配信形式が変わったら、入力台帳と到達条件を照合する。製品コードとの一致、全探索、50分入力、破壊検出と復元を確認した後だけ、この同期コミット、検証器の `SYNC_COMMIT`、基準JSONを更新する。同期先には対象ファイルと同じ内容を持ち、新規cloneでも参照できるコミットを使う。未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証範囲を守る

Python実コードへの有限入力検査であり、無制限の形式証明や全asyncio順序の網羅ではない。7操作の長さ0〜4の全列を調べ、操作後は16回イベントループを進める。存在しないFutureの完了、切断中の言語変更などは違法列として除外する。末尾ではASRとMTが公平に成功し、送信が正常に完了すると仮定する。

探索操作は `sentence`、`asr`、`mt`、`disconnect`、`rejoin`、`set_en`、`set_zh`。既存履歴3件と未完了の復元を用意し、新着PCMを入れてから操作列を適用する。固定英中生徒2人と再参加する生徒1人、MT容量4・復元水位3を使う。背圧と回収の固定ケースは容量・水位を1へ縮める。空き時の復元成功では残件を1件にする。水位より多い履歴を一括復元しても成功するという保証には使わない。

50分は100msのPCM30,000フレームを、発話ごとの排出待ちなしで入力する。事前の履歴3件に続く新着600発話を英中各600字幕と比較し、再参加300回・言語切替600回を重ねる。morphを使い、ASRと各言語MTはそれぞれ0.8仮想秒で成功する。確定予定から8仮想秒の進捗期限と末尾10仮想秒の排出を確認する。統計は既定の2秒間隔で配信する。保持上限は観測したコンテナーとタスクだけで、プロセス全体のメモリー量ではない。

PCMと計算は識別可能な代役、VADはenergy、分類はsurface、途中字幕と記録は無効。物理マイク、実ブラウザー、Silero、WebSocket受信、実LAN、実モデルの精度と実時間性能、推論失敗・永久ハング、記録保存は対象外。未翻訳履歴による推論競合は固定ケース、翻訳済み履歴の反復復元は50分入力で検査する。実モデルの50分計測は [lesson-endurance](../lesson-endurance/SKILL.md) の別コマンドを使う。
