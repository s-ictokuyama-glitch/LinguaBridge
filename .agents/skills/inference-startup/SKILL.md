---
name: inference-startup
description: 推論プロセスのモデル準備中の期限・取消し・停止と、再生成後の準備停止による字幕継続の欠落を実コードで検証する。Use when changing ProcessInferenceRunner startup, generation retirement, factory or warmup, or investigating captions stopping during model reload.
---

# モデルの再準備をまたぐ字幕継続を確認する

モデル準備を待つ要求が期限切れ・取消しになっても共有する準備を壊さず、準備完了後は後続の音声を翻訳し、停止時には全子プロセスを回収する。
これに加え、授業途中で再生成したモデルの準備が永久停止した場合にも、後続字幕を再開できるかを検査する。
違反すると、先生が話し続けても生徒の翻訳字幕が更新されない。

## 実行する

リポジトリ直下で Python 3.12.10 と固定依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/inference_startup.py --mutation-check --json .audit/inference-startup.json
```

同じ依存がある場合は `.venv/Scripts/python.exe` でも実行できる。
Windows の制限環境で `socket.socketpair` の初期化が停止したら、実行権限を確認して同じコマンドを再実行する。これは製品の反例ではない。

終了コードと JSON の `status` を両方確認する。
現行コードは再生成後の準備永久停止から自動復旧しないため、基準結果は `FAIL` である。既知の失敗を成功扱いに変えない。
[基準結果](../../../scripts/formal/inference_startup_baseline.json) と [検証範囲・反例](../../../scripts/formal/inference_startup.md) を照合する。
`bounded` は60ケースの `PASS`、`native` は準備を解放する4対照の `PASS` と解放しない4ケースの `startup_recovery` 違反を確認する。`native` の全8ケースで `pipeline_stop_returned` と `all_children_reaped` を確認する。
`mutations` は破壊3件の `detected: true` と `restored: PASS`、準備停止の感度対照の `intervened: PASS` と `restored: FAIL` を確認する。感度対照は一時コピーにだけ適用する介入で、正常な遅い準備の共有を壊すため修正案には使わない。
Traceback、外側監視の期限超過、破壊箇所の置換不一致、結果なしは検証器か環境の失敗として調査する。

## 反例を判断する

1. 正常な遅い準備の対照が成功し、準備を完了させた後の英中字幕が届くことを確認する。
2. 永久停止ケースでは停止を解放していないこと、後続入力、同じ子 PID の生存、字幕の欠落を確認する。
3. `Pipeline.stop` の完了と子 PID の消滅を字幕再開とは別に判定する。
4. 一時コピーの破壊で該当する性質が失敗し、復元すると元の結果へ戻ることを確認する。

各要求は期限どおりに終了していても、授業の字幕は復旧しない場合がある。
短い要求期限ごとに正常なモデル準備をやり直さない動作は意図されたものなので、単純な再起動を正解にしない。
実コードで成立する反例は保持し、製品コードの修正は別の依頼で行う。

## 実装との同期を確認する

最終同期コミットは `c39c85913ed4f1b12cc611464918c984520cff2c`。

| 対象 | 確認する契約 |
|---|---|
| `server/inference_process.py` | `_serve` の factory・warmup、`_prepare`、`_get_generation`、`run` の起動待ち・期限・取消し、`_retire`、`_dispose`、`aclose` |
| `server/inference.py` | 結果型と `finish_cleanup` |
| `server/pipeline.py` | `start`、`_run_warmup`、`feed_audio`、`_asr_worker`、`_process_segment`、`_publish_turn`、`drain`、`stop` |
| `server/caption_delivery.py` | `warmup`、`publish`、`_mt_worker`、`_translate`、`drain`、`stop` |
| `server/main.py` | 本番が ASR・MT に `ProcessInferenceRunner` を構築する契約。起動の配線を照合する |
| `server/config.py`、`config.yaml` | 要求ごとの期限とモデル設定。専用の準備期限はない |
| `scripts/formal/lesson_liveness.py` | 再利用する音声入力・決定的計算・受信記録の代役 |
| `scripts/formal/lesson_endurance.py` | 再利用する `pid_exists` のOS上の生存確認 |

```powershell
git diff c39c85913ed4f1b12cc611464918c984520cff2c -- server/inference_process.py server/inference.py server/pipeline.py server/caption_delivery.py server/main.py server/config.py config.yaml scripts/formal/lesson_liveness.py scripts/formal/lesson_endurance.py
```

世代の所有権、準備待ちの共有、await 境界、期限の起点、取消し、子プロセス回収との対応を確認する。
製品の自動復旧を追加して `native` が `PASS` になった場合は、既知の `FAIL` を前提にした感度対照も更新する。その対照の `ERROR` を製品の退行とは扱わず、新しい実装で準備の復旧を壊す変更へ差し替え、破壊検出と復元後の `PASS` を確認する。正常な遅い準備を共有する対照は維持する。
実コードとの一致、全ケース、破壊検出と復元を確認した後だけ、この同期コミット、検証器の `SYNC_COMMIT`、基準結果を更新する。
未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証範囲を守る

Python の実コードに対する有限操作列の全列挙と実プロセスの固定ケースであり、全入力・全 OS スケジュールの形式証明ではない。
全列挙は逐次の deadline・cancel の長さ0〜3、停止位置2箇所、終端release・closeの60ケースに限る。実プロセスの字幕経路はASR・MT、停止位置2箇所、準備の解放有無の8ケースとする。
計算と音声は決定的な代役を使う。準備の停止は factory と warmup に注入する。
観測用の期限は製品の復旧時間の保証ではない。実モデルでの自然発生頻度、50分の実時間性能、意味的精度、マイク、実 LAN、画面、記録保存は対象外。
通常の長時間入力と認識・翻訳本体の永久停止は [lesson-endurance](../lesson-endurance/SKILL.md)、スレッド版の呼出し制御は [inference-lifecycle](../inference-lifecycle/SKILL.md) を併用する。
