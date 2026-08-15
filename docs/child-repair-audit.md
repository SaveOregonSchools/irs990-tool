# Child-row repair audit

`audit_child_repair.py` compares every repeated XML child family in a backed-up
pre-repair database with a clean repaired staging database. It is the required
pre/post evidence report before a repaired database is cut over or used to
build the global risk network.

The auditor never opens either SQLite database writable. Both connections use
URI `mode=ro`, `PRAGMA query_only=ON`, and a consistent read transaction.
Writers must still remain stopped so an attached source WAL cannot grow during
the potentially long comparison.

## Production command

Use explicit, versioned report names and the verified pre-repair backup—not the
active production database:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
.\.venv\Scripts\python.exe audit_child_repair.py `
  --source-db db\backup\irs990-before-child-repair-YYYYMMDD-HHMMSS.db `
  --repaired-db db\irs990-repaired-YYYYMMDD-HHMMSS.db `
  --summary-csv "exports\child-repair-summary-$stamp.csv" `
  --detail-csv "exports\child-repair-detail-$stamp.csv" `
  --detail-json "exports\child-repair-detail-$stamp.json" `
  --fail-on-new
```

`--fail-on-new` is recommended for this repair because the manifest-aware clean
rebuild is expected to use the same filing population and extractor snapshot.
Omit it only when newly extracted child rows have been separately reviewed and
are intentionally allowed.

The default audit treats extractor-driven payload changes as failures. For the
current clean-rebuild repair only, the following explicit opt-in enables three
narrow verifiers:

```powershell
--allow-verified-extractor-enrichments `
--extractor-enrichment-xml-root "C:\Projects\IRSDB\XML"
```

The XML root is mandatory with the opt-in. Every XML-backed check resolves the
repaired return's selected `source_file`, requires it to remain inside that
root, and requires its EIN, tax year, return type, filing ID, and object ID to
match the repaired return. A failed verifier is classified `unexplained` and
gates even if `--fail-on-new` is absent. Omitting the opt-in leaves every such
payload change under the normal hard-fail behavior.

The command writes reports atomically and returns:

- `0` when all configured gates pass;
- `2` when the completed audit contains a gating discrepancy;
- `3` for an operational error such as a missing database or unsafe report
  path.

The tool refuses to use either database path—or its `-wal`, `-shm`, or
`-journal` companion path—as a report path. All three report paths must also be
distinct.

## Exact returns and provenance population

Before inspecting children, the auditor streams both `returns` tables through
an index led by `filing_id`. It requires exactly the same filing population and
the same portable `source_file` provenance for every filing. Exact normalized
paths match. When an XML archive has moved, paths also match if they identify
the same IRS object and share at least the trailing archive-directory component
and XML filename. A filename-only match is deliberately insufficient, so a
different archive directory or object remains a hard failure. The auditor also
requires the filename object ID to agree with the filing ID. Blank sources,
malformed object coverage, duplicate return rows, missing filings, newly
introduced filings, and changed source provenance are all hard cutover
failures. New return rows fail even when `--fail-on-new` is omitted; that option
controls only newly introduced child sets.

The `returns` summary also records the exact distinct filing, source-file, and
normalized-object counts for both databases. Each side must have one return,
filing ID, source file, and object ID per loaded filing, matching the clean
loader's manifest-coverage invariant.

## Bounded comparison strategy

For every table in `MULTIROW_CHILD_TABLES`, the auditor requires an index led
by `filing_id`. It streams payload rows in filing-index order from each
database:

```sql
SELECT filing_id, payload_columns...
FROM child_table INDEXED BY filing_id_index
ORDER BY filing_id;
```

For each filing, the tool builds a typed, deterministic, order-independent
multiset digest. Two domain-separated SHA-256 row hashes are accumulated modulo
2^256 together with the exact row count, so memory remains constant regardless
of table size or the number of filings. This detects changed payloads even when
old and repaired counts are equal. Only a digest/count mismatch triggers a
second indexed lookup that materializes the two payload multisets for exact
classification; that memory is bounded to one mismatched filing at a time.

Source-manifest validation remains a required complementary check because it
proves the rebuild selected the same authoritative XML object and source
variant before the database comparison runs.

## Classifications and gates

| Classification | Meaning | Default gate |
|---|---|---|
| `expected_exact_replay_cleanup` | The repaired payload multiset has the same payload signatures and lower multiplicities. Removed rows are exact copies. | Pass |
| `verified_extractor_enrichment` | An explicitly enabled, allowlisted extractor enrichment passed its complete directional and XML checks. | Pass |
| `missing_in_rebuild` | A source child set became empty in the repaired database. | Fail |
| `new_in_rebuild` | A child set exists only in the repaired database. | Report; fail with `--fail-on-new` |
| `content_changed` | Payload signatures changed, or row additions/removals are not exact replay cleanup. | Fail |
| `unexplained` | Schema/index mismatch, orphan filing, changed snapshot count, invalid grant number, unavailable grant core total, or another invariant failure. | Fail |

`whole_set_replay_factor` is populated when every payload multiplicity was an
exact common multiple of the clean set. Partial exact-copy cleanup remains an
expected cleanup but has no whole-set factor.

## Opt-in extractor enrichment proofs

`--allow-verified-extractor-enrichments` never permits a general payload
difference. It recognizes only:

- `grants`: source `cash_grant_amt IS NULL` to repaired numeric zero. Every
  other field must be identical. A directional multiset proof requires every
  repaired row to come from an allowed source signature and every distinct
  source signature to remain represented; only duplicate source multiplicity
  may be removed as replay cleanup. The complete repaired grant multiset must
  also exactly equal a fresh current extraction of the selected XML. Rows whose
  only substantive value is the repaired cash zero are rejected. An explicit
  noncash zero or any other populated recipient/purpose/noncash field is not
  treated as blank.
- `irs990_pf_officer_dir_trst_key_empl_info_grp`: source NULL values may be
  populated only in `employee_benefits_amt` and/or `expense_account_amt`, with
  all other fields unchanged and replay reduction proven directionally. The
  complete repaired filing multiset must exactly equal a fresh extraction of
  its selected XML. Each newly populated value must originate from the
  diagnosed alternate XML tag (`EmployeeBenefitProgramAmt` or
  `ExpenseAccountOtherAllwncAmt`); primary-tag-only changes are rejected.
- `irs990_schedule_c_supplemental_info`: source rows must be a directional
  submultiset of repaired rows, at least one row must be new, and the complete
  repaired multiset must exactly equal a fresh current-extractor result from
  the selected XML.

Grant, PF, and Schedule C verification parse every candidate filing rather than
sampling (approximately 7,600 grant and 500,000 PF filings for this repair).
The auditor retains only one XML tree and one filing's payload counters at a
time, so memory is bounded, but the integrity check adds substantial runtime.

Repaired-only rows in these three allowlisted tables hard-gate without the
explicit opt-in even when `--fail-on-new` is absent. Other child tables retain
the general `new_in_rebuild` behavior described above.

## Reports

The summary CSV has a `returns` population/provenance row, one row per repeated
child table, and `__TOTAL__`. It records indexed row/filing counts,
source-file/object coverage, mismatch classifications, extra rows, whole-set
replay candidates, grant inflation counts, and gate failures.

The detail CSV contains bounded evidence sampled in filing-index order. By
default it writes at most 1,000 mismatched filing/table rows per audit scope and
25,000 rows overall, preventing a broad systemic mismatch from producing a
multi-gigabyte report. `--detail-limit-per-table` and `--detail-limit-total`
can lower or raise those positive limits. The summary remains exact and records
`detail_evidence_rows`, `detail_rows_written`, and `detail_rows_suppressed` for
every scope. The JSON contains the same bounded details, complete exact summary,
database size/page/schema identity, limit/suppression totals, and final gate
state.

Each written detail row includes filing provenance, old/new counts, typed
multiset digests, exact/new row counts, replay factor, classification, and
notes.

For `grants`, the detail reports also compare cash plus noncash detail totals to
the form-specific core grants-paid total in both databases. The material and
inflated flags use the dashboard thresholds: a difference of at least $10,000
or 20 percent is material, and detail above 125 percent of core is inflated.
A remaining clean-source mismatch is reported for investigation; it is not by
itself treated as fraud.

## Cutover acceptance

Do not cut over when the command returns nonzero. For a strict manifest-clean
rebuild, acceptance requires:

- exact `returns` filing population and exact `source_file`/object coverage;
- all 19 child tables present with a leading `filing_id` index and identical
  non-surrogate schemas;
- zero `missing_in_rebuild`, `content_changed`, and `unexplained` rows;
- zero `new_in_rebuild` rows when `--fail-on-new` is used;
- every source row reduction classified as exact replay cleanup;
- all remaining repaired grant inflation cases individually explained;
- `PRAGMA integrity_check` equal to `ok`, exact filing/object coverage, and the
  representative dashboard smoke checks documented in
  [fraud-risk-operations.md](fraud-risk-operations.md).

Retain all three reports with the verified backups and source-selection audit.
