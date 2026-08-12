# OLMS labor-organization sidecar

The OLMS suite extends the Flask research console with annual labor-organization
filings from the U.S. Department of Labor Office of Labor-Management Standards.
It is a research aid, not a legal-compliance determination system.

## Architecture and configuration

OLMS data lives in a separate SQLite sidecar. The importer never adds OLMS rows
to the large IRS database and never copies large IRS tables into the sidecar.

| Setting | Purpose | Default |
|---|---|---|
| `OLMS_DB_PATH` | Application-facing OLMS SQLite sidecar. | `db/olms.db` |
| `OLMS_DATA_ROOT` | Machine-local directory containing numeric annual folders. | None |
| `IRS_DB_PATH` | Read-only IRS database used for optional deterministic EIN matching. | `db/irs990.db` |

Expected unpacked layout:

```text
OLMS_DATA_ROOT/
  2000/
    lm_data_meta.txt
    lm_data_data_2000.txt
    ...
  2026/
    lm_data_meta.txt
    lm_data_data_2026.txt
    ...
```

The annual folders, `olms.db`, WAL/SHM files, and generated audit CSVs are local
data and are ignored by Git.

`common.py` exposes `connect_olms_ro()` for OLMS-only queries and
`connect_olms_irs_ro()` for explicitly cross-database, read-only work. Flask
query modules never open the sidecar for writes.

## Build and refresh

Full atomic rebuild:

```powershell
python build_olms_db.py `
  --input-dir C:/Projects/IRSDB/OLMS/unpacked `
  --db db/olms.db `
  --rebuild
```

If `OLMS_DATA_ROOT` and `OLMS_DB_PATH` are configured, the shorter form is:

```powershell
python build_olms_db.py --rebuild
```

Refresh the current 2026 folder without risking the working sidecar:

```powershell
python build_olms_db.py --refresh-year 2026
```

Build or refresh a selected set:

```powershell
python build_olms_db.py --rebuild --years 2025,2026
```

A rebuild is created beside the destination under a temporary name. The tool
imports, derives, indexes, validates, runs `PRAGMA integrity_check`, and only
then atomically replaces the destination. A refresh copies the existing
sidecar with SQLite's backup API, replaces the selected source year in that
copy, rebuilds derived layers, and performs the same final validation.

The default build performs deterministic OLMS-to-IRS and counterparty-to-IRS
matching when the IRS database exists. Diagnostic builds can use
`--skip-irs-matching`. That option produces explicit unmatched rows; it does
not invent matches.

## Discovered source schema

Every annual `*_meta.txt` file is parsed. The importer records field order, DOL
type, nullability, source filename, source year, and a schema hash in
`olms_schema_versions`. Source tables use a canonical superset of every
discovered column. A field absent in a year remains `NULL`; an unexpected new
table gets a sanitized generic table rather than crashing discovery.

Known DOL tables are mapped as follows:

| DOL logical source | Sidecar table |
|---|---|
| `lm_data` | `filings` |
| `ar_assets_*` | `assets_*` |
| `ar_liabilities_*` | `liabilities_*` |
| `ar_receipts_*` | `receipts_*` |
| `ar_disbursements_*` | `disbursements_*` |
| `ar_payer_payee` | `payer_payee` |
| `ar_rates_dues_fees` | `rates_dues_fees` |
| `ar_membership` | `membership` |
| `ar_erds_codes` | `erds_codes` |

DOL identifiers including `RPT_ID`, `OID`, `PAYER_PAYEE_ID`, and `F_NUM` are
preserved. Each source row also carries a source year, filename, logical line,
import run, and content hash.

## Parsing, repairs, and quarantine

The importer does not treat every physical line as a complete record. It uses
the metadata-defined field count, data types, and nullability rules.

- A normal record must have the exact field count and validate every typed and
  required field.
- Too-short physical lines buffer subsequent lines. An embedded newline is
  normalized to a space only when the combined logical record validates.
- For extra pipe segments, typed fields keep fixed alignment while plausible
  text fields may absorb adjacent segments. The fewest-merge interpretation is
  accepted only when it is unique.
- Ambiguous repairs, invalid numbers/dates, NOT NULL failures, and unrecoverable
  short records are quarantined with the complete raw input.
- No value is truncated, shifted silently, coerced to zero, or dropped without
  an audit record.

Malformed central `filings` rows fail final validation by default. Detail-row
quarantines allow `COMPLETED_WITH_WARNINGS` so long as every row is audited.
`--allow-filing-errors` exists for an explicitly reviewed exceptional build.

Audit tables include `import_runs`, `import_sources`, `import_years`,
`import_table_stats`, `import_repairs`, `import_errors`,
`import_duplicate_conflicts`, `import_orphans`, and
`olms_schema_versions`. Each completed build writes:

```text
exports/olms_import_summary.csv
exports/olms_import_repairs.csv
exports/olms_import_errors.csv
exports/olms_duplicate_conflicts.csv
```

Identical duplicate natural keys are deduplicated with every occurrence stored
in the duplicate audit. Conflicting natural keys are removed from the
application-facing source table and preserved as full JSON snapshots for
review. Detail `RPT_ID` values that do not join to a filing remain source rows
but are listed in `import_orphans`.

## Derived identity and filing periods

`organizations` has one row per OLMS `F_NUM`; names are display attributes, not
identity keys. Historical names and addresses remain in `filings`.

`filing_periods` groups original and amended reports by `F_NUM` and covered
period. Financial research uses `latest_rpt_id`. Timeliness uses the original
filing receive date. If no `AMENDMENT=0` report was observed, the result is
`ORIGINAL_NOT_OBSERVED` rather than an automatic lateness conclusion.

Education scope is deterministic and editable:

- NEA is a strong `likely_education` indicator.
- AFT defaults to `education_or_mixed`, with health-care-only terms kept
  uncertain.
- Education-related name terms can produce `likely_education`.
- Other organizations remain `uncertain`, but all filings are still imported.

Durable include/exclude decisions are stored in
`config/olms_scope_overrides.csv`.

## Filing timeliness and potential missing filings

For an observed annual LM report:

```text
due date = PD_COVERED_TO + 90 calendar days
```

An original report received on or before that date is `FILED_ON_TIME`; a later
normal original is `FILED_LATE` with calendar days late. A hardship filing with
a later electronic receive date is `HARDSHIP_REVIEW`, because the source does
not provide the potentially timely paper date.

Potential missing filing detection is deliberately conservative. It requires
two latest consecutive annual periods with the same fiscal-year-end month/day,
an apparently active organization, a passed expected due date, and no report
for the expected next period anywhere in the loaded corpus. Historical gaps
require surrounding consistent annual periods. Irregular latest periods become
`FYE_CHANGED_REVIEW`; short histories become `INSUFFICIENT_HISTORY`; terminated
organizations do not receive an automatic next-year expectation.

Every result stores its explanation, data-as-of date, and rule version. A
missing result describes an expected annual LM financial report and does not
predict whether LM-2, LM-3, or LM-4 would have been required. It is labeled
`POTENTIAL_MISSING_FILING`, never a violation.

## OLMS-to-IRS matching

OLMS annual bulk files do not provide EINs. Matching is deterministic and
explainable:

- exact normalized OLMS name plus ZIP5 and a unique EIN is very high confidence;
- exact normalized name plus city/state and a unique EIN is high confidence;
- exact name plus state or ambiguous strong candidates remain review rows;
- fuzzy name alone is never auto-accepted.

Local numbers are retained during normalization. Affiliation alone is never an
organization name. Because the main IRS `returns` table may be very large and
lacks a name index, the builder makes one streaming read-only pass and retains
only identities whose normalized names occur in OLMS targets. It persists the
small candidate/audit layer, not a copy of IRS returns.

Results live in `irs_matches`; `v_accepted_irs_matches` exposes one accepted EIN
per `F_NUM`. Multiple F_NUMs may legitimately map to the same EIN. Durable
`accept`, `reject`, and `unmatch` actions live in
`config/olms_irs_match_overrides.csv` and override automation.

## Counterparties and payments

Only `PAYER_PAYEE_TYPE=1002` payees feed paid-out research. Counterparty IDs are
deterministic hashes of exact normalized name plus ZIP5, exact normalized name
plus city/state, or a clearly marked weaker name-only signature. Original
aliases remain in `counterparty_aliases`. Exact name plus strong location can
optionally connect a counterparty to a unique IRS EIN; unmatched vendors,
individuals, and organizations are normal.

The accounting separation is enforced in views:

- `v_payment_payees` and summary views use the annual `payer_payee.TOTAL`, with
  itemized and non-itemized components shown separately.
- `v_payment_transactions` and transaction views use individual
  `disbursements_general.AMOUNT` rows.
- Summary totals and itemized transaction amounts are never added together.

Code 503 powers `v_grants_paid_summary` and `v_grant_transactions`.
Non-grant categories power `v_vendor_payments_summary` and
`v_vendor_transactions`. DOL code descriptions come from `erds_codes` rather
than duplicated magic-number labels in query modules.

## Research modules

- OLMS Union Deep Dive
- OLMS Filing Compliance / Timeliness
- OLMS Grants / Contributions Paid
- OLMS Vendors / Contractors / Payees
- OLMS Grantee / Vendor Explorer
- OLMS / IRS Match Audit
- OLMS Import / Data Quality Audit

All filtering is performed by indexed SQL. Preview queries apply a bounded SQL
`LIMIT`; CSV exports stream full results.

## Future expansion

Schema discovery, generic unexpected tables, and stored rule versions allow new
2027+ form fields and rule versions without assuming that current LM-2/LM-3/LM-4
layouts or thresholds are permanent. This phase intentionally does not scrape
DOL websites, download archives, ingest other LM form families, add OLMS to Ask
Database, or make automated legal-violation determinations.
