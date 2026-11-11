---
name: lesson-endurance
description: 50分授業の連続音声入力、処理待ち・保持量、正の推論期限、永久ハング後の字幕再開と終了を検証する。Use when changing Pipeline workers, InferenceRunner or ProcessInferenceRunner timeout and lifecycle, CaptionDelivery queue/cache, or claiming long-lesson performance and hang recovery.
---

# 50分授業の継続とハング復旧を確認する

指定した推論時間の下で、50分の入力を止めずに英中字幕へ届け、保持状態を有界に保つ。永久に返らない推論からも後続字幕と終了が回復することを別に要求する。
違反すると授業途中で字幕が止まり、停止後もプロセスが残る。

## 実コードの検証を実行する

リポジトリ直下でPython 3.12.10を使う。

```powershell
uv run --python 3.12.10 --no-project --with-requirements scripts/formal/requirements.txt python -X utf8 -B scripts/formal/lesson_endurance.py --mutation-check --json .audit/lesson-endurance.json
```

同じ依存がある場合は `.venv/Scripts/python.exe` でも実行できる。
Windowsの制限環境でasyncioの初期化が停止した場合は、実行権限を確認して再実行する。
正の期限6ケース、3000秒の8構成、実プロセス4ケース、5破壊の `detected: true` と `restored: PASS` を確認する。
`--quick` は3000秒の1構成だけ、`--controlled-only` は実プロセスを省く。変更後の最終判定には既定の全ケースを使う。

[基準結果](../../../scripts/formal/lesson_endurance_baseline.json)は終了コード0、全体 `PASS` である。
ASR・MTの永久ハングでは、停止を解除せず旧PIDの回収と後続の英中字幕を確認する。
各ケースで再度ハングさせ、`stop_hang_pid_alive: true` を確認してから停止する。
`all_children_reaped: true` と `process_exit.status: PASS` の両方を要求する。
修正前後の結果と実モデルの確認は[検証記録](../../../scripts/formal/inference_recovery_run.md)を参照する。

`violations` の `property`、`trace`、`expected`、`actual` を読む。
実プロセスでは `released_control`、`pipeline_stop_returned`、`process_exit` を区別する。
Traceback、結果なし、破壊の置換不一致は検証器または環境の失敗であり、検出成功ではない。
範囲と実装の対応は [検証の説明](../../../scripts/formal/lesson_endurance.md) と照合する。

## 実モデルの50分性能を確認する

性能を主張する変更では、実モデルとアプリ依存を用意し、`config.yaml` のモデル配置を確認する。
音源は `tests/fixtures/ja/*.wav` を使う。なければ `scripts/make_fixture_audio.ps1` で生成する。
起動時間と最後の排出待ちを含めて約55分確保し、既存の計測フォルダーを再利用しない。

```powershell
$run = Join-Path .audit ('lesson-endurance-' + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe -X utf8 -B scripts/replay_client.py --minutes 50 --students 10 --langs en,zh --engine hy-mt2 --asr haas-onnx --mt-cache-size 0 --no-config-update --drain-seconds 120 --port 18765 --out-dir $run
$manifest = Get-Content -Raw (Join-Path $run source-manifest.json) | ConvertFrom-Json
$report = Join-Path $run $manifest.primary_report
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_endurance_report.py $report --json ($run + '-result.json')
```

計測中は製品コード、設定、依存、音源、モデルを変更しない。
測定器の終了コード、manifestの `complete: true`、追加判定の `status` と `failures`、サーバーログのエラーを確認する。
追加判定は3000秒の全量送信、全生徒の配信、全5分窓の字幕、遅延・資源・最終待ち行列を要求する。
確認できなければ性能は未確認または不合格として記録する。仮想時間の成功で代用しない。

判定器を変更した場合は次を実行し、時間不足・字幕欠落・タイムアウト通知・観測欠落が不合格になることを確認する。

```powershell
.venv/Scripts/python.exe -X utf8 -B -m pytest tests/unit/test_lesson_endurance_report.py -q
```

## 実装と同期する

最終同期コミットは `1f5bc911317534a3c6b93cf0941d27a0189c8745`。

| 対象 | 対応する処理 |
|---|---|
| `server/inference.py` | 注入するスレッド経路の期限、標準Future、Executor、stopと共通の停止待機 |
| `server/inference_process.py`、`server/main.py` | 本番のモデル構築、プロセス世代、期限切れ後の再生成と停止時の回収 |
| `server/pipeline.py` | start、warmup、feed_audio、ASR worker、確定発話、stop、統計 |
| `server/audio/ingest.py`、`server/audio/vad.py`、`server/asr_backlog.py`、`server/turn/assembler.py` | PCM変換、分割、音声容量、Turn確定 |
| `server/caption_delivery.py`、`server/caption_history.py`、`server/mt/cache.py` | MT投入・起床・翻訳、履歴、キャッシュ、受信記録 |
| `server/roster.py`、`server/delivery.py` | 英中の受信者と送信 |
| `server/config.py`、`config.yaml` | 既定容量・期限・モデル設定 |
| `scripts/formal/lesson_liveness.py` | 再利用するScenario、標準Futureの制御、入力IDの期待値 |
| `scripts/replay_client.py`、`scripts/acceptance.py`、`scripts/performance_evidence.py` | 実時間の入力・計測・受け入れ・証拠照合。別コマンド |

```powershell
git diff 1f5bc911317534a3c6b93cf0941d27a0189c8745 -- server config.yaml scripts/formal/lesson_liveness.py scripts/replay_client.py scripts/acceptance.py scripts/performance_evidence.py
```

対応するawait、時計、完了・取消し、保持先、計測フィールドが変わったら検証側との一致を見直す。
実コードとの一致、全ケースの結果、破壊と復元を確認した後だけ、この同期コミット、検証器の `SYNC_COMMIT`、基準結果を更新する。
同期先の選定は [inference-startup の同期手順](../inference-startup/SKILL.md#実装との同期を確認する) に従い、新規 clone で上記の差分照合を確認する。
未確認の変更が溜まった場合は `/maintain-formal-verification-skill` を使う。

## 保証範囲を守る

Python実コードへの有限入力の検査であり、全入力・全asyncio順序・全native動作の形式証明ではない。
仮想時間ではenergy VAD・simple Turn・決定的な計算を使う。保持量は指定したコンテナーとタスク数だけを扱う。
実時間の試験は記録した開発機・合成音源・10接続の1回の観測であり、サーバーPCや全授業音声への性能保証ではない。
マイク、実LAN、DOM、認識・翻訳の意味的な精度、記録保存は対象外。
本検証の永久ハングは認識・翻訳処理への注入であり、実モデルでの発生頻度は測らない。
warmup中の停止や要求のキャンセルは `tests/unit/test_inference_process.py` で別に確認する。
既存の `lesson-liveness`、`inference-lifecycle`、`asr-backlog` などの保証を置き換えない。
