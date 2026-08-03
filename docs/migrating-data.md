# Moving an Existing Installation

This guide covers moving the application, its databases, and the original IRS
XML archive between computers. It applies to Windows-to-Linux migrations as well
as later storage reorganizations.

## When to rescan the XML archive

The source inventory stores each XML file as a forward-slash path relative to
`IRS_XML_ROOT`. That separates the portable inventory from the machine-specific
location of the archive.

| Change | Is a new scan needed? |
|---|---|
| Change only the absolute root, while preserving every path beneath it | No. Update `IRS_XML_ROOT`. A verification scan is still recommended after a computer migration. |
| Copy the archive to another computer with the same internal layout | Recommended, to verify the copy and replace any legacy absolute-path entries. |
| Rename or move directories or files beneath the XML root | Yes. The stored relative paths have changed. |
| Add, remove, restore, or quarantine XML files | Yes. The inventory should match the active archive. |
| Do not copy the existing source-inventory sidecar | Yes. The destination needs a new inventory. |

For example, moving `C:/Projects/IRSDB/XML/2024/filing.xml` to
`/srv/irs990-data/xml/2024/filing.xml` changes only the root. Moving it to
`/srv/irs990-data/xml/by-year/2024/filing.xml` also changes its relative path, so
the inventory must be rebuilt.

Historical `returns.source_file` values in the main database are provenance.
They do not need to be rewritten when the archive moves.

## Safe migration procedure

1. Stop database-writing jobs and make a consistent backup of the main database.
   If SQLite WAL mode is active, use SQLite's backup command or checkpoint the
   database before copying it; do not copy only the `.db` file while writes are
   in progress.
2. Copy the XML archive while preserving its directory hierarchy and filename
   case. Linux filesystems are normally case-sensitive.
3. Copy the main database and, if desired, the existing source-inventory and
   grant-work sidecars. Do not copy stale `-wal` or `-shm` files independently.
4. Set `IRS_DB_PATH` and `IRS_XML_ROOT` for the destination paths.
5. Keep the old source inventory as a rollback copy. Build a new sidecar under a
   temporary name instead of overwriting the only known-good inventory.
6. Review the scan summary and CSV reports, then spot-check filing downloads in
   Nonprofit Deep Dive.
7. Set `IRS_XML_INVENTORY_PATH` to the new sidecar and restart the application.
8. Archive the old sidecar only after the new installation is accepted.

Example Linux settings:

```dotenv
IRS_DB_PATH=/var/lib/irs990-tool/db/irs990.db
IRS_XML_ROOT=/srv/irs990-data/xml
IRS_XML_INVENTORY_PATH=/var/lib/irs990-tool/db/irs990_sources.new.db
```

Build the replacement inventory:

```bash
mkdir -p /var/lib/irs990-tool/db /var/lib/irs990-tool/exports
python scan_xml_sources.py \
  --sidecar-db /var/lib/irs990-tool/db/irs990_sources.new.db \
  --main-db /var/lib/irs990-tool/db/irs990.db \
  --report-csv /var/lib/irs990-tool/exports/xml_source_audit.csv \
  --duplicates-csv /var/lib/irs990-tool/exports/xml_source_duplicates.csv
```

The scanner takes the XML directory from `IRS_XML_ROOT` unless `--xml-dir` is
provided. It writes current relative paths to the new sidecar and refreshes the
comparison with filings loaded in the main database. It does not modify the main
database or its historical `returns.source_file` values.

The default candidate-hashing mode is recommended for a production inventory.
`--hash-mode none` is faster but cannot reliably distinguish exact duplicates
from object-ID conflicts.

## Why use a new sidecar

A scan resets the selected sidecar's active source and loaded-filing rows before
repopulating them. Large archives can take a long time to scan. Writing to a new
sidecar means an interruption does not damage the inventory currently used by
the application and switching back is only a configuration change.

## Validation checklist

- The scanner reports the expected total XML count.
- Missing-on-disk and not-loaded counts are understood.
- Object-ID conflicts have been reviewed; exact duplicates are expected or
  quarantined according to the database-build workflow.
- `source_files.relative_path` values do not start with a drive letter, `/`, or
  `..`, and use `/` rather than `\\`.
- Several filings from different years can be downloaded through Nonprofit Deep
  Dive.
- The application and any background jobs use the same `IRS_DB_PATH`,
  `IRS_XML_ROOT`, and `IRS_XML_INVENTORY_PATH` settings.

See the [Database Build Guide](database-build.md) for scanner classifications,
conflict analysis, and quarantine procedures.
