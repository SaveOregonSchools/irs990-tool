# Enhanced Grant Matching Guide

This guide covers the optional enhanced grant-recipient matching workflow.

The goal is to match raw IRS grant rows to recipient EINs as accurately as possible while keeping decisions explainable, reviewable, and reversible.

Back up the database before running real write steps.

## Components

| File | Purpose |
|---|---|
| `resolve_grant_recipients.py` | Deterministic first-pass grant-recipient matching. |
| `grant_ai_assist_v1.py` | Identity building, recipient signatures, candidate generation, rule decisions, optional Ollama adjudication, imports, and applied views. |
| `grant_ai_batch_worker.py` | Linux/Ollama worker for externally adjudicating exported packet batches. |
| `refresh_data_stats.py` | Refreshes cached web-app database statistics after grant matching changes. |
| `batch_enhanced_grant_matches.bat` | Windows launcher for the full enhanced matching rebuild workflow. |
| `batch_enhanced_grant_matches.ps1` | PowerShell implementation used by the launcher. |

## Expected Layout

Typical project folder:

```text
C:\Projects\irs990-tool
```

Typical database:

```text
C:\Projects\irs990-tool\db\irs990.db
```

Expected EO BMF files for the AI-assist identity layer:

```text
eo-bmf\eo1.csv
eo-bmf\eo2.csv
eo-bmf\eo3.csv
eo-bmf\eo4.csv
```

Useful environment variables:

```text
IRS_DB_PATH
IRS_PROJECT_DIR
OLLAMA_ENDPOINTS
OLLAMA_MODEL
OLLAMA_NUM_CTX
OLLAMA_NUM_PREDICT
OLLAMA_TIMEOUT
```

## Database Objects

The deterministic resolver creates or refreshes:

```text
grant_recipient_resolved
```

The AI-assist workflow stores bulky working data in the grant work sidecar DB
(`IRS_GRANT_WORK_DB_PATH`, or `db\grant_matching_work.db` by default):

```text
org_identity
org_identity_token
org_identity_fts
grant_recipient_signature
grant_recipient_signature_grant
grant_recipient_ai_candidate
```

The final accepted/enhanced matching data stays in the main application DB:

```text
grant_recipient_ai_decision
grant_recipient_ai_applied
grant_recipient_resolved_plus_ai_v1
```

The web-app statistics refresh creates or replaces cached rows in:

```text
app_data_stats
app_data_stats_meta
```

The final enhanced matching view is:

```text
grant_recipient_resolved_plus_ai_v1
```

Use these fields for enhanced grants-received analysis:

```text
final_resolved_ein
final_resolved_org_name
final_match_source
final_confidence
```

## Recommended Full Workflow

For the standard post-XML-load rebuild, run the launcher from the repo root:

```powershell
.\batch_enhanced_grant_matches.bat
```

For unattended runs:

```powershell
.\batch_enhanced_grant_matches.ps1 -Yes
```

The PowerShell workflow:

1. Confirms the database path.
2. Runs deterministic grant-recipient resolution.
3. Verifies EO BMF files.
4. Rebuilds `org_identity`.
5. Rebuilds recipient signatures.
6. Generates fast candidates.
7. Generates balanced candidates for signatures with `no_candidates`.
8. Runs reported-EIN triage.
9. Parks nonadjudicable/list-style and blank-recipient signatures.
10. Applies deterministic candidate-rule decisions.
11. Rebuilds the applied/final enhanced matching layer.
12. Writes `exports\grant_match_stats_after_enhanced_grants.csv`.
13. Refreshes the web app's cached Database Statistics page data.
14. Prints the remaining AI/human adjudication queue count.
15. Runs a SQLite WAL checkpoint unless `-SkipCheckpoint` is passed.

Common PowerShell options:

```powershell
.\batch_enhanced_grant_matches.ps1 `
  -DbPath C:\Projects\irs990-tool\db\irs990.db `
  -WorkDbPath C:\Projects\irs990-tool\db\grant_matching_work.db `
  -ProjectDir C:\Projects\irs990-tool `
  -Yes
```

## Manual Workflow

### Step 0: Back Up The Database

```powershell
sqlite3 db\irs990.db ".backup db\irs990_before_grant_ai.db"
```

If you are migrating an existing main DB that still contains the enhanced
matching working tables, move them to the sidecar before rebuilding:

```powershell
py migrate_grant_work_sidecar.py --db db\irs990.db --drop-main --yes
```

The migration verifies sidecar row counts before dropping working tables from
the main DB. It does not move `grant_recipient_ai_decision`,
`grant_recipient_ai_applied`, `grant_recipient_resolved`, or the final enhanced
view because the Flask app and query modules read those from the main DB.

### Step 1: Run Deterministic First-Pass Resolver

```powershell
py resolve_grant_recipients.py --db db\irs990.db --full-refresh --batch-size 100000
```

Dry-run sample:

```powershell
py resolve_grant_recipients.py --db db\irs990.db --dry-run --csv-out exports\grant_recipient_resolved_dry_run.csv --limit 100000
```

Review fields such as:

```text
match_status
match_method
confidence
warning_flags
resolved_ein
candidate_count
```

### Step 2: Verify EO BMF Files

```powershell
py grant_ai_assist_v1.py verify-bmf
```

With a custom BMF folder:

```powershell
py grant_ai_assist_v1.py verify-bmf --bmf-dir C:\Some\Path\eo-bmf
```

### Step 3: Build Organization Identity Tables

```powershell
py grant_ai_assist_v1.py build-identity --full-refresh
```

Identity sources usually include `returns.org_name`, `returns.dba_name`, and EO BMF names/sort names.

### Step 4: Build Recipient Signatures

```powershell
py grant_ai_assist_v1.py build-signatures --full-refresh
```

Signatures collapse many raw grant rows into unique review units based on recipient EIN/name/address/city/state/ZIP/country.

### Step 5: Generate Candidates

Start with fast mode:

```powershell
py grant_ai_assist_v1.py generate-candidates --full-refresh --candidate-mode fast
```

Then run balanced mode for signatures without candidates:

```powershell
py grant_ai_assist_v1.py generate-candidates --candidate-mode balanced --queue-status no_candidates
```

Use broad mode only for targeted batches:

```powershell
py grant_ai_assist_v1.py generate-candidates --candidate-mode broad --queue-status no_candidates --min-total-amount 10000 --limit 10000
```

Candidate modes:

```text
fast      reported EIN + exact name/address/location lookups
balanced  fast + geo-constrained token fallback
broad     balanced + FTS fallback
```

## Triage Before Using Ollama

Use deterministic decisions before spending model time.

### Reported-EIN Triage

Dry run:

```powershell
py grant_ai_assist_v1.py reported-ein-triage --dry-run --include-skips-in-dry-run --limit 10000 --csv-out exports\reported_ein_triage_sample.csv
```

Run for real:

```powershell
py grant_ai_assist_v1.py reported-ein-triage --placeholder-action human_review
```

Purpose: keep valid filing-supplied recipient EINs unless strong evidence suggests they are wrong.
List-style/nonadjudicable reported-EIN rows are parked for human review so they
do not get auto-kept before the nonadjudicable-recipient triage pass.

### Nonadjudicable Recipient Triage

For rows like `See attachment`, `Various recipients`, `Multiple recipients`, or blank recipient names, park them rather than sending them to Ollama.

Dry run:

```powershell
py grant_ai_assist_v1.py nonadjudicable-recipient-triage --dry-run --action human_review --include-blank-recipient-name --csv-out exports\nonadjudicable_sample.csv --limit 10000
```

Run for real:

```powershell
py grant_ai_assist_v1.py nonadjudicable-recipient-triage --action human_review --include-blank-recipient-name
```

This writes decision rows. It does not delete grant records.

## Candidate-Rule Decisions

Candidate-rule decisions write deterministic `SELECT_CANDIDATE` decisions to:

```text
grant_recipient_ai_decision
```

Always dry-run new or changed rules first:

```powershell
py grant_ai_assist_v1.py candidate-rule-decisions --dry-run --rules single_candidate_high_score --limit 10000 --csv-out exports\single_candidate_sample.csv
```

Inspect fields such as:

```text
action
rule_bucket
validation_status
auto_accept
recipient_name
candidate_name
candidate_score
name_score
address_score
candidate_reason
first_pass_warning_flags
```

Common production sequence:

```powershell
py grant_ai_assist_v1.py candidate-rule-decisions --rules exact_name_zip,exact_name_city_state,exact_address_zip_good_name
py grant_ai_assist_v1.py candidate-rule-decisions --rules single_candidate_high_score
py grant_ai_assist_v1.py candidate-rule-decisions --rules exact_name_state_only
py grant_ai_assist_v1.py candidate-rule-decisions --rules large_safe_remaining
py grant_ai_assist_v1.py candidate-rule-decisions --rules address_name_remaining --addr-name-min-name-score 0.70 --high-address-geo-min-name-score 0.70
py grant_ai_assist_v1.py candidate-rule-decisions --rules exact_name_no_geo_distinctive
```

Then rebuild the applied layer:

```powershell
py grant_ai_assist_v1.py apply-decisions --full-refresh
```

Avoid `--regenerate` unless deliberately replacing existing decisions.

## Direct Ollama Adjudication

Use Ollama after deterministic and rule passes have reduced the queue.

Test the endpoint:

```powershell
py grant_ai_assist_v1.py test-ollama --model gemma4:12b --ollama-url http://localhost:11434/api/chat --format-mode schema --debug-raw-out exports\ollama_raw_debug.txt
```

Dry-run a small batch:

```powershell
py grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://localhost:11434/api/chat --limit 25 --dry-run --csv-out exports\ai_decisions_25.csv --progress-every 1 --format-mode schema --max-call-failures 3 --num-predict 700
```

Run a real batch:

```powershell
py grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://localhost:11434/api/chat --limit 1000 --progress-every 25 --format-mode schema --max-call-failures 3 --num-predict 700
```

Then apply:

```powershell
py grant_ai_assist_v1.py apply-decisions --full-refresh
```

## External Batch AI Adjudication

For larger runs, export adjudication packets on Windows, process them with `grant_ai_batch_worker.py` on a Linux/Ollama server, then import the decision files back into the Windows database workflow.

### Export Packets

Single packet file:

```powershell
py grant_ai_assist_v1.py export-adjudication-packets --limit 500 --min-total-amount 10000 --out exports\adjudication_packets_500.jsonl --summary-csv exports\adjudication_packets_500_summary.csv
```

Batch packet directory:

```powershell
py grant_ai_assist_v1.py export-adjudication-batches --out-dir exports\ai_packets --batch-size 10000 --summary-csv exports\ai_packets_summary.csv
```

### Recommended Linux Worker Command

Use 4 workers as a good default based on prior local benchmarking.

```bash
python3 grant_ai_batch_worker.py \
  --input-dir /data/irs990_ai/packets_100k \
  --output-dir /data/irs990_ai/decisions_100k \
  --model gemma4:12b \
  --ollama-url http://127.0.0.1:11434/api/chat \
  --parallel-workers 4 \
  --error-retries 2 \
  --candidate-id-retries 1 \
  --retry-backoff-seconds 2 \
  --progress-every 25
```

The default worker behavior includes:

```text
--error-retries 2
--candidate-id-retries 1
--retry-backoff-seconds 2
--retry-backoff-multiplier 1.5
--think disabled
--format-mode schema
--max-explanation-words 35
```

The worker writes shared run settings once to `worker_run_manifest.json` in the
decision output directory. Individual `decisions_*.jsonl` rows keep only
per-record details such as `processed_at`, retry attempt, and candidate-id retry
count.

### Test A Single Packet File

```bash
python3 grant_ai_batch_worker.py \
  --input-file /data/irs990_ai/packets_100k/adjudication_packets_000001.jsonl \
  --output-dir /data/irs990_ai/decisions_test \
  --model gemma4:12b \
  --ollama-url http://127.0.0.1:11434/api/chat \
  --parallel-workers 4 \
  --limit 25 \
  --overwrite \
  --progress-every 1
```

### Retry And Resume Behavior

`--error-retries` retries transient failures such as Ollama API errors, empty responses, invalid JSON, and malformed structured output.

`--candidate-id-retries` retries when the model appears to select a candidate but omits or garbles the required `candidate_id`.

The worker is resumable by default. If a decision file already contains a `signature_hash`, that signature is skipped on rerun.

Use `--overwrite` only when intentionally deleting and recreating the decision/error files.

Rows that still fail after all retries are written to `errors_*.jsonl`. By default, failed calls are not written as decisions, so they can be retried later. Usually leave `--write-failures-as-human-review` off.

### Optional Custom Ollama Model

If using a Modelfile that embeds the system prompt:

```bash
ollama create grant-ai-adjudicator:gemma4-12b -f Modelfile.grant-ai-adjudicator-v1_27
```

Then run:

```bash
python3 grant_ai_batch_worker.py \
  --input-dir /data/irs990_ai/packets_100k \
  --output-dir /data/irs990_ai/decisions_100k \
  --model grant-ai-adjudicator:gemma4-12b \
  --ollama-url http://127.0.0.1:11434/api/chat \
  --parallel-workers 4 \
  --omit-system-message
```

This may reduce repeated prompt tokens slightly, but each request still needs the grant-recipient packet and candidate list.

### Import Decisions

After copying decisions back to Windows, always dry-run import first:

```powershell
py grant_ai_assist_v1.py import-adjudication-decision-dir `
  --in-dir imports\ai_decisions_100k `
  --glob "decisions_*.jsonl" `
  --dry-run `
  --audit-dir imports\ai_decisions_100k_audit
```

If the audit looks good:

```powershell
py grant_ai_assist_v1.py import-adjudication-decision-dir `
  --in-dir imports\ai_decisions_100k `
  --glob "decisions_*.jsonl" `
  --audit-dir imports\ai_decisions_100k_audit_real
```

When `worker_run_manifest.json` is present beside the decision files, import
uses it to store the actual worker model and model options. Pass
`--source-model` only when you intentionally want to override that label.

Then rebuild the applied/final layer:

```powershell
py grant_ai_assist_v1.py apply-decisions --full-refresh
```

In import audit CSVs, monitor:

```text
validation_status
validation_error
auto_accept
decision
selected_ein
selected_name
reason_codes_json
explanation
```

The safest production rule remains: import all valid decisions if desired, but only apply rows where the importer marks `auto_accept = 1`.

## Stats And Queue Counts

Stats command:

```powershell
py grant_ai_assist_v1.py stats --csv-out exports\grant_match_stats.csv
```

Refresh the web app's cached Database Statistics page:

```powershell
py refresh_data_stats.py --db db\irs990.db
```

The standard `batch_enhanced_grant_matches.ps1` workflow runs this refresh automatically after writing the grant-match stats CSV.

Exact count of signatures still needing decisions:

```sql
ATTACH DATABASE 'db/grant_matching_work.db' AS grant_work;

SELECT
  COUNT(*) AS signatures_left_for_ai_review,
  COALESCE(SUM(s.grant_count),0) AS grants_represented,
  ROUND(COALESCE(SUM(s.total_amount),0),2) AS total_amount
FROM grant_work.grant_recipient_signature s
WHERE EXISTS (
  SELECT 1
  FROM grant_work.grant_recipient_ai_candidate c
  WHERE c.signature_hash = s.signature_hash
)
AND NOT EXISTS (
  SELECT 1
  FROM grant_recipient_ai_decision d
  WHERE d.signature_hash = s.signature_hash
);
```

## Recovery And Cleanup

After heavy writes, checkpoint the WAL:

```powershell
sqlite3 db\irs990.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

If a dry run is interrupted, usually nothing was written except the CSV.

If a real decision pass is interrupted, previously committed decision rows remain. Re-running without `--regenerate` generally skips signatures that already have decisions.

Use the relevant `--full-refresh` stage when a staging table is partial:

```powershell
py grant_ai_assist_v1.py build-identity --full-refresh
py grant_ai_assist_v1.py build-signatures --full-refresh
py grant_ai_assist_v1.py generate-candidates --full-refresh --candidate-mode fast
```

`apply-decisions --full-refresh` is safe and comparatively lightweight. It rebuilds the applied/final layer from existing decisions.
