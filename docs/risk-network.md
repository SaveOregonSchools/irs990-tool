# Precomputed risk-network sidecar

`build_risk_network.py` creates `db\risk_network.db` (or
`IRS_RISK_NETWORK_DB_PATH`) without writing to the IRS source database. The
sidecar contains one filing-year evidence edge per source record, including
grant cash/noncash amounts, contractor compensation, Schedule R relationships,
people roles/compensation, and exact filed addresses.

New builds bind the sidecar to portable identity stored inside the main
database: a stable database UUID, rotating risk-source revision UUID, main-file
size, and SHA-256 of the SQLite header. Neither an absolute path nor a Windows
volume/file identifier is part of the persisted authoritative identity. An
exact checkpointed main/sidecar pair can therefore move together to Linux or a
different filesystem without a rebuild.

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

Recompute an explicit, capped subset against the same unchanged source
snapshot (for example, while reviewing scoring/build-configuration changes):

```powershell
py build_risk_network.py incremental --db db\irs990.db `
  --min-tax-year 2025 --max-filings 10000
```

Incremental mode refuses an unbounded invocation and refuses selections above
`--max-filings` (hard maximum 100,000). It replaces only the selected filing
IDs in the sidecar and recomputes hub metadata for affected nodes. It requires
the sidecar's complete portable source stamp to equal the current main database
stamp. A new revision, header digest, or file size is not accepted: without a
durable changed-filing ledger, a bounded update cannot prove that it covered
every source change. After an append, resolver refresh, applied-layer publish,
manual source change, or other revision advance, run a full rebuild.

Selectors are fail-closed. EINs must be exactly nine digits (plain or
`NN-NNNNNNN`), filing IDs must be nonblank canonical values with no whitespace
and must exist in the selected canonical source, and the minimum tax year may
not exceed the maximum. A malformed member of a repeated/mixed selector list
rejects the entire command; it is never silently dropped. A full plan or build
with selectors performs an exact count, and a zero-row full replacement is
refused so a typo cannot publish an empty sidecar.

A full rebuild is deliberately explicit and fail-closed. It requires the
published `grant_recipient_resolved_plus_ai_v1` view, exact row parity among
`grants`, `grant_recipient_resolved`, and that enhanced view, plus a leading
`filing_id` index/primary key on every streamed source table. The plan checks
objects, columns, indexes, and checkpoint state without scanning source data
tables for an unfiltered plan; a filtered full plan additionally counts its
filing selection exactly. The rebuild repeats those checks and proves unique grant IDs plus exact
`(grant_id, filing_id)` ownership through the raw, resolver, and enhanced
layers. It also independently counts the selected filings and refuses a
truncated streaming result. Required lookup indexes must have real BINARY
columns in leading order (not hidden expression terms or incompatible
collations), and the planner must report an indexed `SEARCH`. Grant-ID keys
must be exact, non-null uniqueness guarantees; nullable unique indexes and the
SQLite `INTEGER PRIMARY KEY DESC` non-rowid trap are rejected.

For the repaired August 2026 archive, the production build completed on August
16 and atomically published `db\risk_network.db` from
`db\irs990-repaired-20260815-003434.db`. The accepted 69.60 GiB sidecar contains
89,256,222 edges for 5,723,809 selected filings and 893,103 covered EINs. The
actual size exceeded the preliminary 25.9-56.1 GiB estimate. Peak usage can be
roughly two to three times the finished size during index/hub construction or
replacement, so retain the conservative 180 GiB free-space floor and recheck
current capacity before any future full build; do not rely on an older
free-space snapshot or launch a second builder while one is running.

The current paths, and the preflight to repeat before a future rebuild, are:

```powershell
$networkPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$networkSource = (Resolve-Path '.\db\irs990-repaired-20260815-003434.db').Path
$networkRoot = (Resolve-Path '.\db').Path
$networkSidecar = Join-Path $networkRoot 'risk_network.db'
$networkScratch = Join-Path $networkRoot '_risk_network_tmp'

New-Item -ItemType Directory -Force -Path $networkScratch | Out-Null
$networkDrive = Get-PSDrive -Name C
$networkDrive | Select-Object Name,Root,@{Name='FreeGiB';Expression={[math]::Round($_.Free / 1GB, 2)}}
if ($networkDrive.Free -lt 180GB) {
  throw 'C: has less than the conservative 180 GiB free-space floor.'
}
[pscustomobject]@{Source=$networkSource; Destination=$networkSidecar; Scratch=$networkScratch}
if ([IO.Path]::GetFullPath($networkSource) -ieq [IO.Path]::GetFullPath($networkSidecar)) {
  throw 'Source and risk-network destination must be different files.'
}
$networkStaleBuilds = Get-ChildItem -LiteralPath $networkRoot -Filter 'risk_network.db.building-*.db'
if ($networkStaleBuilds) {
  $networkStaleBuilds | Select-Object FullName,Length,LastWriteTimeUtc
  throw 'Review the stale .building files before starting a new build; do not delete them blindly.'
}
$networkDestinationAuxiliaries = @(
  Get-Item -LiteralPath ($networkSidecar + '-wal') -ErrorAction SilentlyContinue
  Get-Item -LiteralPath ($networkSidecar + '-journal') -ErrorAction SilentlyContinue
) | Where-Object Length -gt 0
if ($networkDestinationAuxiliaries) {
  $networkDestinationAuxiliaries | Select-Object FullName,Length,LastWriteTimeUtc
  throw 'Checkpoint/recover the old destination or choose a new path; populated auxiliary files make replacement unsafe.'
}
```

Stop Flask, IRS/grant importers, and every other main-database writer. After the
final grant publication, checkpoint/truncate WAL and prove it is empty. This
checkpoint command writes to the source database, so run it only in the stopped
maintenance window:

```powershell
& $networkPython -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); r=c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(); print(r); c.close(); assert r[0] == 0 and r[1] == r[2], r" $networkSource
$networkWal = Get-Item -LiteralPath ($networkSource + '-wal') -ErrorAction SilentlyContinue
if ($networkWal -and $networkWal.Length -gt 0) {
  throw "Source WAL is not empty: $($networkWal.Length) bytes"
}
```

Run the read-only plan, then the confirmed build. Setting process-local
`TEMP`/`TMP` keeps SQLite index-sort scratch in `db\_risk_network_tmp` on `C:`;
the `finally` block restores the caller's values. The builder creates its
`.building-*` database beside the
destination and calls `os.replace` only after quick-check, count, source-status,
filing-state, hub, enhanced-grant provenance, and source-snapshot validation.
Because the temporary and final files are both under `db` on `C:`, that rename
remains same-volume and atomic.
Immediately before the rename it also rechecks that the old destination has no
populated WAL or rollback journal; otherwise SQLite could replay old pages over
the newly validated main file. An existing destination is left untouched on
any such refusal.

```powershell
$networkPreviousTemp = $env:TEMP
$networkPreviousTmp = $env:TMP
try {
  $env:TEMP = $networkScratch
  $env:TMP = $networkScratch
  & $networkPython build_risk_network.py plan `
    --db $networkSource `
    --sidecar $networkSidecar `
    --full
  if ($LASTEXITCODE -ne 0) { throw 'Risk-network plan failed.' }

  & $networkPython build_risk_network.py rebuild `
    --db $networkSource `
    --sidecar $networkSidecar `
    --full `
    --yes
  if ($LASTEXITCODE -ne 0) { throw 'Risk-network rebuild failed.' }
}
finally {
  $env:TEMP = $networkPreviousTemp
  $env:TMP = $networkPreviousTmp
}
```

The full rebuild is a large, hours-long operation. It records the portable
source identity/snapshot, SQLite `data_version`, journal mode, empty-WAL
checkpoint condition, enhanced grant counts, independent selected-filing count,
and source-index preflight. Path, filesystem identity, and modification time are
used only as same-process drift guards while a build is running; they are not
persisted as authoritative lineage. Any source stat, identity/revision,
WAL/journal, or data-version drift observed during the held snapshot prevents
publication and leaves an existing destination untouched. Bounded rebuilds and
incremental refreshes also verify that the caller-selected filing metadata is
still exact inside their held source snapshot.

Enhanced grant edges are scored only for the explicit normalized provenance
allowlist (`deterministic` with a safe resolver status,
`reported_ein_identity_lookup`, `reported_ein_address_location`,
`reported_ein_rule`, and `ai_assisted`). Filing-only unverified identities,
unknown sources, and future/typo variants remain visible but unscored. Final
sidecar validation independently enforces that allowlist before publication,
including bounded builds. The enhanced view is not trusted merely because it
supplies an allowlisted label: each enhanced row is reconstructed against the
deterministic resolver and, for AI/rule rows, the unique applied-decision
artifact (grant/signature, selected EIN/name, confidence, decision, and
model-derived source). Full preflight and the bounded builder both refuse a
spoofed or stale view, and trusted artifact aliases are projected ahead of any
view columns so reserved-name collisions cannot override that proof.

After completion, run the read-only runtime smoke check, inspect final size, and
then set the same path as `IRS_RISK_NETWORK_DB_PATH` for Flask:

```powershell
Get-Item -LiteralPath $networkSidecar | Select-Object FullName,Length,LastWriteTimeUtc
& $networkPython -c "import sys; from queries._risk_network import available,build_metadata; env={'IRS_DB_PATH':sys.argv[1],'IRS_RISK_NETWORK_DB_PATH':sys.argv[2]}; assert available(environ=env); m=build_metadata(__import__('pathlib').Path(sys.argv[2]),environ=env); print(m['meta'])" $networkSource $networkSidecar
$env:IRS_DB_PATH = $networkSource
$env:IRS_RISK_NETWORK_DB_PATH = $networkSidecar
```

The August 2026 build was preceded by an audit and repair of replayed child rows
in the source database. Historical reprocessing had duplicated whole filing
child sets (grants and people rows), which can inflate amounts and edge counts.
For a future full build from a newly repaired or replaced source, repeat the
audit, rebuild deterministic recipient resolution and the applied/enhanced
grant layer, and then rebuild this sidecar in that order.

Stop IRS/grant import writers first;
the builder holds one consistent read snapshot across the entire streamed
build, and concurrent writers could otherwise cause the source WAL to grow for
hours. The Flask/runtime access helper in
`queries/_risk_network.py` opens the finished sidecar in SQLite `mode=ro` with
`query_only=ON`.

## Portability and source-change lifecycle

The runtime prefers the complete portable identity contract. If even one
portable key is present but the set is incomplete or inconsistent, it fails
closed; it never falls back to weaker legacy checks. A wholly legacy sidecar
remains readable only on its original physical source file so it can be upgraded
in place with `migrate_risk_network_portability.py`.

If an older main database has no portable identity and no accepted legacy
sidecar to adopt, initialize it explicitly before its first portable build:

```powershell
.\.venv\Scripts\python.exe migrate_risk_network_portability.py `
  initialize-risk-source-identity --db db\irs990.db --yes
```

Do not use that standalone initializer when a usable legacy sidecar exists;
run the paired `plan`/`apply` migration instead so its legacy lineage can be
proved and adopted before the main file changes.

For a machine move, stop writers, checkpoint/truncate the main database WAL,
verify there is no populated rollback journal, and copy the main database and
`risk_network.db` as one accepted pair. Hash both complete files before and
after transfer. Different absolute paths, drive letters, device/inode values,
or file modification times do not matter. A different database/revision UUID,
fast file-size/header stamp, or populated auxiliary makes the runtime
unavailable. The runtime check deliberately does not hash 47+ GiB on every
dashboard request, so a full-file transfer-hash mismatch also invalidates the
pair even if the fast stamp happens to match.

Supported main-database builders and grant-layer publishers rotate the
risk-source revision when they change network inputs. Direct SQL and external
tools cannot do this automatically. Use the migration utility's explicit
`mark-risk-source-changed` command before such a write when possible, or
immediately afterward while the application remains stopped, and then run a
full rebuild. Never edit the UUID, revision, header digest, size, or snapshot
hash in either database to rebind mismatched files.

```powershell
.\.venv\Scripts\python.exe migrate_risk_network_portability.py `
  mark-risk-source-changed --db db\irs990.db --yes
```

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

This is **not read-only**: it requires the explicit `--full-refresh` flag, builds
and validates a hidden replacement for `grant_recipient_ai_applied`, atomically
swaps that table and `grant_recipient_resolved_plus_ai_v1`, builds two indexes,
runs `ANALYZE`, and commits to the main production database. Before staging it
also verifies every adjudicated decision against its current ordered candidate
set. It reads but does not rebuild the decision table, deterministic matches,
signatures, or candidates.
Back up/checkpoint the main database and stop Flask/background import writers
before running it.
