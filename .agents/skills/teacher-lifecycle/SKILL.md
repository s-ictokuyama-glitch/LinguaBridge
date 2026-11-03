---
name: teacher-lifecycle
description: 先生交代・切断・再接続と手動操作の授業状態を実コードの有界探索で確認する。Use when changing Lesson.join, leave, control, Roster.add, remove, _detach, _close, or their await boundaries and teacher ownership.
---

# 先生交代で授業状態を巻き戻さない

最後の先生が退出して自動停止した授業は、在籍する先生の参加でだけ再開し、古い参加処理の完了では再開しない。
違反すると、先生がいないのに全生徒へ配信中と通知される。
手動の一時停止・終了は先生の再参加でも維持し、交代済みの先生の退出は現在の先生を止めない。

## 実行する

リポジトリ直下で実行する。既存の形式検証と同じ固定版の Python と依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -B scripts/formal/teacher_lifecycle.py --mutation-check --json .audit/teacher-lifecycle-result.json
```

同じ依存を持つ環境では `.venv/Scripts/python.exe -B scripts/formal/teacher_lifecycle.py --mutation-check` でも実行できる。
Windows の制限環境で asyncio の内部 socketpair 生成が停止する場合は、実行権限を確認する。
`status`、`counts`、`violations`、`mutations` を読む。
終了コード0と `PASS` は指定範囲内で反例なし、1と `FAIL` は反例ありを表す。
Traceback、タイムアウト、破壊箇所の不一致は検証器か環境の失敗として調査する。

現在の製品コードの期待結果は `PASS` である。
[検証範囲と修正結果](../../../scripts/formal/teacher_lifecycle.md) を確認する。
[基準結果](../../../scripts/formal/teacher_lifecycle_baseline.json) は Issue #123 修正前の `FAIL` を保持するため、現在の失敗と混同しない。
破壊6件すべての `healthy: PASS` と `detected: true` を確認する。
正常な専用ケースを壊してから期待した性質の違反と終了コード1を確認するため、既存の反例だけを破壊検出には数えない。

## 反例を判断する

`trace` の操作をコードと照合し、`expected` と `actual` を比較する。
`teacher_owner` は名簿に在籍する先生の同一性と一意性、`session_state` は授業状態、`pause_reason` は自動停止の理由を示す。
各性質について探索範囲内で見つかった最短の反例を残す。全スケジューラーに対する最短性は意味しない。

`begin_join_B_at_close_A` は `Roster.add` が先生Bを登録し、Aを除去してから A の `socket.close(4000)` を待つ時点である。
`release_A` と末尾の `release_A_at_tail` は、その実際の await を解放してBの参加処理を完了する。
Bの参加処理中にBの操作を実行せず、在籍中かつ参加完了済みの先生だけが手動操作できることを確認する。
実際の WebSocket 受信処理で成立する順序なら製品不具合として報告する。
入力の合法性や制御移譲点が実装と異なる場合は検証器を修正する。
製品コードの修正は依頼された場合に行い、性質を弱めて既存の反例を消さない。

## 実装との同期を確認する

修正前の製品コードとの同期コミットは `2a1c1f68ae2c372e03b0bc1a50820cf793f73586`。

| ファイル | 照合する処理 |
|---|---|
| `server/lesson.py` | `join`、`leave`、`control`、`_transition`、`auto_paused`、無音・記録OFF時の終了と再開 |
| `server/roster.py` | `add`、`remove`、`_detach`、`_close`、`teacher`、`contains` |
| `server/delivery.py` | `ClientSender.aclose` からソケット切断までの待機境界 |
| `server/pipeline.py` | `set_accepting_audio`、`on_teacher_joined`、`_asr_worker`、`drain` |
| `server/main.py` | `ws_endpoint` の join 完了前は次の受信へ進まない制約と先生操作の認可。実行対象外 |
| `server/config.py`、`server/ws_protocol.py` | 使用設定と参加メッセージの定義 |

```powershell
git diff 2a1c1f68ae2c372e03b0bc1a50820cf793f73586 -- server/lesson.py server/roster.py server/delivery.py server/pipeline.py server/main.py server/config.py server/ws_protocol.py
```

待機境界、先生の登録と除去の順序、自動停止の理由、手動操作の認可を照合する。
`--mutation-check` は `server/` の一時コピーを6通り壊して検出する。置換箇所が変わったら、新実装の同じ性質を壊す操作へ更新する。
製品との一致、全探索、破壊検出を確認した後だけ、この同期コミットと検証器の `SYNC_COMMIT` を同じ値へ進める。
未確認の変更から復旧する場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

Python 実装への有限操作列の全列挙であり、無制限の形式証明ではない。独立した TLA+ モデルは使わない。
逐次探索は7操作の長さ0〜4の列、競合探索は固定の先生A・Bの交代待ちに続く6操作の長さ0〜4の列を対象とする。
先生Cの参加とAの切断解放は各1回までとし、合法な操作の各完了時と末尾の待機解放後を比較する。
同じ文字の再参加は新しいソケットを作り、逐次探索の退出はその文字の最後に参加した接続を使う。
空の音声入力、記録OFF、正常な送信、既知の参加コードを使う。ASRワーカーは空のFlushTurnを排出するだけで、推論を実行しない。
保持する切断は最大1件で、ネットワーク自体、切断失敗、キャンセル、同一ソケットでの役割変更、生徒、認証、未完了の音声・翻訳・保存、任意の並行control、さらに多い先生や長い操作列は対象外。
