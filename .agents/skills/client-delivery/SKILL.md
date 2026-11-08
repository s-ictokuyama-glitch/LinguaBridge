---
name: client-delivery
description: 接続ごとの送信容量・順序・障害切断・他接続への分離を実コードの有界探索で確認する。Use when changing ClientSender.send, _run, _send_one, _kill, aclose, or Roster sender allocation and broadcast fault isolation.
---

# 遅い接続の障害を他の生徒へ広げない

接続ごとの待機列を有界に保ち、受理した字幕を順番に送り、溢れ・送信失敗・期限超過ではその接続だけを1013切断する。
違反すると字幕が逆転・欠落するか、1台の不調で他の生徒への配信も止まる。
言語切替後の履歴字幕はライブ字幕用の1枠を残し、空きが足りなければ接続を維持したまま見送る。
正常な接続終了では受理した残りを順番に送り切り、終了後は新しい送信を受理しない。

## 実行する

リポジトリ直下で実行する。Pythonと依存は既存の形式検証用固定版を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python scripts/formal/client_delivery.py --mutation-check --json .audit/client-delivery-result.json
```

同じ依存がある環境では `.venv/Scripts/python.exe -B scripts/formal/client_delivery.py --mutation-check` も使える。
Windowsの制限環境でasyncio内部のsocketpair生成が停止する場合は、実行権限を確認する。
`status: PASS`、終了コード0、`witnesses` の2件が `PASS`、10件の破壊すべてが `healthy: PASS` と `detected: true` であることを確認する。
終了コード1と `violations` は反例を表す。
Traceback、タイムアウト、破壊箇所の不一致は検証器か環境の失敗として調査する。

## 反例を判断する

`violations` は性質ごとに最初の反例を示す。列は短い順に調べるが、容量1を先に全探索するため、容量をまたいだ最短反例ではない。
`trace` の容量と操作順、`expected`、`actual` を [操作と検証範囲](../../../scripts/formal/client_delivery.md) と照合する。
`capacity` は待機列の容量、`history_reservation` はライブ字幕用の1枠と接続の維持、`admission` は停止後の受付を表す。
`fifo` は送信・受信順、`retry_close` は1013切断、`drain` は正常排出、`timeout` は実際の期限超過、`isolation` は正常な生徒への配信を表す。

ソケット送信の待機境界と、12回の制御移譲で進むタスクを実コードと照合する。
製品で成立する操作なら不具合として報告する。検証器のゲートやスケジューラー前提が新実装に合わなければ検証器を修正する。
期待する配送順・隔離・切断条件を弱めて反例を消さず、製品修正は依頼された場合に行う。
既存の [基準結果](../../../scripts/formal/client_delivery_baseline.json) と比較する。

## 実装との同期を確認する

最終同期コミットは `5381edb92062d1a35972b3a7dee8d387c5b0aa1b`。
これは製品コードを照合したコミットであり、スキルの追加コミットとは異なる。

| ファイル | 照合する処理 |
|---|---|
| `server/delivery.py` | `ClientSender` の全メソッド、`alive`、`pending` |
| `server/roster.py` | `add` の送信キュー割当、`send`、`broadcast`、`aclose` の専用隔離ケース |
| `server/caption_delivery.py` | `_refresh_one_caption` の `drop_if_full=True` 呼出しのみ。実行対象外 |
| `server/config.py` | `LimitsConfig.send_queue_max`、`send_timeout_s` の意味 |

```powershell
git diff 5381edb92062d1a35972b3a7dee8d387c5b0aa1b -- server/delivery.py server/roster.py server/config.py server/caption_delivery.py
```

容量に送信中の1件を含めるか、受付・停止・切断の原子性、送信ワーカー数、closeの待機、制御移譲の回数を照合する。
`--mutation-check` は一時コピーの製品コードに10種類の破壊を加える。置換箇所が変わったら新実装の同じ性質を壊す操作へ更新する。
健全なケースの成功と各破壊の予想した違反を確認する。元の作業ツリーの製品コードは変更しない。
製品コードとの一致、全探索、全破壊の検出を確認した後だけ、この同期コミットと検証器の `SYNC_COMMIT` を同じ値へ進める。
基準結果を更新する場合は反例が増減した理由を記録する。
未確認の変更から復旧する場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

Python実装への有限操作列の全列挙であり、無制限の形式証明ではない。
単一送信ワーカー、容量1と2、8操作の長さ0〜4を調べる。候補9,362列、成立する1,211列である。
操作の間は固定のスケジューラーを使い、全await順序は探索しない。末尾の正常排出は長さ上限の外で行う。
TimeoutErrorの結果注入と、実際の期限0の `wait_for` を別々に確認する。正の期限での経過時間は検証しない。
2接続の隔離は実際のRosterで4回broadcastする専用ケースであり、全列挙との直積ではない。
ソケットのcloseは完了すると仮定する。現在の `aclose` はclose完了を期限なしで待つため、終了全体の時間上限は保証しない。
実ネットワーク、ブラウザー、字幕復元、再参加、先生置換、同時aclose、外部からのタスクキャンセルは対象外とする。
詳しい原子性、障害注入、範囲外は [操作と検証範囲](../../../scripts/formal/client_delivery.md) を読む。
