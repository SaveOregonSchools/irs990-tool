# IRS Form 990 & OLMS Research Tool

A local research application for building, searching, analyzing, and exporting
slim SQLite databases derived from IRS Form 990 e-file XML returns and OLMS
annual labor-organization bulk filings.

The repository includes three related systems:

1. A Flask research console with purpose-built nonprofit, grant, people,
   contractor, lobbying, related-organization, and risk-analysis modules.
2. An OLMS labor-union research and filing-timeliness suite backed by an isolated
   SQLite sidecar.
3. Data-maintenance tools for preflighting XML, building or appending the main
   database, inventorying original XML files, and improving grant-recipient EIN
   matching.

The database and XML collection are intentionally not stored in Git. You can
build a database from XML or point the application at an existing `irs990.db`.
SQLite database files are portable between Windows and Linux when copied from a
clean, consistent snapshot. The precomputed `risk_network.db` is the exception:
its freshness safeguard includes the main database's resolved path and
filesystem identity, so rebuild that sidecar after moving the main database to
another machine or filesystem.

## Capabilities

- Search organizations by EIN or normalized organization name.
- Review multi-year organization, filing, financial, mission, and address data.
- Explore grants paid, grants received, contractors, compensation, people,
  Schedule R relationships, lobbying, and political activity.
- Open single-EIN deep-dive and fraud/risk dashboards with HTML/PDF output.
- Download a ZIP of the original XML filings shown in a nonprofit deep dive.
- Ask plain-English questions through Ollama and review validated, read-only SQL.
- Export full CSV results from supported modules.
- View cached database and enhanced grant-matching statistics.
- Preflight, rebuild, and safely append IRS XML batches.
- Inventory source XML, detect duplicates/conflicts, and quarantine reviewed
  duplicate files without changing the production database.
- Run optional deterministic and AI-assisted grant-recipient resolution.
- Research OLMS labor organizations, annual filing timeliness, grants,
  vendors/payees, counterparties, and high-confidence IRS matches.

This is a research-oriented slim schema, not a complete mirror of every field in
every IRS form and schedule.

## Requirements

- Python 3.10 or newer
- SQLite support in Python
- An existing `irs990.db`, or IRS e-file XML files from which to build one
- Optional: the original XML collection for filing downloads and future updates
- Optional: Ollama for Ask Database and AI-assisted grant matching

Python dependencies are in `requirements.txt`. Development and test dependencies
are in `requirements-dev.txt`.

## Install

Clone the repository and create a fresh virtual environment. Do not copy a
virtual environment between Windows and Linux.

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` for the machine. At minimum, identify the main database. Configure
the XML settings if you want source-file inventory or filing downloads:

```text
IRS_DB_PATH=db/irs990.db
IRS_XML_ROOT=C:/Projects/IRSDB/XML
IRS_XML_INVENTORY_PATH=db/irs990_sources.db
OLMS_DB_PATH=db/olms.db
OLMS_DATA_ROOT=C:/Projects/IRSDB/OLMS/unpacked
```

Linux example:

```text
IRS_DB_PATH=/var/lib/irs990-tool/db/irs990.db
IRS_XML_ROOT=/srv/irs990-data/xml
IRS_XML_INVENTORY_PATH=/var/lib/irs990-tool/db/irs990_sources.db
```

Forward slashes are accepted on Windows and make configuration easier to move
between operating systems.

Place an existing database at `db/irs990.db`, or use any other location through
`IRS_DB_PATH`. The app opens the main database read-only.

## Configuration

| Setting | Purpose | Default |
|---|---|---|
| `IRS_DB_PATH` | Main application SQLite database. | `db/irs990.db` |
| `IRS_XML_ROOT` | Machine-local root of the original XML tree. | None |
| `IRS_XML_INVENTORY_PATH` | XML source inventory sidecar. | `db/irs990_sources.db` |
| `IRS_PROJECT_DIR` | Project/EO-BMF root used by grant-matching tools. | Project-specific |
| `IRS_GRANT_WORK_DB_PATH` | Bulky grant-matching work sidecar. | Beside the main DB |
| `FAC_API_KEY` | Data.gov key for live Federal Audit Clearinghouse audits, awards, and findings in the risk dashboard. | None |
| `SAM_API_KEY` | SAM.gov key for UEI registration and exclusion checks reached through FAC. | None |
| `SAM_MAX_UEIS` | Maximum verified primary-EIN FAC UEIs checked per dashboard request (hard cap 3). | `1` |
| `SAM_REQUEST_BUDGET` | Maximum SAM HTTP calls per uncached dashboard request (hard cap 10). | `3` |
| `FEC_API_KEY` | OpenFEC key for campaign-committee candidate matches. | None |
| `LDA_API_KEY` | Optional LDA.gov token for higher-rate federal lobbying lookups. | Anonymous access |
| `FAC_DB_PATH` | Optional indexed FAC current/historical audit sidecar. | `db/fac_audits.db` |
| `IRS_SCREENING_DB_PATH` | Optional IRS Pub. 78/revocation, OFAC, and HHS-OIG sidecar. | `db/screening_data.db` |
| `IRS_SCREENING_CACHE_DIR` | Ignored download cache for public screening snapshots. | `downloads/screening` |
| `IRS_RISK_NETWORK_DB_PATH` | Optional precomputed relationship-edge sidecar. | `db/risk_network.db` |
| `OLMS_DB_PATH` | OLMS application sidecar. | `db/olms.db` |
| `OLMS_DATA_ROOT` | Unpacked OLMS annual folders. | None |
| `TOOLBOX_HOME_URL` | Optional parent launcher URL shown as an **All tools** header link. | Hidden |
| `OLLAMA_ENDPOINTS` | Comma-separated Ask Database `/api/chat` endpoints. | Local Ollama |
| `OLLAMA_URL` | Ollama endpoint used by grant adjudication commands. | Local Ollama |
| `OLLAMA_MODEL` | Installed Ollama model name. | Workflow-specific |
| `OLLAMA_COMPLEXITY_CONFIG` | Optional Ask Database preset JSON. | Built-in presets |

`common.py` loads `.env` before resolving application paths. Process-level
environment variables take precedence over values in `.env`.

### Fraud/risk API credentials

The local FAC, IRS, OFAC, and HHS sidecars do not require API keys. For fresh
live lookups:

1. Request a free Data.gov key at
   [api.data.gov/signup](https://api.data.gov/signup/). Put the same personal
   key in `FAC_API_KEY` and `FEC_API_KEY`; this replaces the rate-limited
   `DEMO_KEY` used for development.
2. Sign in to SAM.gov, open
   [Account Details](https://sam.gov/workspace/profile/account-details), and
   reveal/request the **Public API Key** using the emailed one-time password.
   Put it in `SAM_API_KEY`. A non-federal personal account with no role may be
   limited to 10 calls/day, so keep the conservative SAM defaults above unless
   the account has a higher quota.
3. LDA.gov works anonymously. For higher-rate access, register at
   [lda.gov/api/register](https://lda.gov/api/register/) and put the token in
   `LDA_API_KEY`.

Keep `.env` local and restart Flask after changing it. The dashboard reports
each source's status, partial/truncated SAM coverage, and whether live or cached
data was used.

## Run the web app

For local use and development:

```powershell
python app.py
```

Windows users can also run `Launch IRS 990 Tool.ps1`. Open the URL printed in the
terminal, normally `http://127.0.0.1:5000`.

`python app.py` starts Flask's development server. A shared Linux deployment
should import `app:app` through a production WSGI server and place it behind the
same private proxy, VPN, or authentication layer used for other internal tools.
The application does not currently provide its own user authentication.

## Build or refresh the OLMS sidecar

Full atomic rebuild from unpacked annual folders:

```powershell
python build_olms_db.py --input-dir C:/Projects/IRSDB/OLMS/unpacked --db db/olms.db --rebuild
```

Atomic current-year refresh:

```powershell
python build_olms_db.py --refresh-year 2026
```

The importer discovers metadata schemas, audits source hashes and row
provenance, performs schema-guided repair or quarantine, derives union and
counterparty identities, calculates conservative filing-timeliness results,
and builds separate payee-summary and transaction views. See the
[OLMS Sidecar Guide](docs/olms.md).

## Original XML and the source inventory

`IRS_XML_ROOT` is deliberately machine-specific. The inventory stores portable
forward-slash relative paths, so the same sidecar can resolve files beneath a
Windows or Linux root without rewriting millions of rows.

Create or refresh the inventory:

```powershell
python scan_xml_sources.py `
  --sidecar-db db/irs990_sources.db `
  --main-db db/irs990.db `
  --report-csv exports/xml_source_audit.csv `
  --duplicates-csv exports/xml_source_duplicates.csv
```

The scanner uses `IRS_XML_ROOT`; `--xml-dir` can override it for one run. A scan
rebuilds the sidecar's source and loaded-filing rows. Newly written entries use
portable relative paths only. Existing sidecars containing absolute Windows
paths remain readable, and the next scan converts their active entries to the
portable format.

The Nonprofit Deep Dive module uses `IRS_XML_ROOT` plus the stored relative path
when producing a filing ZIP. Historical `returns.source_file` strings in the main
database remain provenance and do not need to identify a currently mounted path.

If only the absolute XML root changes and the directory tree beneath it stays
the same, updating `IRS_XML_ROOT` is sufficient. A new scan is recommended after
moving the archive to another computer and is required after renaming or moving
files or directories inside the root. Build a replacement sidecar under a new
filename, validate it, and then switch `IRS_XML_INVENTORY_PATH` so an interrupted
large scan cannot replace the current inventory.

See the [Data Migration Guide](docs/migrating-data.md) for the Windows-to-Linux
procedure and the [Database Build Guide](docs/database-build.md) for duplicate
classification, conflict analysis, quarantine safeguards, and inventory reports.

## Build or update the database

For routine incremental updates, the Flask home page includes **Data
Maintenance → Import New IRS Data**. It guides you through selecting one or
more new XML directories, optionally installing updated EO-BMF files, appending the filings,
and running all deterministic enhanced grant-matching stages. AI adjudication
remains a separate reviewed step. See the [Guided IRS Data Import](docs/data-import.md)
guide.

The build script requires an explicit XML directory. You can pass the configured
root from the shell.

Windows PowerShell, full rebuild:

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/xml
```

Linux, full rebuild:

```bash
python rebuild_irs990_slim_clean.py --db /var/lib/irs990-tool/db/irs990.db --xml-dir /srv/irs990-data/xml
```

Preflight a new batch before appending it:

```powershell
python rebuild_irs990_slim_clean.py `
  --xml-dir C:/path/to/new-xml `
  --preflight `
  --workers 4 `
  --preflight-report exports/preflight_summary.json `
  --preflight-csv exports/preflight_files.csv
```

Append the reviewed batch:

```powershell
python rebuild_irs990_slim_clean.py `
  --db db/irs990.db `
  --xml-dir C:/path/to/new-xml `
  --append
```

Append mode preserves existing data, skips existing filing/object IDs, rebuilds
canonical filing selections and views, creates indexes, and runs SQLite
optimization. It does not re-extract an already-loaded filing.

After a build or append, refresh the cached statistics page:

```powershell
python refresh_data_stats.py --db db/irs990.db
```

See [XML Preflight Guide](docs/preflight.md) and
[Database Build Guide](docs/database-build.md) before modifying a production
database.

## Research modules

| Module | Primary use |
|---|---|
| Ask Database | Generate, inspect, validate, and optionally run read-only SQLite from a plain-English question. |
| Core Data Lookup | Organization, filing, financial, address, mission, status, and indicator data. |
| Find EINs by Organization Name | Deterministic normalized-name matching with optional fuzzy fallback. |
| Grants Paid / Received / Paid-Received | Research grantor and recipient relationships and export results. |
| Nonprofit Deep Dive | Single-EIN trends, yearly summaries, top grantors, compensation, PDF output, and original filing downloads. |
| Fraud & Risk Indicators | Explainable financial, governance, IRS BMF, Schedule C/L, grant-identity, relationship-network, and optional federal public-record indicators. |
| Contractors | Contractor/vendor compensation reported by filers. |
| Find Filings by Person Name | Search officers, employees, contractors, grant recipients, preparers, signers, and supported schedules. |
| Lobbying & Political Activity | Schedule C, political campaign, 527, dues/proxy-tax, and 990-PF indicators. |
| Schedule R: Related Organizations | Related organizations and supported transaction fields. |
| Filings by EIN(s) | Basic canonical filing availability. |
| Database Statistics | Cached coverage and enhanced grant-matching summaries. |
| OLMS Union Deep Dive | Union identity, trends, filing history, grants, payees, and compensation. |
| OLMS Filing Compliance | Observed late filings and conservative potential-missing flags. |
| OLMS Grants / Contributions | Code 503 payee summaries and itemized transactions. |
| OLMS Vendors / Payees | Non-grant union-reported vendors, consultants, and other payees. |
| OLMS Counterparty Explorer | All unions that reported paying one grantee/vendor identity. |
| OLMS / IRS Match Audit | Deterministic F_NUM-to-EIN evidence and manual overrides. |
| OLMS Import Audit | Source hashes, coverage, repairs, quarantines, duplicates, and orphans. |

Query modules are discovered from `queries/` and reloaded when their files
change. See [Query Module Guide](queries/README.md) for the plugin contract.

The **Fraud & Risk Indicators** page can run in local-only or live-source mode.
Live mode checks the Federal Audit Clearinghouse (FAC), exact-UEI USAspending
and SAM.gov records reached through FAC, OpenFEC committee candidates, and the
federal Lobbying Disclosure Act database. FAC results distinguish federal
awards *expended* from Form 990 government-grant revenue and apply the
$750,000 threshold to fiscal years beginning before October 1, 2024 and the
$1,000,000 threshold thereafter. Candidate-only name matches are displayed as
unscored review leads.

No-key local integrations are documented in
[Public screening data](docs/screening-data.md),
[FAC offline data](docs/fac-offline.md), and the
[precomputed relationship network](docs/risk-network.md). The dashboard opens
these sidecars read-only and continues source-by-source when one is absent.
Credential setup, refresh commands, production-data repair ordering, and the
first full network-build checklist are consolidated in the
[fraud/risk operations runbook](docs/fraud-risk-operations.md).

## Ask Database and Ollama

Ask Database uses `queries/ask_database.py`, `ai/irs990_ai_schema.md`, and the
configured Ollama endpoint/model. Generated SQL must:

- start with `SELECT` or `WITH`;
- use approved tables and views;
- avoid forbidden write/admin keywords;
- use approved qualified columns; and
- include a numeric `LIMIT`.

The SQL is displayed for review. See [AI Schema Guide](ai/README.md) and
[Ollama Configuration](config/README.md).

## Enhanced grant-recipient matching

The optional grant workflow combines deterministic resolution, EO BMF identity
data, candidate generation, rule decisions, optional Ollama adjudication, and a
final applied view.

Bulky working tables live in `grant_matching_work.db`; final decisions and
application-facing views remain in `irs990.db`. The standard Windows workflow is:

```powershell
.\batch_enhanced_grant_matches.bat
```

Back up the database and read [Enhanced Grant Matching Guide](docs/grant-matching.md)
before running the workflow. It can take many hours.

## Local data files

| Artifact | Required to run queries? | Notes |
|---|---:|---|
| `irs990.db` | Yes | Main application data and final enhanced grant results. |
| `irs990_sources.db` | Only for XML downloads | Regenerable inventory of the original XML tree. |
| Original XML tree | Only for XML downloads/updates | Keep outside Git; configure with `IRS_XML_ROOT`. |
| `grant_matching_work.db` | No | Preserve to continue enhanced matching without rebuilding working tables. |
| `olms.db` | Only for OLMS modules | Rebuildable OLMS sidecar; never commit it. |
| Unpacked OLMS annual folders | Only for OLMS rebuilds/refreshes | Keep outside Git; configure with `OLMS_DATA_ROOT`. |
| `eo-bmf/` | No | Required when rebuilding the enhanced identity layer. |
| `exports/`, `imports/`, `adj/` | No | Reports, packets, decisions, and audit artifacts. |

Never commit databases, XML archives, WAL/SHM files, EO BMF CSVs, exports, model
decision packets, logs, or `.env`. See [Database Folder Notes](db/README.md).

## Development and tests

Install development dependencies and run the fixture-based suite:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

Tests use small temporary or in-memory SQLite databases; they do not require the
production database.

## Troubleshooting

- **Database not found:** verify `IRS_DB_PATH` or place the file at
  `db/irs990.db`.
- **Original XML download unavailable:** configure `IRS_XML_ROOT`, confirm the
  XML tree preserves its inventoried hierarchy, and refresh `irs990_sources.db`.
- **Module missing:** check the Flask terminal for an import error and confirm
  the file under `queries/` implements the plugin contract.
- **Statistics page empty/stale:** run `refresh_data_stats.py` after builds or
  matching changes.
- **Ollama unavailable:** verify the endpoint and installed model, then try a
  smaller Ask Database complexity preset.
- **Large export:** narrow the state, year, EIN, amount, or max-row filters.

## Repository and documentation map

| Path | Purpose |
|---|---|
| `app.py` | Flask application, page shell, exports, statistics, and filing ZIP downloads. |
| `common.py` | Environment loading, data paths, read-only SQLite connections, and EIN normalization. |
| `queries/` | Research modules and plugin documentation. |
| `rebuild_irs990_slim_clean.py` | Full database rebuild, append, and preflight entry point. |
| `scan_xml_sources.py` | Portable XML inventory, duplicate/conflict analysis, and quarantine workflow. |
| `refresh_data_stats.py` | Cached web statistics refresh. |
| `build_olms_db.py` | Atomic OLMS sidecar rebuild and targeted annual refresh. |
| `olms.py` | OLMS discovery, parsing, audit, derivation, compliance, and matching logic. |
| `resolve_grant_recipients.py` | Deterministic grant-recipient resolution. |
| `grant_ai_assist_v1.py` | Enhanced identity, candidates, decisions, adjudication, and final views. |
| `migrate_grant_work_sidecar.py` | Moves bulky matching work tables out of the main database. |
| `docs/migrating-data.md` | Existing-database, XML archive, and sidecar migration between computers. |
| `docs/database-build.md` | Build, append, inventory, validation, and caveats. |
| `docs/preflight.md` | XML batch preflight and report interpretation. |
| `docs/grant-matching.md` | Enhanced grant matching and adjudication. |
| `docs/olms.md` | OLMS architecture, build, audit, compliance, matching, and query guide. |
| `queries/README.md` | Query plugin contract and current modules. |
| `config/README.md` | Ollama complexity configuration. |
| `db/README.md` | Database placement, backup, and local-file hygiene. |

## License and trademarks

Copyright (C) 2026 Save Oregon Schools, LLC. The software is licensed under the
GNU Affero General Public License version 3; see [LICENSE](LICENSE). It is
distributed without warranty.

The Save Oregon Schools name, logo, and related branding are not licensed for
reuse under the AGPL. See [TRADEMARKS.md](TRADEMARKS.md).
