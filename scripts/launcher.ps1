# 起動ロジックの判定部分（run.ps1 から dot-source する）。副作用を持たせず、
# tests/integration/test_launcher_mode.py から PowerShell のサブプロセスで検証する。

# 同梱の Python（配布パッケージの app\python）があれば配布モード、無ければ .venv の開発モード。
# 配布モードは setup.ps1（ネット取得）を呼ばず、app の隣の data を現地データ領域にする。
function Get-LaunchPlan([Parameter(Mandatory)][string]$AppRoot) {
    $bundledPython = Join-Path $AppRoot "python\python.exe"
    if (Test-Path -LiteralPath $bundledPython) {
        $dataRoot = Join-Path (Split-Path -Parent $AppRoot) "data"
        return [pscustomobject]@{
            Mode       = "distribution"
            Python     = $bundledPython
            DataRoot   = $dataRoot
            NeedsSetup = $false
            ConfigArgs = [string[]]@(
                "--config", (Join-Path $AppRoot "config.yaml"),
                "--config-override", (Join-Path $dataRoot "config.yaml"),
                "--data-root", $dataRoot
            )
        }
    }
    # セットアップ要否は「.venvの有無」ではなく「完了マーカーの有無」で判定する。
    # モデルDL（数GB）の途中で中断されると .venv だけ残るため、.venv基準だと
    # 次回起動でセットアップをスキップしてしまい start.bat だけでは復旧できない。
    [pscustomobject]@{
        Mode       = "development"
        Python     = Join-Path $AppRoot ".venv\Scripts\python.exe"
        DataRoot   = $null
        NeedsSetup = -not (Test-Path -LiteralPath (Join-Path $AppRoot ".venv\.setup-complete"))
        ConfigArgs = [string[]]@()
    }
}
