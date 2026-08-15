# Precomputed risk-network sidecar

`build_risk_network.py` creates `db\risk_network.db` (or
`IRS_RISK_NETWORK_DB_PATH`) without writing to the IRS source database. The
sidecar contains one filing-year evidence edge per source record, including
grant cash/noncash amounts, contractor compensation, Schedule R relationships,
people roles/compensation, and exact filed addresses.

Canonical filings are used by default so amended or superseded submissions do
not double-count a tax year. `--include-noncanonical` is available for a
provenance audit that intentionally needs every submitted return.

Every edge records its source table and row ID, confidence and confidence basis,
whether it is eligible for scoring, filing/tax year, and hub-suppression state.
Name-only Schedule R records and untrusted grant matches remain available as
unscored evidence. Unrelated taxable partnerships are intentionally unscored.

## Safe workflow

Plan a bounded build first; this is read-only:

```powershell
py build_risk_network.py plan --db db\irs990.db --ein 123456789
```

Build a small sidecar for review:

```powershell
py build_risk_network.py rebuild --db db\irs990.db `
  --sidecar db\risk_network_review.db --ein 123456789 --yes
```

Refresh an explicit, capped subset after importing filings:

```powershell
py build_risk_network.py incremental --db db\irs990.db `
  --min-tax-year 2025 --max-filings 10000
```

Incremental mode refuses an unbounded invocation and refuses selections above
`--max-filings` (hard maximum 100,000). It replaces only the selected filing
IDs in the sidecar and recomputes hub metadata for affected nodes.

A full rebuild is deliberately explicit. First inspect the SQLite-statistics
estimate, then schedule the build:

```powershell
py build_risk_network.py plan --db db\irs990.db --full
py build_risk_network.py rebuild --db db\irs990.db --full --yes
```

The full rebuild writes a temporary database and replaces only the network
sidecar after successful completion. On the current multi-million-filing corpus
it is a large, hours-long operation and should be run during a maintenance
window after confirming free disk space.

Before the first full build, audit and repair any replayed child rows in the
source database. Historical reprocessing has been confirmed to duplicate whole
filing child sets (grants and people rows), which can inflate amounts and edge
counts. The dashboard quarantines focal grant years that fail its core-versus-
detail reconciliation, but that does not clean other organizations or every
child family. After a confirmed cleanup, rebuild deterministic recipient
resolution, the applied/enhanced grant layer, and then this sidecar in that
order.

Stop IRS/grant import writers first;
the builder holds one consistent read snapshot across the entire streamed
build, and concurrent writers could otherwise cause the source WAL to grow for
hours. The Flask/runtime access helper in
`queries/_risk_network.py` opens the finished sidecar in SQLite `mode=ro` with
`query_only=ON`.

`network_for_ein()` provides the bounded dashboard read shape: outgoing
evidence, indexed direct incoming EIN relationships, and shared
person/address/contractor neighbors. It excludes unscored edges and hub targets
by default, retains exact filing years, amounts, provenance, and confidence,
and returns build/source-coverage metadata. `build_metadata()` exposes that
metadata without loading relationship rows.

## Enhanced-grant layer restoration

If `grant_recipient_ai_applied` and `grant_recipient_resolved_plus_ai_v1` are
missing but the decision table and signature mapping sidecar are intact, restore
only that final layer with:

```powershell
.\.venv\Scripts\python.exe grant_ai_assist_v1.py apply-decisions `
  --db db\irs990.db --work-db db\grant_matching_work.db --full-refresh
```

This is **not read-only**: it drops/recreates `grant_recipient_ai_applied`,
recreates `grant_recipient_resolved_plus_ai_v1`, builds two indexes, runs
`ANALYZE`, and commits to the main production database. It reads but does not
rebuild the decision table, deterministic matches, signatures, or candidates.
Back up/checkpoint the main database and stop Flask/background import writers
before running it.
