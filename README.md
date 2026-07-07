# IRS 990 Tool

A local research tool for building, querying, and exporting data from IRS Form 990 e-file XML returns.

The project has two main parts:

1. **Database build/update tools** that turn IRS 990 XML filings into a slim local SQLite database.
2. **A Flask research console** with a simple home page, prebuilt query modules, CSV exports, cached database statistics, and optional local Ollama support for validated SQL from plain-English questions.

The database itself is intentionally not stored in GitHub. Build it locally from XML files, or point the app at an existing `irs990.db` file.

---

## License

IRS 990 Tool's software code is copyright (C) 2026 Save Oregon Schools, LLC and
is licensed under the GNU Affero General Public License version 3. See
[`LICENSE`](LICENSE) for the full license text.

IRS 990 Tool is distributed without any warranty; without even the implied
warranty of merchantability or fitness for a particular purpose.

The Save Oregon Schools name, logo, and related branding are not licensed for
reuse under the GNU Affero General Public License. See
[`TRADEMARKS.md`](TRADEMARKS.md) for the project's trademark and branding
notice.

---

## What this tool is for

This project is designed for nonprofit and public-records research where you need to answer questions such as:

- What filings exist for a list of EINs?
- What are an organization's revenues, expenses, assets, grants paid, government grants, lobbying indicators, and mission text across years?
- Which organizations paid grants to a recipient?
- Which organizations received grants from a funder?
- Which contractors/vendors were paid by nonprofits?
- Where does a person appear in officer, compensation, contractor, grant, signer, preparer, or Schedule L/J data?
- What related organizations appear in Schedule R?
- Can a plain-English database question be converted into safe SQLite for review or preview?

The database is a **research-oriented slim schema**, not a complete one-table-per-XML-field mirror of every IRS form and schedule.

---

## Repository map

| Path | Purpose |
|---|---|
| `app.py` | Flask web app, home page, query-console shell, and cached statistics page. Auto-discovers and reloads query modules from `queries/`. |
| `common.py` | Shared database path handling, read-only SQLite connection, and EIN normalization helpers. |
| `queries/` | Prebuilt query modules used by the web console. See [`queries/README.md`](queries/README.md). |
| `refresh_data_stats.py` | Refreshes cached database and grant-matching statistics shown by the web app's Database Statistics page. |
| `static/` | Small web assets used by the Flask app, including the Save Oregon Schools logo. |
| `ai/irs990_ai_schema.md` | Compact schema/prompt guide used by the Ask Database module. See [`ai/README.md`](ai/README.md). |
| `config/ollama_complexity.example.json` | Optional example config for Ask Database complexity presets. See [`config/README.md`](config/README.md). |
| `db/` | Local database folder. `db/irs990.db` is ignored by Git. See [`db/README.md`](db/README.md). |
| `rebuild_irs990_slim_clean.py` | Builds or appends to the slim SQLite database from IRS XML files. |
| `docs/database-build.md` | Detailed database rebuild, append, and validation guide. |
| `docs/preflight.md` | XML preflight scan workflow for checking new XML batches before append. |
| `resolve_grant_recipients.py` | Deterministic grant-recipient resolution workflow. |
| `grant_ai_assist_v1.py` | Advanced grant-recipient candidate generation, rule decisions, Ollama adjudication, and final applied views. |
| `grant_ai_batch_worker.py` | Linux/Ollama batch worker for external grant-recipient adjudication packets. |
| `docs/grant-matching.md` | Detailed enhanced grant matching, AI assist, and batch adjudication workflow. |
| `.env.example` | Example local environment settings. Copy to `.env` if needed. |
| `requirements.txt` | Python dependencies. |

---

## Requirements

- Python 3.10+ recommended
- SQLite
- IRS 990 e-file XML files, if building the database locally
- Optional: Ollama running locally or on a reachable server for Ask Database and grant-recipient AI adjudication

Python package dependencies are listed in `requirements.txt`.

---

## Quick start

From the project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Place the SQLite database at:

```text
db/irs990.db
```

Or set a custom path:

```powershell
$env:IRS_DB_PATH = "C:\Projects\irs990-tool\db\irs990.db"
```

Then start the web app:

```powershell
py app.py
```

Open the local Flask URL shown in the console, usually:

```text
http://127.0.0.1:5000
```

The root URL opens a home page with grouped buttons for the most common modules and supporting tools. Query modules auto-load when selected from the dropdown, and the app auto-detects added or edited files in `queries/` on the next request.

---

## Building the database

For a full rebuild from a folder of IRS XML files:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\XML
```

For adding new XML files to an existing database:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\NewXML --append
```

Append mode preserves the existing database and skips XML filings already present by filing ID / normalized object ID. After loading, it rebuilds `canonical_by_ein_year`, recreates views, creates indexes, and runs SQLite optimization.

Before appending unfamiliar XML batches, run a preflight scan:

```powershell
py rebuild_irs990_slim_clean.py --xml-dir C:\IRSDB\NewXML --preflight --workers 4 --preflight-report exports\preflight_summary.json --preflight-csv exports\preflight_files.csv
```

See [`docs/database-build.md`](docs/database-build.md) for the full rebuild/append guide, flags, validation queries, and caveats. See [`docs/preflight.md`](docs/preflight.md) for preflight scan details.

---

## Query console

The web app opens to a home page with grouped module buttons. Query pages also include a dropdown for switching modules. The selected module loads automatically when the dropdown changes.

The app discovers modules in `queries/`. Each module provides its own form fields, SQL logic, result headers, and CSV export behavior. Module files are reloaded automatically when their `.py` file modification times change, so a running development server usually picks up added or edited query modules on the next page request.

Current query families include:

| Query module | What it does |
|---|---|
| `ask_database.py` | Generates and/or runs validated SQLite from a plain-English question. Also supports validating and running pasted SQL. |
| `filings_by_ein.py` | Lists filings for one or more EINs. |
| `ngo_core_data.py` | Core organization/financial data by EIN, state, and year range. |
| `ngo_ein_by_name.py` | Finds EINs from organization names using deterministic matching and optional fuzzy fallback. |
| `ngo_grants_out.py` | Grants paid by filers/grantors. |
| `ngo_grants_in.py` | Grants received by recipient EINs. |
| `ngo_grants_io.py` | Combined grants paid/received workflow. |
| `ngo_contractors_out.py` | Contractors/vendors reported by filers. |
| `ngo_related_orgs_sched_r.py` | Related organizations from Schedule R. |
| `people_lookup.py` | Finds where a person appears across supported tables/views. |

See [`queries/README.md`](queries/README.md) for the plugin interface and development notes.

---

## Database Statistics page

The home page includes **Database Statistics**, which reads cached rows from:

```text
app_data_stats
app_data_stats_meta
```

Refresh the cache manually with:

```powershell
py refresh_data_stats.py --db db\irs990.db
```

The standard enhanced grant matching batch also refreshes the stats cache after rebuilding grant matching data:

```powershell
.\batch_enhanced_grant_matches.bat
```

The stats page intentionally reads cached rows so opening it does not run expensive full-database summaries.

---

## Ask Database / Ollama

The Ask Database module uses:

- `queries/ask_database.py`
- `ai/irs990_ai_schema.md`
- optional settings from `.env`
- optional complexity presets from a JSON file such as `config/ollama_complexity.json`

Common `.env` settings:

```text
IRS_DB_PATH=db/irs990.db
OLLAMA_ENDPOINTS=http://localhost:11434/api/chat
OLLAMA_MODEL=qwen3.5:9b
OLLAMA_COMPLEXITY_CONFIG=config/ollama_complexity.json
```

Ask Database is intentionally conservative. Generated SQL must be read-only, use approved tables/views, and include a numeric `LIMIT`. The SQL is shown in the UI so it can be reviewed before relying on the output.

See [`ai/README.md`](ai/README.md) and [`config/README.md`](config/README.md).

---

## Grant-recipient resolution and AI assist

The raw IRS grant rows often include recipient names and addresses, but not always reliable recipient EINs. This repo includes an advanced workflow to resolve grant recipients more accurately:

1. `resolve_grant_recipients.py` creates a deterministic first-pass resolution table.
2. `grant_ai_assist_v1.py` builds identity tables, recipient signatures, candidate matches, rule-based decisions, optional Ollama adjudications, and final applied views.
3. The final enhanced layer can be used by grant-received workflows when you want matched recipient EINs beyond the raw reported EIN field.
4. `refresh_data_stats.py` can summarize the resulting database and matching pipeline for the web app's Database Statistics page.

This workflow can write additional tables/views to the database, so back up the database before running it.

For the standard post-XML-load enhanced grant rebuild, run:

```powershell
.\batch_enhanced_grant_matches.bat
```

See [`docs/grant-matching.md`](docs/grant-matching.md).

---

## Data and Git hygiene

The repo is intended to track code and documentation only.

Do **not** commit:

- `db/irs990.db`
- SQLite WAL/SHM files
- large XML datasets
- EO BMF CSVs
- exports, cache files, logs, or local adjudication packets
- `.env`

The tracked `static/save-oregon-schools-logo.png` file is a small app asset and is safe to commit.

The `.gitignore` is set up for the normal local database and output folders, but always check `git status` before committing.

---

## Typical workflows

### Run a known query

1. Start `py app.py`.
2. Open `http://127.0.0.1:5000`.
3. Choose a module from the home page or select one from the query-page dropdown.
4. Enter EINs, organization names, state/year filters, or other module-specific inputs.
5. Click **Run Query** for a preview.
6. Click **Export CSV** for the full module export.

### Add a new query module

1. Add a `.py` file under `queries/`.
2. Define `META`, `render_fields(form)`, `run(form)`, and `export_rows(form)`.
3. Use `common.connect_ro()` for read-only database access.
4. Use parameterized SQL rather than string-inserting user input.
5. Start the app if it is not already running. If it is running, refresh the page; query files are auto-detected on the next request.

### Add new IRS XML filings

1. Put the new XMLs in a separate folder.
2. Run `rebuild_irs990_slim_clean.py --append` against that folder.
3. Check the selected/skipped load summary.
4. Run validation queries or spot-check known filings.
5. Restart the app if needed.

---

## Troubleshooting

### Database not found

Use the default layout:

```text
db/irs990.db
```

Or set `IRS_DB_PATH` to the full database path.

### Query module does not show up

Check that the module is in `queries/`, does not start with `_`, and defines all required plugin functions. Refresh the page to trigger plugin auto-detection. If an import error occurs, it will be printed in the Flask console.

### Database Statistics page is empty

Run the stats refresh script:

```powershell
py refresh_data_stats.py --db db\irs990.db
```

The enhanced grant matching batch also refreshes these cached stats as part of its normal workflow.

### Ask Database cannot reach Ollama

Confirm `OLLAMA_ENDPOINTS` points to a reachable `/api/chat` endpoint and that `OLLAMA_MODEL` names an installed model. Try a smaller query complexity setting if generation times out.

### CSV export looks large

Several modules can return very large result sets. Use state, year, EIN, amount, or max-row filters where available before exporting.

---

## Documentation index

- [`docs/database-build.md`](docs/database-build.md) - database build/append guide
- [`docs/preflight.md`](docs/preflight.md) - XML preflight scan guide
- [`docs/grant-matching.md`](docs/grant-matching.md) - enhanced grant matching, AI assist, and batch adjudication guide
- [`queries/README.md`](queries/README.md) - query module guide
- [`ai/README.md`](ai/README.md) - Ask Database schema guide notes
- [`config/README.md`](config/README.md) - Ollama complexity config notes
- [`db/README.md`](db/README.md) - local database folder notes
