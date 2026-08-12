# Local database files

The default main database location is:

```text
db/irs990.db
```

`common.py` loads `.env` and then uses `IRS_DB_PATH` when it is set. An absolute
path is recommended on a server; the project-relative default is convenient for
local use.

Related local databases may include:

| File | Purpose |
|---|---|
| `irs990.db` | Main application database and final enhanced grant results. |
| `irs990_sources.db` | Regenerable original-XML inventory used for filing downloads. |
| `grant_matching_work.db` | Bulky enhanced grant-matching workspace. |
| `olms.db` | Rebuildable OLMS labor-organization research sidecar. |

All are ignored by Git.

## Configuration

Example `.env` values:

```text
IRS_DB_PATH=db/irs990.db
IRS_XML_INVENTORY_PATH=db/irs990_sources.db
IRS_GRANT_WORK_DB_PATH=db/grant_matching_work.db
OLMS_DB_PATH=db/olms.db
OLMS_DATA_ROOT=C:/Projects/IRSDB/OLMS/unpacked
```

The original XML tree is configured separately with `IRS_XML_ROOT`; it does not
need to be stored inside the repository.

## Create or update data

Full rebuild using the configured XML root in PowerShell:

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/xml
```

Append a reviewed XML batch:

```powershell
python rebuild_irs990_slim_clean.py --db db/irs990.db --xml-dir C:/path/to/new-xml --append
```

Refresh the source inventory:

```powershell
python scan_xml_sources.py --sidecar-db db/irs990_sources.db --main-db db/irs990.db
```

See [Database Build Guide](../docs/database-build.md) before writing to the main
database or quarantining source XML.

Build the isolated OLMS sidecar with `python build_olms_db.py --rebuild`; see
[OLMS Sidecar Guide](../docs/olms.md). OLMS source folders and audit exports are
also local-only data.

## Cached web statistics

The Database Statistics page reads cached rows from `app_data_stats` and
`app_data_stats_meta`. Refresh them after a build, append, or matching change:

```powershell
python refresh_data_stats.py --db db/irs990.db
```

The standard enhanced grant-matching workflow performs this refresh
automatically.

## Backups and SQLite side files

Before rebuilds, appends, migrations, or matching workflows, stop active writers
and create a consistent backup. With the SQLite shell:

```powershell
sqlite3 db/irs990.db ".backup db/irs990_backup_YYYYMMDD.db"
```

A closed, checkpointed database can also be copied directly. Do not treat
`-wal` or `-shm` files as standalone backups, and do not copy an actively
changing database without SQLite's backup mechanism.

Keep these local and out of Git:

```text
*.db
*.db-wal
*.db-shm
*.db.sql
backup/
```
