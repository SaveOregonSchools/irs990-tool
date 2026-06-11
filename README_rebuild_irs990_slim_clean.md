# IRS 990 Slim Database Rebuild Script

This README explains how to use `rebuild_irs990_slim_clean_v3.py` to build or update the slim IRS 990 SQLite database used by the query-console modules.

The script reads IRS e-file XML returns, extracts the subset of fields needed by the current query modules, and writes them into a SQLite database. It supports both full rebuilds and safe incremental appends.

---

## Script

```text
rebuild_irs990_slim_clean_v3.py
```

Typical location:

```bat
C:\IRSDB\db\rebuild_irs990_slim_clean_v3.py
```

Typical database:

```bat
C:\IRSDB\db\irs990.db
```

Typical XML directory:

```bat
C:\IRSDB\XML
```

---

## What the script does

At a high level, the script:

1. Walks the XML directory recursively and finds files ending in `.xml`.
2. Parses each IRS XML file.
3. Extracts return-header data such as EIN, return type, tax year, organization name, address, website, and filing timestamp.
4. Extracts core financial fields, grants, contractors, officers, selected Schedule L and Schedule R data, and selected 990-PF-specific fields.
5. Loads the extracted data into SQLite tables.
6. Rebuilds `canonical_by_ein_year` so each EIN/year points to one canonical filing.
7. Recreates compatibility views such as `grants_compat_v1`, `vw_contractors`, and `sched_r_related_orgs_expanded`.
8. Creates indexes and runs SQLite optimization.

The script is designed for the slim database, not a full IRS XML mirror. It only creates and fills the tables needed by the current query modules.

---

## Full rebuild mode

A full rebuild deletes the existing database file if it already exists, then builds a new database from the XML directory.

```bat
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\XML
```

Use this when:

- You want a clean database from scratch.
- You changed extraction logic and want all filings reprocessed.
- You suspect existing child/detail tables contain duplicate rows from older script runs.
- You want to rebuild after major schema or mapping changes.

Important: without `--append` or `--keep-db`, the script removes the existing DB file before loading.

---

## Append mode for adding new XMLs

Append mode preserves the existing database and loads only XML filings that are not already present.

Recommended command:

```bat
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\NewXML --append
```

Use this when:

- You downloaded newer XMLs from the IRS.
- You found missing XMLs and want to add them.
- You want to add a batch of new filings without rebuilding the full database.

Append mode still rebuilds `canonical_by_ein_year`, views, and indexes after loading the new XMLs. This is intentional: a newly added filing can become the canonical filing for an EIN/tax year.

---

## Duplicate prevention in append mode

In append mode, the script checks the existing `returns` table before loading XML files.

It skips an incoming XML if either of these already exists:

1. The incoming file's `filing_id`, which is the XML filename stem.
2. The normalized object ID derived from that filename stem.

The normalized object ID removes common IRS suffixes:

```text
_public
_private
```

For example, these are treated as the same filing/object:

```text
202331099349100118_public.xml
202331099349100118_private.xml
202331099349100118.xml
```

The script also skips duplicate object IDs within the incoming XML directory itself, so the same filing is not loaded twice during the same run.

The script currently uses the filename stem as the filing identifier/object source. It does not rely on parsing an internal XML object ID field. This matches the way the current database stores `filing_id` values.

---

## `--keep-db` behavior

`--keep-db` is now treated as a safe append alias.

This means these two commands are effectively equivalent:

```bat
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\NewXML --append
```

```bat
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\NewXML --keep-db
```

Prefer `--append` because it makes your intent clearer.

Older versions of the script had a risk: `--keep-db` could preserve the DB but still reload detail rows, creating duplicates in child tables like `grants` or contractor tables. In v3, `--keep-db` uses the same skip-existing logic as `--append`.

---

## Flags

### Required flags

#### `--db`

Path to the SQLite database to create, rebuild, or append to.

Example:

```bat
--db C:\IRSDB\db\irs990.db
```

#### `--xml-dir`

Path to the folder containing IRS XML files. The script searches this folder recursively.

Example:

```bat
--xml-dir C:\IRSDB\XML
```

---

### Optional flags

#### `--append`

Preserves the existing DB and loads only XML files not already present.

Example:

```bat
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\NewXML --append
```

Recommended for incremental updates.

#### `--keep-db`

Alias for append behavior. Preserves the DB and skips already-loaded filings.

Example:

```bat
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\NewXML --keep-db
```

Recommended only if you are used to the older flag name. Otherwise use `--append`.

#### `--workers`

Number of parallel worker processes used to parse XML files.

Default:

```text
CPU count minus 1
```

Example:

```bat
--workers 8
```

Use fewer workers if your machine is memory constrained or if you want to keep the computer responsive during the build.

Use one worker for easier debugging:

```bat
--workers 1
```

#### `--chunksize`

Number of files sent to each worker at a time when using multiprocessing.

Default:

```text
25
```

Example:

```bat
--chunksize 50
```

Most users can leave this alone.

#### `--commit-every`

Number of successfully processed XML files between database commits.

Default:

```text
1000
```

Example:

```bat
--commit-every 500
```

Lower values commit more often, which may reduce loss from an interrupted run but can slow the load. Higher values may load faster but keep more pending work uncommitted.

#### `--vacuum`

Runs SQLite `VACUUM` after the build.

Example:

```bat
--vacuum
```

Use this after a full rebuild if you want to compact the database. It can take a long time. It is usually not necessary for routine append runs.

---

## Common commands

### Full clean rebuild

```bat
cd C:\IRSDB\db
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\XML
```

### Full rebuild with fewer workers

```bat
cd C:\IRSDB\db
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\XML --workers 4
```

### Append new XMLs

```bat
cd C:\IRSDB\db
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\NewXML --append
```

### Append with one worker for debugging

```bat
cd C:\IRSDB\db
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\NewXML --append --workers 1
```

### Full rebuild and compact DB afterward

```bat
cd C:\IRSDB\db
py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\XML --vacuum
```

---

## Output messages

During a run, you will see messages like:

```text
[schema] creating/updating slim schema...
[tables] 20/32
[tables] 32/32
[load] loading XML into slim schema...
[load] XML files found: 10,000; selected: 500; skipped existing: 9,400; skipped duplicate input: 100
[load] 1,000 files
[canon] rebuilding canonical_by_ein_year...
[schema] creating views + indexes...
[opt] ANALYZE / optimize...
[validate] returns: 1,234,567
[validate] canonical_by_ein_year: 1,100,000
[validate] grants: 25,000,000
[validate] contractors: 150,000
[validate] officers: 4,000,000
[done] slim rebuild complete
```

In append mode, the most important line is:

```text
[load] XML files found: ...; selected: ...; skipped existing: ...; skipped duplicate input: ...
```

If `selected` is `0`, the script found no new XML filings to load.

---

## Error log

Parse and header errors are written to:

```text
<parent of xml-dir>\rebuild_irs990_slim_errors.log
```

For example, if you run with:

```bat
--xml-dir C:\IRSDB\XML
```

then the error log will be written to:

```bat
C:\IRSDB\rebuild_irs990_slim_errors.log
```

The script continues processing other XMLs when an individual XML file cannot be parsed or lacks required header fields.

---

## Tables and views created

The script creates a slim schema focused on the query modules. Key tables include:

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

The views are dropped and recreated on every run, including append runs.

---

## 990-PF handling

Version 3 includes several 990-PF-specific fixes, including:

- 990-PF website extraction from `StatementsRegardingActyGrp/WebsiteAddressTxt`.
- 990-PF legislative/political activity indicator extraction.
- 990-PF total grants paid extraction from `TotalGrantOrContriPdDurYrAmt`.
- 990-PF mission-like text extraction from `RestrictionsOnAwardsTxt`.
- 990-PF net investment income extraction.
- 990-PF grant recipient name extraction from `RecipientBusinessName`.
- 990-PF grant amount extraction from `Amt`.
- 990-PF highest-paid contractor extraction from `CompensationOfHghstPdCntrctGrp`.
- 990-PF officer/trustee extraction from `OfficerDirTrstKeyEmplGrp`.

Some fields are still not generally available in 990-PF XML in the same way as regular Form 990, such as organization-level employee count, volunteer count, formation year, organization form, and standard lobbying expenditure amount.

---

## Validation queries

After a build or append, open the DB in DB Browser for SQLite and run checks like these.

### Confirm a specific filing is loaded

```sql
SELECT filing_id, ein, return_type, tax_year, org_name, website
FROM returns
WHERE filing_id = '202331099349100118_public';
```

### Confirm all filings for an EIN

```sql
SELECT filing_id, ein, return_type, tax_year, org_name
FROM returns
WHERE ein = '226029397'
ORDER BY tax_year DESC;
```

### Check 990-PF root fields

```sql
SELECT
  filing_id,
  website_address_txt,
  legislative_political_acty_ind,
  total_grant_or_contri_pd_dur_yr_amt,
  mission_desc_txt
FROM irs990_pf_root
WHERE filing_id = '202331099349100118_public';
```

### Check 990-PF analysis fields

```sql
SELECT
  filing_id,
  net_investment_income_amt,
  contri_paid_rev_and_expnss_amt
FROM irs990_pf_analysis_of_revenue_and_expenses
WHERE filing_id = '202331099349100118_public';
```

### Check grants compatibility view

```sql
SELECT
  filing_id,
  recipient_name,
  cash_amount,
  noncash_amount,
  purpose
FROM grants_compat_v1
WHERE filing_id = '202331099349100118_public'
LIMIT 50;
```

### Check contractors

```sql
SELECT
  filing_id,
  contractor_name,
  services_desc,
  compensation_amt,
  city,
  region
FROM vw_contractors
WHERE filing_id = '202331099349100118_public'
LIMIT 50;
```

### Check row counts

```sql
SELECT 'returns' AS table_name, COUNT(*) AS row_count FROM returns
UNION ALL
SELECT 'canonical_by_ein_year', COUNT(*) FROM canonical_by_ein_year
UNION ALL
SELECT 'grants', COUNT(*) FROM grants
UNION ALL
SELECT 'contractors', COUNT(*) FROM irs990_contractor_compensation_grp
UNION ALL
SELECT 'officers', COUNT(*) FROM officers;
```

---

## Important caveats

### Append mode does not update existing filings

Append mode skips XML filings already present in the database. It is designed for adding missing or newer filings, not replacing old extracted records.

If you need to reprocess a filing because extraction logic changed, use a full rebuild, or manually delete that filing and all of its child rows before appending it again.

### Append mode does not clean old duplicate child rows

The v3 append logic prevents future duplicate loads, but it does not clean duplicates that may already exist from prior versions of the script.

If you suspect old duplicate rows exist, a full rebuild is the safest fix.

### Canonical filings may change after append

After appending new filings, the script rebuilds `canonical_by_ein_year`. If a newly loaded filing is more recent for an EIN/tax year, it may become the canonical filing used by query modules.

### The database should not be open for writing elsewhere

Close DB Browser write transactions and other scripts before running a rebuild or append. Read-only connections are usually fine, but active write locks can cause failures.

---

## Recommended workflow

For routine updates:

1. Download or collect new XML files into a separate folder such as:

   ```bat
   C:\IRSDB\NewXML
   ```

2. Run append mode:

   ```bat
   py rebuild_irs990_slim_clean_v3.py --db C:\IRSDB\db\irs990.db --xml-dir C:\IRSDB\NewXML --append
   ```

3. Check the load summary for selected/skipped counts.

4. Run a few validation SQL queries in DB Browser.

For extraction-code changes:

1. Back up the current database.
2. Run a full rebuild from the complete XML directory.
3. Validate known test filings, especially 990-PF examples.

---

## Quick flag reference

```text
--db             Required. Path to SQLite database.
--xml-dir        Required. Folder containing IRS XML files; searched recursively.
--append         Preserve existing DB and load only XMLs not already present.
--keep-db        Alias for safe append behavior.
--workers        Number of parallel XML parser processes. Default is CPU count minus 1.
--chunksize      Number of XML files per multiprocessing chunk. Default is 25.
--commit-every   Number of processed files between commits. Default is 1000.
--vacuum         Run SQLite VACUUM after build/load.
```
