#!/usr/bin/env python3
"""Move enhanced grant-matching working tables from the main DB to a sidecar DB."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_PROJECT_DIR = os.getenv("IRS_PROJECT_DIR", r"C:\projects\irs990-tool")
DEFAULT_DB = os.getenv("IRS_DB_PATH", str(Path(DEFAULT_PROJECT_DIR) / "db" / "irs990.db"))
DEFAULT_WORK_DB_NAME = "grant_matching_work.db"
WORK_SCHEMA = "grant_work"

WORK_TABLES = [
    "org_identity",
    "org_identity_token",
    "grant_recipient_signature",
    "grant_recipient_signature_grant",
    "grant_recipient_ai_candidate",
]
FTS_TABLE = "org_identity_fts"


def default_work_db_path(db_path: str) -> str:
    env_path = os.getenv("IRS_GRANT_WORK_DB_PATH")
    if env_path:
        return env_path
    return str(Path(db_path).resolve().parent / DEFAULT_WORK_DB_NAME)


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-300000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def table_exists(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_schema WHERE type IN ('table','view') AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def object_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or not row["sql"]:
        raise RuntimeError(f"Missing table schema in main DB: {table}")
    return str(row["sql"])


def qualified_create_table_sql(sql: str, schema: str, table: str) -> str:
    pattern = rf"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"{re.escape(table)}\"|{re.escape(table)})"
    return re.sub(pattern, f"CREATE TABLE {schema}.{table}", sql, count=1, flags=re.IGNORECASE)


def qualified_create_virtual_table_sql(sql: str, schema: str, table: str) -> str:
    pattern = rf"^CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"{re.escape(table)}\"|{re.escape(table)})"
    return re.sub(pattern, f"CREATE VIRTUAL TABLE {schema}.{table}", sql, count=1, flags=re.IGNORECASE)


def qualified_create_index_sql(sql: str, schema: str, table: str) -> str:
    match = re.match(
        rf"^CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s]+)\s+ON\s+(?:\"{re.escape(table)}\"|{re.escape(table)})(.*)$",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise RuntimeError(f"Could not rewrite index SQL for sidecar: {sql}")
    unique = match.group(1) or ""
    index_name = match.group(2)
    suffix = match.group(3)
    return f"CREATE {unique}INDEX IF NOT EXISTS {schema}.{index_name} ON {table}{suffix}"


def copy_table(conn: sqlite3.Connection, table: str, dry_run: bool = False) -> None:
    print(f"Creating sidecar table {table}...", flush=True)
    if not dry_run:
        conn.execute(qualified_create_table_sql(object_sql(conn, table), WORK_SCHEMA, table))
        conn.execute(f"INSERT INTO {WORK_SCHEMA}.{table} SELECT * FROM main.{table}")
        conn.commit()


def create_sidecar_indexes(conn: sqlite3.Connection, tables: Iterable[str], dry_run: bool = False) -> None:
    rows = conn.execute(
        """
        SELECT name, tbl_name, sql
        FROM main.sqlite_schema
        WHERE type='index'
          AND sql IS NOT NULL
          AND tbl_name IN ({})
        ORDER BY tbl_name, name
        """.format(",".join("?" for _ in tables)),
        list(tables),
    ).fetchall()
    for row in rows:
        print(f"Creating sidecar index {row['name']} on {row['tbl_name']}...", flush=True)
        if not dry_run:
            conn.execute(qualified_create_index_sql(str(row["sql"]), WORK_SCHEMA, str(row["tbl_name"])))
            conn.commit()


def create_sidecar_fts(conn: sqlite3.Connection, dry_run: bool = False) -> None:
    if not table_exists(conn, "main", FTS_TABLE):
        print(f"Skipping {FTS_TABLE}; main DB does not have it.", flush=True)
        return
    print(f"Creating sidecar FTS table {FTS_TABLE}...", flush=True)
    if not dry_run:
        conn.execute(qualified_create_virtual_table_sql(object_sql(conn, FTS_TABLE), WORK_SCHEMA, FTS_TABLE))
        print(f"Rebuilding sidecar FTS table {FTS_TABLE}...", flush=True)
        conn.execute(f"INSERT INTO {WORK_SCHEMA}.{FTS_TABLE}({FTS_TABLE}) VALUES('rebuild')")
        conn.commit()


def count_rows(conn: sqlite3.Connection, schema: str, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {schema}.{table}").fetchone()[0])


def verify_counts(conn: sqlite3.Connection, tables: Iterable[str]) -> None:
    for table in tables:
        main_count = count_rows(conn, "main", table)
        side_count = count_rows(conn, WORK_SCHEMA, table)
        print(f"{table}: main={main_count:,} sidecar={side_count:,}", flush=True)
        if main_count != side_count:
            raise RuntimeError(f"Count mismatch for {table}: main={main_count}, sidecar={side_count}")
    if table_exists(conn, "main", FTS_TABLE) and table_exists(conn, WORK_SCHEMA, FTS_TABLE):
        side_fts_count = count_rows(conn, WORK_SCHEMA, FTS_TABLE)
        identity_count = count_rows(conn, WORK_SCHEMA, "org_identity")
        print(f"{FTS_TABLE}: sidecar={side_fts_count:,} identity_rows={identity_count:,}", flush=True)
        if side_fts_count != identity_count:
            raise RuntimeError(f"FTS count mismatch: {side_fts_count} != {identity_count}")


def drop_main_work_tables(conn: sqlite3.Connection, dry_run: bool = False) -> None:
    for table in [FTS_TABLE, *reversed(WORK_TABLES)]:
        if table_exists(conn, "main", table):
            print(f"Dropping main.{table}...", flush=True)
            if not dry_run:
                conn.execute(f"DROP TABLE main.{table}")
                conn.commit()


def analyze_sidecar(conn: sqlite3.Connection, dry_run: bool = False) -> None:
    for table in [*WORK_TABLES, FTS_TABLE]:
        if table_exists(conn, WORK_SCHEMA, table):
            print(f"Analyzing sidecar {table}...", flush=True)
            if not dry_run:
                conn.execute(f"ANALYZE {WORK_SCHEMA}.{table}")
    if not dry_run:
        conn.commit()


def migrate(args: argparse.Namespace) -> None:
    db_path = str(Path(args.db).resolve())
    work_db_path = str(Path(args.work_db or default_work_db_path(db_path)).resolve())
    if Path(db_path) == Path(work_db_path):
        raise RuntimeError("--work-db must be a separate database file")
    if Path(work_db_path).exists() and not (args.overwrite_sidecar or args.dry_run):
        raise RuntimeError(f"Sidecar DB already exists: {work_db_path}. Use --overwrite-sidecar to replace it.")

    print(f"Main DB: {db_path}", flush=True)
    print(f"Sidecar DB: {work_db_path}", flush=True)
    print(f"Drop main working tables: {bool(args.drop_main)}", flush=True)
    if args.drop_main and not args.yes:
        raise RuntimeError("--drop-main requires --yes")

    if args.overwrite_sidecar and Path(work_db_path).exists() and not args.dry_run:
        Path(work_db_path).unlink()
    if not args.dry_run:
        Path(work_db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    try:
        missing = [table for table in WORK_TABLES if not table_exists(conn, "main", table)]
        if missing:
            raise RuntimeError(f"Main DB is missing expected working table(s): {', '.join(missing)}")

        if args.dry_run:
            for table in WORK_TABLES:
                copy_table(conn, table, dry_run=True)
            create_sidecar_fts(conn, dry_run=True)
            create_sidecar_indexes(conn, WORK_TABLES, dry_run=True)
            if args.drop_main:
                drop_main_work_tables(conn, dry_run=True)
            print("Migration complete.", flush=True)
            return

        conn.execute(f"ATTACH DATABASE ? AS {WORK_SCHEMA}", (work_db_path,))
        conn.execute(f"PRAGMA {WORK_SCHEMA}.journal_mode=WAL")
        conn.execute(f"PRAGMA {WORK_SCHEMA}.synchronous=NORMAL")

        if not args.dry_run:
            for table in [FTS_TABLE, *WORK_TABLES]:
                conn.execute(f"DROP TABLE IF EXISTS {WORK_SCHEMA}.{table}")
            conn.commit()

        for table in WORK_TABLES:
            copy_table(conn, table, dry_run=args.dry_run)
        create_sidecar_fts(conn, dry_run=args.dry_run)
        create_sidecar_indexes(conn, WORK_TABLES, dry_run=args.dry_run)

        if not args.dry_run:
            verify_counts(conn, WORK_TABLES)
        analyze_sidecar(conn, dry_run=args.dry_run)

        if args.drop_main:
            drop_main_work_tables(conn, dry_run=args.dry_run)

        print("Migration complete.", flush=True)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Move enhanced grant-matching working tables to a sidecar SQLite DB.")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Main SQLite database path (default: {DEFAULT_DB})")
    parser.add_argument("--work-db", default=None, help="Sidecar DB path; defaults to IRS_GRANT_WORK_DB_PATH or grant_matching_work.db beside --db")
    parser.add_argument("--drop-main", action="store_true", help="Drop moved working tables from the main DB after sidecar counts verify")
    parser.add_argument("--overwrite-sidecar", action="store_true", help="Replace an existing sidecar DB")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing either DB")
    parser.add_argument("--yes", action="store_true", help="Required with --drop-main")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        migrate(args)
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
