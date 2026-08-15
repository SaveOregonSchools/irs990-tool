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
the same `source_file` mapping for every filing. It also normalizes the XML
filename to its IRS object ID and requires that object to agree with the filing
ID. Blank sources, malformed object coverage, duplicate return rows, missing
filings, newly introduced filings, and changed source provenance are all hard
cutover failures. New return rows fail even when `--fail-on-new` is omitted;
that option controls only newly introduced child sets.

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
| `missing_in_rebuild` | A source child set became empty in the repaired database. | Fail |
| `new_in_rebuild` | A child set exists only in the repaired database. | Report; fail with `--fail-on-new` |
| `content_changed` | Payload signatures changed, or row additions/removals are not exact replay cleanup. | Fail |
| `unexplained` | Schema/index mismatch, orphan filing, changed snapshot count, invalid grant number, unavailable grant core total, or another invariant failure. | Fail |

`whole_set_replay_factor` is populated when every payload multiplicity was an
exact common multiple of the clean set. Partial exact-copy cleanup remains an
expected cleanup but has no whole-set factor.

## Reports

The summary CSV has a `returns` population/provenance row, one row per repeated
child table, and `__TOTAL__`. It records indexed row/filing counts,
source-file/object coverage, mismatch classifications, extra rows, whole-set
replay candidates, grant inflation counts, and gate failures.

The detail CSV has one row per mismatched filing/table pair. It includes filing
provenance, old/new counts, typed multiset digests, exact/new row counts,
replay factor, classification, and notes. The JSON contains the same detail
plus database size/page/schema identity, the complete summary, and final gate
state.

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
