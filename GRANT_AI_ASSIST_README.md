# IRS 990 Grant Recipient Resolution + AI Assist README

This README documents the current grant-recipient resolution workflow using:

1. `resolve_grant_recipients_v2_1_fast.py` — deterministic first-pass resolver.
2. `grant_ai_assist_v1.py` — current AI/rule-assisted second-pass resolver. In this README, `grant_ai_assist_v1.py` means the **v1.25 distinctive exact-name/no-geo version** or a later compatible replacement saved under that drop-in filename.

The goal is to match as many grants as possible to the correct recipient EIN while keeping the process explainable, auditable, and reversible.

---

## 1. Current version summary

### Deterministic first pass

`resolve_grant_recipients_v2_1_fast.py` creates:

```text
grant_recipient_resolved
```

It reads raw `grants` rows and attempts deterministic resolution using:

```text
reported recipient EIN
normalized name + ZIP
normalized name + city/state
normalized name + address
unique address/ZIP evidence
address-narrowed name evidence
optional fuzzy matching if explicitly enabled
```

The fast version bulk-loads rows before creating secondary indexes.

### AI/rule-assisted second pass

`grant_ai_assist_v1.py` now includes:

```text
EO BMF identity import
recipient signature generation
candidate generation in fast/balanced/broad modes
reported-EIN triage
nonadjudicable-recipient triage
candidate-rule diagnostics and decisions
Ollama adjudication
external export/import adjudication
stats reporting
final applied view creation
```

### v1.25 addition

v1.25 adds the rule:

```text
distinctive_exact_name_no_geo
```

This handles a cautious subset of exact-name/no-geography cases where:

```text
candidate_count = 1
exact normalized recipient name = true
no ZIP/city/state/address match
recipient name is distinctive
recipient has a U.S. state
reported EIN is blank/invalid rather than usable
no contradiction flag
not a placeholder/list recipient
```

It intentionally excludes short acronyms, generic names, foreign/blank-state rows, and broad exact-name-only cases.

---

## 2. Expected project layout

Recommended project folder:

```text
C:\Projects\irs990-tool
```

Expected files/folders:

```text
C:\Projects\irs990-tool\grant_ai_assist_v1.py
C:\Projects\irs990-tool\resolve_grant_recipients_v2_1_fast.py
C:\Projects\irs990-tool\db\irs990.db
C:\Projects\irs990-tool\eo-bmf\eo1.csv
C:\Projects\irs990-tool\eo-bmf\eo2.csv
C:\Projects\irs990-tool\eo-bmf\eo3.csv
C:\Projects\irs990-tool\eo-bmf\eo4.csv
```

Default paths inside the script are:

```text
Project folder: C:\projects\irs990-tool
Database:       C:\projects\irs990-tool\db\irs990.db
Model:          gemma4:12b
Ollama URL:     http://localhost:11434/api/chat
```

You can override them with flags or environment variables:

```text
IRS_PROJECT_DIR
IRS_DB_PATH
OLLAMA_MODEL
OLLAMA_URL
```

Example:

```powershell
$env:IRS_PROJECT_DIR = "C:\Projects\irs990-tool"
$env:IRS_DB_PATH = "C:\Projects\irs990-tool\db\irs990.db"
$env:OLLAMA_URL = "http://192.168.7.221:11434/api/chat"
$env:OLLAMA_MODEL = "gemma4:12b"
```

---

## 3. Database objects created

### Created by the deterministic resolver

```text
grant_recipient_resolved
```

Important columns include:

```text
grant_id
filing_id
grantor_ein
grantor_name
tax_year
recipient_reported_ein
recipient_reported_name
recipient_city
recipient_state
recipient_zip
cash_amount
noncash_amount
total_amount
purpose
resolved_ein
resolved_org_name
match_status
match_method
confidence
name_score
address_score
warning_flags
candidate_count
```

### Created by the AI/rule-assisted script

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

Key final fields:

```text
final_resolved_ein
final_resolved_org_name
final_match_source
final_confidence
```

Use `final_resolved_ein` for enhanced grants-received queries.

---

## 4. Recommended full workflow from scratch

Open PowerShell or Command Prompt:

```powershell
cd C:\Projects\irs990-tool
```

### Step 0 — Back up the database

```powershell
sqlite3 C:\Projects\irs990-tool\db\irs990.db ".backup C:\Projects\irs990-tool\db\irs990_before_grant_ai.db"
```

### Step 1 — Run deterministic first-pass resolver

```powershell
python resolve_grant_recipients_v2_1_fast.py --db C:\Projects\irs990-tool\db\irs990.db --full-refresh --batch-size 100000
```

Dry run option:

```powershell
python resolve_grant_recipients_v2_1_fast.py --db C:\Projects\irs990-tool\db\irs990.db --dry-run --csv-out exports\grant_recipient_resolved_dry_run.csv --limit 100000
```

Check:

```text
match_status
match_method
confidence
warning_flags
resolved_ein blank vs nonblank
```

### Step 2 — Verify EO BMF files

```powershell
python grant_ai_assist_v1.py verify-bmf
```

If the EO BMF files are somewhere else:

```powershell
python grant_ai_assist_v1.py verify-bmf --bmf-dir C:\Some\Path\eo-bmf
```

### Step 3 — Build organization identity table

```powershell
python grant_ai_assist_v1.py build-identity --full-refresh
```

This builds:

```text
org_identity
org_identity_token
org_identity_fts
```

Identity sources include:

```text
returns.org_name
returns.dba_name
EO BMF NAME
EO BMF SORT_NAME
```

Usually leave `--include-bmf-ico` off. ICO can be a person, attorney, accountant, or mailing contact.

If the WAL file becomes large after this step:

```powershell
sqlite3 C:\Projects\irs990-tool\db\irs990.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### Step 4 — Build recipient signatures

```powershell
python grant_ai_assist_v1.py build-signatures --full-refresh
```

This collapses many raw grant rows into unique signatures based on:

```text
reported EIN
recipient name
street address
city
state
ZIP5
country
```

A single AI/rule decision can then apply to many grant rows.

### Step 5 — Generate candidates

Start with fast mode:

```powershell
python grant_ai_assist_v1.py generate-candidates --full-refresh --candidate-mode fast
```

Then optionally run balanced mode only for signatures without candidates:

```powershell
python grant_ai_assist_v1.py generate-candidates --candidate-mode balanced --queue-status no_candidates
```

Use broad mode only for targeted batches:

```powershell
python grant_ai_assist_v1.py generate-candidates --candidate-mode broad --queue-status no_candidates --min-total-amount 10000 --limit 10000
```

Candidate modes:

```text
fast      reported EIN + exact name/address/location lookups
balanced  fast + geo-constrained token fallback
broad     balanced + FTS fallback
```

---

## 5. Triage and deterministic rule workflow

The preferred approach is to reduce the AI queue with deterministic decisions before using Ollama.

### Reported-EIN triage

Run dry first:

```powershell
python grant_ai_assist_v1.py reported-ein-triage --dry-run --include-skips-in-dry-run --limit 10000 --csv-out exports\reported_ein_triage_sample.csv
```

Run for real:

```powershell
python grant_ai_assist_v1.py reported-ein-triage
```

Purpose:

```text
Keep valid filing-supplied recipient EINs unless there is strong evidence they are wrong.
Do not send normal reported-EIN cases to Ollama.
```

Important flags:

```text
--unverified-action keep|human_review
--unsafe-action human_review|ollama
--allow-contradictions
--regenerate
--include-skips-in-dry-run
```

### Nonadjudicable-recipient triage

For rows like `See attachment`, `Various recipients`, or `Multiple recipients`, do not use model time.

Dry run:

```powershell
python grant_ai_assist_v1.py nonadjudicable-recipient-triage --dry-run --action human_review --csv-out exports\nonadjudicable_sample.csv --limit 10000
```

Run for real:

```powershell
python grant_ai_assist_v1.py nonadjudicable-recipient-triage --action human_review
```

Include blank recipient names only if you decide they should be parked too:

```powershell
python grant_ai_assist_v1.py nonadjudicable-recipient-triage --action human_review --include-blank-recipient-name
```

This does **not** delete records. It writes a decision row so future AI/export steps skip those signatures.

---

## 6. Candidate-rule decisions

Candidate-rule decisions write deterministic `SELECT_CANDIDATE` decisions into:

```text
grant_recipient_ai_decision
```

They are applied to the final view only after:

```powershell
python grant_ai_assist_v1.py apply-decisions --full-refresh
```

### Always dry-run new rules first

Example:

```powershell
python grant_ai_assist_v1.py candidate-rule-decisions --dry-run --rules single_candidate_high_score --limit 10000 --csv-out exports\single_candidate_sample.csv
```

Inspect:

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

### Current rule aliases

#### `large_safe_remaining`

Expands to:

```text
same_ein_exact_name_geo
same_ein_high_name_geo
clear_top_exact_name_geo
clear_top_high_name_geo
```

Use:

```powershell
python grant_ai_assist_v1.py candidate-rule-decisions --rules large_safe_remaining
```

#### `address_name_remaining`

Expands to:

```text
same_ein_exact_address_zip_moderate_name
same_ein_exact_address_city_state_moderate_name
same_ein_high_address_geo_high_name
```

Default threshold is 0.75. A reviewed looser threshold can be run with:

```powershell
python grant_ai_assist_v1.py candidate-rule-decisions ^
  --rules address_name_remaining ^
  --addr-name-min-name-score 0.70 ^
  --high-address-geo-min-name-score 0.70
```

#### `exact_name_no_geo_distinctive`

v1.25 alias for:

```text
distinctive_exact_name_no_geo
```

Dry run:

```powershell
python grant_ai_assist_v1.py candidate-rule-decisions ^
  --dry-run ^
  --rules exact_name_no_geo_distinctive ^
  --csv-out exports\candidate_rule_distinctive_exact_name_no_geo_dryrun.csv
```

Run for real after review:

```powershell
python grant_ai_assist_v1.py candidate-rule-decisions --rules exact_name_no_geo_distinctive
```

Default guardrails:

```text
single candidate only
exact normalized name
no ZIP/city-state/state/address match
U.S. recipient state required
recipient name must be distinctive
minimum 3 non-stopword tokens
minimum normalized length 18
excludes short acronyms and generic labels
excludes usable reported-EIN cases
excludes contradictions by default
excludes nonadjudicable placeholders by default
```

Useful v1.25 flags:

```text
--exact-name-no-geo-min-score 55.0
--exact-name-no-geo-min-tokens 3
--exact-name-no-geo-min-length 18
--exact-name-no-geo-allow-non-us
--exact-name-no-geo-confidence 0.925
```

Do **not** use `--exact-name-no-geo-allow-non-us` broadly unless you have reviewed the foreign/blank-state exact-name cases. They can match foreign grantees to U.S. EIN-bearing affiliates.

### Individual candidate rules currently supported

```text
reported_ein_candidate
single_candidate_high_score
exact_address_zip_good_name
exact_name_zip
exact_name_city_state
exact_name_state_only
all_candidates_same_ein_high_score
all_candidates_same_ein_strong_evidence
same_ein_exact_name_geo
same_ein_high_name_geo
clear_top_exact_name_geo
clear_top_high_name_geo
exact_address_high_name_clear
same_ein_exact_address_zip_moderate_name
same_ein_exact_address_city_state_moderate_name
same_ein_high_address_geo_high_name
same_ein_zip_city_state_high_name
distinctive_exact_name_no_geo
clear_best_candidate
```

### Candidate-rule common flags

```text
--dry-run
--csv-out PATH
--include-skipped
--limit N
--state OR
--min-total-amount AMOUNT
--queue-status STATUS
--regenerate
--include-reported-ein
--include-contradictions
--include-nonadjudicable-placeholders
--auto-accept-threshold 0.92
```

Avoid `--regenerate` unless deliberately replacing existing decisions.

---

## 7. Apply decisions

After any real decision-writing step, rebuild the applied/final layer:

```powershell
python grant_ai_assist_v1.py apply-decisions --full-refresh
```

This rebuilds:

```text
grant_recipient_ai_applied
grant_recipient_resolved_plus_ai_v1
```

It does **not** rebuild:

```text
org_identity
grant_recipient_signature
grant_recipient_ai_candidate
grant_recipient_resolved
```

---

## 8. Queue counts and stats

### Exact count of signatures still needing AI/rule decisions

```powershell
sqlite3 C:\projects\irs990-tool\db\irs990.db "SELECT COUNT(*) AS signatures_left_for_ai_review, COALESCE(SUM(s.grant_count),0) AS grants_represented, ROUND(COALESCE(SUM(s.total_amount),0),2) AS total_amount FROM grant_recipient_signature s WHERE EXISTS (SELECT 1 FROM grant_recipient_ai_candidate c WHERE c.signature_hash = s.signature_hash) AND NOT EXISTS (SELECT 1 FROM grant_recipient_ai_decision d WHERE d.signature_hash = s.signature_hash);"
```

This means:

```text
has at least one candidate
and has no decision yet
```

### Stats command

```powershell
python grant_ai_assist_v1.py stats --csv-out exports\grant_match_stats.csv
```

While the final view is not ready:

```powershell
python grant_ai_assist_v1.py stats --skip-final-view --csv-out exports\grant_match_stats.csv
```

Useful sections:

```text
raw_grants
deterministic_resolver
org_identity
signatures
candidates
ai_decisions
applied_ai
final_view
```

---

## 9. Ollama adjudication

Use Ollama only after deterministic/rule passes have reduced the queue.

Test the endpoint:

```powershell
python grant_ai_assist_v1.py test-ollama --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --format-mode schema --debug-raw-out exports\ollama_raw_debug.txt
```

Dry-run a small batch:

```powershell
python grant_ai_assist_v1.py adjudicate ^
  --model gemma4:12b ^
  --ollama-url http://192.168.7.221:11434/api/chat ^
  --limit 25 ^
  --dry-run ^
  --csv-out exports\ai_decisions_25.csv ^
  --progress-every 1 ^
  --format-mode schema ^
  --max-call-failures 3 ^
  --num-predict 700
```

Run a real batch:

```powershell
python grant_ai_assist_v1.py adjudicate ^
  --model gemma4:12b ^
  --ollama-url http://192.168.7.221:11434/api/chat ^
  --limit 1000 ^
  --progress-every 25 ^
  --format-mode schema ^
  --max-call-failures 3 ^
  --num-predict 700
```

Then:

```powershell
python grant_ai_assist_v1.py apply-decisions --full-refresh
```

Important adjudication flags:

```text
--model MODEL
--ollama-url URL
--format-mode schema|json|none
--num-predict N
--num-ctx N
--timeout SECONDS
--retries N
--max-call-failures N
--dry-run
--csv-out PATH
--debug-raw-out PATH
--think        # off by default; normally leave off
--no-reported-ein-triage
--no-nonadjudicable-recipient-triage
```

Gemma is slower but more conservative. Qwen was faster and more JSON-stable in testing, but was too decisive and less explainable for broad auto-use.

---

## 10. External export/import adjudication

Export packets:

```powershell
python grant_ai_assist_v1.py export-adjudication-packets ^
  --limit 500 ^
  --min-total-amount 10000 ^
  --out exports\adjudication_packets_500.jsonl ^
  --summary-csv exports\adjudication_packets_500_summary.csv
```

Import decisions dry run:

```powershell
python grant_ai_assist_v1.py import-adjudication-decisions ^
  --in-file exports\chatgpt_decisions_500.jsonl ^
  --dry-run ^
  --audit-csv exports\chatgpt_import_audit.csv
```

Import for real:

```powershell
python grant_ai_assist_v1.py import-adjudication-decisions ^
  --in-file exports\chatgpt_decisions_500.jsonl ^
  --source-model external:chatgpt
```

Then:

```powershell
python grant_ai_assist_v1.py apply-decisions --full-refresh
```

Import validation still checks that selected candidate IDs exist and that guardrails pass.

---

## 11. Query-module integration

The enhanced grants-received query module adds a checkbox:

```text
Use enhanced grant-recipient matching
```

Unchecked:

```text
uses the old grants_compat_v1.recipient_ein path
```

Checked:

```text
uses final_resolved_ein from the enhanced matching layer
```

For the Learning Policy Institute test, enhanced mode included all original rows and added many correctly matched rows.

After applying new decisions, always run:

```powershell
python grant_ai_assist_v1.py apply-decisions --full-refresh
```

before expecting enhanced query modules to reflect them.

---

## 12. Recovery and cleanup

### Checkpoint WAL after heavy writes

```powershell
sqlite3 C:\Projects\irs990-tool\db\irs990.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### If a dry run is interrupted

Usually nothing was written except the CSV.

### If a real decision pass is interrupted

Previously committed decision rows remain. Re-running without `--regenerate` generally skips signatures that already have decisions.

### If identity/signature/candidate tables are partial

Use the relevant `--full-refresh` command for that stage:

```powershell
python grant_ai_assist_v1.py build-identity --full-refresh
python grant_ai_assist_v1.py build-signatures --full-refresh
python grant_ai_assist_v1.py generate-candidates --full-refresh --candidate-mode fast
```

Do not use `--full-refresh` on early stages unless you intend to rebuild them. `apply-decisions --full-refresh` is safe and lightweight compared with rebuilding identity/signatures/candidates.

---

## 13. Current recommended next-stage sequence

After applying v1.24 address/name at `0.70`, test the v1.25 exact-name/no-geo distinctive rule:

```powershell
python grant_ai_assist_v1.py candidate-rule-decisions ^
  --dry-run ^
  --rules exact_name_no_geo_distinctive ^
  --csv-out exports\candidate_rule_distinctive_exact_name_no_geo_dryrun.csv
```

If reviewed and clean:

```powershell
python grant_ai_assist_v1.py candidate-rule-decisions --rules exact_name_no_geo_distinctive
python grant_ai_assist_v1.py apply-decisions --full-refresh
```

Then recount the queue. If the remaining queue is still too large, export another bucket summary before using Gemma broadly.
