---
name: verify
description: LinguaBridge の先生・生徒Web画面を起動し、実ブラウザーで参加・字幕・授業操作・表示設定・記録を検証する。画面変更や配信経路の動作確認、再現手順と証拠の保存に使う。
---

# LinguaBridge を検証する

リポジトリルートで操作する。最初に [機能マップ](features/README.md) を読み、依頼対象の機能を選ぶ。
主な操作面は `/teacher` と生徒用 `/`。配布版の初回導入、学校LAN、物理端末の試験は [現地チェックリスト](../../../docs/field-verification-checklist.md) を使う。

## Launch

既存 `.venv` の Python 3.12 とモデルを使う。空の作業領域に検証設定を作り、製品CLIを別ポートで起動する。
依存が無い場合は README の開発セットアップを行う。モデルを無断でダウンロードせず、欠けたファイルを報告する。

PowerShell で次を実行する。モデルの場所とWi-FiのIPはこのPCで確認済みの値。
別のPCやIP変更後は、モデルの実在パスと `server.network.list_addresses()` の `usable` な実NICアドレスを読み取って指定する。

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$verifyModelDir = 'C:\Users\esesm\AppData\Local\LinguaBridge\models'
$verifyAdvertiseIp = '192.168.1.6'
$verifyRun = Join-Path (Get-Location) ('artifacts/verify/' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $verifyRun | Out-Null
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py launch --run-dir "$verifyRun" --models-dir "$verifyModelDir" --advertise-ip "$verifyAdvertiseIp"
```

`config.accuracy.yaml` はこの開発機にある未追跡の任意設定で、スキルには同梱しない。無い場合は既定設定を使うか、検証対象の実在する設定ファイルを指定する。
任意の設定で検証する場合はlaunchに `--config-override config.accuracy.yaml` を追加する。
このプロファイルはKotobaと長めのプリロールを使う。既存Kotobaモデルが必要で、既定ReazonSpeechよりCPU処理が遅くなる。
ヘルパーは指定プロファイルを読み込み、実効設定全体を保存してから専用ポート・記録先へ隔離する。明示した設定ファイルが無ければ失敗する。

`instance.json` の `url` を操作対象にする。起動ヘルパーはOS割当ての空きポート、専用 `tmp/data`、空の証明書領域を使う。
`server.main` はHTTPを `0.0.0.0` にbindするため同じLANにも公開される。先生のマイクが使えるloopback HTTPをブラウザーから使う。
実NICの自動選択が曖昧なら `server.network.list_addresses()` の `usable` 候補を読み、`--advertise-ip` に実在IPv4を指定して新しいrunを作る。
ready条件は所有プロセスの待受け、`/ready` の200、`/api/config` のen/zh、`/api/teacher-info` の4桁コードが揃うこと。
60秒で揃わなければログを保存して起動を失敗させる。モデルのロード時間を推測で成功扱いしない。
Windowsのサンドボックスで `GetExtendedTcpTable failed` が出た場合は、同じコマンドを承認された通常ユーザー環境で実行する。
Edgeにも同じ制約が出る場合がある。所有確認や実ブラウザーを省略して続行しない。

## Doctor

操作前と異常時に、同じrunに対して読み取り専用チェックを実行する。

```powershell
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py doctor --run-dir "$verifyRun"
```

PIDと生成時刻、起動引数、待受けの所有者、設定とモデルの場所、現在のserver/webソース、readiness、参加情報を照合する。
失敗したらそのインスタンスを操作せず、`server.stdout.log` と `server.stderr.log` を読む。
生徒がまだ0人なのは起動直後の正常状態。授業は未開始から始める。DB、ログイン、事前の授業データは不要。

## Drive

Edge と Playwright を使う。既存 `.qa-tools` にPlaywrightがある。無ければ `.venv/Scripts/python.exe -m pip install --target .qa-tools playwright` で準備する。
ブラウザーは `C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe` を使用する。存在しない場合は実在するEdgeへの `--browser` を指定する。

まず参加と表示設定を検証する。

```powershell
.venv/Scripts/python.exe .agents/skills/verify/scripts/student_preferences.py --run-dir "$verifyRun"
```

このコマンドはdoctorを通した所有インスタンスだけを操作する。独立した先生・生徒contextを開き、先生画面のコードを生徒画面で入力する。
参加、英語選択、文字サイズ大、原文非表示、ページ再読み込みと再参加、設定保持、QR用クエリ経由の中国語参加を確認する。
マイクを取得しないため、この経路だけなら物理マイクは不要。

他の機能は対応するマップを読む。座標ではなく `#start-btn`、`#lang-options button[data-code="en"]` など実装内のIDを使う。
クリックや入力はブラウザー操作で行う。`state` の書き換えやpipelineへの直接注入を実ユーザー経路の証明に使わない。
開発機に既存 `scripts/qa_native_browser.py` がある場合の限定的な再現ケースもマップに記載している。未追跡の補助QAは同梱しないため、利用条件は機能マップのDriving conventionsを読む。そのfake ASR/MTと模擬音声の結果を実モデルの証明へ昇格させない。

## Evidence

証拠は `artifacts/verify/<run>/` に残す。ヘルパーの `student-preferences/result.json` は操作履歴、URL、観測値、成功・失敗、例外を保存する。
`*.aria.txt`、`*.png`、Playwright `0-trace.zip` と `1-trace.zip` を合わせて確認する。traceは開始前から記録し、操作と変化を追えるようにする。
スクリーンショットだけで合格としない。参加は先生の生徒数、設定保持は再読み込み後の画面、記録は保存ファイルも照合する。
字幕は先生側の原文と生徒側の訳文、選択言語、順序、終了状態を合わせて証明する。
録音を扱う場合は検証用音声を使い、許可のない授業音声を収集しない。
物理マイクの代わりに検証用WAVを使う場合はEdgeの `--use-file-for-fake-audio-capture=<絶対パス>%noloop` とmedia streamフラグを使う。
アプリの `getUserMedia`、AudioWorklet、WebSocket、実VAD・ASR・翻訳を通し、送出PCMも保存する。物理収音の成功とは区別する。
元WAVのASR成功だけでは配信経路の合格にしない。ブラウザー処理とVAD後の入力で結果が変わる場合がある。

ブラウザーの実行ログと終了コードを保存する。既存QAを使う場合は、その出力先の `browser.json` で対象scenarioが1件以上あり、期待したscopeとpassであることも読む。
学校LAN、HTTPSの別端末利用、物理マイク、認識・翻訳の意味精度、45分授業は、その実際の経路を通すまで未検証とする。
起動ヘルパーは `start.bat` のUAC、依存導入、ファイアウォール、スリープ設定を実行しない。これらは配布先のWindowsで別途確認する。
dry-runという名前だけで副作用なしと判断しない。対象コマンドのファイル、接続先、プロセスを観測する。

## Cleanup

失敗した試行でも必ず実行する。

```powershell
.venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py cleanup --run-dir "$verifyRun"
Get-ChildItem -LiteralPath $verifyRun -File
```

ヘルパーは記録したPIDの生成時刻と起動引数を照合してから、そのプロセスだけを停止する。
Windowsの `.venv` ランチャーと実サーバーが別PIDの場合は両方を記録して停止する。
専用 `tmp` のみ削除し、設定、起動ログ、操作ログ、画像、trace、`cleanup.json` を残す。
`cleanup.json` の停止確認と証拠ファイルの残存を確認して完了とする。名前による一括killや、既存8000/8443番への接続は行わない。
並列runは別ポート・別データ・別browser contextを持つ。同じrunを2人で操作しない。NICとモデルファイルは読み取りで共有する。

## Helpers

- [instance.py](scripts/instance.py) の `launch`、`doctor`、`cleanup` は上記コマンドで使う。
- [student_preferences.py](scripts/student_preferences.py) は参加と表示設定の証拠を保存する。
- 開発機での過去の実行結果は [検証記録](references/proof.md) を参照する。機能マップの保守には `/maintain-verification-skill` を使う。
