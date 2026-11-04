---
name: caption-recovery
description: 再接続字幕の復元漏れ・誤配信を実コードの有界探索で確認する。Use when changing CaptionDelivery.replay_history, set_language, publish, _process_mt_job, _enqueue_mt, Lesson.join, set_lang, Roster routing, or replay backpressure and reconnect behavior.
---

# 再接続字幕を回収可能に保つ

復元対象の字幕は対象生徒へ届けるか、混雑時は1013切断して再接続で回収可能にする。
黙って失うと、生徒の字幕に穴が残り、接続を維持したまま復元の機会を失う。
復元字幕は対象の接続にだけ送り、翻訳待ちの間に言語が変わったら変更後の言語で送る。

## 実行する

リポジトリ直下で次を実行する。Python 3.12.10 と依存を uv が用意する。生徒ページの復元規則を実行するため Node.js も必要。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python scripts/formal/caption_recovery.py --mutation-check --json .audit/caption-recovery-result.json
```

同じ依存の既存環境では `.venv/Scripts/python.exe scripts/formal/caption_recovery.py --mutation-check` でも実行できる。
`status`、`counts`、`violations`、`witnesses`、`mutations` を読む。
終了コード0は指定範囲内で反例なし、1は反例ありを表す。Traceback は検証器か環境の失敗として調査する。
Windows の制限環境で asyncio の socketpair 生成に停止したら、実行権限を確認する。

Issue #130 の修正後は、全探索が `PASS` となり、反例は0件になる。
[検証範囲と結果](../../../scripts/formal/caption_recovery.md) と [修正前の基準結果](../../../scripts/formal/caption_recovery_baseline.json) を比較する。
`witnesses` の4件は `PASS`、破壊7件は `detected: true` である。
正常な専用ケースに破壊を加えてから予想した違反を確認するため、既存の失敗だけを破壊検出に数えることはない。

## 反例を判断する

`trace` を `Lesson.join`、MT開始、言語変更、新着発話、MT完了の順に実コードと照合する。
`replay_recoverable` は復元対象の未受信seqと1013切断の有無を示す。
`replay_isolation` は復元を要求していない生徒へのseq1の誤送信を示す。
`replay_language` は最初のMT完了前に確定した言語と受信言語の不一致を示す。`screen_recovery` は生徒ページの復元規則に実際の配信内容を渡した後のカードと last_seq を比較する。

実際には制御を譲らない `await` の途中へ割込みを追加していないことを確認する。
MTは必ず成功し、操作列終了後はキューを公平に排出するという前提を守る。
製品で成立する操作で未受信と接続維持が残るなら不具合として報告する。
障害、履歴の期限切れ、別の表示仕様を追加した場合は期待値との関係を検討し、検証器の誤りと不具合を区別する。
保証を弱めて既存の反例を消さず、製品修正は依頼された場合に行う。

## 実装との同期を確認する

最終同期コミットは `d78968533639ca29108318dd9e572407f26c1c10`。結果 JSON の `sync_commit` に出る。
修正前の基準結果は `f4f77289ca4a69b1c401cb39b4f93e5e70cb51a4` のまま比較に使う。

| ファイル | 照合する処理 |
|---|---|
| `server/caption_delivery.py` | `confirm`、`publish`、`_enqueue_mt`、`_mt_worker`、`_process_mt_job`、`replay_history`、`set_language`。言語切替後の履歴更新は操作列で通る範囲のみ |
| `server/lesson.py` | 正しい生徒用の参加コードでの `join`、`set_lang` |
| `server/roster.py` | `add`、`remove`、`retry`、`contains`、`send`、`send_language`、`active_langs` |
| `server/delivery.py` | `ClientSender.send`、`_run`、`aclose` |
| `server/caption_history.py` | `append`、`since` |
| `server/config.py` | MT水位・容量、言語、記録OFF、履歴保持数 |
| `server/mt/fake_engine.py`、`server/inference.py` | 決定的な訳文と正常完了、最初の翻訳の待機境界 |
| `web/caption-recovery.js`、`web/student.js` | 字幕の受理、履歴開始位置、連続受信位置。`student.js` は呼出し側の照合のみ |

```powershell
git diff d78968533639ca29108318dd9e572407f26c1c10 -- server/caption_delivery.py server/pipeline.py server/lesson.py server/roster.py server/delivery.py server/caption_history.py server/config.py server/mt/fake_engine.py server/inference.py web/caption-recovery.js web/student.js
```

対象処理が変わったら、MTの待機境界、復元対象、切断条件、操作集合を照合して検証器を更新する。
`--mutation-check` は一時コピーの製品コードだけを壊す。置換箇所が変わると実行エラーになるので、新実装に合う破壊へ更新する。
製品コードとの一致、全探索、全破壊の検出を確認した後に、最終同期コミットと検証器の `SYNC_COMMIT` をそろえる。
未確認の変更から復旧する場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

Python実装を実行する有限入力列の全列挙であり、無制限の形式証明ではない。
対象生徒1人と復元を要求しない生徒2人、2言語、最初の未完了MT1件を扱う。
5操作の長さ0〜4の全列を水位1と2で探索する。MT容量8、履歴は最大5件で失効しない。
最初のMT結果を最後に解放し、その後の翻訳を成功させて排出する。全await順序の探索ではない。
キャッシュを無効にし、実翻訳モデル、翻訳失敗、タイムアウト、送信障害、履歴失効、ブラウザーの DOM 描画、授業終了・記録は対象外とする。復元規則は Node.js で実行する。接続をまたぐ画面状態、表示中の言語切替、DOM は Edge QA で確認する。
操作・原子性・判定の詳細は [検証範囲と反例](../../../scripts/formal/caption_recovery.md) を読む。
