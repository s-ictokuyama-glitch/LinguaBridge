---
name: inference-lifecycle
description: 推論の期限切れ・キャンセル・停止後も直列性と呼出結果を保つことを実コードの有界探索で確認する。Use when changing InferenceRunner.run, stop, executor ownership, or ASR and MT inference timeout and cancellation boundaries.
---

# 推論の待機終了と実行終了を区別する

同じ InferenceRunner の推論は、呼出側が期限切れやキャンセルになっても直列に実行し、各要求に対応する結果だけを返す。違反すると、ASRや翻訳モデルの並行呼出し、不要な待ち行列、別要求の結果の混入につながる。

この有界探索は、明示的なエンジン・Executor注入で使うスレッド版 `InferenceRunner` を対象とする。
本番の `ProcessInferenceRunner` の永久ハング復旧は [lesson-endurance](../lesson-endurance/SKILL.md)、起動・取消し・停止の競合は `tests/unit/test_inference_process.py` で確認する。

## 実行する

リポジトリ直下で実行する。Python 3.12.10 の標準ライブラリだけを使う。

```powershell
uv run --python 3.12.10 --no-project python -X utf8 -B scripts/formal/inference_lifecycle.py --mutation-check
```

同じ版の既存環境では `.venv/Scripts/python.exe -X utf8 -B scripts/formal/inference_lifecycle.py --mutation-check` も使える。Windowsの制限環境で内部 socketpair の生成が停止した場合は、実行権限を確認して同じコマンドを再実行する。

終了コード0、`status: PASS`、`attempted: 111111`、`legal: 1850`、`native_witnesses: 4`、6件すべての `detected: true` を確認する。`--json .audit/inference-lifecycle-result.json` で結果を保存できる。元の `server/` は変更せず、一時コピーの `inference.py` を壊して検出する。

## 反例を判断する

`violations` の `property`、`trace`、`expected`、`actual` を読む。終了コード1は性質の違反、Tracebackや実行期限超過は検証器か環境の失敗として調査する。

- `caller_outcome` は成功値の要求ID、例外の種類、期限切れ、キャンセルの対応を確認する。`timeout=None` で処理自体が投げた TimeoutError は InferenceFailed である。
- `execution_order` は開始と完了の順序、重複実行を確認する。
- `queued_cancellation` は未着手のキャンセルと停止時の待ち行列の取消しを確認する。実行中の Future は呼出側のキャンセル後も残る。
- `nonblocking_stop` は実スレッドで、稼働中の処理を解放する前に stop が戻ることを確認する。stop用の別スレッドが開始してから2秒以内の帰還を要求する。OSのスケジュールに依存するため、期限違反はスレッドが実際に稼働したかも調べる。
- `serial_execution` は既定の実スレッドで、最初の処理を保留中に後続処理が始まらないことを確認する。

たとえば `keep_queued_on_stop` の反例は、`ok,ok,stop` の順に1件を実行中、もう1件を待機中にして停止すると、2件目の Future がキャンセルされないことである。

単独の列は `--case ok,ok,stop` で再現する。単独列では実スレッド4ケースと全列挙を省くため、変更の検証は最後に既定コマンドで実行する。検証側のずれを調べるときは、[検証範囲と境界](../../../scripts/formal/inference_lifecycle.md) と実装の await を照合する。保証を弱めて反例を消さず、製品コードの修正は依頼された場合に行う。

## 実装との同期を確認する

最終同期コミットは `5aa0ca374beb563376b247eee60fd811e954e977`。検証器の `SYNC_COMMIT` と [基準結果](../../../scripts/formal/inference_lifecycle_baseline.json) にも記録する。

| ファイル | 照合する処理 |
|---|---|
| `server/inference.py` | InferenceRunner の既定 Executor、run の投入・Future接続・例外分類・取消し、stop の停止指定。直接の検証対象 |
| `server/pipeline.py` | 同じASR runnerを使う warmup、途中認識、確定認識、停止。呼出側の照合のみ |
| `server/caption_delivery.py` | 同じMT runnerを使う warmup、翻訳、停止。呼出側の照合のみ |
| `server/main.py`、`server/asr/`、`server/config.py`、`config.yaml` | ASRの選択と各エンジンの warmup・transcribe の呼出契約。ソースで照合する |
| `tests/unit/test_inference.py` | Executor の制御方式と通常テストとの対応 |

```powershell
git diff 5aa0ca374beb563376b247eee60fd811e954e977 -- server/inference.py server/pipeline.py server/caption_delivery.py server/main.py server/asr/ server/config.py config.yaml tests/unit/test_inference.py
```

対応する実装が変わったら、Futureの状態遷移、待機のキャンセル伝播、Executorの直列性を照合する。通常版の全探索、実スレッド、全破壊の検出を確認した後で、同期コミットと基準結果を更新する。破壊の置換箇所が一致しなければ、新実装に合う破壊へ更新する。未確認の変更から同期を戻すときは `/maintain-formal-verification-skill` を使える。

## 保証の限界を守る

Python実装を実行する有限入力列の全列挙であり、SMT検証や無制限の形式証明ではない。制御用 Executor では最大2要求、10操作の長さ0〜5を調べ、各列末尾で残った推論を完了して停止する。既定の実スレッドでは3要求を使う4ケースを調べる。

制御用 Executor は標準 Future のキャンセル規則と単一実行先の契約を仮定する。単独の期限切れは timeout=0、通常の待機は timeout=None で表す。操作間で8周回のコールバック処理を進め、完了とキャンセルだけは同じ周回の両順序も調べる。全await順序や全スレッド順序の網羅を意味しない。

Haas-ja のモデル読込み、内部スレッド、認識結果を含む実モデルの動作は対象外。GPU、処理中の副作用、ASRとMTを同時に使う経路、字幕・翻訳キャッシュ・記録への反映、正の期限と完了の同時競合、遅延の上限は対象外。既存の [asr-backlog](../asr-backlog/SKILL.md) と [caption-recovery](../caption-recovery/SKILL.md) の検証も併用する。
