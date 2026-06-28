# IRS 990 XML preflight support

This package includes two options:

1. `apply_preflight_to_rebuild.py` — patches `rebuild_irs990_slim_clean.py` so it gains a native `--preflight` mode.
2. `irs990_preflight_scan.py` — standalone companion scanner that imports the existing rebuild script and runs the same extractor without changing your repo file.

## Recommended integration

From the repo root:

```bash
python apply_preflight_to_rebuild.py rebuild_irs990_slim_clean.py
```

The patcher creates a timestamped backup, modifies the script, and compiles it to catch syntax errors.

Then run:

```bash
python rebuild_irs990_slim_clean.py \
  --xml-dir /path/to/2017_2018_xml \
  --preflight \
  --workers 4 \
  --preflight-report preflight_summary.json \
  --preflight-csv preflight_files.csv
```

For a quick test run:

```bash
python rebuild_irs990_slim_clean.py \
  --xml-dir /path/to/2017_2018_xml \
  --preflight \
  --workers 1 \
  --preflight-max-files 500 \
  --preflight-report preflight_sample_summary.json \
  --preflight-csv preflight_sample_files.csv
```

## Standalone option

If you want to scan without editing the rebuild script:

```bash
python irs990_preflight_scan.py \
  --xml-dir /path/to/2017_2018_xml \
  --loader rebuild_irs990_slim_clean.py \
  --workers 4 \
  --report preflight_summary.json \
  --csv preflight_files.csv
```

## What it checks

The preflight scan recursively finds XML files and reports:

- XML parse errors
- missing required header fields
- return type and `returnVersion` inventory
- unknown `(ReturnTypeCd, returnVersion)` combinations
- recognized form nodes: `IRS990`, `IRS990EZ`, `IRS990PF`
- extraction coverage for core fields, grants, contractors, officers, Schedule L, and Schedule R
- caveats such as grant indicators without extracted grant detail rows
- filename year vs `TaxYr` mismatches, which are common in IRS bulk downloads
- older `Filer/InCareOfNm` placement that was not captured by the old header path list

## After preflight looks good

Run your normal append:

```bash
python rebuild_irs990_slim_clean.py \
  --db db/irs990.db \
  --xml-dir /path/to/2017_2018_xml \
  --append \
  --workers 4 \
  --commit-every 1000
```
