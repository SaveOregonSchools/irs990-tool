# Fraud/risk dashboard operations

This runbook covers the data and credentials used by
`/query/fraud_risk_dashboard`. Run commands from the project root in
PowerShell.

> **Interpretation warning:** the Review Priority Score is a deterministic,
> unvalidated heuristic, not a fraud probability, legal conclusion, or adverse
> finding. Name matches, network links, and external records are investigative
> leads. Verify identity, dates, source records, and context before escalation.

## Deployment state after acceptance testing on August 21, 2026

| Operation | State in this workspace |
|---|---|
| Full IRS/OFAC/HHS screening download and atomic sidecar build | **Run.** `db\screening_data.db` exists (about 1.82 GiB). |
| Full risk-network planning command | **Completed.** The full plan selected 5,723,809 filings and estimated a 92,650,326 source-row ceiling. |
| FAC current/historical bulk download and sidecar build | **Run and verified.** `db\fac_audits.db` is 24.32 GiB, source-as-of August 14, 2026, and covers 19,153,987 accepted source rows and 1,135,938 reports from 1998–2026. |
| Production child-row replay audit/repair and post-repair grant rebuild | **Completed and verified.** The repaired main database is `db\irs990-repaired-20260815-003434.db`; its matching grant workspace and enhanced applied layer are complete. |
| Full risk-network build | **Completed and verified.** The 69.60 GiB `db\risk_network.db` was published on August 16, 2026 after full validation. It contains 89,256,222 edges for 5,723,809 selected filings and 893,103 covered EINs, with exact lineage to `db\irs990-repaired-20260815-003434.db`. |
| Personal API registration and secret installation | **Completed and acceptance-tested locally.** Personal non-demo FAC/FEC, SAM, and LDA credentials load from the ignored `.env`; secret values were not printed or committed. |

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
IRS_DB_PATH=db/irs990-repaired-20260815-003434.db
IRS_GRANT_WORK_DB_PATH=db/grant_matching_work-repaired-20260815-003434.db
FAC_API_KEY=replace_with_personal_data_gov_key
FEC_API_KEY=replace_with_the_same_personal_data_gov_key
SAM_API_KEY=replace_with_personal_sam_key
SAM_MAX_UEIS=1
SAM_REQUEST_BUDGET=3
LDA_API_KEY=replace_with_personal_lda_key_or_leave_blank
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
.\.venv\Scripts\python.exe -c "import common; from queries._risk_network import available, risk_network_path; print(risk_network_path(), available())"
```

The second command must return `True` for this completed deployment. `False`
means the sidecar is missing, incomplete, or stale for the configured physical
identity and size/mtime of `IRS_DB_PATH`.

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

## Completed August 2026 repair/build procedure

Production history has confirmed replayed whole child-row sets for filings,
including grants and people. This can inflate amounts and relationship edges.
The dashboard quarantines focal grant years whose core total and extracted
grant detail materially disagree, but that is not a database-wide repair and
does not cover every repeated child family. The loader now deletes every known
repeated child family transactionally when replacing a filing, preventing the
same future replay behavior.

This deployment used the following order. Repeat it before a future full build
from any newly repaired or replaced source; do not build the global network
from known-dirty children:

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
   there is no supported targeted-replacement CLI; do not improvise production
   `DELETE` statements. First prove that the portable source manifest selects
   exactly the production population without opening a destination database:

   ```powershell
   .\.venv\Scripts\python.exe rebuild_irs990_slim_clean.py `
     --manifest-selection-only `
     --manifest-db db\irs990_sources.db `
     --expected-selection-count 5904356
   ```

   The selector preserves the loaded source path for every object-ID conflict
   and rejects unresolved, ambiguous, quarantined, missing, changed-size/mtime,
   hash-mismatched, or out-of-root sources. The safest supported repair is then
   a manifest-verified clean build into a new, versioned staging database:

   ```powershell
   $stamp = Get-Date -Format yyyyMMdd-HHmmss
   $repairDb = "db\irs990-repaired-$stamp.db"
   .\.venv\Scripts\python.exe rebuild_irs990_slim_clean.py `
     --db $repairDb `
     --manifest-clean-rebuild `
     --manifest-db db\irs990_sources.db `
     --expected-selection-count 5904356
   ```

   `IRS_XML_ROOT` supplies the archive path; use one explicit `--xml-dir` to
   override it. Selection and all path/count checks finish before staging writes
   begin. Manifest mode refuses the active DB and every existing staging path,
   builds to a temporary file, fails hard on any extraction/header or coverage
   error, and publishes the new staging filename only after validation and WAL
   checkpoint. Validate filing/child counts, canonical rows, duplicate
   signatures, `PRAGMA integrity_check`, and representative dashboard years in staging.
   Preserve and reconcile reviewed decision records before cutover; keep the
   backup for rollback.

   Produce the retained old-versus-clean child audit before rebuilding grant
   resolution. Both databases are opened read-only. The audit compares exact
   returns/source-file/object coverage and streams an order-independent payload
   multiset digest for every filing in all 19 child tables; full payload sets
   are materialized only for digest or count mismatches:

   ```powershell
   $auditStamp = Get-Date -Format yyyyMMdd-HHmmss
   $xmlRoot = (Resolve-Path $env:IRS_XML_ROOT).Path
   .\.venv\Scripts\python.exe audit_child_repair.py `
     --source-db db\backup\irs990-before-child-repair-YYYYMMDD-HHMMSS.db `
     --repaired-db db\irs990-repaired-YYYYMMDD-HHMMSS.db `
     --source-manifest-db db\irs990_sources.db `
     --manifest-xml-root $xmlRoot `
     --summary-csv "exports\child-repair-summary-$auditStamp.csv" `
     --detail-csv "exports\child-repair-detail-$auditStamp.csv" `
     --detail-json "exports\child-repair-detail-$auditStamp.json" `
     --allow-verified-extractor-enrichments `
     --extractor-enrichment-xml-root $xmlRoot `
     --fail-on-new
   ```

   Do not proceed on a nonzero exit. Review every remaining clean grant
   reconciliation warning even when the structural audit passes. The explicit
   enrichment flag is limited to the diagnosed grant NULL-to-zero, alternate-tag
   PF benefit/expense, and selected-XML Schedule C changes; omitting it preserves
   hard-fail behavior. Grant, PF, and Schedule C candidates are re-extracted
   from their root-confined selected XML one filing at a time, so budget
   additional audit runtime. The separate manifest flags also verify historical
   `returns.source_file` layout migrations against the completed scan, exact
   rebuild selection, unchanged selected file, and typed XML header; a basename,
   partial directory tail, non-selected duplicate, or out-of-root path never
   passes. The August 2026 repair contains 311,335 such verified path-only
   candidates (222,964 from the former 2022 cycle layout and 88,371 from the
   former `Found/2019` layout). See
   [Child-row repair audit](child-repair-audit.md) for classifications,
   report fields, limitations, and acceptance gates.
4. **Rebuild grant resolution and reapply decisions.** Use the sequence below,
   not the all-in-one batch script: reviewed rows must be migrated after current
   signatures/candidates exist but before regenerated rules fill undecided
   signatures.

   ```powershell
   $python = ".\.venv\Scripts\python.exe"
   $project = "C:\Projects\irs990-tool"
   $source = "db\backup\irs990-before-child-repair-YYYYMMDD-HHMMSS.db"
   $main = "db\irs990-repaired-YYYYMMDD-HHMMSS.db"
   $work = "db\grant_matching_work-repaired-YYYYMMDD-HHMMSS.db"

   & $python resolve_grant_recipients.py --db $main --full-refresh --batch-size 100000
   & $python grant_ai_assist_v1.py verify-bmf --project-dir $project
   & $python grant_ai_assist_v1.py build-identity --db $main --work-db $work --project-dir $project --full-refresh
   & $python grant_ai_assist_v1.py build-signatures --db $main --work-db $work --full-refresh
   & $python grant_ai_assist_v1.py generate-candidates --db $main --work-db $work --full-refresh --candidate-mode fast
   & $python grant_ai_assist_v1.py generate-candidates --db $main --work-db $work --candidate-mode balanced --queue-status no_candidates
   ```

   A clean staging DB does not inherit old human/AI decisions. Dry-run the
   dedicated migration against the read-only pre-repair backup. Review both
   reports and resolve every quarantine before applying:

   ```powershell
   $stamp = Get-Date -Format yyyyMMdd-HHmmss
   & $python grant_ai_assist_v1.py migrate-reviewed-decisions `
     --source-db $source `
     --db $main `
     --work-db $work `
     --audit-csv "exports\reviewed-decision-migration-dry-$stamp.csv" `
     --quarantine-jsonl "exports\reviewed-decision-quarantine-dry-$stamp.jsonl"

   $stamp = Get-Date -Format yyyyMMdd-HHmmss
   & $python grant_ai_assist_v1.py migrate-reviewed-decisions `
     --source-db $source `
     --db $main `
     --work-db $work `
     --audit-csv "exports\reviewed-decision-migration-apply-$stamp.csv" `
     --quarantine-jsonl "exports\reviewed-decision-quarantine-apply-$stamp.jsonl" `
     --apply
   ```

   Run `--apply` offline with all Flask, SQLite, and builder connections stopped.
   It uses one potentially long exclusive transaction across same-filesystem
   main/work DBs. Reports publish only after commit; if an error says
   `DATABASE COMMITTED`, preserve the named temporary reports and reconcile
   before rerunning. The migration retains specific source evidence fields but
   refreshes candidate/validation fields; it is not a byte-for-byte copy or
   proof of human review. See
   [Migrate reviewed decisions after a clean rebuild](grant-matching.md#migrate-reviewed-decisions-after-a-clean-rebuild)
   for readiness, quarantine, replacement-audit, and recovery details.

   Regenerate deterministic decisions without `--regenerate`, so the migrated
   rows keep precedence, then rebuild the applied layer:

   ```powershell
   & $python grant_ai_assist_v1.py reported-ein-triage --db $main --work-db $work --placeholder-action human_review
   & $python grant_ai_assist_v1.py nonadjudicable-recipient-triage --db $main --work-db $work --action human_review --include-blank-recipient-name
   & $python grant_ai_assist_v1.py candidate-rule-decisions --db $main --work-db $work --rules exact_name_zip,exact_name_city_state,exact_address_zip_good_name
   & $python grant_ai_assist_v1.py candidate-rule-decisions --db $main --work-db $work --rules single_candidate_high_score
   & $python grant_ai_assist_v1.py candidate-rule-decisions --db $main --work-db $work --rules exact_name_state_only
   & $python grant_ai_assist_v1.py candidate-rule-decisions --db $main --work-db $work --rules large_safe_remaining
   & $python grant_ai_assist_v1.py candidate-rule-decisions --db $main --work-db $work --rules address_name_remaining --addr-name-min-name-score 0.70 --high-address-geo-min-name-score 0.70
   & $python grant_ai_assist_v1.py candidate-rule-decisions --db $main --work-db $work --rules exact_name_no_geo_distinctive
   & $python grant_ai_assist_v1.py apply-decisions --db $main --work-db $work --full-refresh
   ```

5. **Cut over the validated main/work pair, then build the risk network.** Update
   both `IRS_DB_PATH` and `IRS_GRANT_WORK_DB_PATH` to the matching versioned
   files, restart every long-lived process, smoke-test local dashboard results,
   and only then run the full network commands below. Updating only the main
   path would fall back to the generic sibling `grant_matching_work.db`. Keep
   writers stopped through the network snapshot so the source WAL cannot grow
   for hours.

## Full risk-network build

The accepted production build completed on `C:` and atomically published
`db\risk_network.db` from the repaired versioned source. Its final size is
69.60 GiB. The following paths and capacity preflight are retained for a future
full rebuild; never launch a second builder while one is active:

```powershell
$networkSource = (Resolve-Path 'db\irs990-repaired-20260815-003434.db').Path
$networkRoot = (Resolve-Path 'db').Path
$networkSidecar = Join-Path $networkRoot 'risk_network.db'
$networkScratch = Join-Path $networkRoot '_risk_network_tmp'
New-Item -ItemType Directory -Force -Path $networkScratch | Out-Null
$networkDrive = Get-PSDrive -Name C
$networkDrive | Select-Object Name,Root,@{Name='FreeGiB';Expression={[math]::Round($_.Free / 1GB, 2)}}
if ($networkDrive.Free -lt 180GB) {
  throw 'C: has less than the conservative 180 GiB free-space floor.'
}
```

The preliminary plan estimated a **25.9-56.1 GiB** finished sidecar, but the
accepted build finished at **69.60 GiB**. Use the observed size for future
planning and budget a conservative **two to three times final size** for the
`.building-*` database, old sidecar during replacement, rollback/index work,
and SQLite sort scratch.
Retain a conservative **180 GiB free-space floor** and recheck current `C:`
capacity rather than relying on an older snapshot. Keep process-local
`TEMP`/`TMP` on `$networkScratch` while the builder runs so index-sort scratch
also remains on `C:`. Treat it as a multi-hour/overnight maintenance job and
monitor free space.

Stop every writer, checkpoint the source WAL, inspect the plan, and only then
run the confirmed full build. The plan requires the enhanced grant view and all
source `filing_id` indexes; rebuild start additionally proves unique grant keys,
exact `(grant_id, filing_id)` ownership across the raw/resolver/enhanced layers,
artifact-backed enhanced provenance, and an independent nonzero selected-filing
count. Selector syntax, missing filing IDs, reversed year ranges, expression or
non-BINARY lookup indexes, and nullable pseudo-keys fail closed. Atomic
publication also refuses a
populated WAL or rollback journal beside an old destination, because those old
pages could otherwise be replayed over the validated replacement. See
[Precomputed risk-network sidecar](risk-network.md) for the complete path, WAL,
stale-temp, provenance-scoring, validation, and cutover checks.

```powershell
$networkPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
& $networkPython -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); r=c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(); print(r); c.close(); assert r[0] == 0 and r[1] == r[2], r" $networkSource

$networkPreviousTemp = $env:TEMP
$networkPreviousTmp = $env:TMP
try {
  $env:TEMP = $networkScratch
  $env:TMP = $networkScratch
  & $networkPython build_risk_network.py plan --db $networkSource --sidecar $networkSidecar --full
  if ($LASTEXITCODE -ne 0) { throw 'Risk-network plan failed.' }
  & $networkPython build_risk_network.py rebuild --db $networkSource --sidecar $networkSidecar --full --yes
  if ($LASTEXITCODE -ne 0) { throw 'Risk-network rebuild failed.' }
}
finally {
  $env:TEMP = $networkPreviousTemp
  $env:TMP = $networkPreviousTmp
}
```

The accepted build passed the local `available()` smoke check and dashboard
acceptance testing. After any future rebuild, repeat both checks. Any later
replacement, relocation, or in-place size/mtime change to the main database
intentionally marks the sidecar stale until it is rebuilt.
