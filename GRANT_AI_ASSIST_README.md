# IRS 990 grant-recipient resolution and AI assist

This document explains the optional grant-recipient matching workflow.

The purpose is to match raw grant rows to the correct recipient EIN as accurately as possible while keeping decisions explainable, reviewable, and reversible.

The workflow has two layers:

1. `resolve_grant_recipients.py` — deterministic first-pass matching.
2. `grant_ai_assist_v1.py` — identity building, recipient signatures, candidate generation, deterministic rule decisions, optional Ollama adjudication, external decision import, and final applied views.

Back up the database before running real write steps.

---

## Expected project layout

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

---

## Database objects

The deterministic resolver creates or refreshes:

```text
grant_recipient_resolved
```

The AI-assist workflow can create or refresh:

```text
org_identity
org_identity_token
org_identity_fts
grant_recipient_signature
grant_recipient_signature_grant
grant_recipient_ai_candidate
grant_recipient_ai_decision
grant_recipient_ai_applied
grant_recipient_resolved_plus_ai_v1
```

The final enhanced matching view is:

```text
grant_recipient_resolved_plus_ai_v1
```

Use these final fields for enhanced grants-received analysis:

```text
final_resolved_ein
final_resolved_org_name
final_match_source
final_confidence
```

---

## Recommended full workflow

### Step 0 — Back up the database

```powershell
sqlite3 db\irs990.db ".backup db\irs990_before_grant_ai.db"
```

### Step 1 — Run deterministic first-pass resolver

```powershell
py resolve_grant_recipients.py --db db\irs990.db --full-refresh --batch-size 100000
```

Dry run sample:

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

### Step 2 — Verify EO BMF files

```powershell
py grant_ai_assist_v1.py verify-bmf
```

With a custom BMF folder:

```powershell
py grant_ai_assist_v1.py verify-bmf --bmf-dir C:\Some\Path\eo-bmf
```

### Step 3 — Build organization identity tables

```powershell
py grant_ai_assist_v1.py build-identity --full-refresh
```

Identity sources usually include `returns.org_name`, `returns.dba_name`, and EO BMF names/sort names.

### Step 4 — Build recipient signatures

```powershell
py grant_ai_assist_v1.py build-signatures --full-refresh
```

Signatures collapse many raw grant rows into unique review units based on recipient EIN/name/address/city/state/ZIP/country.

### Step 5 — Generate candidates

Start with fast mode:

```powershell
py grant_ai_assist_v1.py generate-candidates --full-refresh --candidate-mode fast
```

Then optionally run balanced mode for signatures without candidates:

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

---

## Triage before using Ollama

Reduce the queue with deterministic decisions before spending model time.

### Reported-EIN triage

Dry run:

```powershell
py grant_ai_assist_v1.py reported-ein-triage --dry-run --include-skips-in-dry-run --limit 10000 --csv-out exports\reported_ein_triage_sample.csv
```

Run for real:

```powershell
py grant_ai_assist_v1.py reported-ein-triage
```

Purpose: keep valid filing-supplied recipient EINs unless strong evidence suggests they are wrong.

### Nonadjudicable-recipient triage

For rows like `See attachment`, `Various recipients`, or `Multiple recipients`, park them rather than sending them to Ollama.

Dry run:

```powershell
py grant_ai_assist_v1.py nonadjudicable-recipient-triage --dry-run --action human_review --csv-out exports\nonadjudicable_sample.csv --limit 10000
```

Run for real:

```powershell
py grant_ai_assist_v1.py nonadjudicable-recipient-triage --action human_review
```

This writes decision rows. It does not delete grant records.

---

## Candidate-rule decisions

Candidate-rule decisions write deterministic `SELECT_CANDIDATE` decisions to:

```text
grant_recipient_ai_decision
```

Always dry-run new rules first:

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

Run a rule for real only after review:

```powershell
py grant_ai_assist_v1.py candidate-rule-decisions --rules large_safe_remaining
```

Then rebuild the applied layer:

```powershell
py grant_ai_assist_v1.py apply-decisions --full-refresh
```

Avoid `--regenerate` unless deliberately replacing existing decisions.

---

## Ollama adjudication

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

---

## External export/import adjudication

Export packets:

```powershell
py grant_ai_assist_v1.py export-adjudication-packets --limit 500 --min-total-amount 10000 --out exports\adjudication_packets_500.jsonl --summary-csv exports\adjudication_packets_500_summary.csv
```

Import decisions as a dry run:

```powershell
py grant_ai_assist_v1.py import-adjudication-decisions --in-file exports\chatgpt_decisions_500.jsonl --dry-run --audit-csv exports\chatgpt_import_audit.csv
```

Import for real:

```powershell
py grant_ai_assist_v1.py import-adjudication-decisions --in-file exports\chatgpt_decisions_500.jsonl --source-model external:chatgpt
```

Then apply:

```powershell
py grant_ai_assist_v1.py apply-decisions --full-refresh
```

---

## Stats and queue counts

Stats command:

```powershell
py grant_ai_assist_v1.py stats --csv-out exports\grant_match_stats.csv
```

If the final view is not ready yet:

```powershell
py grant_ai_assist_v1.py stats --skip-final-view --csv-out exports\grant_match_stats.csv
```

Exact count of signatures still needing decisions:

```sql
SELECT
  COUNT(*) AS signatures_left_for_ai_review,
  COALESCE(SUM(s.grant_count),0) AS grants_represented,
  ROUND(COALESCE(SUM(s.total_amount),0),2) AS total_amount
FROM grant_recipient_signature s
WHERE EXISTS (
  SELECT 1
  FROM grant_recipient_ai_candidate c
  WHERE c.signature_hash = s.signature_hash
)
AND NOT EXISTS (
  SELECT 1
  FROM grant_recipient_ai_decision d
  WHERE d.signature_hash = s.signature_hash
);
```

---

## Recovery and cleanup

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
