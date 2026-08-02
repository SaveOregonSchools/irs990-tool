#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build and maintain a sidecar inventory of IRS 990 XML source files.

The scanner records every XML file under a source directory, identifies duplicate
object IDs, hashes duplicate candidates, and optionally quarantines exact content
duplicates. It can also compare the source inventory with the production returns
table without modifying the production database.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from rebuild_irs990_slim_clean import object_id_from_filing_id


DEFAULT_XML_DIR = Path(r"C:\Projects\irsdb\xml")
DEFAULT_SIDECAR_DB = Path("db") / "irs990_sources.db"
CHUNK_SIZE = 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iter_xml_files(root: Path) -> Iterable[Path]:
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(dirs, key=str.casefold)
        for fn in sorted(files, key=str.casefold):
            if fn.lower().endswith(".xml"):
                yield Path(base, fn)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def canonical_xml_sha256(path: Path) -> str:
    """Hash parsed XML while ignoring indentation, line endings, and attr order."""
    root = ET.parse(str(path)).getroot()
    h = hashlib.sha256()

    def visit(node: ET.Element) -> None:
        h.update(b"<")
        h.update(local_name(node.tag).encode("utf-8", errors="replace"))
        for key, value in sorted(node.attrib.items(), key=lambda kv: local_name(kv[0])):
            h.update(b" ")
            h.update(local_name(key).encode("utf-8", errors="replace"))
            h.update(b"=")
            h.update(str(value).strip().encode("utf-8", errors="replace"))
        text = (node.text or "").strip()
        if text:
            h.update(b">")
            h.update(text.encode("utf-8", errors="replace"))
        for child in list(node):
            visit(child)
        h.update(b"</")
        h.update(local_name(node.tag).encode("utf-8", errors="replace"))
        h.update(b">")

    visit(root)
    return h.hexdigest()


def path_depth(path: str) -> int:
    return len(Path(path).parts)


def norm_rel_for_loaded_match(path: str) -> str:
    return path.replace("/", "\\").lower().replace("_old\\", "\\")


def choose_primary(rows: Sequence[Dict[str, object]]) -> Dict[str, object]:
    return sorted(rows, key=lambda r: (path_depth(str(r["source_file"])), str(r["source_file"]).casefold()))[0]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def build_schema(conn: sqlite3.Connection) -> None:
    conn.execute("DROP VIEW IF EXISTS v_source_file_audit")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scan_meta (
          key TEXT PRIMARY KEY,
          value TEXT
        );

        CREATE TABLE IF NOT EXISTS source_files (
          source_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
          scan_id TEXT NOT NULL,
          xml_root TEXT NOT NULL,
          source_file TEXT NOT NULL UNIQUE,
          relative_path TEXT NOT NULL,
          filename TEXT NOT NULL,
          filing_id TEXT NOT NULL,
          object_id TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          sha256 TEXT,
          canonical_sha256 TEXT,
          duplicate_status TEXT NOT NULL DEFAULT 'unique',
          duplicate_group_key TEXT,
          keep_source_file TEXT,
          quarantine_status TEXT,
          quarantine_file TEXT,
          scanned_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS loaded_filings (
          filing_id TEXT PRIMARY KEY,
          object_id TEXT NOT NULL,
          source_file TEXT,
          ein TEXT,
          return_type TEXT,
          tax_year INTEGER,
          return_ts TEXT,
          imported_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_source_object ON source_files(object_id);
        CREATE INDEX IF NOT EXISTS idx_source_hash ON source_files(sha256);
        CREATE INDEX IF NOT EXISTS idx_source_duplicate ON source_files(duplicate_status, object_id);
        CREATE INDEX IF NOT EXISTS idx_loaded_object ON loaded_filings(object_id);
        """
    )
    ensure_schema_columns(conn)
    conn.executescript(
        """
        CREATE VIEW IF NOT EXISTS v_source_file_audit AS
        SELECT
          s.source_file,
          s.relative_path,
          s.filing_id,
          s.object_id,
          s.size_bytes,
          s.sha256,
          s.canonical_sha256,
          s.duplicate_status,
          s.keep_source_file,
          CASE WHEN lf_exact.filing_id IS NOT NULL THEN 1 ELSE 0 END AS loaded_by_exact_filing_id,
          CASE WHEN lf_object.filing_id IS NOT NULL THEN 1 ELSE 0 END AS loaded_by_object_id,
          lf_object.filing_id AS loaded_filing_id,
          lf_object.source_file AS loaded_source_file,
          lf_object.ein,
          lf_object.return_type,
          lf_object.tax_year
        FROM source_files s
        LEFT JOIN loaded_filings lf_exact ON lf_exact.filing_id = s.filing_id
        LEFT JOIN loaded_filings lf_object ON lf_object.object_id = s.object_id;
        """
    )
    conn.commit()


def ensure_schema_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1].lower() for row in conn.execute("PRAGMA table_info(source_files)")}
    if "canonical_sha256" not in existing:
        conn.execute("ALTER TABLE source_files ADD COLUMN canonical_sha256 TEXT")


def reset_scan_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM source_files")
    conn.execute("DELETE FROM loaded_filings")
    conn.commit()


def collect_files(xml_dir: Path, scan_id: str, hash_mode: str, scanned_at: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for p in iter_xml_files(xml_dir):
        st = p.stat()
        filing_id = p.stem
        rows.append(
            {
                "scan_id": scan_id,
                "xml_root": str(xml_dir),
                "source_file": str(p),
                "relative_path": str(p.relative_to(xml_dir)),
                "filename": p.name,
                "filing_id": filing_id,
                "object_id": object_id_from_filing_id(filing_id),
                "size_bytes": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
                "sha256": None,
                "canonical_sha256": None,
                "duplicate_status": "unique",
                "duplicate_group_key": None,
                "keep_source_file": None,
                "quarantine_status": None,
                "quarantine_file": None,
                "scanned_at": scanned_at,
            }
        )

    by_object: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_object[str(row["object_id"])].append(row)

    for object_id, group in by_object.items():
        if len(group) == 1:
            if hash_mode == "all":
                group[0]["sha256"] = sha256_file(Path(str(group[0]["source_file"])))
            continue

        should_hash = hash_mode in {"all", "candidates"}
        if should_hash:
            for row in group:
                row["sha256"] = sha256_file(Path(str(row["source_file"])))

        by_hash: Dict[Optional[str], List[Dict[str, object]]] = defaultdict(list)
        for row in group:
            by_hash[row["sha256"]].append(row)

        hashes = {k for k in by_hash if k}
        has_exact_dupes = any(k and len(v) > 1 for k, v in by_hash.items())
        has_conflicts = len(hashes) > 1 or not hashes

        for sha, hash_group in by_hash.items():
            primary = choose_primary(hash_group)
            for row in hash_group:
                row["duplicate_group_key"] = object_id if sha is None else f"{object_id}:{sha}"
                row["keep_source_file"] = primary["source_file"]
                if len(hash_group) == 1:
                    row["duplicate_status"] = "object_id_conflict"
                elif row["source_file"] == primary["source_file"]:
                    row["duplicate_status"] = "primary_duplicate_group"
                elif sha:
                    row["duplicate_status"] = "exact_duplicate"
                else:
                    row["duplicate_status"] = "object_id_conflict"

        if has_conflicts and not has_exact_dupes:
            for row in group:
                row["duplicate_status"] = "object_id_conflict"

    return rows


def insert_source_files(conn: sqlite3.Connection, rows: Sequence[Dict[str, object]]) -> None:
    cols = [
        "scan_id",
        "xml_root",
        "source_file",
        "relative_path",
        "filename",
        "filing_id",
        "object_id",
        "size_bytes",
        "mtime_ns",
        "sha256",
        "canonical_sha256",
        "duplicate_status",
        "duplicate_group_key",
        "keep_source_file",
        "quarantine_status",
        "quarantine_file",
        "scanned_at",
    ]
    sql = f"INSERT INTO source_files ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
    conn.executemany(sql, [[row.get(c) for c in cols] for row in rows])
    conn.commit()


def analyze_conflicts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        """
        SELECT source_file_id, object_id, source_file
        FROM source_files
        WHERE duplicate_status = 'object_id_conflict'
        ORDER BY object_id, source_file COLLATE NOCASE
        """
    ).fetchall()
    updated = 0
    errors = 0
    for row in rows:
        try:
            canonical_hash = canonical_xml_sha256(Path(row["source_file"]))
        except Exception as e:
            canonical_hash = f"ERROR:{type(e).__name__}:{e}"
            errors += 1
        conn.execute(
            "UPDATE source_files SET canonical_sha256 = ? WHERE source_file_id = ?",
            (canonical_hash, row["source_file_id"]),
        )
        updated += 1
        if updated % 10000 == 0:
            conn.commit()
            print(f"[conflicts] canonicalized {updated:,}/{len(rows):,}")
    conn.commit()
    equivalent_groups = conn.execute(
        """
        SELECT COUNT(*)
        FROM (
          SELECT object_id
          FROM source_files
          WHERE duplicate_status = 'object_id_conflict'
          GROUP BY object_id
          HAVING COUNT(DISTINCT canonical_sha256) = 1
             AND MIN(canonical_sha256) NOT LIKE 'ERROR:%'
        )
        """
    ).fetchone()[0]
    different_groups = conn.execute(
        """
        SELECT COUNT(*)
        FROM (
          SELECT object_id
          FROM source_files
          WHERE duplicate_status = 'object_id_conflict'
          GROUP BY object_id
          HAVING COUNT(DISTINCT canonical_sha256) > 1
              OR MIN(canonical_sha256) LIKE 'ERROR:%'
        )
        """
    ).fetchone()[0]
    return {
        "conflict_files_canonicalized": updated,
        "conflict_canonical_errors": errors,
        "conflict_equivalent_groups": equivalent_groups,
        "conflict_different_groups": different_groups,
    }


def import_loaded_filings(conn: sqlite3.Connection, main_db: Optional[Path], imported_at: str) -> int:
    if not main_db:
        return 0
    main_db = main_db.expanduser().resolve()
    if not main_db.exists():
        raise FileNotFoundError(f"main database not found: {main_db}")

    prod = sqlite3.connect(f"file:{main_db.as_posix()}?mode=ro", uri=True)
    try:
        prod.row_factory = sqlite3.Row
        table = prod.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='returns' LIMIT 1"
        ).fetchone()
        if not table:
            raise RuntimeError(f"main database has no returns table: {main_db}")
        rows = prod.execute(
            """
            SELECT filing_id, source_file, ein, return_type, tax_year, return_ts
            FROM returns
            WHERE filing_id IS NOT NULL AND filing_id <> ''
            """
        ).fetchall()
    finally:
        prod.close()

    conn.executemany(
        """
        INSERT OR REPLACE INTO loaded_filings
          (filing_id, object_id, source_file, ein, return_type, tax_year, return_ts, imported_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        [
            (
                r["filing_id"],
                object_id_from_filing_id(r["filing_id"]),
                r["source_file"],
                r["ein"],
                r["return_type"],
                r["tax_year"],
                r["return_ts"],
                imported_at,
            )
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


def write_csv(conn: sqlite3.Connection, path: Path, sql: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(sql).fetchall()
    with path.open("w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows([dict(r) for r in rows])
        else:
            f.write("")
    return len(rows)


def write_conflict_groups_csv(conn: sqlite3.Connection, path: Path) -> int:
    return write_csv(
        conn,
        path,
        """
        SELECT
          object_id,
          COUNT(*) AS file_count,
          COUNT(DISTINCT sha256) AS byte_hash_count,
          COUNT(DISTINCT canonical_sha256) AS canonical_hash_count,
          MIN(size_bytes) AS min_size_bytes,
          MAX(size_bytes) AS max_size_bytes,
          CASE
            WHEN COUNT(DISTINCT canonical_sha256) = 1
             AND MIN(canonical_sha256) NOT LIKE 'ERROR:%'
            THEN 'canonical_equivalent'
            ELSE 'canonical_different'
          END AS conflict_kind,
          GROUP_CONCAT(source_file, char(10)) AS source_files
        FROM source_files
        WHERE duplicate_status = 'object_id_conflict'
        GROUP BY object_id
        ORDER BY conflict_kind, object_id
        """,
    )


def conflict_resolution_rows(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
          s.source_file_id,
          s.object_id,
          s.source_file,
          s.relative_path,
          s.size_bytes,
          s.sha256,
          s.canonical_sha256,
          lf.source_file AS loaded_source_file
        FROM source_files s
        JOIN loaded_filings lf ON lf.object_id = s.object_id
        WHERE s.duplicate_status = 'object_id_conflict'
        ORDER BY s.object_id, s.relative_path COLLATE NOCASE
        """
    ).fetchall()
    by_object: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_object[row["object_id"]].append(row)

    out: List[Dict[str, object]] = []
    for object_id, group in by_object.items():
        exact_matches = [
            row for row in group
            if (row["loaded_source_file"] or "").replace("/", "\\").lower().endswith(
                row["relative_path"].replace("/", "\\").lower()
            )
        ]
        normalized_matches = [
            row for row in group
            if norm_rel_for_loaded_match(row["loaded_source_file"] or "").endswith(
                norm_rel_for_loaded_match(row["relative_path"])
            )
        ]
        if len(exact_matches) == 1:
            keep_id = exact_matches[0]["source_file_id"]
            strategy = "loaded_relative_path"
            status = "resolved"
        elif len(normalized_matches) == 1:
            keep_id = normalized_matches[0]["source_file_id"]
            strategy = "loaded_relative_path_normalized_old"
            status = "resolved"
        else:
            keep_id = None
            strategy = "manual_review"
            status = "unresolved"

        for row in group:
            action = "review"
            if status == "resolved":
                action = "keep" if row["source_file_id"] == keep_id else "quarantine_conflict"
            out.append(
                {
                    "object_id": object_id,
                    "resolution_status": status,
                    "resolution_strategy": strategy,
                    "recommended_action": action,
                    "source_file_id": row["source_file_id"],
                    "relative_path": row["relative_path"],
                    "source_file": row["source_file"],
                    "loaded_source_file": row["loaded_source_file"],
                    "size_bytes": row["size_bytes"],
                    "sha256": row["sha256"],
                    "canonical_sha256": row["canonical_sha256"],
                }
            )
    return out


def write_conflict_resolution_csv(conn: sqlite3.Connection, path: Path) -> int:
    rows = conflict_resolution_rows(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        else:
            f.write("")
    return len(rows)


def validate_quarantine_path(xml_dir: Path, quarantine_dir: Path) -> Path:
    q = quarantine_dir.expanduser().resolve()
    root = xml_dir.expanduser().resolve()
    if q == root or root in q.parents:
        raise ValueError("quarantine directory must be outside the XML source directory")
    return q


def quarantine_exact_duplicates(conn: sqlite3.Connection, xml_dir: Path, quarantine_dir: Path, yes: bool) -> int:
    if not yes:
        raise ValueError("--quarantine-duplicates requires --yes")
    quarantine_dir = validate_quarantine_path(xml_dir, quarantine_dir)
    rows = conn.execute(
        """
        SELECT source_file_id, source_file, relative_path
        FROM source_files
        WHERE duplicate_status = 'exact_duplicate'
        ORDER BY source_file COLLATE NOCASE
        """
    ).fetchall()
    moved = 0
    for row in rows:
        src = Path(row["source_file"])
        if not src.exists():
            conn.execute(
                "UPDATE source_files SET quarantine_status=? WHERE source_file_id=?",
                ("missing_before_quarantine", row["source_file_id"]),
            )
            continue
        dest = quarantine_dir / row["relative_path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}.{row['source_file_id']}{dest.suffix}")
        shutil.move(str(src), str(dest))
        conn.execute(
            """
            UPDATE source_files
            SET quarantine_status = 'moved', quarantine_file = ?
            WHERE source_file_id = ?
            """,
            (str(dest), row["source_file_id"]),
        )
        moved += 1
    conn.commit()
    return moved


def quarantine_resolved_conflicts(conn: sqlite3.Connection, xml_dir: Path, quarantine_dir: Path, yes: bool) -> int:
    if not yes:
        raise ValueError("--quarantine-resolved-conflicts requires --yes")
    loaded_count = conn.execute("SELECT COUNT(*) FROM loaded_filings").fetchone()[0]
    if not loaded_count:
        raise ValueError("--quarantine-resolved-conflicts requires --main-db so loaded source paths are available")
    quarantine_dir = validate_quarantine_path(xml_dir, quarantine_dir)
    rows = [r for r in conflict_resolution_rows(conn) if r["recommended_action"] == "quarantine_conflict"]
    moved = 0
    for row in rows:
        src = Path(str(row["source_file"]))
        if not src.exists():
            conn.execute(
                "UPDATE source_files SET quarantine_status=? WHERE source_file_id=?",
                ("missing_before_conflict_quarantine", row["source_file_id"]),
            )
            continue
        dest = quarantine_dir / str(row["relative_path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}.{row['source_file_id']}{dest.suffix}")
        shutil.move(str(src), str(dest))
        conn.execute(
            """
            UPDATE source_files
            SET quarantine_status = 'conflict_moved', quarantine_file = ?
            WHERE source_file_id = ?
            """,
            (str(dest), row["source_file_id"]),
        )
        moved += 1
    conn.commit()
    return moved


def summarize(conn: sqlite3.Connection) -> Dict[str, int]:
    out = {
        "source_files": conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
        "loaded_filings": conn.execute("SELECT COUNT(*) FROM loaded_filings").fetchone()[0],
    }
    for status, count in conn.execute(
        "SELECT duplicate_status, COUNT(*) FROM source_files GROUP BY duplicate_status"
    ):
        out[f"status_{status}"] = count
    return out


def run(args: argparse.Namespace) -> Dict[str, int]:
    xml_dir = Path(args.xml_dir).expanduser().resolve()
    if not xml_dir.exists():
        raise FileNotFoundError(f"XML directory not found: {xml_dir}")
    if not xml_dir.is_dir():
        raise NotADirectoryError(f"XML path is not a directory: {xml_dir}")

    sidecar_db = Path(args.sidecar_db).expanduser().resolve()
    scanned_at = utc_now()
    scan_id = scanned_at.replace(":", "").replace("+", "Z")

    conn = connect(sidecar_db)
    try:
        build_schema(conn)
        reset_scan_tables(conn)
        rows = collect_files(xml_dir, scan_id, args.hash_mode, scanned_at)
        insert_source_files(conn, rows)
        loaded = import_loaded_filings(conn, Path(args.main_db) if args.main_db else None, scanned_at)
        conn.execute("INSERT OR REPLACE INTO scan_meta VALUES (?,?)", ("last_scan_id", scan_id))
        conn.execute("INSERT OR REPLACE INTO scan_meta VALUES (?,?)", ("last_scanned_at", scanned_at))
        conn.execute("INSERT OR REPLACE INTO scan_meta VALUES (?,?)", ("last_xml_root", str(xml_dir)))
        conn.commit()

        if args.report_csv:
            count = write_csv(
                conn,
                Path(args.report_csv),
                "SELECT * FROM v_source_file_audit ORDER BY object_id, source_file COLLATE NOCASE",
            )
            print(f"[report] wrote source audit CSV rows: {count:,} -> {args.report_csv}")
        if args.duplicates_csv:
            count = write_csv(
                conn,
                Path(args.duplicates_csv),
                """
                SELECT * FROM source_files
                WHERE duplicate_status <> 'unique'
                ORDER BY object_id, sha256, source_file COLLATE NOCASE
                """,
            )
            print(f"[report] wrote duplicate CSV rows: {count:,} -> {args.duplicates_csv}")
        conflict_stats: Dict[str, int] = {}
        if args.analyze_conflicts:
            conflict_stats = analyze_conflicts(conn)
            print(f"[conflicts] canonicalized conflict files: {conflict_stats['conflict_files_canonicalized']:,}")
            print(f"[conflicts] canonical-equivalent object IDs: {conflict_stats['conflict_equivalent_groups']:,}")
            print(f"[conflicts] canonical-different object IDs: {conflict_stats['conflict_different_groups']:,}")
        if args.conflict_groups_csv:
            if not args.analyze_conflicts:
                print("[report] warning: --conflict-groups-csv is most useful with --analyze-conflicts")
            count = write_conflict_groups_csv(conn, Path(args.conflict_groups_csv))
            print(f"[report] wrote conflict group CSV rows: {count:,} -> {args.conflict_groups_csv}")
        if args.conflict_resolution_csv:
            count = write_conflict_resolution_csv(conn, Path(args.conflict_resolution_csv))
            print(f"[report] wrote conflict resolution CSV rows: {count:,} -> {args.conflict_resolution_csv}")

        moved = 0
        if args.quarantine_duplicates:
            moved = quarantine_exact_duplicates(conn, xml_dir, Path(args.quarantine_duplicates), args.yes)
            print(f"[quarantine] moved exact duplicate XML files: {moved:,}")
        conflict_moved = 0
        if args.quarantine_resolved_conflicts:
            conflict_moved = quarantine_resolved_conflicts(
                conn,
                xml_dir,
                Path(args.quarantine_resolved_conflicts),
                args.yes,
            )
            print(f"[quarantine] moved resolved conflict XML files: {conflict_moved:,}")

        summary = summarize(conn)
        summary.update(conflict_stats)
        summary["loaded_imported_this_scan"] = loaded
        summary["quarantined"] = moved
        summary["conflicts_quarantined"] = conflict_moved
        print(f"[scan] sidecar database: {sidecar_db}")
        print(f"[scan] XML files inventoried: {summary['source_files']:,}")
        print(f"[scan] loaded filings imported: {summary['loaded_filings']:,}")
        for key in sorted(k for k in summary if k.startswith("status_")):
            print(f"[scan] {key.removeprefix('status_')}: {summary[key]:,}")
        return summary
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Scan IRS XML source files into a sidecar manifest database.")
    ap.add_argument("--xml-dir", default=str(DEFAULT_XML_DIR), help="Root directory containing IRS XML files.")
    ap.add_argument("--sidecar-db", default=str(DEFAULT_SIDECAR_DB), help="SQLite sidecar DB to create/update.")
    ap.add_argument("--main-db", default=None, help="Optional production irs990.db for comparison with returns.")
    ap.add_argument("--report-csv", default=None, help="Optional full source audit CSV output path.")
    ap.add_argument("--duplicates-csv", default=None, help="Optional duplicate/conflict CSV output path.")
    ap.add_argument("--conflict-groups-csv", default=None, help="Optional compact one-row-per-conflict-object CSV.")
    ap.add_argument(
        "--conflict-resolution-csv",
        default=None,
        help="Optional conflict keep/quarantine recommendation CSV based on loaded source paths.",
    )
    ap.add_argument(
        "--hash-mode",
        choices=["candidates", "all", "none"],
        default="candidates",
        help="Hash duplicate object-ID candidates only, all XML files, or none.",
    )
    ap.add_argument(
        "--quarantine-duplicates",
        default=None,
        help="Move exact hash duplicates to this directory. Must be outside --xml-dir and requires --yes.",
    )
    ap.add_argument(
        "--quarantine-resolved-conflicts",
        default=None,
        help="Move object_id_conflict files that do not match the loaded source path. Requires --main-db and --yes.",
    )
    ap.add_argument(
        "--analyze-conflicts",
        action="store_true",
        help="Parse object_id_conflict XML files and hash canonicalized XML for semantic comparison.",
    )
    ap.add_argument("--yes", action="store_true", help="Confirm quarantine file moves.")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
