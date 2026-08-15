# Fraud/risk dashboard operations

This runbook covers the data and credentials used by
`/query/fraud_risk_dashboard`. Run commands from the project root in
PowerShell.

> **Interpretation warning:** the Review Priority Score is a deterministic,
> unvalidated heuristic, not a fraud probability, legal conclusion, or adverse
> finding. Name matches, network links, and external records are investigative
> leads. Verify identity, dates, source records, and context before escalation.

## Deployment state on August 14, 2026

| Operation | State in this workspace |
|---|---|
| Full IRS/OFAC/HHS screening download and atomic sidecar build | **Run.** `db\screening_data.db` exists (about 1.82 GiB). |
| Full risk-network planning command | **Run read-only.** It estimated 5,723,809 filings and 98,925,700 possible source rows. |
| FAC current/historical bulk download and sidecar build | **Run and verified.** `db\fac_audits.db` is 24.32 GiB, source-as-of August 14, 2026, and covers 19,153,987 accepted source rows and 1,135,938 reports from 1998–2026. |
| Production child-row replay audit/repair and post-repair grant rebuild | **Not run.** Complete these before the first full network build. |
| Full risk-network build | **Not run.** `db\risk_network.db` is absent. |
| Personal API registration and secret installation | **Not performed.** Shared `DEMO_KEY` values are configured for FAC/FEC development but can exhaust their shared quota; no SAM key is installed. Secret values were not printed. |

## Credentials and no-key coverage

No key is needed for local IRS filing/BMF/grant/person/address/Schedule R
analysis; the risk-network builder; IRS Publication 78 and automatic-revocation
files; OFAC SDN and consolidated lists; HHS-OIG LEIE; FAC current and historic
bulk files; or USAspending. LDA.gov also works anonymously, currently at the
official 15-requests/minute limit. USAspending is queried only after FAC yields
an exact primary-EIN UEI.

Keys enable these live checks:

| Setting | Used for | Official acquisition steps |
|---|---|---|
| `FAC_API_KEY`, `FEC_API_KEY` | Live FAC and OpenFEC | Open the [Data.gov signup form](https://api.data.gov/signup/), submit it, verify the email message, and copy the issued key. One personal Data.gov key can be placed in both settings. See the [Data.gov developer manual](https://api.data.gov/docs/developer-manual/). |
| `SAM_API_KEY` | SAM entity registration and exclusions | Create/sign in to an individual SAM.gov account, open [Account Details](https://sam.gov/workspace/profile/account-details), find **Public API Key**, select the eye icon, enter the one-time password sent to the account email, and copy the revealed key. The [official Entity API guide](https://open.gsa.gov/api/entity-api/) documents the process and quotas; the [Exclusions API guide](https://open.gsa.gov/api/exclusions-api/) covers the second check. A non-federal personal account with no SAM role may receive only 10 requests/day. |
| `LDA_API_KEY` (optional) | Higher-rate LDA searches | Anonymous access needs no setup. For the higher registered limit, complete the [official registration form](https://lda.gov/api/register/) (email, name, username, password, and terms), then use the issued token. The [LDA API terms](https://lda.gov/api/tos/) currently state 120 requests/minute with a key versus 15/minute anonymously; also see the [API documentation](https://lda.gov/api/). |

FAC bulk data and its findings/corrective-action narratives come from the
official [2016-present download page](https://www.fac.gov/data/download/current/)
and [1998-2015 archive](https://www.fac.gov/data/download/historic/). They do
not require Data.gov, SAM.gov, or Login.gov accounts.

## Configure, restart, and verify

Keep `.env` local and never commit it. Populate only the credentials obtained
for this installation; retain the conservative SAM defaults:

```dotenv
IRS_DB_PATH=db/irs990.db
FAC_API_KEY=replace_with_personal_data_gov_key
FEC_API_KEY=replace_with_the_same_personal_data_gov_key
SAM_API_KEY=replace_with_personal_sam_key
SAM_MAX_UEIS=1
SAM_REQUEST_BUDGET=3
LDA_API_KEY=
FAC_DB_PATH=db/fac_audits.db
IRS_SCREENING_DB_PATH=db/screening_data.db
IRS_SCREENING_CACHE_DIR=downloads/screening
IRS_RISK_NETWORK_DB_PATH=db/risk_network.db
```

An empty `LDA_API_KEY` deliberately uses anonymous LDA access. SAM defaults to
the newest FAC primary-EIN UEI and at most three requests per uncached lookup.
Its cache is process-local for five minutes, so a restart or later repeat uses
quota again.

Stop the current Flask process and restart it so `common.py` reloads `.env`:

```powershell
.\.venv\Scripts\python.exe app.py
```

Then open <http://127.0.0.1:5000/query/fraud_risk_dashboard>, enter a known EIN,
choose **Run configured live APIs**, and submit once. Review each source's
status/provenance. `not_configured`, `partial`, `truncated`, rate-limit, and
timeout states are explicit; a SAM partial result can be expected when the
bounded budget omits UEIs or pages. Do not repeatedly submit merely to test SAM.

Local, no-network smoke checks:

```powershell
.\.venv\Scripts\python.exe -c "from queries._risk_screening import lookup_irs_status; r=lookup_irs_status('000587764'); print(r.get('available'), len(r.get('records') or r.get('results') or []), r.get('error'))"
.\.venv\Scripts\python.exe -c "from queries._risk_network import available, risk_network_path; print(risk_network_path(), available())"
```

The second command should remain `False` until the full network sidecar exists
and matches the current physical identity and size/mtime of `IRS_DB_PATH`.

## Refresh public sidecars

Screening first build (already completed here):

```powershell
.\.venv\Scripts\python.exe build_screening_sidecar.py --download
```

Normal all-source refresh (run monthly and after material OFAC changes):

```powershell
.\.venv\Scripts\python.exe build_screening_sidecar.py --refresh-downloads
```

Do not use a source-specific command for production: it creates a replacement
sidecar containing only that family. The normal command stages and validates
complete source groups before atomically installing the database. See
[screening-data.md](screening-data.md) for rollback, provenance, and candidate-
only limitations.

Print the current official FAC URLs without downloading:

```powershell
.\.venv\Scripts\python.exe build_fac_db.py --print-download-urls
```

First complete no-key FAC build (completed here on August 14, 2026; retain for
fresh installations or a from-scratch rebuild):

```powershell
$asOf = Get-Date -Format yyyy-MM-dd
.\.venv\Scripts\python.exe build_fac_db.py `
  --download-current `
  --download-historic `
  --download-dir imports\fac `
  --db db\fac_audits.db `
  --source-as-of $asOf
```

Subsequent atomic refresh:

```powershell
$asOf = Get-Date -Format yyyy-MM-dd
.\.venv\Scripts\python.exe build_fac_db.py `
  --download-current `
  --download-historic `
  --download-dir imports\fac `
  --refresh-downloads `
  --db db\fac_audits.db `
  --source-as-of $asOf `
  --replace
```

Rerun the same command after interruption; downloads and the database build are
resumable, and the finished sidecar is replaced only after validation. Verify a
bounded lookup with a real target EIN:

```powershell
.\.venv\Scripts\python.exe -c "import json; from fac_bulk import lookup_fac_by_ein; print(json.dumps(lookup_fac_by_ein('123456789'), indent=2, default=str))"
```

See [fac-offline.md](fac-offline.md) for source dictionaries, checksums, and
download-only/manual alternatives.

## Required maintenance before the first full network build

Production history has confirmed replayed whole child-row sets for filings,
including grants and people. This can inflate amounts and relationship edges.
The dashboard quarantines focal grant years whose core total and extracted
grant detail materially disagree, but that is not a database-wide repair and
does not cover every repeated child family. The loader now deletes every known
repeated child family transactionally when replacing a filing, preventing the
same future replay behavior.

Use this order; do not build the global network from the known-dirty children:

1. **Stop writers.** Stop Flask, guided imports, XML loaders, grant jobs, DB
   Browser writes, and any other process holding either SQLite database.
2. **Checkpoint and back up.** Allow enough space for the roughly 50 GiB main
   database plus the grant workspace. The following uses SQLite's online backup
   API and fails if the WAL checkpoint is busy:

   ```powershell
   $stamp = Get-Date -Format yyyyMMdd-HHmmss
   New-Item -ItemType Directory -Force db\backup | Out-Null
   .\.venv\Scripts\python.exe -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); ck=s.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(); print('checkpoint',ck); assert ck[0]==0,'checkpoint busy'; d=sqlite3.connect(sys.argv[2]); s.backup(d); qc=d.execute('PRAGMA quick_check').fetchone()[0]; print('backup quick_check',qc); assert qc=='ok','backup failed quick_check'; d.close(); s.close()" db\irs990.db "db\backup\irs990-before-child-repair-$stamp.db"
   if (Test-Path db\grant_matching_work.db) { .\.venv\Scripts\python.exe -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); ck=s.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(); print('checkpoint',ck); assert ck[0]==0,'checkpoint busy'; d=sqlite3.connect(sys.argv[2]); s.backup(d); qc=d.execute('PRAGMA quick_check').fetchone()[0]; print('backup quick_check',qc); assert qc=='ok','backup failed quick_check'; d.close(); s.close()" db\grant_matching_work.db "db\backup\grant_matching_work-before-child-repair-$stamp.db" }
   ```

3. **Audit and reprocess affected filings.** Retain a CSV of filing ID, source
   XML, affected child tables, pre/post counts, and reconciliation results.
   Compare source XML with all repeated child families, not just grants and
   people. `--append` and `--keep-db` deliberately skip existing filings, and
   there is currently no supported targeted-replacement CLI; do not improvise
   production `DELETE` statements. The safest supported repair is a clean build
   into a new, versioned staging database from the authoritative XML archive:

   ```powershell
   $stamp = Get-Date -Format yyyyMMdd-HHmmss
   $repairDb = "db\irs990-repaired-$stamp.db"
   .\.venv\Scripts\python.exe rebuild_irs990_slim_clean.py `
     --db $repairDb `
     --xml-dir D:\path\to\authoritative-irs-xml-archive
   ```

   Do not point this command at the active DB: without append flags the builder
   deletes its destination first. Validate filing/child counts, canonical rows,
   duplicate signatures, `PRAGMA integrity_check`, and representative dashboard
   years in staging. Preserve and reconcile reviewed decision records before
   cutover; keep the backup for rollback.
4. **Rebuild grant resolution and reapply decisions.** Against the repaired DB,
   run the documented full workflow, which rebuilds deterministic resolution,
   identity/signature/candidate layers, rule decisions, and the applied view:

   ```powershell
   .\batch_enhanced_grant_matches.ps1 `
     -DbPath C:\full\path\to\db\irs990-repaired-YYYYMMDD-HHMMSS.db `
     -WorkDbPath C:\full\path\to\db\grant_matching_work.db `
     -ProjectDir C:\Projects\irs990-tool `
     -Yes
   ```

   A clean staging DB does not automatically inherit old human/AI decisions.
   After signatures and candidates are regenerated, dry-run and re-import the
   retained decision JSONL files (or perform a separately reviewed, keyed table
   migration), and accept only decisions that still map to the same signature
   and a valid candidate. See [grant-matching.md](grant-matching.md) for the
   decision import commands. Rerun `apply-decisions` after that import. If only
   the applied layer is missing after all grant inputs and decisions are
   validated, restore it with:

   ```powershell
   .\.venv\Scripts\python.exe grant_ai_assist_v1.py apply-decisions `
     --db C:\full\path\to\db\irs990-repaired-YYYYMMDD-HHMMSS.db `
     --work-db db\grant_matching_work.db `
     --full-refresh
   ```

5. **Cut over the validated main DB, then build the risk network.** Update
   `IRS_DB_PATH`, restart, smoke-test local dashboard results, and only then run
   the full network commands below. Keep writers stopped through the network
   snapshot so the source WAL cannot grow for hours.

## Full risk-network build

Re-run the read-only plan after repair/cutover because its estimate depends on
current SQLite statistics. Replace the path below if `.env` points
`IRS_DB_PATH` at a versioned file:

```powershell
.\.venv\Scripts\python.exe build_risk_network.py plan --db db\irs990.db --full
```

The August 14 plan estimated a **27.6-59.9 GiB** finished sidecar. Reserve at
least about **70 GiB free for a first build**; an atomic replacement can briefly
need the old and new sidecars together, so budget roughly **130 GiB** when
refreshing a near-upper-bound sidecar. Runtime has not been measured here.
Treat it as a multi-hour/overnight maintenance job and monitor free space; the
builder writes a temporary database and atomically replaces only a completed
sidecar.

Full command (not yet run here):

```powershell
.\.venv\Scripts\python.exe build_risk_network.py rebuild `
  --db db\irs990.db `
  --full `
  --yes
```

After completion, restart Flask, rerun the local `available()` smoke check, and
confirm the dashboard reports complete indexed-network coverage and current
source lineage. Any later replacement or in-place size/mtime change to the main
database intentionally marks the sidecar stale until it is rebuilt.
