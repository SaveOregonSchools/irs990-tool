# AGENTS.md

## Project overview

This is an IRS Form 990 research tool. It contains:

- A Flask query console in `app.py`
- Query plugins under `queries/`
- Shared DB helpers in `common.py`
- SQLite schema/reference material under `ai/`
- Rebuild and import scripts for IRS XML / grant / contractor / people data

The production database is large and should not be committed to GitHub.

## Safety rules

- Never commit SQLite database files, IRS XML archives, WAL files, CSV exports, AI decision batches, or local secrets.
- Do not remove read-only safeguards from database query code.
- Preserve validation rules for natural-language SQL generation.
- Prefer small, reviewable changes.
- Use branches and pull requests for all changes.

## Local database assumptions

The app reads the SQLite database path from `IRS_DB_PATH`.
If unset, `common.py` defaults to `C:\Projects\irs990-tool\db\irs990.db`.

Codex should not assume the full database is available in its cloud environment. For tests, create tiny fixture databases or use in-memory SQLite.

## Query plugin contract

Each query plugin in `queries/` should expose:

- `META`
- `HEADERS`
- `render_fields(form)`
- `run(form)`
- `export_rows(form)`

Do not break existing query modules unless the task explicitly requires it.

## Testing expectations

When changing query logic:

- Add or update a small fixture test when practical.
- Validate SQL syntax.
- Confirm exported headers match displayed headers.
- Avoid full-table scans unless the module intentionally supports large exports.

## Natural-language SQL module

The Ask Database module currently uses Ollama endpoints and a schema guide.
Any cloud/OpenAI migration should preserve:

- SELECT/WITH-only validation
- Approved table/view restrictions
- Numeric LIMIT requirement
- Forbidden keyword checks
- Column validation
