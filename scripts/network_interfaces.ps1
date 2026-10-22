# Local, read-only NIC inventory. No route probes or network configuration changes.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$adapters = @(Get-NetAdapter -IncludeHidden)
$addresses = @(Get-NetIPAddress -AddressFamily IPv4)
$items = @($addresses | ForEach-Object {
    $address = $_
    $adapter = $adapters | Where-Object { $_.ifIndex -eq $address.InterfaceIndex } | Select-Object -First 1
    $role = "unknown"
    if ($null -ne $adapter) {
        if ($adapter.HardwareInterface -eq $true -and $adapter.Virtual -ne $true) { $role = "physical" }
        elseif ($adapter.HardwareInterface -eq $false -or $adapter.Virtual -eq $true) { $role = "virtual" }
    }
    [PSCustomObject]@{
        name = $address.InterfaceAlias
        ip = $address.IPAddress
        up = ($null -ne $adapter -and "$($adapter.Status)" -eq "Up" -and "$($address.AddressState)" -eq "Preferred")
        role = $role
    }
})
ConvertTo-Json -InputObject $items -Compress
