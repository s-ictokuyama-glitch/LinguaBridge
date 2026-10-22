# 配布モードの初回処理のうち、管理者権限が必要な部分（run.ps1 が UAC で昇格して1回だけ起動する）。
# VC++ ランタイム → ファイアウォールの受信許可 → AC 接続時のスリープ・休止の無効化。
# 1つが失敗しても残りは行う。昇格したウィンドウは閉じるので、経過は -LogPath に残す。
# すべて成功したら終了コード 0。

param(
    [Parameter(Mandatory)][int]$HttpPort,
    [Parameter(Mandatory)][int]$HttpsPort,
    [Parameter(Mandatory)][string]$VcRedist,
    [Parameter(Mandatory)][string]$LogPath
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\os_setup.ps1"

$steps = [ordered]@{
    "VC++ ランタイム"          = { Install-VcRuntime -Installer $VcRedist }
    "ファイアウォールの受信許可" = { Grant-LinguaBridgeFirewall -Ports @($HttpPort, $HttpsPort); $true }
    "スリープ・休止の無効化"     = { Disable-AcSleep }
}
$ok = $true
try { Start-Transcript -LiteralPath $LogPath -Force | Out-Null } catch { Write-Host "[注意] 経過を $LogPath に残せません: $_" }
foreach ($name in $steps.Keys) {
    try {
        if (-not (& $steps[$name])) { $ok = $false }
    } catch {
        Write-Host "[エラー] ${name}: $_"
        $ok = $false
    }
}
try { Stop-Transcript | Out-Null } catch {}
if ($ok) { exit 0 } else { exit 1 }
