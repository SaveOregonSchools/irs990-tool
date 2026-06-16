# IRS 990 Grant Recipient Resolution + AI Assist — README

This README documents the recommended workflow for resolving grant recipients to EINs using:

1. `resolve_grant_recipients_v2_1_fast.py` — deterministic first-pass resolver.
2. `grant_ai_assist_v1.py` — AI-assisted second-pass resolver. In this document, `grant_ai_assist_v1.py` refers to the current v1.10 shortcut-audit version, even if the working file name on disk is different.

The goal is to match as many grants as possible to the correct recipient EIN while keeping the process explainable, auditable, and conservative.

---

## 1. Expected project layout

Recommended project folder:

```bat
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

The AI-assist script defaults to:

```text
Project folder: C:\projects\irs990-tool
Database:       C:\projects\irs990-tool\db\irs990.db
Model:          gemma4:12b
Ollama URL:     http://localhost:11434/api/chat
```

You can override defaults with command-line flags or environment variables:

```text
IRS_PROJECT_DIR
IRS_DB_PATH
OLLAMA_MODEL
OLLAMA_URL
```

Example:

```bat
set IRS_PROJECT_DIR=C:\Projects\irs990-tool
set IRS_DB_PATH=C:\Projects\irs990-tool\db\irs990.db
set OLLAMA_URL=http://192.168.7.221:11434/api/chat
set OLLAMA_MODEL=gemma4:12b
```

---

## 2. Database objects created

### Deterministic resolver creates

```text
grant_recipient_resolved
```

This is the deterministic first-pass table. It keeps the raw reported grant-recipient data and adds a deterministic `resolved_ein`, `resolved_org_name`, `match_status`, `match_method`, confidence, and warning flags.

### AI-assist script creates

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

The final view to use after the AI process is:

```text
grant_recipient_resolved_plus_ai_v1
```

The key final fields are:

```text
final_resolved_ein
final_resolved_org_name
final_match_source
final_confidence
```

---

## 3. Recommended full workflow

Open Command Prompt:

```bat
cd /d C:\Projects\irs990-tool
```

### Step 0 — Back up the database

Recommended before major rebuilds:

```bat
sqlite3 C:\Projects\irs990-tool\db\irs990.db ".backup C:\Projects\irs990-tool\db\irs990_before_grant_ai.db"
```

---

### Step 1 — Run deterministic first-pass resolver

This must run before the AI-assist script can build signatures.

Recommended full run:

```bat
python resolve_grant_recipients_v2_1_fast.py --db C:\Projects\irs990-tool\db\irs990.db --full-refresh --batch-size 100000
```

Why this comes first:

```text
The AI-assist pipeline uses grant_recipient_resolved to find unresolved, low-confidence, ambiguous, warning-flagged, or otherwise hard cases.
```

Optional dry run:

```bat
python resolve_grant_recipients_v2_1_fast.py --db C:\Projects\irs990-tool\db\irs990.db --dry-run --csv-out grant_recipient_resolved_dry_run.csv --limit 100000
```

What to check:

```text
match_status
match_method
confidence
warning_flags
resolved_ein blank vs nonblank
```

---

### Step 2 — Verify EO BMF files

```bat
python grant_ai_assist_v1.py verify-bmf
```

Why this comes next:

```text
The AI-assist identity table can include IRS EO BMF names and addresses. This step confirms eo1.csv through eo4.csv are available before building org_identity.
```

If your BMF files are somewhere else:

```bat
python grant_ai_assist_v1.py verify-bmf --bmf-dir C:\Some\Other\Path\eo-bmf
```

---

### Step 3 — Build organization identity table

Recommended full refresh:

```bat
python grant_ai_assist_v1.py build-identity --full-refresh
```

What it does:

```text
Builds org_identity from:
- returns.org_name
- returns.dba_name
- EO BMF NAME
- EO BMF SORT_NAME

Also builds:
- org_identity_token for token search
- org_identity_fts for broad full-text candidate search
```

Why this comes before signatures/candidates:

```text
Candidate generation needs a searchable universe of possible organizations/EINs.
```

Do not normally use `--include-bmf-ico` at first. ICO can be a person, accountant, attorney, or mailing contact, so it is better treated cautiously.

After this step, if the WAL file is large and no other script is running:

```bat
sqlite3 C:\Projects\irs990-tool\db\irs990.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

---

### Step 4 — Build recipient signatures

Recommended full refresh:

```bat
python grant_ai_assist_v1.py build-signatures --full-refresh
```

What it does:

```text
Collapses many raw grant rows into unique recipient signatures.

A signature is based on things like:
- reported recipient EIN
- normalized recipient name
- normalized street address
- city
- state
- ZIP5
- country
```

Why this is important:

```text
AI adjudication works on signatures, not individual grant rows. If 500 grants have the same recipient name/address pattern, the model only needs to adjudicate one signature.
```

Useful targeted examples:

```bat
python grant_ai_assist_v1.py build-signatures --full-refresh --state OR
python grant_ai_assist_v1.py build-signatures --full-refresh --min-total-amount 10000
python grant_ai_assist_v1.py build-signatures --full-refresh --limit 100000
```

---

### Step 5 — Generate candidate EINs, fast pass

Recommended first candidate pass:

```bat
python grant_ai_assist_v1.py generate-candidates --full-refresh --candidate-mode fast
```

What fast mode does:

```text
Uses high-signal indexed lookups only:
- reported EIN
- exact normalized name
- name + ZIP
- name + city/state
- name + street/address
- street + ZIP
- street + city/state
```

Why fast mode first:

```text
It finds the easiest and safest candidate sets at scale without making every signature pay for token or FTS searches.
```

Note about status updates:

```text
By default, candidate_count and ai_queue_status are bulk-updated at the end for speed. During a long run, progress messages are more accurate than stats output.
```

---

### Step 6 — Generate candidates, balanced pass for leftovers

After fast mode finishes, run balanced mode only on signatures that still have no candidates:

```bat
python grant_ai_assist_v1.py generate-candidates --candidate-mode balanced --queue-status no_candidates
```

What balanced mode adds:

```text
Geo-constrained token fallback using org_identity_token.
```

Recommended targeted examples:

```bat
python grant_ai_assist_v1.py generate-candidates --candidate-mode balanced --queue-status no_candidates --min-total-amount 10000 --limit 100000
python grant_ai_assist_v1.py generate-candidates --candidate-mode balanced --queue-status no_candidates --state OR
```

---

### Step 7 — Generate candidates, broad pass only for targeted hard leftovers

Broad mode adds FTS fallback and can be expensive. Do not run it across millions of leftovers unless you really mean to.

Recommended targeted broad mode:

```bat
python grant_ai_assist_v1.py generate-candidates --candidate-mode broad --queue-status no_candidates --min-total-amount 10000 --limit 10000
```

Or Oregon-only:

```bat
python grant_ai_assist_v1.py generate-candidates --candidate-mode broad --queue-status no_candidates --state OR --min-total-amount 5000
```

---

### Step 8 — Optional name backfill

Dry run:

```bat
python grant_ai_assist_v1.py backfill-ein-names --dry-run
```

Real run:

```bat
python grant_ai_assist_v1.py backfill-ein-names
```

What it does:

```text
If an EIN is already known but the org name field is blank, fill the name from org_identity.
```

This is lightweight and can be run incrementally. It does not rerun matching.

---

### Step 9 — Reported-EIN shortcuts before AI adjudication

This step creates auto-accepted decisions without calling Ollama where:

```text
- a reported recipient EIN exists,
- org_identity has a name for that EIN,
- the deterministic resolver did not flag a contradiction,
- the recipient name does not clearly disagree with the reported EIN identity.
```

Dry run first:

```bat
python grant_ai_assist_v1.py reported-ein-shortcuts --limit 1000 --dry-run --csv-out reported_ein_shortcuts_sample.csv
```

What to check in the CSV:

```text
selected_ein
selected_name
reported_ein
recipient_name
identity_source
identity_name_score
first_pass_warning_flags
shortcut_reason
reason_codes_json
validation_status should be ok
auto_accept should be 1
```

Real full run:

```bat
python grant_ai_assist_v1.py reported-ein-shortcuts
```

Why this comes before AI:

```text
It avoids sending obvious reported-EIN cases to Ollama. It is much faster and safer than model adjudication for those rows.
```

---

### Step 10 — Test Ollama before adjudication

Use the actual AI server URL:

```bat
python grant_ai_assist_v1.py test-ollama --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --format-mode schema --debug-raw-out ollama_raw_debug.txt
```

Expected result:

```text
Ollama call succeeded
message.content is non-empty JSON
done_reason = stop
```

The script defaults to `think=False`. Do not use `--think` unless debugging; thinking mode previously caused empty `message.content` responses when the model spent the output budget on hidden reasoning.

---

### Step 11 — Dry-run AI adjudication

Start small:

```bat
python grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --limit 25 --dry-run --csv-out ai_decisions_25.csv --progress-every 1 --format-mode schema --max-call-failures 1 --num-predict 700
```

Then try a larger dry run:

```bat
python grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --limit 250 --dry-run --csv-out ai_decisions_250.csv --progress-every 10 --format-mode schema --max-call-failures 3 --num-predict 700
```

What to check in the dry-run CSV:

```text
validation_status
validation_error
decision
selected_candidate_id
selected_ein
selected_name
auto_accept
needs_human_review
reason_codes_json
explanation
```

Good signs:

```text
validation_status = ok for most rows
NO_MATCH for placeholder recipients such as Eligible Patients / Various Recipients / See Attached List
SELECT_CANDIDATE only where candidate evidence is strong
auto_accept = 1 only for high-confidence validated selections
```

Bad signs:

```text
ollama_call_failed
candidate_id_not_in_candidate_list
selected_grantor_for_placeholder_recipient
confidence_out_of_range
many invalid rows
model using sample_grantor_name as if it were recipient evidence
```

---

### Step 12 — Store real AI adjudication decisions

After dry runs look good:

```bat
python grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --limit 1000 --progress-every 25 --format-mode schema --max-call-failures 3 --num-predict 700
```

For targeted runs:

```bat
python grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --state OR --limit 1000 --progress-every 25 --format-mode schema --num-predict 700

python grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --min-total-amount 10000 --limit 1000 --progress-every 25 --format-mode schema --num-predict 700
```

Do not try to adjudicate millions of signatures. Ollama is slow relative to deterministic matching. Prioritize by dollars, state, grant count, or investigation.

---

### Step 13 — Apply accepted decisions

After reported-EIN shortcuts and AI adjudication have both run, apply accepted decisions once:

```bat
python grant_ai_assist_v1.py apply-decisions --full-refresh
```

Important:

```text
apply-decisions --full-refresh only rebuilds:
- grant_recipient_ai_applied
- grant_recipient_resolved_plus_ai_v1

It does not rebuild:
- org_identity
- grant_recipient_signature
- grant_recipient_ai_candidate
- grant_recipient_ai_decision
- grant_recipient_resolved
```

You can also run it after shortcuts only if you want interim shortcut-only stats, and then run it again after AI adjudication.

---

### Step 14 — Run stats

Full stats:

```bat
python grant_ai_assist_v1.py stats --csv-out grant_match_stats.csv
```

While the final view is not built or if you want faster stats:

```bat
python grant_ai_assist_v1.py stats --skip-final-view --csv-out grant_match_stats.csv
```

Section-only examples:

```bat
python grant_ai_assist_v1.py stats --section deterministic_resolver --skip-final-view --csv-out stats_deterministic.csv
python grant_ai_assist_v1.py stats --section signatures --skip-final-view --csv-out stats_signatures.csv
python grant_ai_assist_v1.py stats --section candidates --skip-final-view --csv-out stats_candidates.csv
python grant_ai_assist_v1.py stats --section ai_decisions --skip-final-view --csv-out stats_ai_decisions.csv
```

To see how many signatures require adjudication, check:

```text
section = signatures
metric  = ai_queue_status
bucket  = candidates_ready
```

The `signatures` column is the number of unique recipient signatures ready for adjudication. The `grants_represented` column is the number of raw grant rows represented by those signatures.

Stricter direct SQL for signatures that have candidates and no decision yet:

```bat
sqlite3 C:\Projects\irs990-tool\db\irs990.db "SELECT COUNT(*) AS signatures_requiring_adjudication, COALESCE(SUM(s.grant_count),0) AS grants_represented, COALESCE(SUM(s.total_amount),0) AS total_amount FROM grant_recipient_signature s WHERE EXISTS (SELECT 1 FROM grant_recipient_ai_candidate c WHERE c.signature_hash = s.signature_hash) AND NOT EXISTS (SELECT 1 FROM grant_recipient_ai_decision d WHERE d.signature_hash = s.signature_hash);"
```

---

## 4. Recommended normal command sequence

```bat
cd /d C:\Projects\irs990-tool

sqlite3 C:\Projects\irs990-tool\db\irs990.db ".backup C:\Projects\irs990-tool\db\irs990_before_grant_ai.db"

python resolve_grant_recipients_v2_1_fast.py --db C:\Projects\irs990-tool\db\irs990.db --full-refresh --batch-size 100000

python grant_ai_assist_v1.py verify-bmf
python grant_ai_assist_v1.py build-identity --full-refresh
python grant_ai_assist_v1.py build-signatures --full-refresh
python grant_ai_assist_v1.py generate-candidates --full-refresh --candidate-mode fast
python grant_ai_assist_v1.py generate-candidates --candidate-mode balanced --queue-status no_candidates

python grant_ai_assist_v1.py backfill-ein-names --dry-run
python grant_ai_assist_v1.py backfill-ein-names

python grant_ai_assist_v1.py reported-ein-shortcuts --limit 1000 --dry-run --csv-out reported_ein_shortcuts_sample.csv
python grant_ai_assist_v1.py reported-ein-shortcuts

python grant_ai_assist_v1.py test-ollama --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --format-mode schema --debug-raw-out ollama_raw_debug.txt

python grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --limit 250 --dry-run --csv-out ai_decisions_250.csv --progress-every 10 --format-mode schema --max-call-failures 3 --num-predict 700

python grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --limit 1000 --progress-every 25 --format-mode schema --max-call-failures 3 --num-predict 700

python grant_ai_assist_v1.py apply-decisions --full-refresh
python grant_ai_assist_v1.py stats --csv-out grant_match_stats.csv
```

---

## 5. When to use `--full-refresh`

| Command | What `--full-refresh` rebuilds | Use when |
|---|---|---|
| `resolve_grant_recipients_v2_1_fast.py --full-refresh` | `grant_recipient_resolved` | First run, logic changed, or deterministic results need a clean rebuild. |
| `build-identity --full-refresh` | `org_identity`, `org_identity_token`, `org_identity_fts` | BMF files changed, identity logic changed, or previous identity build was interrupted. |
| `build-signatures --full-refresh` | `grant_recipient_signature`, `grant_recipient_signature_grant` | Deterministic resolver was rebuilt or signature queueing logic changed. |
| `generate-candidates --full-refresh` | `grant_recipient_ai_candidate` | First candidate pass, candidate logic changed, or partial candidate run should be discarded. |
| `adjudicate --full-refresh` | `grant_recipient_ai_decision` | Rare. Use only if you want to discard all stored AI/shortcut decisions. |
| `apply-decisions --full-refresh` | `grant_recipient_ai_applied` and final view | Safe and recommended after shortcuts/adjudication. Does not rebuild upstream tables. |

Avoid `adjudicate --full-refresh` after you have useful shortcut or AI decisions unless you intentionally want to delete them.

---

## 6. Recovery from interrupted runs

### Interrupted deterministic resolver

If interrupted during normal database write, rerun:

```bat
python resolve_grant_recipients_v2_1_fast.py --full-refresh --batch-size 100000
```

### Interrupted `build-identity`

Drop/rebuild with:

```bat
python grant_ai_assist_v1.py build-identity --full-refresh
```

### Interrupted `build-signatures`

Drop/rebuild with:

```bat
python grant_ai_assist_v1.py build-signatures --full-refresh
```

### Interrupted `generate-candidates --full-refresh`

Rerun:

```bat
python grant_ai_assist_v1.py generate-candidates --full-refresh --candidate-mode fast
```

If interrupted during a non-full-refresh balanced/broad pass, you can usually continue. To refresh status counts if needed, run a small stats check or rerun targeted candidate generation with `--regenerate` for the intended subset.

### Interrupted `adjudicate --dry-run`

Safe. It only writes partial CSV output.

### Interrupted real `adjudicate`

Already committed decisions can stay. Rerun without `--regenerate`; existing decisions are skipped.

### Large WAL file

After heavy writes finish and no process is using the database:

```bat
sqlite3 C:\Projects\irs990-tool\db\irs990.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

Do not manually delete `irs990.db-wal` while scripts are running.

---

## 7. Dry-run checklist

### Deterministic resolver dry run

Command:

```bat
python resolve_grant_recipients_v2_1_fast.py --dry-run --csv-out deterministic_sample.csv --limit 100000
```

Check:

```text
match_status
match_method
resolved_ein
confidence
warning_flags
```

### Reported-EIN shortcut dry run

Command:

```bat
python grant_ai_assist_v1.py reported-ein-shortcuts --limit 1000 --dry-run --csv-out reported_ein_shortcuts_sample.csv
```

Check:

```text
validation_status = ok
auto_accept = 1
selected_ein = reported_ein
identity_name_score not extremely low unless recipient name is blank/placeholder
first_pass_warning_flags does not show contradiction
```

### AI adjudication dry run

Command:

```bat
python grant_ai_assist_v1.py adjudicate --model gemma4:12b --ollama-url http://192.168.7.221:11434/api/chat --limit 250 --dry-run --csv-out ai_decisions_250.csv --progress-every 10 --format-mode schema --max-call-failures 3 --num-predict 700
```

Check:

```text
validation_status = ok for most rows
invalid rows are explainable and safe
NO_MATCH for placeholder/non-organization recipients
candidate_id populated for SELECT_CANDIDATE
selected_ein belongs to a provided candidate
auto_accept count is conservative
```

---

## 8. Command-line flag reference

### 8.1 Deterministic resolver: `resolve_grant_recipients_v2_1_fast.py`

| Flag | Default | Description |
|---|---:|---|
| `--db` | `IRS_DB_PATH` or default DB path | SQLite IRS 990 database path. |
| `--dry-run` | off | Do not write DB table; write results to CSV. |
| `--csv-out` | `grant_recipient_resolved_dry_run.csv` | CSV path for dry run. |
| `--full-refresh` | off | Drop/recreate `grant_recipient_resolved` before processing all grants. |
| `--enable-fuzzy` | off | Enable conservative fuzzy name fallback within geographic pools. |
| `--fuzzy-threshold` | `0.92` | Minimum fuzzy name score when fuzzy mode is enabled. |
| `--accepted-ein-name-threshold` | `0.72` | Minimum name similarity to accept a known reported EIN when recipient name exists. |
| `--bad-ein-name-threshold` | `0.55` | Below this score, a conflicting reported EIN is flagged as likely bad. |
| `--address-unique-min-name-score` | `0.35` | Minimum name similarity to accept a unique exact-address match. Low by design because address is primary signal. |
| `--address-name-threshold` | `0.72` | Minimum name similarity to select one EIN when multiple EINs share the same exact address. |
| `--address-name-margin` | `0.08` | Required gap between best and second-best name score when multiple EINs share an address. |
| `--batch-size` | `50000` | Rows per DB commit in normal mode. Larger can be faster but uses more memory. |
| `--progress-every` | `50000` | Progress interval. Use `0` to disable. |
| `--flush-csv-every` | `100000` | Dry-run CSV flush interval. Use `0` to disable periodic flush. |
| `--no-defer-indexes` | off | In full-refresh mode, create indexes before loading rows. Slower; mainly debugging. |
| `--limit` | `0` | Process only N grant rows for testing. `0` means no limit. |
| `--min-grant-id` | none | Lower `grants.id` bound. |
| `--max-grant-id` | none | Upper `grants.id` bound. |

---

### 8.2 `verify-bmf`

```bat
python grant_ai_assist_v1.py verify-bmf [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--project-dir` | `C:\projects\irs990-tool` | Main project folder containing `eo-bmf`. |
| `--bmf-dir` | none | Explicit EO BMF directory. Overrides project-dir/eo-bmf. |

---

### 8.3 `build-identity`

```bat
python grant_ai_assist_v1.py build-identity [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--db` | default DB path | SQLite database path. |
| `--project-dir` | `C:\projects\irs990-tool` | Main project folder containing `eo-bmf`. |
| `--bmf-dir` | none | Explicit EO BMF directory. |
| `--full-refresh` | off | Drop and rebuild `org_identity`. |
| `--skip-returns` | off | Do not import identity rows from `returns`. |
| `--skip-bmf` | off | Do not import EO BMF files. |
| `--include-bmf-ico` | off | Also index BMF `ICO` as low-priority alias. Use cautiously. |
| `--no-tokens` | off | Do not build `org_identity_token`. |
| `--no-fts` | off | Do not create/rebuild FTS5 table. |
| `--batch-size` | `50000` | Insert batch size. |

---

### 8.4 `build-signatures`

```bat
python grant_ai_assist_v1.py build-signatures [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--db` | default DB path | SQLite database path. |
| `--full-refresh` | off | Rebuild signature tables. |
| `--statuses` | `unresolved,conflicting_ein_match,reported_ein_not_found_name_matched,address_unique,address_narrowed_name_match,fuzzy_probable` | Comma-separated deterministic match statuses to queue. |
| `--low-confidence-threshold` | `0.90` | Queue rows at or below this deterministic confidence. |
| `--min-total-amount` | none | Queue only signatures/grants above this amount threshold. |
| `--state` | none | Filter by recipient state. |
| `--min-grant-id` | none | Lower grant ID bound. |
| `--max-grant-id` | none | Upper grant ID bound. |
| `--limit` | none | Limit source grant rows scanned. |
| `--flush-every` | `250000` | Flush/commit interval while building signatures. |

---

### 8.5 `generate-candidates`

```bat
python grant_ai_assist_v1.py generate-candidates [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--db` | default DB path | SQLite database path. |
| `--full-refresh` | off | Drop/rebuild candidate table. |
| `--regenerate` | off | Regenerate candidates even if they already exist. |
| `--state` | none | Filter signatures by recipient state. |
| `--min-total-amount` | none | Process only signatures at/above amount threshold. |
| `--queue-status` | none | Process only signatures with this queue status, such as `no_candidates`. |
| `--limit` | none | Limit signatures processed. |
| `--max-candidates` | `20` | Maximum candidates stored per signature. |
| `--min-candidate-score` | `45.0` | Minimum candidate score to keep. |
| `--candidate-mode` | `fast` | `fast`, `balanced`, or `broad`. |
| `--enough-candidates` | `8` | In balanced/broad mode, skip fallback once this many distinct EINs are found. |
| `--token-limit` | `50` | Token fallback limit. |
| `--no-fts` | off | Disable FTS even in broad mode. |
| `--commit-every` | `5000` | Commit interval. |
| `--status-update-every` | `0` | Bulk-update signature status every N processed signatures. Default `0` updates only at end for speed. |

Candidate modes:

| Mode | Meaning | Use case |
|---|---|---|
| `fast` | Exact/EIN/address/name lookups only. | First full pass. |
| `balanced` | Fast mode plus geo-constrained token fallback. | Leftovers with `--queue-status no_candidates`. |
| `broad` | Balanced mode plus FTS fallback. | Targeted high-value hard cases only. |

---

### 8.6 `test-ollama`

```bat
python grant_ai_assist_v1.py test-ollama [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--model` | `gemma4:12b` | Ollama model name. |
| `--ollama-url` | `http://localhost:11434/api/chat` | Ollama chat API URL. |
| `--timeout` | `180` | HTTP timeout seconds. |
| `--num-ctx` | `4096` | Ollama context window. |
| `--num-predict` | `700` | Max output tokens. |
| `--format-mode` | `schema` | `schema`, `json`, or `none`. |
| `--think` | off | Enable thinking mode. Usually leave off. |
| `--debug-raw-out` | none | Append raw Ollama responses to this file. |

---

### 8.7 `adjudicate`

```bat
python grant_ai_assist_v1.py adjudicate [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--db` | default DB path | SQLite database path. |
| `--model` | `gemma4:12b` | Ollama model. |
| `--ollama-url` | `http://localhost:11434/api/chat` | Ollama chat API URL. |
| `--timeout` | `180` | HTTP timeout seconds. |
| `--num-ctx` | `8192` | Ollama context window. |
| `--num-predict` | `500` | Max output tokens. Use `700` if needed. |
| `--full-refresh` | off | Drop/rebuild decision table. Use rarely. |
| `--regenerate` | off | Regenerate decisions even if one exists. |
| `--state` | none | Filter signatures by state. |
| `--min-total-amount` | none | Filter signatures by total amount. |
| `--limit` | `100` | Number of signatures to adjudicate. |
| `--max-candidates` | `20` | Candidate count included in prompt. |
| `--auto-accept-threshold` | `0.92` | Minimum confidence for auto-accept. |
| `--dry-run` | off | Write CSV only; do not store decisions. |
| `--csv-out` | `ai_grant_decisions.csv` | CSV path for dry run. |
| `--commit-every` | `50` | DB commit interval for real decisions. |
| `--flush-every` | `10` | Dry-run CSV flush interval. |
| `--progress-every` | `10` | Progress interval. |
| `--format-mode` | `schema` | Ollama format parameter mode. Try `json` if schema mode causes issues. |
| `--think` | off | Enable thinking mode. Usually leave off. |
| `--ollama-retries` | `1` | Retries per signature after an Ollama failure. |
| `--retry-sleep` | `2.0` | Seconds between retries. |
| `--max-call-failures` | `3` | Stop after this many total call failures. Use `0` to disable. |
| `--max-consecutive-call-failures` | `3` | Stop after this many consecutive failures. Use `0` to disable. |
| `--fail-fast` | off | Stop after first Ollama call failure. |
| `--debug-raw-out` | none | Append raw Ollama responses to debug file. |
| `--no-reported-ein-shortcut` | off | Disable pre-Ollama known reported-EIN shortcut. |
| `--reported-ein-shortcut-min-name-score` | `0.35` | Minimum weak name agreement for reported-EIN shortcut when recipient name is real. Blank/placeholder names bypass this. |
| `--reported-ein-shortcut-allow-contradictions` | off | Allow shortcut even when first-pass warning/status suggests reported EIN may be wrong. Normally leave off. |

---

### 8.8 `backfill-ein-names`

```bat
python grant_ai_assist_v1.py backfill-ein-names [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--db` | default DB path | SQLite database path. |
| `--dry-run` | off | Print eligible counts without updating. |

---

### 8.9 `reported-ein-shortcuts`

```bat
python grant_ai_assist_v1.py reported-ein-shortcuts [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--db` | default DB path | SQLite database path. |
| `--regenerate` | off | Overwrite existing decisions for eligible signatures. |
| `--state` | none | Filter by recipient state. |
| `--min-total-amount` | none | Filter by total amount. |
| `--queue-status` | none | Filter by signature queue status. |
| `--limit` | none | Limit scanned signatures. |
| `--max-candidates` | `20` | Candidate context loaded for audit/decision row. |
| `--min-name-score` | `0.35` | Minimum name score unless recipient name is blank/placeholder. |
| `--allow-contradictions` | off | Allow shortcut even when warnings/status suggest reported EIN may be wrong. Normally leave off. |
| `--dry-run` | off | Write audit CSV only; do not store decisions. |
| `--csv-out` | `reported_ein_shortcuts.csv` | Dry-run audit CSV path. |
| `--commit-every` | `5000` | Commit interval for real run. |
| `--flush-every` | `5000` | Dry-run CSV flush interval. |
| `--progress-every` | `50000` | Progress interval. |

---

### 8.10 `apply-decisions`

```bat
python grant_ai_assist_v1.py apply-decisions [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--db` | default DB path | SQLite database path. |
| `--full-refresh` | off | Rebuild applied table and final view. Recommended after decision batches. |
| `--min-confidence` | `0.92` | Minimum stored decision confidence to apply. |
| `--batch-size` | `50000` | Insert batch size. |

---

### 8.11 `stats`

```bat
python grant_ai_assist_v1.py stats [flags]
```

| Flag | Default | Description |
|---|---:|---|
| `--db` | default DB path | SQLite database path. |
| `--top-n` | `50` | Maximum rows for grouped breakdowns. |
| `--section` | none | Print only one section. Choices: `raw_grants`, `deterministic_resolver`, `org_identity`, `signatures`, `candidates`, `ai_decisions`, `applied_ai`, `final_view`. |
| `--skip-final-view` | off | Skip counting final resolved view if it is expensive or not built. |
| `--csv-out` | none | Optional CSV output path. |
| `--json-out` | none | Optional JSON output path. |
| `--no-print` | off | Do not print tables to console. |

---

## 9. Final integration reminder

The original grants-received queries that rely only on `recipient_ein` will not automatically benefit from AI/deterministic resolution until they are updated to use the final resolved layer.

The final view to use is:

```text
grant_recipient_resolved_plus_ai_v1
```

The key search column is:

```text
final_resolved_ein
```

Use `final_match_source` and `final_confidence` to show or filter the source and confidence of the match.
