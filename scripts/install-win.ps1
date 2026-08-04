$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectDir = Split-Path -Parent $scriptDir
$serviceName = "Linkora"

$pythonPath = Join-Path $projectDir ".venv\Scripts\python.exe"
$mainPath = Join-Path $projectDir "main.py"

Write-Host "Installing 灵桥 (Linkora) service..."
Write-Host "Project: $projectDir"

if (-not (Test-Path $pythonPath)) {
    Write-Error "Python not found at $pythonPath"
}

$nssmPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssmPath) {
    Write-Error "nssm not found. Please install nssm first: https://nssm.cc/download"
}

& $nssmPath install $serviceName $pythonPath $mainPath
& $nssmPath set $serviceName WorkingDirectory $projectDir
& $nssmPath set $serviceName Start SERVICE_AUTO_START
& $nssmPath start $serviceName

Write-Host "Service installed and started."
Write-Host "Logs: $projectDir\logs\"
Write-Host "To uninstall: run scripts\uninstall-win.ps1"
Write-Host "To check status: nssm status $serviceName"
