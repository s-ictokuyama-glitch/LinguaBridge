# 起動ロジックの判定と初回処理の手順（run.ps1 から dot-source する）。昇格・証明書生成などの
# 実行は引数で受け取り、tests/integration/test_launcher_mode.py と test_first_run.py から
# PowerShell のサブプロセスで偽の実行関数に差し替えて検証する。

# 同梱の Python（配布パッケージの app\python）があれば配布モード、無ければ .venv の開発モード。
# 配布モードは setup.ps1（ネット取得）を呼ばず、app の隣の data を現地データ領域にする。
function Get-LaunchPlan([Parameter(Mandatory)][string]$AppRoot) {
    $bundledPython = Join-Path $AppRoot "python\python.exe"
    if (Test-Path -LiteralPath $bundledPython) {
        $dataRoot = Join-Path (Split-Path -Parent $AppRoot) "data"
        $marker = Join-Path $dataRoot ".first-run-complete"
        $certFile = Join-Path $dataRoot "certs\cert.pem"  # 配布用の既定の設定の cert_dir（data 基準）
        return [pscustomobject]@{
            Mode       = "distribution"
            Python     = $bundledPython
            DataRoot   = $dataRoot
            NeedsSetup = $false
            # 初回処理（UAC）の完了マーカーと証明書は data に置き、app の差し替えをまたいで残す
            FirstRunMarker = $marker
            FirstRunLog    = Join-Path $dataRoot "first-run.log"
            CertFile       = $certFile
            NeedsFirstRun  = -not ((Test-Path -LiteralPath $marker) -and (Test-Path -LiteralPath $certFile))
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
        FirstRunMarker = $null
        FirstRunLog    = $null
        CertFile       = $null
        NeedsFirstRun  = $false
        NeedsSetup = -not (Test-Path -LiteralPath (Join-Path $AppRoot ".venv\.setup-complete"))
        ConfigArgs = [string[]]@()
    }
}

# 管理者として PowerShell スクリプトを起動し、終了を待って終了コードを返す。
# UAC で「いいえ」が選ばれたら $null、それ以外の理由で起動できなければ -1。
function Start-ElevatedScript([Parameter(Mandatory)][string]$Arguments) {
    try {
        $process = Start-Process -FilePath "powershell.exe" -ArgumentList $Arguments -Verb RunAs -PassThru
    } catch {
        $inner = $_.Exception
        while ($inner) {
            # ERROR_CANCELLED: UAC の確認が取り消された
            if ($inner -is [System.ComponentModel.Win32Exception] -and $inner.NativeErrorCode -eq 1223) { return $null }
            $inner = $inner.InnerException
        }
        Write-Host "[注意] 管理者として起動できませんでした: $($_.Exception.Message)" -ForegroundColor Yellow
        return -1
    }
    $null = $process.Handle  # ハンドルを保持しないと -Verb RunAs の ExitCode が取れないことがある
    $process.WaitForExit()
    return $process.ExitCode
}

# 配布モードの初回処理。UAC で1回だけ昇格し、続けて証明書を昇格の外で作る。
# 証明書スクリプトは server.main を経由して多くの依存を読み込むので、VC++ ランタイムを先に入れる。
#   -Elevate : 昇格した first_run_admin.ps1 の終了コードを返す（Start-ElevatedScript と同じ約束）
#   -MakeCert: 証明書スクリプトを実行し、その終了コードを返す
# マーカーは昇格した処理が済んだときだけ書く（書かなければ次回また尋ねる）。証明書は
# 初回と、証明書ファイルが無いときに作る。戻り値: not-needed / completed / denied / failed。
# どの場合もサーバーの起動は続ける。
function Invoke-FirstRun {
    param(
        [Parameter(Mandatory)][string]$Marker,
        [Parameter(Mandatory)][string]$CertFile,
        [Parameter(Mandatory)][string]$LogPath,
        [Parameter(Mandatory)][int]$HttpPort,
        [Parameter(Mandatory)][int]$HttpsPort,
        [Parameter(Mandatory)][scriptblock]$MakeCert,
        [Parameter(Mandatory)][scriptblock]$Elevate
    )
    $needsElevation = -not (Test-Path -LiteralPath $Marker)
    $needsCert = $needsElevation -or -not (Test-Path -LiteralPath $CertFile)
    if (-not $needsCert) { return "not-needed" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Marker) | Out-Null

    if ($needsElevation) {
        Write-Host "============================================================" -ForegroundColor Yellow
        Write-Host "  初回の準備を行います（このPCで1回だけ）。" -ForegroundColor Yellow
        Write-Host "  このあと出る管理者の確認（UAC）で「はい」を押してください。" -ForegroundColor Yellow
        Write-Host "============================================================" -ForegroundColor Yellow
        $adminExit = & $Elevate
    }

    Write-Host "このPC用の証明書を用意しています..."
    $certExit = & $MakeCert
    $status = "completed"
    if ($certExit -eq 3) {  # make_cert.py の EXIT_UNCHANGED_NOT_READY（既存を変えずに残した）
        Write-Warning "既存の証明書は採用IPに対して正常と確認できませんでした（上の検証結果を参照）。"
        Write-Warning "別端末でHTTPSを使う場合は docs\certificate-recovery.md の手順で再発行してください。"
    } elseif ($certExit -ne 0) {
        Write-Host "[注意] 証明書を生成できませんでした（上のメッセージを参照）。次回の start.bat でもう一度行います。" -ForegroundColor Yellow
        $status = "failed"
    }
    if (-not $needsElevation) { return $status }

    if ($null -eq $adminExit) {
        Write-Host ""
        Write-Host "[注意] 管理者の確認で「はい」が選ばれなかったため、次の初回設定を省きました。" -ForegroundColor Yellow
        Write-Host "  - Visual C++ ランタイムのインストール（未導入のPCではサーバーが起動できないことがあります）" -ForegroundColor Yellow
        Write-Host "  - ファイアウォールの受信許可（TCP $HttpPort, $HttpsPort）（生徒端末からつながらないことがあります）" -ForegroundColor Yellow
        Write-Host "  - AC接続時のスリープ・休止の無効化（授業中にスリープすることがあります）" -ForegroundColor Yellow
        Write-Host "  やり直し方: start.bat をもう一度実行し、管理者の確認で「はい」を押してください。" -ForegroundColor Yellow
        Write-Host "  このままサーバーを起動します。" -ForegroundColor Yellow
        Write-Host ""
        return "denied"
    }
    if ($adminExit -ne 0) {
        Write-Host ""
        Write-Host "[注意] 初回設定の一部が完了しませんでした（終了コード $adminExit）。" -ForegroundColor Yellow
        Write-Host "  経過は $LogPath にあります。" -ForegroundColor Yellow
        Write-Host "  次回の start.bat でもう一度行います。このままサーバーを起動します。" -ForegroundColor Yellow
        Write-Host ""
        return "failed"
    }
    "completed $(Get-Date -Format o)" | Set-Content -Encoding UTF8 -LiteralPath $Marker
    Write-Host "初回の準備が完了しました。次回からは管理者の確認は出ません。" -ForegroundColor Green
    return $status
}
