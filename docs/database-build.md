# Database Build Guide

This document explains how to use `rebuild_irs990_slim_clean.py` to build or update the slim IRS 990 SQLite database used by the query console.

The script reads IRS e-file XML returns, extracts the subset of fields needed by the current query modules, and writes them into SQLite. It supports both full rebuilds and safe incremental appends.

---

## Script

```text
rebuild_irs990_slim_clean.py
```

Typical database location inside this repo:

```text
db/irs990.db
```

A larger external database path is also fine:

```text
C:\IRSDB\db\irs990.db
```

---

## What the script does

At a high level, the script:

1. Recursively walks an XML directory and finds files ending in `.xml`.
2. Parses each IRS XML file.
3. Extracts return-header data such as EIN, return type, tax year, organization name, address, website, return timestamp, and amended-return flag.
4. Extracts core financial fields, grants, contractors, officers, selected Schedule L data, selected Schedule R data, and selected 990-PF fields.
5. Loads extracted data into SQLite tables.
6. Rebuilds `canonical_by_ein_year` so each EIN/year points to one canonical filing.
7. Recreates compatibility views such as `grants_compat_v1`, `vw_contractors`, and `sched_r_related_orgs_expanded`.
8. Creates indexes and runs SQLite optimization.

This is a slim research schema, not a complete mirror of every XML element.

---

## Full rebuild mode

A full rebuild deletes the existing database file if it already exists, then builds a new database from the XML directory.

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\XML
```

Use a full rebuild when:

- you want a clean database from scratch;
- extraction logic changed and all filings should be reprocessed;
- old child/detail tables may contain duplicate rows from earlier runs;
- a major schema or mapping change was made.

Without `--append` or `--keep-db`, the script removes the existing DB file before loading.

---

## Append mode for new XMLs

Append mode preserves the existing database and loads only XML filings that are not already present.

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\NewXML --append
```

Use append mode when:

- you downloaded newer XMLs;
- you found missing XMLs and want to add them;
- you want to add a small batch without rebuilding the full database.

Append mode still rebuilds `canonical_by_ein_year`, views, and indexes after loading. A newly added filing may become the canonical filing for an EIN/tax year.

---

## Duplicate prevention in append mode

In append mode, the script checks the existing `returns` table before loading XML files.

It skips incoming XMLs when either already exists:

1. the incoming filename stem as `filing_id`; or
2. the normalized object ID derived from the filename stem.

The normalized object ID strips common IRS suffixes:

```text
_public
_private
```

These are treated as the same underlying filing/object:

```text
202331099349100118_public.xml
202331099349100118_private.xml
202331099349100118.xml
```

The script also skips duplicate object IDs inside the incoming XML directory itself.

---

## `--keep-db` behavior

`--keep-db` is treated as a safe append alias.

These are equivalent:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\NewXML --append
```

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\NewXML --keep-db
```

Prefer `--append` because it makes the intent clearer.

---

## Flags

| Flag | Required? | Purpose |
|---|---:|---|
| `--db PATH` | Yes | SQLite database to create, rebuild, or append to. |
| `--xml-dir PATH` | Yes | Folder containing IRS XML files. Searched recursively. |
| `--append` | No | Preserve DB and load only XML filings not already present. |
| `--keep-db` | No | Alias for safe append behavior. |
| `--workers N` | No | Number of parallel XML parser processes. Default is CPU count minus 1. |
| `--chunksize N` | No | Number of files sent to each worker at a time. Default is usually 25. |
| `--commit-every N` | No | Number of processed XML files between database commits. |
| `--vacuum` | No | Run SQLite `VACUUM` after build/load. Can take a long time. |

Use `--workers 1` for easier debugging.

---

## Common commands

Preflight a new XML batch before appending:

```powershell
py rebuild_irs990_slim_clean.py --xml-dir C:\IRSDB\NewXML --preflight --workers 4 --preflight-report exports\preflight_summary.json --preflight-csv exports\preflight_files.csv
```

See [XML Preflight Guide](preflight.md) for how to review preflight output.

Full clean rebuild:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\XML
```

Full rebuild with fewer workers:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\XML --workers 4
```

Append new XMLs:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\NewXML --append
```

Append with one worker for debugging:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\NewXML --append --workers 1
```

Full rebuild and compact afterward:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\XML --vacuum
```

---

## Output messages

During a run, you will see progress messages like:

```text
[schema] creating/updating slim schema...
[load] loading XML into slim schema...
[load] XML files found: 10,000; selected: 500; skipped existing: 9,400; skipped duplicate input: 100
[canon] rebuilding canonical_by_ein_year...
[schema] creating views + indexes...
[opt] ANALYZE / optimize...
[validate] returns: 1,234,567
[validate] canonical_by_ein_year: 1,100,000
[validate] grants: 25,000,000
[done] slim rebuild complete
```

In append mode, the most important line is:

```text
[load] XML files found: ...; selected: ...; skipped existing: ...; skipped duplicate input: ...
```

If `selected` is `0`, the script found no new XML filings to load.

---

## Error log

Parse and header errors are written near the XML directory parent as:

```text
rebuild_irs990_slim_errors.log
```

The script continues processing other XMLs when an individual XML file cannot be parsed or lacks required header fields.

---

## Key tables and views created

Key tables include:

```text
returns
canonical_by_ein_year
core_hot
grants
irs990_contractor_compensation_grp
officers
highest_comp_employees
former_key_people
irs990_root
irs990_ez_root
irs990_pf_root
irs990_pf_analysis_of_revenue_and_expenses
irs990_pf_form990_pfbalance_sheets_grp
return_header_all
```

Key views include:

```text
grants_compat_v1
vw_contractors
sched_r_related_orgs_expanded
```

Views are dropped and recreated on each run, including append runs.

---

## 990-PF handling

The current script includes 990-PF-specific handling for fields such as:

- website extraction;
- legislative/political activity indicator;
- total grants/contributions paid;
- mission-like restrictions text;
- net investment income;
- grant recipient name and amount extraction;
- highest-paid contractors;
- officers/directors/trustees/key employees.

Some fields are not generally available in 990-PF XML in the same way as standard Form 990, including organization-level employee count, volunteer count, formation year, organization form, and standard lobbying expenditure amount.

---

## Validation queries

Confirm a specific filing:

```sql
SELECT filing_id, ein, return_type, tax_year, org_name, website
FROM returns
WHERE filing_id = '202331099349100118_public';
```

Confirm all filings for an EIN:

```sql
SELECT filing_id, ein, return_type, tax_year, org_name
FROM returns
WHERE ein = '226029397'
ORDER BY tax_year DESC;
```

Check row counts:

```sql
SELECT 'returns' AS table_name, COUNT(*) AS row_count FROM returns
UNION ALL SELECT 'canonical_by_ein_year', COUNT(*) FROM canonical_by_ein_year
UNION ALL SELECT 'grants', COUNT(*) FROM grants
UNION ALL SELECT 'contractors', COUNT(*) FROM irs990_contractor_compensation_grp
UNION ALL SELECT 'officers', COUNT(*) FROM officers;
```

Check grant rows:

```sql
SELECT filing_id, recipient_name, cash_amount, noncash_amount, purpose
FROM grants_compat_v1
WHERE filing_id = '202331099349100118_public'
LIMIT 50;
```

Check contractors:

```sql
SELECT filing_id, contractor_name, services_desc, compensation_amt, city, region
FROM vw_contractors
WHERE filing_id = '202331099349100118_public'
LIMIT 50;
```

---

## Important caveats

### Append mode does not update existing filings

Append mode skips XML filings already present in the database. It is for adding missing or newer filings, not replacing old extracted records. Reprocess existing filings through a full rebuild unless you deliberately delete a filing and all child rows first.

### Append mode does not clean old duplicate child rows

The current append logic prevents future duplicate loads, but it does not remove duplicates that may already exist from older versions. A full rebuild is the safest fix for old duplicate detail rows.

### Canonical filings may change after append

After appending new filings, the script rebuilds `canonical_by_ein_year`. If a newly loaded filing is more recent for an EIN/tax year, it may become the canonical filing used by query modules.

### Web statistics cache is separate

The database build script does not refresh the Flask app's cached Database Statistics page. After a rebuild or append, run this if you want the web stats page to reflect the latest database contents:

```powershell
py refresh_data_stats.py --db db\irs990.db
```

The enhanced grant matching batch runs this refresh automatically after rebuilding the grant matching layer.

### Avoid active writers while rebuilding

Close DB Browser write transactions and other scripts before running a rebuild or append. Read-only app connections are usually fine, but active write locks can cause failures.
