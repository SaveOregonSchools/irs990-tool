# XML Preflight Guide

Use preflight scans to check a batch of IRS XML files before appending them to the SQLite database. Preflight is especially useful for older bulk-download years, mixed return versions, or unfamiliar XML sources.

Preflight does not write to SQLite.

## Recommended Command

The rebuild script has native preflight mode:

```powershell
py rebuild_irs990_slim_clean.py `
  --xml-dir C:\IRSDB\XML\17-18 `
  --preflight `
  --workers 4 `
  --preflight-report exports\preflight_summary.json `
  --preflight-csv exports\preflight_files.csv
```

For a smaller sample:

```powershell
py rebuild_irs990_slim_clean.py `
  --xml-dir C:\IRSDB\XML\17-18 `
  --preflight `
  --workers 1 `
  --preflight-max-files 5000 `
  --preflight-report exports\preflight_sample_summary.json `
  --preflight-csv exports\preflight_sample_files.csv
```

Use `--workers 1` when debugging a specific XML/parser issue. Use more workers for large scans after the sample looks good.

## Standalone Scanner

`irs990_preflight_scan.py` remains available as a companion scanner. It imports the current `rebuild_irs990_slim_clean.py` and runs the same extractor logic without writing a database.

```powershell
py irs990_preflight_scan.py `
  --xml-dir C:\IRSDB\XML\17-18 `
  --loader rebuild_irs990_slim_clean.py `
  --workers 4 `
  --report exports\preflight_summary.json `
  --csv exports\preflight_files.csv
```

The native `--preflight` mode is preferred for normal use because it is built directly into the rebuild workflow. The standalone scanner is useful when you want a separate entry point or are comparing scanner behavior.

## What Preflight Checks

Preflight recursively finds XML files and reports:

- XML parse errors.
- Missing required header fields.
- Return type and `returnVersion` inventory.
- Unknown `(ReturnTypeCd, returnVersion)` combinations.
- Recognized form nodes: `IRS990`, `IRS990EZ`, and `IRS990PF`.
- Extraction coverage for core fields, grants, contractors, officers, Schedule L, and Schedule R.
- Caveats such as grant indicators without extracted grant detail rows.
- Older `Filer/InCareOfNm` placement that is not captured by expected header paths.

The current grant-detail warning logic avoids warning merely because a 990-PF reports individual grants. It keeps the warning when organization-grant signals are present, such as:

- `GrantsToOrganizationsInd` is true.
- `MoreThan5000KToOrgInd` is true.
- Grant amount fields are positive and the filing is not explicitly individual-only.

## Reading Results

Start with the console summary:

```text
[preflight] status: ...
[preflight] return types: ...
[preflight] schema versions: ...
[preflight] extraction totals: ...
[preflight] caveats: ...
```

Then review the JSON summary for caveat counts and sample files. Use the CSV for file-by-file inspection and filtering.

Treat these as high-priority before appending:

- `parse_error`
- `missing_required_header`
- `unsupported_return_type`
- `no_recognized_main_form`
- `extractor_error`

Treat these as review/spot-check signals:

- `unknown_form_version_combo`
- `all_core_hot_fields_blank`
- `grant_signal_without_detail_rows`
- `pf_contributions_paid_without_detail_rows`
- `filer_incareof_unmapped`

## After Preflight Looks Good

Run the normal append:

```powershell
py rebuild_irs990_slim_clean.py `
  --db db\irs990.db `
  --xml-dir C:\IRSDB\XML\17-18 `
  --append `
  --workers 4 `
  --commit-every 1000
```

After append, spot-check row counts and known filings. See [Database Build Guide](database-build.md) for validation queries and rebuild/append caveats.
