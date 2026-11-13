---
name: asr-backlog
description: ASR待ち行列の容量・順序・キャンセル後の排出完了を実コードの有界探索で確認する。Use when changing AsrBacklog.offer, next, join, its waiting/active accounting, or the single-consumer contract used by Pipeline._asr_worker and drain.
---

# ASR待ちを安全に排出する

受理したイベントを投入順に一度ずつ取り出し、待機中と処理中の音声を容量に数え、正常終了・例外・キャンセル後には処理中の容量と完了待ちを解放する。
違反すると全生徒の字幕が欠落・逆転するか、授業終了時の記録保存が待ち続ける。

## 実行する

リポジトリ直下で実行する。Python と依存は既存の形式検証用固定版を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python scripts/formal/asr_backlog.py --mutation-check --json .audit/asr-backlog-result.json
```

同じ依存がある環境では `.venv/Scripts/python.exe -B scripts/formal/asr_backlog.py --mutation-check` も使える。
Windows の制限環境で asyncio の内部 socketpair 生成が停止する場合は実行権限を確認する。
`status: PASS`、終了コード0、破壊5件すべての `healthy: PASS` と `detected: true` を確認する。
`FAIL` と終了コード1は反例を示す。Traceback・タイムアウト・破壊箇所の不一致は検証器か環境の失敗として調査する。

## 反例を判断する

`violations` は性質ごとの最短反例を示す。`trace` の操作を [操作と検証範囲](../../../scripts/formal/asr_backlog.md) で読み、実コードの呼出し順と `expected` / `actual` を照合する。
`capacity` は確定音声の受理・破棄、`interim_admission` は途中認識の受理・省略、`fifo` はイベント同一性と順序、`accounting` は容量・件数・統計、`completion` は `join` の待機と完了を表す。
単一コンシューマーと実際の制御移譲点を守っているなら製品不具合として報告する。複数コンシューマーなど成立しない操作や、スケジューラー前提の誤りなら検証器を修正する。
既存の反例は保持し、製品コードの修正は依頼された場合に行う。

## 実装との同期を確認する

最終同期コミットは `4efcb9f6edd2b1e39aea9c173d9b8b29870861f1`。

| ファイル | 照合する処理 |
|---|---|
| `server/asr_backlog.py` | `AsrBacklog` の全メソッドとプロパティ、`OfferResult`、`FlushTurn` |
| `server/audio/vad.py` | `Segment`、`InterimSegment`、`TurnBoundary` の定義 |
| `server/pipeline.py` | `start`、`_enqueue_asr`、`_asr_worker`、`drain` の ASR 側。呼出し順と単一コンシューマー契約のみ |

```powershell
git diff 4efcb9f6edd2b1e39aea9c173d9b8b29870861f1 -- server/asr_backlog.py server/audio/vad.py server/pipeline.py
```

対象が変わったら、容量の規則、イベントの種類、`next` の待機境界、ワーカー数、`join` の意味を照合する。
`--mutation-check` は一時コピーの製品コードを5通り壊して確認する。置換箇所が変わったら、新実装の同じ性質を壊す操作へ更新する。
製品との一致、全探索、各破壊の検出を確認した後だけ、この同期コミットと検証器の `SYNC_COMMIT` を同じ値へ進める。
未確認の変更を取り込んだ場合は `/maintain-formal-verification-skill` を使う。

## 保証の限界を守る

Python 実装への有限操作列の全列挙であり、無制限の形式証明ではない。独立した TLA+ モデルは使わない。
12操作の長さ0〜4の全列から単一コンシューマーで成立する列を検証する。音声は0.5・1・2秒、容量1.5秒、途中認識の下限1秒、16kHzとする。
空なら容量超過の確定音声1件も受け入れる既存規則を含む。各列の末尾は正常に排出する。
ASR推論自体の成否・制限時間、executor の残留処理、VAD、Turn 組立、配信、記録、複数ワーカー、キューのメモリー総量は対象外。
`Pipeline` は呼出し契約を照合するだけで、このコマンドでは実行しない。
