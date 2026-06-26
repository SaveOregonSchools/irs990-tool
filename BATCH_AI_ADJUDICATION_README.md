# Batch AI adjudication workflow, v1.26

This workflow lets you run the expensive remaining grant-recipient adjudication on a Linux AI server without copying the full IRS 990 SQLite database.

The Windows/project machine exports self-contained JSONL packet files. The Linux server reads those packets, calls Ollama, and writes decision JSONL files. The Windows machine then imports the decisions, validates them against the local database candidate table, and applies only accepted decisions to the final enhanced grant view.

## Files

Use these files:

```text
C:\projects\irs990-tool\grant_ai_assist_v1.py       # copy from grant_ai_assist_v1_26_batch_adjudication.py
Linux AI server: grant_ai_batch_worker.py
```

The exported packet files contain:

```text
signature_hash
recipient signature fields
first-pass resolver status/warnings
candidate EIN list
candidate scores/reasons
response template
```

They do **not** contain your whole SQLite database.

## Recommended strategy

Do not process all remaining signatures first. Start with the largest dollar bucket.

From your latest amount-bucket check, the `>= $100,001` signature bucket accounted for most of the remaining dollar value. Start there.

A good first production sequence is:

```text
1. Export high-dollar packets from Windows.
2. Copy packet directory and worker script to Linux.
3. Run a small Linux test: one file, 25 records.
4. Run a real Linux batch with 1-2 parallel workers.
5. Copy decision files back to Windows.
6. Dry-run import and inspect audit CSV.
7. Real import.
8. apply-decisions --full-refresh.
9. Recount remaining queue.
```

## 1. Export packet batches on Windows

From PowerShell:

```powershell
cd C:\projects\irs990-tool

python grant_ai_assist_v1.py export-adjudication-batches `
  --min-total-amount 100000 `
  --batch-size 10000 `
  --out-dir exports\ai_packets_100k `
  --prefix adjudication_packets `
  --manifest exports\ai_packets_100k\adjudication_packets_manifest.json `
  --summary-csv exports\ai_packets_100k\packet_summary.csv
```

This creates files like:

```text
exports\ai_packets_100k\adjudication_packets_000001.jsonl
exports\ai_packets_100k\adjudication_packets_000002.jsonl
...
exports\ai_packets_100k\adjudication_packets_manifest.json
exports\ai_packets_100k\packet_summary.csv
```

### Useful export flags

```text
--min-total-amount 100000      Export only signatures whose aggregated signature amount is >= 100,000.
--max-total-amount 100000      Optional upper bound for a bucketed run.
--state OR                     Optional state filter.
--limit 25000                  Optional total packet limit across all output files.
--batch-size 10000             Packets per JSONL file.
--max-candidates 20            Candidate rows per signature.
--include-schema               Include JSON schema in every packet; easier to inspect, larger files.
--overwrite                    Replace existing packet files with the same prefix.
```

### Bucketed exports

For the next lower bucket:

```powershell
python grant_ai_assist_v1.py export-adjudication-batches `
  --min-total-amount 50000 `
  --max-total-amount 100000 `
  --batch-size 10000 `
  --out-dir exports\ai_packets_50k_100k `
  --prefix adjudication_packets
```

For a small test export:

```powershell
python grant_ai_assist_v1.py export-adjudication-batches `
  --min-total-amount 100000 `
  --limit 100 `
  --batch-size 25 `
  --out-dir exports\ai_packets_test `
  --prefix adjudication_packets `
  --overwrite
```

## 2. Copy packets and worker to Linux

Example using `scp` from Windows PowerShell:

```powershell
scp grant_ai_batch_worker.py user@192.168.7.221:/data/irs990_ai/
scp -r exports\ai_packets_100k user@192.168.7.221:/data/irs990_ai/packets_100k
```

Adjust paths/user/host as needed.

## 3. Test Ollama on Linux with a tiny run

On the Linux AI server:

```bash
cd /data/irs990_ai

python3 grant_ai_batch_worker.py \
  --input-dir /data/irs990_ai/packets_100k \
  --output-dir /data/irs990_ai/decisions_test \
  --model gemma4:12b \
  --ollama-url http://127.0.0.1:11434/api/chat \
  --parallel-workers 1 \
  --max-files 1 \
  --limit 25 \
  --overwrite \
  --progress-every 1
```

This writes:

```text
/data/irs990_ai/decisions_test/decisions_000001.jsonl
/data/irs990_ai/decisions_test/errors_000001.jsonl
```

If the error file is empty or very small and the decisions look valid, proceed.

## 4. Run production processing on Linux

Single-worker conservative run:

```bash
python3 grant_ai_batch_worker.py \
  --input-dir /data/irs990_ai/packets_100k \
  --output-dir /data/irs990_ai/decisions_100k \
  --model gemma4:12b \
  --ollama-url http://127.0.0.1:11434/api/chat \
  --parallel-workers 1 \
  --progress-every 25
```

Two-worker run, if your server handles parallel Gemma requests well:

```bash
python3 grant_ai_batch_worker.py \
  --input-dir /data/irs990_ai/packets_100k \
  --output-dir /data/irs990_ai/decisions_100k \
  --model gemma4:12b \
  --ollama-url http://127.0.0.1:11434/api/chat \
  --parallel-workers 2 \
  --progress-every 25
```

### Worker flags

```text
--parallel-workers 2           Number of concurrent Ollama calls from this worker.
--num-predict 700              Output token budget. Lower can be faster; too low may truncate JSON.
--num-ctx 8192                 Context window.
--timeout 180                  Seconds before one Ollama request fails.
--format-mode schema           Use Ollama structured schema output.
--think                        Enable model thinking. Default is off and recommended.
--resume / --no-resume          Default resume skips signatures already written to the output decision file.
--overwrite                    Delete output/error files for the selected input and rerun.
--write-failures-as-human-review
                               Optional. Default writes failed calls only to errors_*.jsonl so they can be retried.
```

## 5. Resuming failed or interrupted Linux runs

The worker is resumable by default.

If a run stops halfway, rerun the same command with the same output directory. It reads existing `decisions_*.jsonl` files and skips signatures already processed.

Do **not** use `--overwrite` unless you want to redo a batch from scratch.

Errors go to:

```text
errors_000001.jsonl
errors_000002.jsonl
...
```

If errors are due to transient network/model problems, rerun the worker. It will skip already completed decisions and try unfinished packet records again.

## 6. Copy decisions back to Windows

From Windows PowerShell:

```powershell
cd C:\projects\irs990-tool
mkdir imports\ai_decisions_100k -Force
scp -r user@192.168.7.221:/data/irs990_ai/decisions_100k/* imports\ai_decisions_100k\
```

You only need the `decisions_*.jsonl` files for import. Keep `errors_*.jsonl` for troubleshooting.

## 7. Dry-run import on Windows

Always validate before real import:

```powershell
python grant_ai_assist_v1.py import-adjudication-decision-dir `
  --in-dir imports\ai_decisions_100k `
  --glob "decisions_*.jsonl" `
  --source-model external:linux_gemma4_12b `
  --dry-run `
  --audit-dir imports\ai_decisions_100k_audit
```

Review the audit CSVs in:

```text
imports\ai_decisions_100k_audit\
```

Look for:

```text
validation_status
validation_error
auto_accept
selected_ein
selected_name
```

If validation errors are high, do not real-import yet. Inspect the reasons.

## 8. Real import on Windows

```powershell
python grant_ai_assist_v1.py import-adjudication-decision-dir `
  --in-dir imports\ai_decisions_100k `
  --glob "decisions_*.jsonl" `
  --source-model external:linux_gemma4_12b `
  --audit-dir imports\ai_decisions_100k_audit_real
```

This writes validated decisions to:

```text
grant_recipient_ai_decision
```

The import stores all validated decisions, but only decisions with `auto_accept=1` and sufficient confidence are applied into the final enhanced view by the next step.

## 9. Apply decisions

```powershell
python grant_ai_assist_v1.py apply-decisions --full-refresh
```

This rebuilds:

```text
grant_recipient_ai_applied
grant_recipient_resolved_plus_ai_v1
```

It does not rebuild identity/candidates/signatures.

## 10. Recount remaining queue

```powershell
sqlite3 C:\projects\irs990-tool\db\irs990.db "SELECT COUNT(*) AS signatures_left_for_ai_review, COALESCE(SUM(s.grant_count),0) AS grants_represented, ROUND(COALESCE(SUM(s.total_amount),0),2) AS total_amount FROM grant_recipient_signature s WHERE EXISTS (SELECT 1 FROM grant_recipient_ai_candidate c WHERE c.signature_hash = s.signature_hash) AND NOT EXISTS (SELECT 1 FROM grant_recipient_ai_decision d WHERE d.signature_hash = s.signature_hash);"
```

## Suggested operating pattern

Start with high-dollar signatures:

```text
>= $100,001
$50,001-$100,000
$10,001-$50,000
```

Do the low-dollar buckets later, or only if needed.

## Database size note

Your DB object-size report showed large working tables, especially:

```text
grant_recipient_ai_decision      ~6.8 GB
grant_recipient_resolved         ~5.7 GB
grants                           ~3.6 GB
grant_recipient_ai_candidate     ~3.1 GB
org_identity                     ~2.9 GB
grant_recipient_signature        ~1.9 GB
```

This is mostly real working data, not junk. After the matching project is stable, you can consider archiving/rebuilding some working tables, but do not drop them while you are still generating candidates or importing AI decisions.

Periodically checkpoint WAL files:

```powershell
sqlite3 C:\projects\irs990-tool\db\irs990.db "PRAGMA wal_checkpoint(TRUNCATE);"
```
