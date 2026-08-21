param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "py" }

Set-Location $projectRoot

if (-not $env:IRS_DB_PATH) {
    $dbPath = if ($python -eq "py") {
        & py -c "from common import DB_PATH; print(DB_PATH)"
    } else {
        & $python -c "from common import DB_PATH; print(DB_PATH)"
    }
    if ($LASTEXITCODE -ne 0 -or -not $dbPath) {
        throw "Unable to resolve IRS_DB_PATH from .env/common.py"
    }
    $env:IRS_DB_PATH = ([string]$dbPath).Trim()
}

$url = "http://127.0.0.1:5000"
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($appUrl)
        Start-Sleep -Seconds 2
        Start-Process $appUrl
    } -ArgumentList $url | Out-Null
}

Write-Host "Starting IRS 990 Tool at $url"
Write-Host "Database: $env:IRS_DB_PATH"
Write-Host "Close this window or press Ctrl+C to stop the Flask app."

if ($python -eq "py") {
    & py app.py
} else {
    & $python app.py
}
