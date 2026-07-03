#!/usr/bin/env python3
"""
Backfill expanded lobbying and political-campaign fields from IRS XML files.

This script is intentionally narrow: it updates the expanded Schedule C table,
Schedule C supplemental explanations, and new 990-PF political/legislative
indicators without rebuilding the full IRS 990 database.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from rebuild_irs990_slim_clean import (
    IRS990PF_COLS,
    SCHEDC_COLS,
    build_schema,
    calculated_lobbying_expense,
    db_connect,
    ensure_schema_columns,
    extract_file,
    object_id_from_filing_id,
)


def iter_xml_files(root: Path) -> Iterable[str]:
    for base, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(".xml"):
                yield str(Path(base, fn))


def existing_filing_keys(conn: sqlite3.Connection) -> Tuple[set, set]:
    filing_ids = set()
    object_ids = set()
    try:
        rows = conn.execute("SELECT filing_id FROM returns")
    except sqlite3.OperationalError:
        return filing_ids, object_ids
    for (filing_id,) in rows:
        if filing_id:
            filing_ids.add(filing_id)
            object_ids.add(object_id_from_filing_id(filing_id))
    return filing_ids, object_ids


def select_xml_files(xml_dir: Path, conn: sqlite3.Connection, all_filings: bool, limit: int = 0) -> Tuple[List[str], Dict[str, int]]:
    existing_ids, existing_object_ids = existing_filing_keys(conn)
    filter_existing = bool(existing_ids) and not all_filings
    selected: List[str] = []
    seen_input_object_ids = set()
    stats = {
        "total": 0,
        "selected": 0,
        "skipped_not_in_db": 0,
        "skipped_duplicate_input": 0,
    }

    for fp in iter_xml_files(xml_dir):
        stats["total"] += 1
        stem = Path(fp).stem
        object_id = object_id_from_filing_id(stem)
        if filter_existing and stem not in existing_ids and object_id not in existing_object_ids:
            stats["skipped_not_in_db"] += 1
            continue
        if object_id in seen_input_object_ids:
            stats["skipped_duplicate_input"] += 1
            continue
        seen_input_object_ids.add(object_id)
        selected.append(fp)
        stats["selected"] += 1
        if limit and len(selected) >= limit:
            break
    return selected, stats


def has_nonblank(row: Dict[str, object], ignore: Sequence[str] = ("filing_id",)) -> bool:
    ignored = set(ignore)
    return any(v not in (None, "") for k, v in row.items() if k not in ignored)


def insert_singleton(conn: sqlite3.Connection, table: str, filing_id: str, row: Dict[str, object], cols: Sequence[str]) -> None:
    vals = [filing_id] + [row.get(c) for c in cols]
    placeholders = ",".join("?" for _ in vals)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} (filing_id,{','.join(cols)}) VALUES ({placeholders})",
        vals,
    )


def apply_extracted(conn: sqlite3.Connection, row: Dict[str, object]) -> Dict[str, int]:
    counts = {"schedule_c": 0, "schedule_c_supplemental": 0, "pf_root": 0}
    header = row["header"]
    filing_id = header["filing_id"]

    schedule_c = row.get("irs990_schedule_c_root") or {}
    if has_nonblank(schedule_c):
        insert_singleton(conn, "irs990_schedule_c_root", filing_id, schedule_c, SCHEDC_COLS)
        if not str(header.get("return_type") or "").startswith(("990PF", "990T")):
            conn.execute(
                "UPDATE core_hot SET lobbying_expense = ? WHERE filing_id = ?",
                [calculated_lobbying_expense(schedule_c), filing_id],
            )
        conn.execute("DELETE FROM irs990_schedule_c_supplemental_info WHERE filing_id = ?", [filing_id])
        counts["schedule_c"] += 1

        for supp in row.get("irs990_schedule_c_supplemental_info") or []:
            conn.execute(
                """
                INSERT INTO irs990_schedule_c_supplemental_info (
                  filing_id, form_and_line_reference_desc, explanation_txt
                ) VALUES (?,?,?)
                """,
                [
                    supp.get("filing_id"),
                    supp.get("form_and_line_reference_desc"),
                    supp.get("explanation_txt"),
                ],
            )
            counts["schedule_c_supplemental"] += 1

    pf_root = row.get("irs990_pf_root") or {}
    if str(header.get("return_type") or "").startswith("990PF") and has_nonblank(pf_root):
        insert_singleton(conn, "irs990_pf_root", filing_id, pf_root, IRS990PF_COLS)
        counts["pf_root"] += 1

    return counts


def import_lobbying_data(
    db_path: Path,
    xml_dir: Path,
    workers: int,
    chunksize: int,
    commit_every: int,
    all_filings: bool = False,
    limit: int = 0,
    dry_run: bool = False,
) -> Dict[str, int]:
    conn = db_connect(db_path)
    totals = {
        "files_seen": 0,
        "files_selected": 0,
        "files_processed": 0,
        "errors": 0,
        "schedule_c": 0,
        "schedule_c_supplemental": 0,
        "pf_root": 0,
    }

    try:
        build_schema(conn)
        ensure_schema_columns(conn)
        files, stats = select_xml_files(xml_dir, conn, all_filings=all_filings, limit=limit)
        totals["files_seen"] = stats["total"]
        totals["files_selected"] = stats["selected"]
        print(
            f"[select] XML files seen: {stats['total']:,}; selected: {stats['selected']:,}; "
            f"skipped not in DB: {stats['skipped_not_in_db']:,}; "
            f"skipped duplicate input: {stats['skipped_duplicate_input']:,}"
        )
        if dry_run:
            print("[dry-run] schema checked and files selected; no rows will be written")

        if not files:
            return totals

        iterator = map(extract_file, files)
        executor: Optional[ProcessPoolExecutor] = None
        if workers > 1:
            executor = ProcessPoolExecutor(max_workers=workers)
            iterator = executor.map(extract_file, files, chunksize=chunksize)

        try:
            for row in iterator:
                if "error" in row:
                    totals["errors"] += 1
                    print(f"[error] {row['error']}", file=sys.stderr)
                    continue

                totals["files_processed"] += 1
                if not dry_run:
                    counts = apply_extracted(conn, row)
                    for key, value in counts.items():
                        totals[key] += value

                    if totals["files_processed"] % commit_every == 0:
                        conn.commit()
                        print(f"[import] processed {totals['files_processed']:,}/{len(files):,}")
                elif totals["files_processed"] % commit_every == 0:
                    print(f"[dry-run] parsed {totals['files_processed']:,}/{len(files):,}")
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    return totals


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Backfill expanded Schedule C and 990-PF lobbying/political fields.")
    ap.add_argument("--db", required=True, help="SQLite database to update.")
    ap.add_argument("--xml-dir", required=True, help="Root directory containing IRS XML files.")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--chunksize", type=int, default=25)
    ap.add_argument("--commit-every", type=int, default=1000)
    ap.add_argument("--all-filings", action="store_true", help="Process all XML files, even if their filing_id is not in returns.")
    ap.add_argument("--limit", type=int, default=0, help="Optional cap on selected XML files for testing.")
    ap.add_argument("--dry-run", action="store_true", help="Check schema and parse selected XML files without writing rows.")
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db)
    xml_dir = Path(args.xml_dir)

    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2
    if not xml_dir.exists():
        print(f"ERROR: XML directory not found: {xml_dir}", file=sys.stderr)
        return 2

    totals = import_lobbying_data(
        db_path=db_path,
        xml_dir=xml_dir,
        workers=args.workers,
        chunksize=args.chunksize,
        commit_every=args.commit_every,
        all_filings=args.all_filings,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print("[done] " + ", ".join(f"{k}={v:,}" for k, v in totals.items()))
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
