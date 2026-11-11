# 配布パッケージ

配布ZIPを展開したサーバーPCで、Pythonやモデルを別途用意せずにLive Translationを使えることを確認する。
開発機での自動検証と、開発環境のないWindowsでの初回導入は、それぞれ実施した入口だけを合格にする。

## Sub-features

- `package-identity` は期待するZIPのSHA-256、cleanな版情報、設定、展開した全ファイルを照合する。
- `package-use` は同梱Python・依存・モデルによる起動、先生・生徒の参加と表示設定保持を確認する。
- `package-captions` はブラウザーのマイク入力から日本語原文、英語・中国語字幕、記録保存までを確認する。
- `package-asr-switch` は追加同梱版で ReazonSpeech へ切り替え、Haas-ja へ戻す経路を確認する。
- `package-first-run` は開発環境のないWindowsで `start.bat`、UAC、証明書、OS初期設定を確認する。
- `package-restart-update` は2回目の起動と、本体領域を差し替えた後の現地データ保持を確認する。

## How to get to it (user POV)

配布担当者から案内された共有リンクからZIPをダウンロードし、プロパティでブロックを解除して `C:\LiveTranslation` へ展開する。
`start.bat` をダブルクリックする。初回UACで「はい」を選び、開いた先生画面から授業を操作する。
更新ではサーバーを止め、新ZIPの `app` を差し替え、既存 `data` を引き継ぐ。

## Driving it with instance.py and Playwright

Preconditions:

- Windows、検証器用の `.venv` と `psutil`、`.qa-tools` のPlaywright、Edgeがある。
- 対象ZIPと、配布記録で確認した期待SHA-256・完全なコミットIDを指定する。入力ZIPから期待値を作り直して別版を通さない。
- ZIPは `start.bat` と `app` を直下に持ち、`app/version.json` がcleanである。ZIPが無ければREADMEの `scripts/build_package.py` で対象版をビルドする。未コミット変更を含む配布物はこの経路では拒否する。
- 空きディスクと稼働中の実NIC IPv4が必要。loopbackを案内IPに指定しない。

対象ZIPのパス、配布記録のSHA-256とコミット、このPCの実NIC IPv4を入力する。旧版を再現する場合は [過去の配布ZIP検証記録](../references/distribution-proof.md) の値を使う。
入力ZIPの更新は行わない。各runは専用の展開先、データ、ポートを持つ。

```powershell
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$verifyZip = (Resolve-Path -LiteralPath (Read-Host '対象ZIPのパス')).Path
$verifySha = Read-Host '配布記録のSHA-256'
$verifyCommit = Read-Host '配布記録の完全なコミットID'
$verifyAdvertiseIp = Read-Host 'サーバーPCの実NIC IPv4'
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
- **モデル構成。** 標準版の `package-version.json` に `haas-ja-int8` と `hy-mt2` があり、ReazonSpeech 追加同梱版には `reazonspeech` もあることを確認する。両方の既定起動では `effective-config.json` の `asr.engine` が `haas-onnx`、`asr.haas_onnx.quantization` が `int8` となり、`package-runtime.json` に同梱の ONNX Runtime とモデル入力のパスが記録される。旧版や構成を変更したZIPは、ビルド時に選んだモデル・実効設定と照合する。
- **起動と実操作。** doctorは所有PID・生成時刻・引数・待受け、入力ZIPと全展開物、readiness、en/zh、参加情報を照合する。`student-preferences/result.json` のpassと操作履歴、trace、先生の人数1・中国語内訳、再読込後の文字サイズLと原文OFFを確認する。画像だけで成功にしない。
- **字幕と記録。** 同じrunへ [先生の授業操作](lesson-control.md)、[字幕と接続復元](captions-reconnection.md)、[授業記録](recording.md) のレシピを適用する。既存の `server/` 内部への注入やfake ASR・翻訳を使わない。WAVマイク代替はSKILL.mdのEvidenceに従い、音源SHA・操作trace・画面の原文とen/zh字幕・WebSocket送信を保存する。記録ON中の発話と、授業終了後のJSONL・言語別Markdownを照合し、cleanup前に `recording-evidence/` へコピーする。
- **追加同梱したASRへの切り替え。** 専用Windowsで ReazonSpeech 追加同梱版を使う。[READMEの切り替え手順](../../../../README.md#音声認識を-reazonspeech-に切り替える) に従い `data/config.yaml` で `asr.engine` を `sherpa` にして `start.bat` で再起動する。起動ログのASRと検証用発話の字幕を確認する。上書きを削除して再起動し、Haas-ja でも同じ観測を行う。ZIP用ヘルパーの `--config-override` は利用できない。
- **初回導入。** `package-first-run` は専用のまっさらなWindows 11または復元可能なVMで行う。証明書の自動生成には、物理NICの利用可能なIPv4アドレスが1件に絞れるか、`data/config.yaml` の `server.advertise_ip` が利用可能なNICのIPv4を一意に指定している必要がある。開始時のOS・CPU・VC++導入状態、Python・pipの有無、ネット切断状態を記録し、READMEどおりZIPをブロック解除して展開する。`start.bat` のダブルクリックからUAC「はい」、先生ページの自動表示、同梱Pythonのプロセスまで記録する。`data/first-run.log`、`data/.first-run-complete`、`data/certs/cert.pem` と鍵、診断結果を保存する。VC++の導入済み状態、`Get-NetFirewallRule -DisplayName 'LinguaBridge TCP *'` とそのポートフィルター、`powercfg /query` でACスリープ・休止の値を照合する。起動前後の観測がない初期設定は未検証とする。別端末から先生・生徒が参加し、マイク、字幕、記録も実操作する。
- **再起動と更新。** 上記専用Windowsでサーバーを止め、もう一度 `start.bat` を開く。UACが再表示されず、証明書と現地設定・記録が保持されることを照合する。さらに新ZIPのappを差し替え、既存dataの証明書・設定・記録のハッシュを前後比較する。新しい版で起動して過去の記録が読めることも確認する。`asr.engine` を `sherpa` にする上書きを保持する場合は ReazonSpeech 追加同梱版を使う。
- **片付けと証拠。** `cleanup.json` の停止とtmp削除を読み、instanceに記録されたプロセスが終了していること、画像・trace・設定・ログ・退避記録が残ることを確認する。初回導入用VMは証拠を外部へ保存してから専用snapshotへ戻す。検証器はrunのtmpだけを消し、元ZIPは保持する。

## Gotchas

- 検証器の `.venv` は制御用。サーバーはZIP内のPythonを使う。この開発機のVC++ランタイムやOS DLLは利用できるため、成功してもfresh Windowsでの導入成功にはならない。
- 自動経路は `start.bat`、UAC、証明書発行、ファイアウォール、電源設定変更を実行しない。証明書を空にしたloopback HTTPの検証であり、別端末HTTPSや学校LANは別途確認する。
- 起動・参加・表示設定だけの結果では `package-captions` を合格にしない。物理マイクをWAVで代替した場合は物理収音を未検証とする。
- ZIP内のリンク、Windowsの予約名・大小文字衝突・展開先外へのパスを拒否する。ZIPと `--models-dir` または `--config-override` の併用も拒否する。
- TCP所有確認やEdge起動がサンドボックスに阻まれたら、許可された通常ユーザー環境で同じコマンドを実行する。所有確認を省略しない。
- 全ファイル照合で時間とディスクを使う。同じrunを二人で操作せず、失敗後もfinallyのcleanupを実行する。所有情報が変わったPIDは停止を拒否するため、ログを読み、自分が開始したプロセスと確認できるまで手動終了しない。
- 初回導入用のWindowsが無い場合は、その入口を前提不足と記録する。開発機で成功した範囲だけを検証済みとして報告する。
