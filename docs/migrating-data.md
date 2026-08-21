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
3. Copy the main database and the application sidecars needed on the destination:
   the grant-work database, FAC audit database, screening database, source
   inventory, OLMS database, and risk-network database. Copy `risk_network.db`
   together with the exact checkpointed main database it describes. Current
   builds validate a portable database UUID, risk-source revision, file size,
   and SQLite-header digest, so a path, drive, filesystem, or operating-system
   change alone does not invalidate the pair. Do not copy stale `-wal`, `-shm`,
   or rollback-journal files independently.
4. Set `IRS_DB_PATH`, `IRS_GRANT_WORK_DB_PATH`, `FAC_DB_PATH`,
   `IRS_SCREENING_DB_PATH`, `IRS_RISK_NETWORK_DB_PATH`, and any applicable XML
   or OLMS paths for the destination.
5. Keep the old source inventory as a rollback copy. Build a new sidecar under a
   temporary name instead of overwriting the only known-good inventory.
6. Review the scan summary and CSV reports, then spot-check filing downloads in
   Nonprofit Deep Dive.
7. Set `IRS_XML_INVENTORY_PATH` to the new sidecar and restart the application.
8. Archive the old sidecar only after the new installation is accepted.

Example Linux settings:

```dotenv
IRS_DB_PATH=/var/lib/irs990-tool/db/irs990.db
IRS_GRANT_WORK_DB_PATH=/var/lib/irs990-tool/db/grant_matching_work.db
FAC_DB_PATH=/var/lib/irs990-tool/db/fac_audits.db
IRS_SCREENING_DB_PATH=/var/lib/irs990-tool/db/screening_data.db
IRS_RISK_NETWORK_DB_PATH=/var/lib/irs990-tool/db/risk_network.db
IRS_XML_ROOT=/srv/irs990-data/xml
IRS_XML_INVENTORY_PATH=/var/lib/irs990-tool/db/irs990_sources.new.db
OLMS_DB_PATH=/var/lib/irs990-tool/db/olms.db
```

Before the first move of a legacy risk-network sidecar, upgrade its physical
lineage metadata in place while the accepted main/sidecar pair is still on the
source machine. The migration is metadata-only; it does not rewrite network
edges. Run its read-only plan, review the paths and invariants, and then run the
confirmed apply command shown below:

```powershell
.\.venv\Scripts\python.exe migrate_risk_network_portability.py plan `
  --db db\irs990.db --sidecar db\risk_network.db
.\.venv\Scripts\python.exe migrate_risk_network_portability.py apply `
  --db db\irs990.db --sidecar db\risk_network.db --yes
```

Do not edit identity or sidecar metadata by hand. The migration records the
adopted legacy preimage, installs the portable identity, refreshes compatibility
metadata, checks that network table counts did not change, and writes an ignored
receipt for audit/recovery. It is resumable if interrupted between its guarded
main-database and sidecar phases.

Run this migration before any newly instrumented append, resolver, applied-layer,
or manual main-database writer. Such a write changes the legacy file stamp and
can make safe adoption of the old sidecar impossible, requiring a full rebuild.

After checkpointing, hash the main and risk-network files on the source and
verify those hashes after transfer. Then run this read-only destination check:

```bash
python -c "from queries._risk_network import available,risk_network_path; print(risk_network_path(), available())"
```

It must print `True`. A copied portable pair remains valid even though Windows
and Linux report different paths, device numbers, inodes, and modification
times. If it prints `False`, keep the network panel unavailable and diagnose a
partial copy, nonempty SQLite auxiliary, identity/revision mismatch, or stale
sidecar. Never make it pass by changing metadata. Rebuild only when the files
are not the exact validated pair or the main database has changed:

```bash
mkdir -p /var/lib/irs990-tool/db/_risk_network_tmp
export TMPDIR=/var/lib/irs990-tool/db/_risk_network_tmp
export SQLITE_TMPDIR=/var/lib/irs990-tool/db/_risk_network_tmp
python build_risk_network.py plan \
  --db /var/lib/irs990-tool/db/irs990.db \
  --sidecar /var/lib/irs990-tool/db/risk_network.db \
  --full
python build_risk_network.py rebuild \
  --db /var/lib/irs990-tool/db/irs990.db \
  --sidecar /var/lib/irs990-tool/db/risk_network.db \
  --full --yes
```

Plan for at least 180 GiB free on the destination filesystem during that full
rebuild.

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
- The main/risk-network file hashes match the source-machine transfer manifest,
  no populated `-wal` or rollback-journal file is present, and the risk-network
  `available()` smoke check returns `True`.

See the [Database Build Guide](database-build.md) for scanner classifications,
conflict analysis, and quarantine procedures.
