# Batch AI Adjudication Workflow — v1.27

This guide covers the Linux/Ollama batch worker for grant-recipient adjudication packets exported from the Windows IRS 990 database workflow.

## Files

- `grant_ai_assist_v1.py` — Windows/database-side script. Exports packets and imports decisions.
- `grant_ai_batch_worker.py` — Linux/Ollama worker. Reads packet JSONL files and writes decision JSONL files.
- `grant_ai_adjudicator_system_prompt_v1_27.txt` — optional standalone prompt file.
- `Modelfile.grant-ai-adjudicator-v1_27` — optional Ollama Modelfile that embeds the system prompt.

## v1.27 worker changes

Compared with v1.26, the Linux worker now adds:

- `--error-retries N` to retry transient Ollama/API/JSON failures before writing to `errors_*.jsonl`.
- `--retry-backoff-seconds` and `--retry-backoff-multiplier` for retry pacing.
- `--candidate-id-retries N` to retry when the model tries to select a candidate but omits or garbles `candidate_id`.
- stricter JSON schema: `candidate_id` is now required on every response.
- stronger prompt language: `SELECT_CANDIDATE` must include an exact candidate ID such as `C1`.
- `--system-prompt-file` for testing prompt variants without editing the script.
- `--omit-system-message` for use with a custom Ollama Modelfile that already embeds the system prompt.

## Recommended worker command

Use 4 workers based on the earlier benchmark where 4 workers was faster than 1, 2, or 8.

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

The default values already include:

```text
--error-retries 2
--candidate-id-retries 1
--retry-backoff-seconds 2
--retry-backoff-multiplier 1.5
--think disabled
--format-mode schema
```

## Testing a single packet file

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

## Retry behavior

### `--error-retries`

Retries failures such as:

- Ollama HTTP/API errors.
- empty `message.content`.
- invalid JSON returned by Ollama.
- malformed structured output that cannot be parsed.

Example:

```bash
--error-retries 2 --retry-backoff-seconds 2 --retry-backoff-multiplier 1.5
```

This gives each packet up to three total tries: the initial call plus two retries.

### `--candidate-id-retries`

Retries when the model appears to select a candidate but fails to return a valid `candidate_id`.

The retry message gives the model an explicit list such as:

```text
C1 = EIN 123456789, name Example Org; C2 = EIN 987654321, name Example Foundation
```

If the retry still fails, the worker safely converts the row to:

```json
{
  "decision": "HUMAN_REVIEW",
  "reason_codes": ["worker_missing_or_invalid_candidate_id"]
}
```

## Resuming interrupted runs

The worker is resumable by default. If a decision file already contains a `signature_hash`, that signature is skipped on rerun.

```bash
python3 grant_ai_batch_worker.py \
  --input-dir /data/irs990_ai/packets_100k \
  --output-dir /data/irs990_ai/decisions_100k \
  --model gemma4:12b \
  --parallel-workers 4
```

Use `--overwrite` only when you intentionally want to delete and recreate the decision/error files.

## Error files

Rows that still fail after all retries are written to:

```text
errors_000001.jsonl
errors_000002.jsonl
...
```

By default, failed calls are not written as decisions. That means they can be retried later. If you want failures to become explicit human-review decisions, use:

```bash
--write-failures-as-human-review
```

Usually, leave that off.

## Optional: custom Ollama model with embedded system prompt

The worker normally sends a system prompt with every request. You can instead create a custom Ollama model that embeds the system prompt using the provided Modelfile.

On the Linux server:

```bash
ollama create grant-ai-adjudicator:gemma4-12b -f Modelfile.grant-ai-adjudicator-v1_27
```

Then run the worker with the custom model and omit the system message:

```bash
python3 grant_ai_batch_worker.py \
  --input-dir /data/irs990_ai/packets_100k \
  --output-dir /data/irs990_ai/decisions_100k \
  --model grant-ai-adjudicator:gemma4-12b \
  --ollama-url http://127.0.0.1:11434/api/chat \
  --parallel-workers 4 \
  --omit-system-message
```

This may reduce repeated prompt tokens slightly, but it will not eliminate per-request input because the grant-recipient packet and candidate list must still be sent for each signature.

## Alternative: prompt file

If you want to test prompt changes without editing the script:

```bash
python3 grant_ai_batch_worker.py \
  --input-dir /data/irs990_ai/packets_100k \
  --output-dir /data/irs990_ai/decisions_100k \
  --model gemma4:12b \
  --system-prompt-file grant_ai_adjudicator_system_prompt_v1_27.txt \
  --parallel-workers 4
```

## Windows import workflow

After copying decisions back to Windows, always dry-run import first:

```powershell
python grant_ai_assist_v1.py import-adjudication-decision-dir `
  --in-dir imports\ai_decisions_100k `
  --glob "decisions_*.jsonl" `
  --source-model external:linux_gemma4_12b `
  --dry-run `
  --audit-dir imports\ai_decisions_100k_audit
```

If the audit looks good:

```powershell
python grant_ai_assist_v1.py import-adjudication-decision-dir `
  --in-dir imports\ai_decisions_100k `
  --glob "decisions_*.jsonl" `
  --source-model external:linux_gemma4_12b `
  --audit-dir imports\ai_decisions_100k_audit_real
```

Then rebuild the applied/final layer:

```powershell
python grant_ai_assist_v1.py apply-decisions --full-refresh
```

## What to monitor in import audit CSVs

Look at:

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

The safest production rule remains:

```text
Import all valid decisions if desired, but only apply rows where the Windows importer marks auto_accept = 1.
```

