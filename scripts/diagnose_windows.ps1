# Read-only OS inventory. Never invoke setup, elevation, or Set/New/Remove commands.
param(
    [ValidateRange(1, 65535)][int]$HttpPort,
    [ValidateRange(1, 65535)][int]$HttpsPort
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$report = @{}
try {
    $interfaces = @(Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias, IPAddress, PrefixLength, AddressState)
    $report.interfaces = @{ status = "observed"; detail = "IPv4 interfaces"; items = $interfaces }
} catch {
    $report.interfaces = @{ status = "unknown"; detail = "IPv4 inventory unavailable" }
}
try {
    $listeners = @(Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in @($HttpPort, $HttpsPort) } | ForEach-Object {
        $processName = "unknown"
        try { $processName = (Get-Process -Id $_.OwningProcess).ProcessName } catch {}
        [PSCustomObject]@{
            LocalAddress = $_.LocalAddress; LocalPort = $_.LocalPort
            OwningProcess = $_.OwningProcess; ProcessName = $processName
        }
    })
    $report.listeners = @{ status = "observed"; detail = "Configured ports only; empty means no listener observed"; items = $listeners }
} catch {
    $report.listeners = @{ status = "unknown"; detail = "Listener inventory unavailable" }
}
# Preserve completed observations even if the firewall provider stalls.
$report | ConvertTo-Json -Depth 8 -Compress
try {
    $connections = @(Get-NetConnectionProfile | Select-Object InterfaceAlias, NetworkCategory, IPv4Connectivity)
    $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore | Select-Object Name, Enabled, DefaultInboundAction, AllowInboundRules, AllowLocalFirewallRules)
    $report.firewall = @{
        status = "unknown"; inventory_status = "unknown"
        detail = "Profiles collected; rule inventory incomplete; effective permission unconfirmed"
        connections = $connections; profiles = $profiles
    }
    $report | ConvertTo-Json -Depth 8 -Compress
    $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True -Direction Inbound | ForEach-Object {
        $rule = $_
        $filters = @($rule | Get-NetFirewallPortFilter)
        $matchingFilters = @($filters | Where-Object {
            $filter = $_
            $portMatches = @($filter.LocalPort | Where-Object {
                $value = "$_"
                if ($value -eq "Any" -or $value -in @("$HttpPort", "$HttpsPort")) { $true }
                elseif ($value -match '^(\d+)-(\d+)$') {
                    ($HttpPort -ge [int]$Matches[1] -and $HttpPort -le [int]$Matches[2]) -or
                    ($HttpsPort -ge [int]$Matches[1] -and $HttpsPort -le [int]$Matches[2])
                }
            })
            "$($filter.Protocol)" -in @("TCP", "6", "Any", "256") -and $portMatches.Count -gt 0
        })
        if ($matchingFilters.Count -gt 0) {
            [PSCustomObject]@{
                Name = $rule.Name; DisplayName = $rule.DisplayName
                Action = "$($rule.Action)"; Profile = "$($rule.Profile)"
                Ports = @($matchingFilters | Select-Object Protocol, LocalPort, RemotePort)
                Addresses = @($rule | Get-NetFirewallAddressFilter | Select-Object LocalAddress, RemoteAddress)
                Applications = @($rule | Get-NetFirewallApplicationFilter | Select-Object Program, Package)
                Services = @($rule | Get-NetFirewallServiceFilter | Select-Object Service)
            }
        }
    })
    # Inventory success does NOT prove effective permission or remote reachability.
    $report.firewall = @{
        status = "unknown"; inventory_status = "observed"
        detail = "Rules collected; effective permission requires remote HTTP test. Other restrictions may apply."
        connections = $connections; profiles = $profiles; rules = $rules
    }
} catch {
    $report.firewall = @{ status = "unknown"; inventory_status = "unknown"; detail = "Firewall inventory unavailable" }
}
$report | ConvertTo-Json -Depth 8 -Compress
