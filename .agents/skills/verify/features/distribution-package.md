# 配布パッケージ

配布ZIPを展開したサーバーPCで、Pythonやモデルを別途用意せずにLive Translationを使えることを確認する。
開発機での自動検証と、開発環境のないWindowsでの初回導入は、それぞれ実施した入口だけを合格にする。

## Sub-features

- `package-identity` は期待するZIPのSHA-256、cleanな版情報、設定、展開した全ファイルを照合する。
- `package-use` は同梱Python・依存・モデルによる起動、先生・生徒の参加と表示設定保持を確認する。
- `package-captions` はブラウザーのマイク入力から日本語原文、英語・中国語字幕、記録保存までを確認する。
- `package-first-run` は開発環境のないWindowsで `start.bat`、UAC、証明書、OS初期設定を確認する。
- `package-restart-update` は2回目の起動と、本体領域を差し替えた後の現地データ保持を確認する。

## How to get to it (user POV)

READMEの配布ZIPをダウンロードし、プロパティでブロックを解除して `C:\LiveTranslation` へ展開する。
`start.bat` をダブルクリックする。初回UACで「はい」を選び、開いた先生画面から授業を操作する。
更新ではサーバーを止め、新ZIPの `app` と `start.bat` を差し替え、既存 `data` を引き継ぐ。

## Driving it with instance.py and Playwright

Preconditions:

- Windows、検証器用の `.venv` と `psutil`、`.qa-tools` のPlaywright、Edgeがある。
- 対象ZIPと、配布記録で確認した期待SHA-256・完全なコミットIDを指定する。入力ZIPから期待値を作り直して別版を通さない。
- ZIPは `start.bat` と `app` を直下に持ち、`app/version.json` がcleanである。ZIPが無ければREADMEの `scripts/build_package.py` で対象版をビルドする。未コミット変更を含む配布物はこの経路では拒否する。
- 空きディスクと稼働中の実NIC IPv4が必要。loopbackを案内IPに指定しない。

以下はこの開発機で確認したZIPの再実行コマンド。別の配布物を検証するときは、先頭のZIP・期待SHA・コミット・実NICを依頼対象の記録に合わせる。
入力ZIPの更新は行わない。各runは専用の展開先、データ、ポートを持つ。

```powershell
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$verifyZip = (Resolve-Path -LiteralPath 'artifacts/school-access-148-151/dist-final/LiveTranslation-20261103-2142-9d2c6ac.zip').Path
$verifySha = 'ed5dba31854cee0394fe770924a8f6d3a7691b6923b45aea7ca2b30475beac68'
$verifyCommit = '9d2c6ac8677a9ec5081ea89c46e1fc56bf8aadf9'
$verifyAdvertiseIp = '192.168.1.6'
$verifyRun = Join-Path (Get-Location) ('artifacts/verify/' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-zip-' + [guid]::NewGuid().ToString('N').Substring(0,8))
try {
    .venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py launch --run-dir "$verifyRun" --distribution-zip "$verifyZip" --expected-sha256 $verifySha --expected-commit $verifyCommit --advertise-ip $verifyAdvertiseIp
    if ($LASTEXITCODE -ne 0) { throw 'ZIP launch failed. Read failure.json and package-probe/server logs.' }
    .venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py doctor --run-dir "$verifyRun"
    if ($LASTEXITCODE -ne 0) { throw 'Doctor failed.' }
    .venv/Scripts/python.exe .agents/skills/verify/scripts/student_preferences.py --run-dir "$verifyRun"
    if ($LASTEXITCODE -ne 0) { throw 'Browser verification failed.' }
    # 音声・記録も対象なら、ここで下記の対応機能マップを同じrunへ適用する。
} finally {
    .venv/Scripts/python.exe .agents/skills/verify/scripts/instance.py cleanup --run-dir "$verifyRun"
    if ($LASTEXITCODE -ne 0) { throw 'Cleanup failed. Inspect ownership and logs before retrying.' }
    Get-ChildItem -LiteralPath $verifyRun -File
}
```

- **ZIPの同定と展開。** `package-archive.json` の実測SHAと期待SHA、`package-version.json` のコミットと `dirty=false` を読む。`package-files.json` は全展開ファイルのSHA-256を持つ。展開先は `tmp/package`、空データは `tmp/data`。設定・Python・モデルの欠落を開発側のファイルで補わない。
- **実行環境。** `package-runtime.json` のexecutable、modules、sys_path、inputsが展開app内にあることを確認する。プローブと本起動は同梱Pythonを空のdata作業ディレクトリから実行する。Python環境変数と開発用PATHを除外し、モデル取得をofflineにする。overrideはポート、空の証明書領域、記録OFFと保存先だけ。配布のASR・翻訳・VAD設定を維持する。
- **起動と実操作。** doctorは所有PID・生成時刻・引数・待受け、入力ZIPと全展開物、readiness、en/zh、参加情報を照合する。`student-preferences/result.json` のpassと操作履歴、trace、先生の人数1・中国語内訳、再読込後の文字サイズLと原文OFFを確認する。画像だけで成功にしない。
- **字幕と記録。** 同じrunへ [先生の授業操作](lesson-control.md)、[字幕と接続復元](captions-reconnection.md)、[授業記録](recording.md) のレシピを適用する。既存の `server/` 内部への注入やfake ASR・翻訳を使わない。WAVマイク代替はSKILL.mdのEvidenceに従い、音源SHA・操作trace・画面の原文とen/zh字幕・WebSocket送信を保存する。記録ON中の発話と、授業終了後のJSONL・言語別Markdownを照合し、cleanup前に `recording-evidence/` へコピーする。
- **初回導入。** `package-first-run` は専用のまっさらなWindows 11または復元可能なVMで行う。開始時のOS・CPU・VC++導入状態、Python・pipの有無、ネット切断状態を記録し、READMEどおりZIPをブロック解除して展開する。`start.bat` のダブルクリックからUAC「はい」、先生ページの自動表示、同梱Pythonのプロセスまで記録する。`data/first-run.log`、`data/.first-run-complete`、`data/certs/cert.pem` と鍵、診断結果を保存する。VC++の導入済み状態、`Get-NetFirewallRule -DisplayName 'LinguaBridge TCP *'` とそのポートフィルター、`powercfg /query` でACスリープ・休止の値を照合する。起動前後の観測がない初期設定は未検証とする。別端末から先生・生徒が参加し、マイク、字幕、記録も実操作する。
- **再起動と更新。** 上記専用Windowsでサーバーを止め、もう一度 `start.bat` を開く。UACが再表示されず、証明書と現地設定・記録が保持されることを照合する。さらに新ZIPのappとstart.batを差し替え、既存dataの証明書・設定・記録のハッシュを前後比較する。新しい版で起動して過去の記録が読めることも確認する。
- **片付けと証拠。** `cleanup.json` の停止とtmp削除を読み、instanceに記録されたプロセスが終了していること、画像・trace・設定・ログ・退避記録が残ることを確認する。初回導入用VMは証拠を外部へ保存してから専用snapshotへ戻す。検証器はrunのtmpだけを消し、元ZIPは保持する。

## Gotchas

- 検証器の `.venv` は制御用。サーバーはZIP内のPythonを使う。この開発機のVC++ランタイムやOS DLLは利用できるため、成功してもfresh Windowsでの導入成功にはならない。
- 自動経路は `start.bat`、UAC、証明書発行、ファイアウォール、電源設定変更を実行しない。証明書を空にしたloopback HTTPの検証であり、別端末HTTPSや学校LANは別途確認する。
- 起動・参加・表示設定だけの結果では `package-captions` を合格にしない。物理マイクをWAVで代替した場合は物理収音を未検証とする。
- ZIP内のリンク、Windowsの予約名・大小文字衝突・展開先外へのパスを拒否する。ZIPと `--models-dir` または `--config-override` の併用も拒否する。
- TCP所有確認やEdge起動がサンドボックスに阻まれたら、許可された通常ユーザー環境で同じコマンドを実行する。所有確認を省略しない。
- 全ファイル照合で時間とディスクを使う。同じrunを二人で操作せず、失敗後もfinallyのcleanupを実行する。所有情報が変わったPIDは停止を拒否するため、ログを読み、自分が開始したプロセスと確認できるまで手動終了しない。
- 初回導入用のWindowsが無い場合は、その入口を前提不足と記録する。開発機で成功した範囲だけを検証済みとして報告する。
