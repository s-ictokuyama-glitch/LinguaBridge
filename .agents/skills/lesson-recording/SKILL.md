---
name: lesson-recording
description: 授業記録の所属・欠落と保存失敗時の保護を実コードの有界探索で確認する。Use when changing Lesson.control, Pipeline.on_lesson_state, finalize_recording, _publish_turn, _process_segment, drain, or SessionRecorder, or investigating recordings across lesson end and restart.
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

既存の同じ依存環境では `.venv/Scripts/python.exe scripts/formal/lesson_recording.py` でも実行できる。
結果の `counts`、`violations`、`mutations` を読む。
`PASS` と終了コード 0 は指定範囲内で反例なしを表す。`FAIL` と終了コード 1 は反例ありを表す。
Traceback は検証器か環境の失敗として調査し、反例と区別する。
Windows の制限環境で asyncio が内部 socketpair の生成に停止したら、実行権限を確認する。

## 反例を判断する

`trace` をコードの呼び出し順に追い、`expected` と実際の JSONL の `actual` を比べる。
既存の反例と抽象化の境界は [検証範囲と結果](../../../scripts/formal/README.md) を読む。
現在のコードには未完了 ASR を伴う反例がある。既知という理由で終了コードを成功に変えない。
命題を弱めたり、検出した製品コードをこのスキルの作成作業として修正したりしない。
入力が製品で成立することと、await の境界を確認できたら不具合として報告する。
注入した失敗や想定した原子性がコードと合わなければ、検証器の誤りとして修正して再実行する。

## 変更との同期を確認する

最終同期コミットは `30c3d455829c7ebb6615383c634d89fe41d87759`。
この値は製品コードを最後に照合したコミットであり、スキルを追加したコミットとは異なる。

| ファイル | 照合する処理 |
|---|---|
| `server/lesson.py` | `Lesson.control`、`_transition`、`set_recording` |
| `server/pipeline.py` | `on_lesson_state`、`finalize_recording`、`_publish_turn`、`_process_segment`、`_asr_worker`、`feed_audio`、`flush_audio`、`drain` |
| `server/recorder.py` | `SessionRecorder.add`、`write`、`_write_markdown` |
| `server/asr_backlog.py` | `AsrBacklog.next`、`join`、`is_idle` |
| `server/turn/assembler.py` | simple 戦略の `add_segment` と `flush` |
| `server/config.py` | VAD、Turn、記録、ASR の制限時間 |
| `server/asr/fake_engine.py` | 入力振幅と認識文の対応 |

`git diff 30c3d455829c7ebb6615383c634d89fe41d87759 -- server/lesson.py server/pipeline.py server/recorder.py server/asr_backlog.py server/turn/assembler.py server/config.py server/asr/fake_engine.py` を確認する。
対象処理が変わったら、操作集合、障害注入、時間予算、期待する所属を実装に照らして更新する。
`--mutation-check` は一時コピーの製品コードから記録切替と保存待ちをそれぞれ削除し、検出を確認する。
実行元の製品コードは変更しない。破壊用の置換箇所が見つからなければ、新しい実装に合わせて見直す。
製品コードとの一致を確認して全探索と破壊検出を実行した後だけ、同期コミットを進める。
未確認の変更から復旧する場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

実装に入力列を全列挙する有界検証であり、無限の入力や全システムの形式証明ではない。
この検証は Python の実装を直接実行する。独立した TLA+ モデルは使用しない。
通常操作は初期 start と確定発話1件、6操作の長さ0〜4の全列、末尾 end を対象とする。
非同期部分は未完了 ASR 1件と end、restart、次の確定発話の4順序を start と pause の両再開で調べる。
ASR 上限は90秒とし、30秒の drain 超過を即時例外で最大2回注入する。実時間の経過は検証しない。
保存失敗は write 前と最初の Markdown 書込前の OSError を対象とする。
発話順、seq、時刻、翻訳、Markdown の内容、実モデル、通信、クラッシュ、同時 control は対象外。
