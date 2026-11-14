---
name: teacher-continuity
description: 先生の切断・再参加・交代をまたぎ、受理済み音声と再開後の音声が英中字幕へ届くことを実コードの有界探索と50分仮想入力で確認する。Use when changing Connection.receive or handling, Lesson.leave or join, Roster teacher replacement, or Pipeline and CaptionDelivery behavior with pending audio or translation at teacher disconnect.
---

# 先生の再参加後も英中字幕が届くことを確認する

切断前に受理した音声を確定し、再参加後の発話も英中生徒へ順序どおり一度ずつ届ける。手動の一時停止は再参加しても維持し、交代済み先生の音声は受理しない。違反すると、画面が配信中でも字幕が欠けるか、旧先生の音声が混入する。

既存の [teacher-lifecycle](../teacher-lifecycle/SKILL.md) は無音、[morph-lesson](../morph-lesson/SKILL.md) は先生の切断・交代なしで検査する。このスキルは処理待ちの音声・翻訳と先生再参加の接続部分を扱う。

## 全検証を実行する

リポジトリ直下でPython 3.12.10と固定依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/teacher_continuity.py --mutation-check --json .audit/teacher-continuity.json
```

同じ依存を持つ環境では次のコマンドも使える。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/teacher_continuity.py --mutation-check --json .audit/teacher-continuity.json
```

Windowsの制限環境でasyncioの内部socketpair生成が停止した場合は、ローカル通信の実行権限を確認する。環境の停止を製品の反例に数えない。

終了コードとJSONの `status` を照合する。0と `PASS` は指定範囲で反例なし、1と `FAIL` は性質の違反、2と `ERROR` は検証器または環境の失敗を示す。

- 4,681候補中 `legal_passed: 210`、`illegal: 4471`、`failed: 0`、固定6ケースの `PASS` を確認する。
- `sustained` は3000秒、28,500 PCMフレーム、再参加300回、受理900断片、英中各750字幕を確認する。先生不在時はPCMを送らない。
- `mutations` の12件すべてで `healthy: PASS` と `detected: true` を確認する。検証器は一時コピーの製品コードを壊し、終了コード1と指定した性質の違反を照合する。
- `restored: PASS` と `restored_matches: true` を確認する。元コードで全検査を再実行し、破壊前の結果と一致させる。

[基準結果](../../../scripts/formal/teacher_continuity_baseline.json) と [検証範囲・選定理由](../../../scripts/formal/teacher_continuity.md) を参照する。`--witness asr_pending` は固定ケースの切り分け用。`--bounded-only` は50分入力を省き、`--sustained-only --seconds 240` は長時間入力を短縮する。最終確認は既定の範囲で行う。

## 反例を実コードと照合する

`violations` の `property`、`trace`、`expected`、`actual` を読む。`lesson_state` は自動再開と手動停止の保持、`bilingual_delivery` は受理音声から両言語字幕までの欠落・混入・重複・順序を表す。`sustained_deadline`、`retained_state_bound`、`audio_admission`、`stop_completion` は継続中の進捗、保持量、破棄、停止後の回収を表す。`*_reached` は指定した状況へ到達したかを確認する前提条件である。

`trace` が実際の受信順とawait境界で成立するか確認する。同じ接続ではjoinが戻ってから次のフレームを処理する。`takeover` の旧先生PCMは、新先生登録後に旧ソケットのcloseを待つ間に送る。終了済み接続への再入力ではない。

入力と期待値が実装に対応していれば製品の反例として残す。検証器の待機点や合法性が誤っていれば検証器を直す。性質を弱めて失敗を消さず、製品修正は依頼された場合に行う。

## 実装との同期を確認する

最終同期コミットは `7c6d2ba2deb734f69e15661b4d61fd4e35867380`。

| 対象 | 照合する処理 |
|---|---|
| `server/connection.py` | `receive` の参加・操作・PCM認可、`handling` の退出と例外捕捉 |
| `server/lesson.py` | `join`、`leave`、`control`、`_transition`、`shutdown` の自動停止・再開・flush |
| `server/roster.py`、`server/delivery.py` | 先生登録・旧接続除去、送信、close待機、停止 |
| `server/pipeline.py` | 音声受理、flushとASRの順序、確定発話、排出、停止 |
| `server/audio/ingest.py`、`server/audio/vad.py`、`server/asr_backlog.py`、`server/turn/` | PCM変換、分割、待ち行列、morph確定と保留 |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/mt/cache.py` | MT満杯時の待機、起床、配信、保持上限 |
| `server/inference.py`、`server/asr/base.py`、`server/asr/hallucination.py`、`server/mt/base.py` | 標準Futureの完了、認識結果、翻訳契約、停止 |
| `server/config.py`、`server/ws_protocol.py` | 設定とメッセージ形式。`server/main.py` の受信直列化は読むだけで実行しない |
| `scripts/formal/morph_lesson.py`、`scripts/formal/lesson_liveness.py`、`scripts/formal/lesson_endurance.py` | 再利用するLessonCase、Recognition、Fragment、Executor、Socket、比較、callback処理、仮想時計 |

```powershell
git diff 7c6d2ba2deb734f69e15661b4d61fd4e35867380 -- server scripts/formal/morph_lesson.py scripts/formal/lesson_liveness.py scripts/formal/lesson_endurance.py
```

待機境界や音声・確定条件が変わったら台帳と入力計画を見直す。破壊の置換箇所が変わったら、新しい実装で同じ契約を壊す操作へ更新する。製品との一致、全探索、50分入力、破壊検出と復元を確認した後だけ、この同期コミット、検証器の `SYNC_COMMIT`、基準JSONを更新する。同期先には検証対象と同じ内容を持ち、新規cloneからも参照できるコミットを選ぶ。未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証範囲を守る

Python実コードへの有限入力検査であり、無制限の形式証明や全asyncio順序の網羅ではない。操作列の末尾ではASRとMTが公平に完了し、ソケットcloseも正常に完了すると仮定する。入力台帳から独立に期待値を作り、末尾の追加flushより前に欠落を判定する。

50分は100ms入力とASR・各言語MTそれぞれ0.8仮想秒の固定計画で、発話ごとの排出待ちはしない。確定予定から8仮想秒の進捗期限、末尾10仮想秒の排出を調べる。PCM・計算・ソケットは代役、VADはenergy、morph分類はsurface、partialは無効、統計配信は3600秒間隔である。

物理マイク、実ブラウザーとLAN、実モデルの精度・実時間50分性能、推論失敗や永久ハング、授業記録の保存、生徒の再接続と履歴復元は対象外。既存の関連スキルの保証を置き換えない。
