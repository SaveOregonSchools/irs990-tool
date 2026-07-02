[CmdletBinding()]
param(
    [string]$ProjectDir = "",
    [string]$DbPath = "",
    [string]$Python = "python",
    [string]$Sqlite = "sqlite3",
    [switch]$Yes,
    [switch]$SkipCheckpoint
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

if ([string]::IsNullOrWhiteSpace($ProjectDir)) {
    $scriptPath = if (-not [string]::IsNullOrWhiteSpace($PSCommandPath)) {
        $PSCommandPath
    } else {
        $MyInvocation.MyCommand.Path
    }
    $ProjectDir = Split-Path -Parent $scriptPath
}
$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
if ([string]::IsNullOrWhiteSpace($DbPath)) {
    if (-not [string]::IsNullOrWhiteSpace($env:IRS_DB_PATH)) {
        $DbPath = $env:IRS_DB_PATH
    } else {
        $DbPath = Join-Path $ProjectDir "db\irs990.db"
    }
}

$env:IRS_PROJECT_DIR = $ProjectDir
$env:IRS_DB_PATH = $DbPath
$exportsDir = Join-Path $ProjectDir "exports"
New-Item -ItemType Directory -Force -Path $exportsDir | Out-Null

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host ""
    Write-Host "==> $Label"
    Write-Host "    $Exe $($Arguments -join ' ')"
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Invoke-PythonStep {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    Invoke-External -Label $Label -Exe $Python -Arguments $Arguments
}

Write-Host "This script builds/rebuilds enhanced grant matching data."
Write-Host "Run it only after loading new XML files into the database."
Write-Host ""
Write-Host "Project:  $ProjectDir"
Write-Host "Database: $DbPath"
Write-Host ""
Write-Host "The entire process can take many hours, depending on the amount of IRS data."

if (-not (Test-Path -LiteralPath $DbPath)) {
    throw "Database not found at $DbPath. Pass -DbPath or set IRS_DB_PATH."
}

if (-not $Yes) {
    $choice = Read-Host "Do you want to continue? [Y/N]"
    if ($choice -notmatch "^[Yy]") {
        Write-Host "Canceled."
        exit 0
    }
}

Push-Location $ProjectDir
try {
    Invoke-PythonStep "Refresh deterministic grant resolution" @(
        "resolve_grant_recipients.py",
        "--db", $DbPath,
        "--full-refresh",
        "--batch-size", "100000"
    )

    Invoke-PythonStep "Verify EO BMF files" @(
        "grant_ai_assist_v1.py",
        "verify-bmf",
        "--project-dir", $ProjectDir
    )

    Invoke-PythonStep "Rebuild organization identity" @(
        "grant_ai_assist_v1.py",
        "build-identity",
        "--db", $DbPath,
        "--project-dir", $ProjectDir,
        "--full-refresh"
    )

    Invoke-PythonStep "Rebuild grant recipient signatures" @(
        "grant_ai_assist_v1.py",
        "build-signatures",
        "--db", $DbPath,
        "--full-refresh"
    )

    Invoke-PythonStep "Generate fast candidates" @(
        "grant_ai_assist_v1.py",
        "generate-candidates",
        "--db", $DbPath,
        "--full-refresh",
        "--candidate-mode", "fast"
    )

    Invoke-PythonStep "Generate balanced candidates for signatures without candidates" @(
        "grant_ai_assist_v1.py",
        "generate-candidates",
        "--db", $DbPath,
        "--candidate-mode", "balanced",
        "--queue-status", "no_candidates"
    )

    Invoke-PythonStep "Run reported EIN triage" @(
        "grant_ai_assist_v1.py",
        "reported-ein-triage",
        "--db", $DbPath,
        "--placeholder-action", "human_review"
    )

    Invoke-PythonStep "Park nonadjudicable/list-style and blank-recipient signatures" @(
        "grant_ai_assist_v1.py",
        "nonadjudicable-recipient-triage",
        "--db", $DbPath,
        "--action", "human_review",
        "--include-blank-recipient-name"
    )

    Invoke-PythonStep "Apply highest-confidence candidate rules" @(
        "grant_ai_assist_v1.py",
        "candidate-rule-decisions",
        "--db", $DbPath,
        "--rules", "exact_name_zip,exact_name_city_state,exact_address_zip_good_name"
    )

    Invoke-PythonStep "Apply single high-score candidate rule" @(
        "grant_ai_assist_v1.py",
        "candidate-rule-decisions",
        "--db", $DbPath,
        "--rules", "single_candidate_high_score"
    )

    Invoke-PythonStep "Apply exact name/address/state rule" @(
        "grant_ai_assist_v1.py",
        "candidate-rule-decisions",
        "--db", $DbPath,
        "--rules", "exact_name_state_only"
    )

    Invoke-PythonStep "Apply large safe remaining candidate rules" @(
        "grant_ai_assist_v1.py",
        "candidate-rule-decisions",
        "--db", $DbPath,
        "--rules", "large_safe_remaining"
    )

    Invoke-PythonStep "Apply address/name remaining candidate rules with reviewed looser threshold" @(
        "grant_ai_assist_v1.py",
        "candidate-rule-decisions",
        "--db", $DbPath,
        "--rules", "address_name_remaining",
        "--addr-name-min-name-score", "0.70",
        "--high-address-geo-min-name-score", "0.70"
    )

    Invoke-PythonStep "Apply distinctive exact-name/no-geo rule" @(
        "grant_ai_assist_v1.py",
        "candidate-rule-decisions",
        "--db", $DbPath,
        "--rules", "exact_name_no_geo_distinctive"
    )

    Invoke-PythonStep "Rebuild applied/final enhanced grant layer" @(
        "grant_ai_assist_v1.py",
        "apply-decisions",
        "--db", $DbPath,
        "--full-refresh"
    )

    Invoke-PythonStep "Write grant match stats CSV" @(
        "grant_ai_assist_v1.py",
        "stats",
        "--db", $DbPath,
        "--csv-out", (Join-Path $exportsDir "grant_match_stats_after_enhanced_grants.csv")
    )

    Invoke-PythonStep "Refresh web data statistics cache" @(
        "refresh_data_stats.py",
        "--db", $DbPath
    )

    $remainingSql = @"
SELECT
  COUNT(*) AS signatures_left_for_ai_review,
  COALESCE(SUM(s.grant_count),0) AS grants_represented,
  ROUND(COALESCE(SUM(s.total_amount),0),2) AS total_amount
FROM grant_recipient_signature s
WHERE EXISTS (
  SELECT 1
  FROM grant_recipient_ai_candidate c
  WHERE c.signature_hash = s.signature_hash
)
AND NOT EXISTS (
  SELECT 1
  FROM grant_recipient_ai_decision d
  WHERE d.signature_hash = s.signature_hash
);
"@
    Invoke-External "Count signatures still needing AI or human adjudication" $Sqlite @($DbPath, $remainingSql)

    if (-not $SkipCheckpoint) {
        Invoke-External "Checkpoint and truncate SQLite WAL" $Sqlite @($DbPath, "PRAGMA wal_checkpoint(TRUNCATE);")
    }
}
finally {
    Pop-Location
}
