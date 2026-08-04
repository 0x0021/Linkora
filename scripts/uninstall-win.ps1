$serviceName = "DingTalkAI"

Write-Host "Uninstalling 灵桥 (Linkora) service..."

$nssmPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssmPath) {
    Write-Error "nssm not found"
}

& $nssmPath stop $serviceName 2>$null
& $nssmPath remove $serviceName confirm

Write-Host "Service uninstalled."
