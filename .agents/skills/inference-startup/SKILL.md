---
name: inference-startup
description: 推論プロセスの準備期限・取消し・回収と、再準備の永久停止後の字幕復旧を実コードで検証する。Use when changing ProcessInferenceRunner startup, generation retirement, factory or warmup, or investigating captions stopping during model reload.
---

# モデルの再準備をまたぐ字幕継続を確認する

モデル準備を待つ要求が期限切れ・取消しになっても共有する準備を壊さず、準備完了後は後続の音声を翻訳し、停止時には全子プロセスを回収する。
授業途中で再生成したモデルの準備が永久停止した場合は、準備期限後に待機者なしでも子を回収し、後続音声の英語・中国語字幕を再開する。
違反すると、先生が話し続けても生徒の翻訳字幕が更新されない。

## 実行する

リポジトリ直下で Python 3.12.10 と固定依存を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/inference_startup.py --mutation-check --json .audit/inference-startup.json
```

同じ依存がある場合は `.venv/Scripts/python.exe` でも実行できる。
Windows の制限環境で `socket.socketpair` の初期化が停止したら、実行権限を確認して同じコマンドを再実行する。これは製品の反例ではない。

終了コードと JSON の `status` を両方確認する。
[基準結果](../../../scripts/formal/inference_startup_baseline.json) と [検証範囲・反例](../../../scripts/formal/inference_startup.md) を照合する。
`bounded` は60ケース、`native` は全8ケースの `PASS` を確認する。準備を解放する4対照は同じPIDを再利用し、解放しない4ケースは `reaped_without_new_requests: true` と後続ID 3・4の英中字幕を確認する。全8ケースで `pipeline_stop_returned` と `all_children_reaped` を確認する。
`mutations` は破壊4種の `detected: true` と `restored: PASS` を確認する。準備期限の無効化は4条件すべてで `startup_recovery` 違反を検出する。破壊は一時コピーにだけ適用する。
Traceback、外側監視の期限超過、破壊箇所の置換不一致、結果なしは検証器か環境の失敗として調査する。

## 反例を判断する

1. 正常な遅い準備の対照が成功し、準備を完了させた後の英中字幕が届くことを確認する。
2. 永久停止ケースでは停止を解放していないこと、新要求なしでの旧PID回収、後続入力、英中字幕への到達を確認する。違反時も後続字幕の実測値を記録する。
3. `Pipeline.stop` の完了と子 PID の消滅を字幕再開とは別に判定する。
4. 一時コピーの破壊で該当する性質が失敗し、復元すると元の結果へ戻ることを確認する。

各要求は期限どおりに終了していても、授業の字幕は復旧しない場合がある。
短い要求期限ごとに正常なモデル準備をやり直さない動作は意図されたものなので、単純な再起動を正解にしない。
実コードで成立する反例は保持し、修正の前後を同じ経路で確認する。
製品コードは修正を依頼された場合だけ変更する。

## 実装との同期を確認する

最終同期コミットは `dc9c07088472dcaf94350836b33d4a9057020411`。

| 対象 | 確認する契約 |
|---|---|
| `server/inference_process.py` | `_serve` の factory・warmup、`_prepare`、`_get_generation`、`run` の起動待ち・期限・取消し、`_retire`、`_dispose`、`aclose` |
| `server/inference.py` | 結果型と `finish_cleanup` |
| `server/pipeline.py` | `start`、`_run_warmup`、`feed_audio`、`_asr_worker`、`_process_segment`、`_publish_turn`、`drain`、`stop` |
| `server/caption_delivery.py` | `warmup`、`publish`、`_mt_worker`、`_translate`、`drain`、`stop` |
| `server/main.py` | 本番が ASR・MT に `ProcessInferenceRunner` を構築する契約。起動の配線を照合する |
| `server/config.py`、`config.yaml` | 要求期限と独立した正・有限の `limits.inference_startup_timeout_s`。ASR・MTへ明示配線する |
| `scripts/formal/lesson_liveness.py` | 再利用する音声入力・決定的計算・受信記録の代役 |
| `scripts/formal/lesson_endurance.py` | 再利用する `pid_exists` のOS上の生存確認 |

```powershell
git diff dc9c07088472dcaf94350836b33d4a9057020411 -- server/inference_process.py server/inference.py server/pipeline.py server/caption_delivery.py server/main.py server/config.py config.yaml scripts/formal/lesson_liveness.py scripts/formal/lesson_endurance.py
```

世代の所有権、準備待ちの共有、await 境界、期限の起点、取消し、子プロセス回収との対応を確認する。
準備期限は `process.start()` の直前から計測し、要求の期限・取消しで更新しない。準備タスク自身が期限や失敗で回収を予約して戻り、次世代は旧世代の回収後に作る。初回準備の失敗ログと `/ready` の503も通常テストで確認する。正常な遅い準備を共有する対照は維持する。
実コードとの一致、全ケース、破壊検出と復元を確認した後だけ、この同期コミット、検証器の `SYNC_COMMIT`、基準結果を更新する。
未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証範囲を守る

Python の実コードに対する有限操作列の全列挙と実プロセスの固定ケースであり、全入力・全 OS スケジュールの形式証明ではない。
全列挙は逐次の deadline・cancel の長さ0〜3、停止位置2箇所、終端release・closeの60ケースに限る。実プロセスの字幕経路はASR・MT、停止位置2箇所、準備の解放有無の8ケースとする。
計算と音声は決定的な代役を使う。準備の停止は factory と warmup に注入する。
実プロセスの字幕検証は要求1秒・準備5秒を使う。観測用の期限も製品既定の準備120秒も、実モデルの準備時間や復旧時間の保証ではない。同期の `process.start()` 自体の中断、実モデルでの自然発生頻度、50分の実時間性能、意味的精度、マイク、実 LAN、画面、記録保存は対象外。
通常の長時間入力と認識・翻訳本体の永久停止は [lesson-endurance](../lesson-endurance/SKILL.md)、スレッド版の呼出し制御は [inference-lifecycle](../inference-lifecycle/SKILL.md) を併用する。
