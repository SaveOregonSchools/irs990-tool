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

A larger external database path is also fine and is recommended for a server:

```text
/var/lib/irs990-tool/db/irs990.db
```

The commands below use project-relative database paths and generic XML
placeholders. Substitute absolute paths when data lives elsewhere.

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
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/xml
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
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/new-xml --append
```

Use append mode when:

- you downloaded newer XMLs;
- you found missing XMLs and want to add them;
- you want to add a small batch without rebuilding the full database.

To download one known filing from the public GivingTuesday Data Commons S3
mirror, pass its IRS object ID (or full XML filename/HTTPS URL) and an output
directory:

```powershell
python download_irs990_xml.py 201821349349309507 C:\path\to\new-xml
```

The downloader validates that the response is IRS return XML and refuses to
replace an existing file unless `--overwrite` is supplied.

Append mode still rebuilds `canonical_by_ein_year`, views, and indexes after loading. A newly added filing may become the canonical filing for an EIN/tax year.

### Risk-network source identity

Every newly built main database contains an `app_dataset_identity` singleton
with a stable database UUID and a rotating risk-source revision UUID. These
values are data identity, not a local filename: an exact checkpointed copy keeps
the same identity on Windows, Linux, another volume, or another filesystem.

Append mode preserves the database UUID and rotates the risk-source revision
before its first source-data commit. The deterministic grant resolver does the
same. The applied enhanced-grant publisher rotates inside its atomic visible
cutover, and a clean rebuild receives a new database UUID. These changes
immediately mark an older network sidecar stale until the source is fully
rebuilt into the network. A bounded incremental refresh cannot advance a global
source revision safely because it cannot prove that every changed filing was
selected. A failed atomic cutover rolls its revision change back with its data
changes; multi-commit import/resolver workflows remain conservatively stale if
they stop after an intermediate commit.

Direct SQLite edits and third-party writers cannot rotate this application
revision automatically. Before any such write that can affect returns,
canonical filings, grants, contractors, people, addresses, Schedule R
relationships, or the enhanced grant layer, run the explicit
`mark-risk-source-changed` operation in
`migrate_risk_network_portability.py`. If an untracked write has already
occurred, run it immediately afterward while the application remains stopped.
The runtime also compares the checkpointed main-file size and SQLite header as a
conservative defense, but those values are not a substitute for the revision
protocol. Then perform a full network rebuild. Stop writers and
checkpoint/truncate WAL before copying or building either database.

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

For a durable inventory of the XML files on disk, and to safely find duplicate
files before appending or rebuilding, use the source manifest sidecar scanner.

```powershell
python scan_xml_sources.py `
  --sidecar-db db/irs990_sources.db `
  --main-db db/irs990.db `
  --report-csv exports/xml_source_audit.csv `
  --duplicates-csv exports/xml_source_duplicates.csv
```

The sidecar scanner rebuilds `db\irs990_sources.db` with one row per XML file,
plus a copy of loaded filing IDs from `returns` when `--main-db` is supplied.
It uses `IRS_XML_ROOT` unless `--xml-dir` overrides it. Inventory paths are
stored relative to that root with forward slashes, so the sidecar can move
between Windows and Linux. Existing absolute-path sidecars remain readable; the
next scan replaces active rows with portable entries. The main database's
historical `returns.source_file` values are retained as matching provenance.
`source_files.relative_path` is authoritative. Legacy compatibility columns such
as `source_file`, `keep_source_file`, and `quarantine_file` also contain relative
values after a new scan; the old per-row `xml_root` column is left blank. The
actual root exists only in `IRS_XML_ROOT` or the current `--xml-dir` argument.
It hashes duplicate object-ID candidates by default and classifies them as:

- `unique`: no other XML file has the same normalized object ID.
- `primary_duplicate_group`: the retained file for an exact duplicate group.
- `exact_duplicate`: same normalized object ID and same SHA-256 content hash as
  the retained file.
- `object_id_conflict`: same normalized object ID, but different or unknown file
  content. Review these manually.

### Manifest-verified clean rebuild

For a clean rebuild that must reproduce the source choice behind the current
database, explicitly enable manifest selection. First run the read-only
selection proof; `IRS_XML_ROOT` supplies the archive root unless `--xml-dir`
overrides it:

```powershell
.\.venv\Scripts\python.exe rebuild_irs990_slim_clean.py `
  --manifest-selection-only `
  --manifest-db db\irs990_sources.db `
  --expected-selection-count 5904356
```

This opens the manifest read-only and does not open, create, or remove a
destination database. It requires exactly one `loaded_filings` row and one
selected XML per normalized object ID. Unique sources are retained, exact byte
duplicates use their single manifest primary, and every `object_id_conflict`
must resolve uniquely to the historical source path recorded in
`loaded_filings`. Absolute/traversal paths, ambiguous or unresolved groups,
selected quarantine rows, missing files, size/mtime changes, SHA-256 changes
where the manifest has a digest, stale scan rows, and object/filing count
mismatches reject the entire selection. Long selections report progress every
100,000 validated objects/files.

Only after selection succeeds, build into a new staging filename:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
$repairDb = "db\irs990-repaired-$stamp.db"
.\.venv\Scripts\python.exe rebuild_irs990_slim_clean.py `
  --db $repairDb `
  --manifest-clean-rebuild `
  --manifest-db db\irs990_sources.db `
  --expected-selection-count 5904356
```

The complete manifest and filesystem validation occurs before the loader can
write anything. Manifest mode refuses `IRS_DB_PATH`, any destination that
already exists, and any destination within the XML root; always provide a new
staging filename. The loader builds beside that filename, fails on the first
XML extraction/header error, verifies exact return/filing/object/source coverage
and `PRAGMA quick_check`, checkpoints the WAL, and only then atomically installs
the requested staging file. A failed run removes its temporary build and never
publishes a partial destination. Multi-worker extraction submits at most twice
the worker count in ordered batches, avoiding the unbounded task queue created
by older `ProcessPoolExecutor.map` implementations while retaining deterministic
input/result order. Rescan the archive and re-import
`loaded_filings` into the manifest whenever the relative XML tree or production
source population changes.

### Rescanning after a move

Changing only the absolute archive location does not invalidate a portable
inventory. If the relative tree is unchanged, update `IRS_XML_ROOT`; the same
sidecar will resolve beneath the new Windows or Linux root. A scan on the new
computer is nevertheless recommended to verify the transferred archive and to
replace any legacy absolute-path entries.

Rescan whenever files or directories are renamed or moved inside the XML root,
or whenever XML files are added, removed, restored, or quarantined. Those changes
alter the relative-path inventory. For a large archive, scan into a new sidecar
filename and switch `IRS_XML_INVENTORY_PATH` after reviewing the results. This
preserves the current inventory if the scan is interrupted.

See [Moving an Existing Installation](migrating-data.md) for a safe
Windows-to-Linux procedure, example commands, and a validation checklist.

To move exact duplicates out of the XML tree after reviewing the report, use an
explicit quarantine directory outside `--xml-dir`:

```powershell
python scan_xml_sources.py `
  --sidecar-db db/irs990_sources.db `
  --duplicates-csv exports/xml_source_duplicates.csv `
  --quarantine-duplicates C:/path/to/xml-duplicates-quarantine `
  --yes
```

Quarantine only moves `exact_duplicate` files. It does not remove
`object_id_conflict` files.

After quarantine, rescan the XML folder so the sidecar reflects the files still
in the active source tree:

```powershell
python scan_xml_sources.py `
  --sidecar-db db/irs990_sources.db `
  --main-db db/irs990.db `
  --duplicates-csv exports/xml_source_duplicates_after_quarantine.csv
```

To investigate `object_id_conflict` rows, ask the scanner to parse the conflicting
XML files and hash a canonicalized XML structure. This ignores indentation, line
endings, and attribute order, so it can separate formatting-only differences from
real XML content differences:

```powershell
python scan_xml_sources.py `
  --sidecar-db db/irs990_sources.db `
  --analyze-conflicts `
  --conflict-groups-csv exports/xml_source_conflict_groups.csv
```

The conflict group CSV writes one row per conflicting object ID. Conflict groups
marked `canonical_equivalent` have different bytes but the same parsed XML
structure. Groups marked `canonical_different` need manual review before moving
or deleting anything.

When the production database has `returns.source_file` paths, the scanner can
also create a keep/move recommendation for conflicts. It keeps the current XML
file whose relative path matches the source path recorded in `returns`, even if
the XML root folder has moved:

```powershell
python scan_xml_sources.py `
  --sidecar-db db/irs990_sources.db `
  --main-db db/irs990.db `
  --conflict-resolution-csv exports/xml_source_conflict_resolution.csv
```

Review the `recommended_action` column. Rows marked `keep` stay in the active
XML folder, rows marked `quarantine_conflict` are the extra conflicting copies,
and rows marked `review` did not resolve to exactly one loaded source file.

After reviewing the recommendation CSV, move only resolved extra conflict copies:

```powershell
python scan_xml_sources.py `
  --sidecar-db db/irs990_sources.db `
  --main-db db/irs990.db `
  --conflict-resolution-csv exports/xml_source_conflict_resolution.csv `
  --quarantine-resolved-conflicts C:/path/to/xml-conflicts-quarantine `
  --yes
```

---

## `--keep-db` behavior

`--keep-db` is treated as a safe append alias.

These are equivalent:

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/new-xml --append
```

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/new-xml --keep-db
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
python rebuild_irs990_slim_clean.py --xml-dir C:/path/to/new-xml --preflight --workers 4 --preflight-report exports/preflight_summary.json --preflight-csv exports/preflight_files.csv
```

See [XML Preflight Guide](preflight.md) for how to review preflight output.

Full clean rebuild:

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/xml
```

Manifest selection-only proof and manifest-verified staging rebuild:

```powershell
python rebuild_irs990_slim_clean.py --manifest-selection-only --manifest-db db/irs990_sources.db --xml-dir C:/path/to/xml --expected-selection-count 5904356
python rebuild_irs990_slim_clean.py --db db/irs990-repaired.db --manifest-clean-rebuild --manifest-db db/irs990_sources.db --xml-dir C:/path/to/xml --expected-selection-count 5904356
```

Full rebuild with fewer workers:

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/xml --workers 4
```

Append new XMLs:

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/new-xml --append
```

Append with one worker for debugging:

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/new-xml --append --workers 1
```

Full rebuild and compact afterward:

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/xml --vacuum
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
python refresh_data_stats.py --db db/irs990.db
```

The enhanced grant matching batch runs this refresh automatically after rebuilding the grant matching layer.

### Avoid active writers while rebuilding

Close DB Browser write transactions and other scripts before running a rebuild or append. Read-only app connections are usually fine, but active write locks can cause failures.
