$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "py" }

Set-Location $projectRoot

if (-not $env:IRS_DB_PATH) {
    $env:IRS_DB_PATH = Join-Path $projectRoot "db\irs990.db"
}

$url = "http://127.0.0.1:5000"
Start-Job -ScriptBlock {
    param($appUrl)
    Start-Sleep -Seconds 2
    Start-Process $appUrl
} -ArgumentList $url | Out-Null

Write-Host "Starting IRS 990 Tool at $url"
Write-Host "Database: $env:IRS_DB_PATH"
Write-Host "Close this window or press Ctrl+C to stop the Flask app."

if ($python -eq "py") {
    & py app.py
} else {
    & $python app.py
}
