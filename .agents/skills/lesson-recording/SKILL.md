---
name: lesson-recording
description: 授業記録の所属・欠落と保存失敗時の保護を実コードの有界探索で確認する。Use when changing Lesson.control, shutdown, its recording finalizer, Pipeline._publish_turn, drain, or SessionRecorder, or investigating recordings across lesson end and restart.
---

# 授業記録を授業ごとに保つ

記録対象の発話は、その発話を受け付けた授業の記録に一度だけ保存する。
違反すると、先生の授業記録が欠落するか、前授業の内容が次授業へ混ざる。
保存に失敗した場合は、未保存の記録を保持したまま再開を拒否する。

## 実行する

リポジトリ直下で次を実行する。Python 3.12.10 と依存を uv が用意する。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python scripts/formal/lesson_recording.py --mutation-check --json .audit/lesson-recording-result.json
```

既存の同じ依存環境では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_recording.py --mutation-check` でも実行できる。
結果の `counts`、`violations`、`mutations` を読む。
`PASS` と終了コード 0 は指定範囲内で反例なしを表す。`FAIL` と終了コード 1 は反例ありを表す。
Traceback は検証器か環境の失敗として調査し、反例と区別する。
Windows の制限環境で asyncio が内部 socketpair の生成に停止したら、実行権限を確認する。

## 反例を判断する

`trace` をコードの呼び出し順に追い、`expected` と実際の JSONL の `actual` を比べる。
既存の反例と抽象化の境界は [検証範囲と結果](../../../scripts/formal/README.md) を読む。
未完了 ASR と保存待機の期限を交錯させ、処理中の再開拒否、追加操作なしの元授業への最終保存を確認する。
期待する所属と件数を維持する。反例が出たら、製品不具合と検証器の誤りを区別する。
入力が製品で成立することと、await の境界を確認できたら不具合として報告する。
注入した失敗や想定した原子性がコードと合わなければ、検証器の誤りとして修正して再実行する。

## 変更との同期を確認する

最終同期コミットは `d78968533639ca29108318dd9e572407f26c1c10`。結果 JSON の `sync_commit` に出る。

| ファイル | 照合する処理 |
|---|---|
| `server/lesson.py` | `Lesson.control`、`_transition`、`set_recording`、`shutdown`、`_record_utterance`、`_finalize_lesson`、`_finish_recording`、`_recording_finished` |
| `server/pipeline.py` | `set_accepting_audio`、`_publish_turn`、`_process_segment`、`_asr_worker`、`stop`、`feed_audio`、`flush_audio`、`drain` |
| `server/recorder.py` | `SessionRecorder.add`、`write`、`_write_markdown` |
| `server/asr_backlog.py` | `AsrBacklog.next`、`join`、`is_idle` |
| `server/turn/assembler.py` | simple 戦略の `add_segment` と `flush` |
| `server/config.py` | VAD、Turn、記録、ASR の制限時間 |
| `server/asr/fake_engine.py`、`server/inference.py` | 入力振幅と認識文の対応、制御可能な executor を使う ASR 待機 |
| `server/caption_delivery.py` | `confirm`、`publish`、`drain`。生徒不在で翻訳ジョブは発生しない |

`git diff d78968533639ca29108318dd9e572407f26c1c10 -- server/lesson.py server/pipeline.py server/recorder.py server/asr_backlog.py server/turn/assembler.py server/config.py server/asr/fake_engine.py server/inference.py server/caption_delivery.py` を確認する。
対象処理が変わったら、操作集合、障害注入、時間予算、期待する所属を実装に照らして更新する。
`--mutation-check` は一時コピーで記録器の交換と再開前の保存待ちを削除し、暫定保存を誤って封印する破壊も加えて検出を確認する。
実行元の製品コードは変更しない。破壊用の置換箇所が見つからなければ、新しい実装に合わせて見直す。
製品コードとの一致を確認して全探索と破壊検出を実行した後だけ、最終同期コミットと検証器の `SYNC_COMMIT` をそろえる。
未確認の変更から復旧する場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

実装に入力列を全列挙する有界検証であり、無限の入力や全システムの形式証明ではない。
この検証は Python の実装を直接実行する。独立した TLA+ モデルは使用しない。
通常操作は初期 start と確定発話1件、6操作の長さ0〜4の全列、末尾 end を対象とする。
非同期部分は未完了 ASR 1件と end、restart、次の確定発話の4順序を start と pause の両再開で調べる。
ASR 上限は90秒とする。保存完了を待つ間にイベントループの時計を30秒進め、保存待機の期限切れを最大2回起こす。バックグラウンドの排出は継続し、拒否された再開は完了後に再試行する。実時間の経過は検証しない。
保存失敗は write 前と最初の Markdown 書込前の OSError を対象とする。
暫定保存は遅着結果を補うため更新し、最終保存されたファイルだけ不変性を確認する。
発話順、seq、時刻、翻訳、Markdown の内容、実モデル、通信、サーバー停止、クラッシュ、同時 control はこの有界探索の対象外。
通常の pytest は別途、morph、複数 ASR、遅延翻訳、記録OFFでの再開拒否、WebSocket を閉じる前の終了通知と保存を確認する。
