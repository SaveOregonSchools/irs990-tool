# Database folder

This folder is the default location for the local IRS 990 SQLite database:

```text
db/irs990.db
```

The database file is intentionally not tracked in GitHub.

---

## Default behavior

By default, `common.py` looks for the database at:

```text
<project-root>/db/irs990.db
```

To use a database somewhere else, set `IRS_DB_PATH`.

PowerShell:

```powershell
$env:IRS_DB_PATH = "C:\IRSDB\db\irs990.db"
```

Windows CMD:

```bat
set IRS_DB_PATH=C:\IRSDB\db\irs990.db
```

---

## Files that should stay local

Do not commit these files:

```text
irs990.db
irs990.db-shm
irs990.db-wal
*.db
*.db-shm
*.db-wal
*.db.sql
```

Large database files, WAL files, exports, logs, and schema dumps should remain local unless you intentionally publish a separate release artifact somewhere outside the repo.

---

## Creating or updating the database

Full rebuild:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\XML
```

Append new XMLs:

```powershell
py rebuild_irs990_slim_clean.py --db db\irs990.db --xml-dir C:\IRSDB\NewXML --append
```

See [`../docs/database-build.md`](../docs/database-build.md) for details.

---

## Web app statistics cache

The Flask app's **Database Statistics** page reads cached summary rows from the local SQLite database instead of running expensive summaries each time the page opens.

Refresh those cached rows with:

```powershell
py refresh_data_stats.py --db db\irs990.db
```

This creates or updates:

```text
app_data_stats
app_data_stats_meta
```

The enhanced grant matching batch also refreshes these cached stats automatically.

---

## Backup examples

SQLite shell backup:

```powershell
sqlite3 db\irs990.db ".backup db\irs990_backup_YYYYMMDD.db"
```

Copy backup after closing writers:

```powershell
Copy-Item db\irs990.db db\irs990_backup_YYYYMMDD.db
```

When the app is only reading, copying is usually fine. Before major rebuilds, appends, or grant-recipient matching workflows, make a backup.
