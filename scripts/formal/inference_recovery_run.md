# Issue #180の推論ハング復旧

2026年11月11日、WindowsとPython 3.12.10で確認した。モデル準備後、実行中のASR・翻訳要求が正の期限を超えた場合に、後続の音声から英語・中国語字幕を再開する。停止時には推論の子プロセスを回収する。

## 原因と変更

修正前の `InferenceRunner` は、実行中の単一スレッドを取り消せなかった。呼出側の期限と `shutdown(wait=False)` では推論は終了せず、後続処理とPythonの終了を妨げた。既存の再現コマンドではASRとMTの永久ハングがともに失敗し、停止を解除した対照は成功した。

設定から起動するASRとMTは、それぞれモデルを保持する子プロセスで実行する。実行中の要求が期限切れになった子を終了して回収し、次の要求でモデルを再生成する。要求の期限は呼出開始から数える。準備待ちの要求が期限切れになっても、共有するモデルの準備を繰り返しやり直さない。モデル準備自体の永久ハングは自動復旧の対象外だが、停止時に回収する。

明示的なエンジン・Executor注入は既存のスレッド経路を使う。製品のプロセス起動が失敗した場合にスレッドへ切り替える処理はない。

## 実行した検証

結果の抜粋は [inference_recovery_result.json](inference_recovery_result.json)、ハングを含む全ケースは [lesson_endurance_baseline.json](lesson_endurance_baseline.json) に保存する。

| 検査 | 結果 |
|---|---|
| 修正前の永久ハング | ASR・MTとも後続の英中字幕なし。Pipeline.stopは戻るがPython終了は失敗 |
| 修正後の永久ハング | 処理を解放せず旧PIDを回収。後続字幕の発話番号・原文・言語・訳文が一致 |
| ハング中の停止 | 入力3で再停止したPIDの生存を確認してからstopを呼ぶ。全子PID消滅、外側Pythonの正常終了 |
| lesson-endurance | 正の期限6ケース、3000秒の仮想入力8構成、プロセス4ケース、破壊5件の検出と復元が成功 |
| 既存の有界探索 | 推論ライフサイクル1850合法列、連続字幕413合法列、再接続1562列。計19破壊を検出 |
| 実モデル | 同梱PythonでHaas ONNXとHy-MT2を使用。認識32回、翻訳64回、英中各32字幕、エラー0 |
| 実モデルの通常停止 | ASR・MTのPID生存を確認して実行。stop帰還後は両PID消滅、外側Pythonの終了コード0 |
| 生徒画面 | 実モデルの受信フレームをEdgeの生徒ページへ再生。英中各32カードが可視、表示停止の2破壊を検出 |

主要な再実行コマンドは次のとおり。モデルと依存は既存環境を使う。

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_endurance.py --mutation-check --json .audit/lesson-endurance.json
.venv/Scripts/python.exe -X utf8 -B -m pytest tests/unit/test_inference_process.py tests/unit/test_pipeline_lifecycle.py -q
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_liveness_real.py --repeats 32 --json .audit/lesson-real.json --screens .audit/lesson-real-screens.json
.venv/Scripts/python.exe -X utf8 -B scripts/formal/lesson_liveness_view.py .audit/lesson-real-screens.json --mutation-check --json .audit/lesson-real-view.json
```

実モデル検証では、上記Pythonの代わりに既存配布物の `app/python/python.exe` を使用し、現在のcheckoutを読み込ませた。配布物の旧コードや新規ZIPを実行したという記録ではない。モデルの場所は `--models-dir` で指定した。

## 確認の限界

永久ハングは入力に結び付けた停止処理をモデルの代役に注入した。実モデルで自然に発生するハングの頻度は測っていない。プロセス生成と再ロードを含むため、旧スレッド試験の0.6秒観測から監視時間を変更した。実モデルの復旧時間を保証するものではない。

実モデル32回は有限の反復試験である。今回の修正で実時間50分の性能試験や新規配布ZIPの検証は行っていない。3000秒の仮想入力結果は実時間の性能値として扱わない。
