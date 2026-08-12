"""OLMS annual bulk-data importer and deterministic derived-data builders.

The Flask application only reads the resulting sidecar.  This module owns all
write paths so source parsing, repairs, matches, and compliance decisions remain
auditable and testable outside a request process.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, TextIO, Tuple


SCHEMA_VERSION = "1"
RULE_VERSION = "LMRDA_ANNUAL_90_DAY_V1"

KNOWN_TABLE_MAP = {
    "lm_data": "filings",
    "ar_assets_total": "assets_total",
    "ar_assets_other": "assets_other",
    "ar_assets_accts_rcvbl": "assets_accounts_receivable",
    "ar_assets_fixed": "assets_fixed",
    "ar_assets_loans_rcvbl": "assets_loans_receivable",
    "ar_assets_investments": "assets_investments",
    "ar_liabilities_total": "liabilities_total",
    "ar_liabilities_other": "liabilities_other",
    "ar_liabilities_accts_paybl": "liabilities_accounts_payable",
    "ar_liabilities_loans_paybl": "liabilities_loans_payable",
    "ar_receipts_total": "receipts_total",
    "ar_receipts_other": "receipts_other",
    "ar_receipts_inv_fa_sales": "receipts_investment_sales",
    "ar_disbursements_total": "disbursements_total",
    "ar_disbursements_genrl": "disbursements_general",
    "ar_disbursements_inv_purchases": "disbursements_investment_purchases",
    "ar_disbursements_emp_off": "disbursements_employee_officer",
    "ar_disbursements_benefits": "disbursements_benefits",
    "ar_payer_payee": "payer_payee",
    "ar_rates_dues_fees": "rates_dues_fees",
    "ar_membership": "membership",
    "ar_erds_codes": "erds_codes",
}

TABLE_KEYS = {
    "filings": ("rpt_id",),
    "assets_total": ("rpt_id",),
    "liabilities_total": ("rpt_id",),
    "receipts_total": ("rpt_id",),
    "disbursements_total": ("rpt_id",),
    "payer_payee": ("rpt_id", "payer_payee_id"),
    "erds_codes": ("code_type", "code"),
}

DISBURSEMENT_CODES = {
    501: "REPRESENTATIONAL",
    502: "POLITICAL",
    503: "CONTRIBUTIONS, GIFTS AND GRANTS",
    504: "GENERAL OVERHEAD",
    505: "UNION ADMINISTRATION",
    506: "GENERAL DISBURSEMENTS",
}

TEXT_TYPES = {"VARCHAR", "CHAR", "TEXT"}
INTEGER_TYPES = {"INTEGER", "BIGINT", "SMALLINT", "NUMBER", "NUMERIC"}
MERGE_NAME_HINTS = (
    "name", "purpose", "description", "address", "street", "city", "unit",
    "class", "category", "title", "security", "terms", "paid_to", "voice",
)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    dol_type: str
    nullable: bool
    position: int

    @property
    def sql_name(self) -> str:
        return self.name.lower()

    @property
    def is_text(self) -> bool:
        return self.dol_type.upper() in TEXT_TYPES

    @property
    def can_absorb_pipe(self) -> bool:
        lower = self.name.lower()
        return self.is_text and any(token in lower for token in MERGE_NAME_HINTS)


@dataclass(frozen=True)
class SourceSpec:
    year: int
    logical_name: str
    table_name: str
    meta_path: Path
    data_path: Path
    columns: Tuple[ColumnSpec, ...]
    schema_hash: str


@dataclass
class ParseResult:
    values: Optional[List[object]] = None
    repair_type: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.values is not None and self.error is None


@dataclass
class LogicalRecord:
    start_line: int
    end_line: int
    raw: str
    result: ParseResult


def now_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sanitize_identifier(value: str) -> str:
    result = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if not result or result[0].isdigit():
        result = "olms_" + result
    return result


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_name(value: object) -> str:
    text = normalize_text(value).upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"\bLOCAL\s+(?:NO\.?|NUMBER|#)\s*(\d+)\b", r"LOCAL \1", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def zip5(value: object) -> str:
    match = re.search(r"\d{5}", str(value or ""))
    return match.group(0) if match else ""


def _parse_date(value: str, timestamp: bool = False) -> str:
    if timestamp:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def validate_value(value: str, column: ColumnSpec) -> object:
    value = value.strip() if column.dol_type.upper() not in TEXT_TYPES else value.strip()
    if value == "":
        if not column.nullable:
            raise ValueError(f"{column.name} is NOT NULL")
        return None
    kind = column.dol_type.upper()
    if kind in INTEGER_TYPES:
        if not re.fullmatch(r"[-+]?\d+", value):
            raise ValueError(f"{column.name} is not a valid integer")
        return int(value)
    if kind == "DATE":
        return _parse_date(value)
    if kind == "TIMESTAMP":
        return _parse_date(value, timestamp=True)
    return value


def parse_metadata(path: Path) -> Tuple[ColumnSpec, ...]:
    columns: List[ColumnSpec] = []
    pattern = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s+(NOT NULL|NULL)\s+([A-Za-z0-9_]+)\s*$")
    for line in path.read_text(encoding="utf-8-sig", errors="strict").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        name, null_text, dol_type = match.groups()
        columns.append(ColumnSpec(name, dol_type.upper(), null_text == "NULL", len(columns)))
    if not columns:
        raise ValueError(f"No OLMS columns found in metadata: {path}")
    return tuple(columns)


def _schema_hash(columns: Sequence[ColumnSpec]) -> str:
    payload = json.dumps(
        [(c.name, c.dol_type, c.nullable, c.position) for c in columns],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def discover_sources(input_root: Path, years: Optional[Sequence[int]] = None) -> List[SourceSpec]:
    input_root = Path(input_root).expanduser().resolve()
    wanted = set(years or [])
    year_dirs = []
    for child in input_root.iterdir():
        if child.is_dir() and child.name.isdigit():
            year = int(child.name)
            if not wanted or year in wanted:
                year_dirs.append((year, child))
    if wanted and wanted - {year for year, _ in year_dirs}:
        missing = ", ".join(str(y) for y in sorted(wanted - {year for year, _ in year_dirs}))
        raise FileNotFoundError(f"OLMS year directorie(s) not found beneath {input_root}: {missing}")
    if not year_dirs:
        raise FileNotFoundError(f"No numeric OLMS year directories found beneath {input_root}")

    sources: List[SourceSpec] = []
    for year, year_dir in sorted(year_dirs):
        for meta_path in sorted(year_dir.glob("*_meta.txt")):
            logical_name = meta_path.name[:-len("_meta.txt")].lower()
            data_path = year_dir / f"{logical_name}_data_{year}.txt"
            if not data_path.exists():
                raise FileNotFoundError(f"Missing data file for {meta_path}: {data_path.name}")
            columns = parse_metadata(meta_path)
            table_name = KNOWN_TABLE_MAP.get(logical_name, sanitize_identifier(logical_name))
            sources.append(
                SourceSpec(year, logical_name, table_name, meta_path, data_path, columns, _schema_hash(columns))
            )
    return sources


def _try_values(parts: Sequence[str], columns: Sequence[ColumnSpec]) -> Optional[List[object]]:
    if len(parts) != len(columns):
        return None
    values: List[object] = []
    try:
        for value, column in zip(parts, columns):
            values.append(validate_value(value, column))
    except ValueError:
        return None
    return values


def parse_record(raw: str, columns: Sequence[ColumnSpec]) -> ParseResult:
    normalized = raw.replace("\r", "").replace("\n", " ")
    parts = normalized.split("|")
    expected = len(columns)
    if len(parts) < expected:
        return ParseResult(error=f"too few fields: expected {expected}, found {len(parts)}")
    if len(parts) == expected:
        values = _try_values(parts, columns)
        if values is None:
            details = []
            for value, column in zip(parts, columns):
                try:
                    validate_value(value, column)
                except ValueError as exc:
                    details.append(str(exc))
            return ParseResult(error="; ".join(details) or "typed or nullability validation failed")
        return ParseResult(values=values)

    # Schema-guided repair. Typed columns consume exactly one segment; plausible
    # free-text columns may absorb adjacent segments containing literal pipes.
    candidates: List[Tuple[List[object], int]] = []
    max_candidates = 64

    def visit(column_index: int, segment_index: int, values: List[object], merged_fields: int) -> None:
        if len(candidates) >= max_candidates:
            return
        if column_index == expected:
            if segment_index == len(parts):
                candidates.append((list(values), merged_fields))
            return
        remaining_columns = expected - column_index - 1
        max_take = len(parts) - segment_index - remaining_columns
        if max_take < 1:
            return
        column = columns[column_index]
        takes = range(1, max_take + 1) if column.can_absorb_pipe else (1,)
        for take in takes:
            value = "|".join(parts[segment_index:segment_index + take])
            try:
                parsed = validate_value(value, column)
            except ValueError:
                continue
            values.append(parsed)
            visit(column_index + 1, segment_index + take, values, merged_fields + (1 if take > 1 else 0))
            values.pop()

    visit(0, 0, [], 0)
    if not candidates:
        return ParseResult(error=f"too many fields: expected {expected}, found {len(parts)}; no schema-valid repair")
    best_merge_count = min(score for _, score in candidates)
    best = [values for values, score in candidates if score == best_merge_count]
    if len(best) != 1:
        return ParseResult(
            error=f"too many fields: expected {expected}, found {len(parts)}; {len(best)} equally plausible repairs"
        )
    return ParseResult(values=best[0], repair_type="LITERAL_PIPE")


def iter_logical_records(handle: TextIO, columns: Sequence[ColumnSpec]) -> Iterator[LogicalRecord]:
    """Yield schema-validated logical records after the already-read header."""
    physical_line = 1
    pending: Optional[Tuple[int, str]] = None
    while True:
        if pending is not None:
            start_line, raw = pending
            pending = None
        else:
            text = handle.readline()
            if text == "":
                break
            physical_line += 1
            start_line, raw = physical_line, text.rstrip("\r\n")

        result = parse_record(raw, columns)
        if result.ok or not (result.error or "").startswith("too few fields"):
            yield LogicalRecord(start_line, start_line, raw, result)
            continue

        combined = raw
        end_line = start_line
        while end_line - start_line < 20:
            next_text = handle.readline()
            if next_text == "":
                yield LogicalRecord(start_line, end_line, combined, result)
                return
            physical_line += 1
            next_line = next_text.rstrip("\r\n")
            next_result = parse_record(next_line, columns)
            trial = combined + "\n" + next_line
            trial_result = parse_record(trial, columns)
            if trial_result.ok:
                trial_result.repair_type = (
                    "EMBEDDED_NEWLINE_AND_LITERAL_PIPE"
                    if trial_result.repair_type else "EMBEDDED_NEWLINE"
                )
                yield LogicalRecord(start_line, physical_line, trial, trial_result)
                break
            if next_result.ok:
                yield LogicalRecord(start_line, end_line, combined, result)
                pending = (physical_line, next_line)
                break
            combined = trial
            end_line = physical_line
            result = trial_result
            if not (result.error or "").startswith("too few fields"):
                yield LogicalRecord(start_line, end_line, combined, result)
                break


def _file_hash_and_encoding(path: Path) -> Tuple[str, str]:
    digest = hashlib.sha256()
    utf8_ok = True
    decoder = None
    try:
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    except Exception:  # pragma: no cover
        decoder = None
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if utf8_ok and decoder is not None:
                try:
                    decoder.decode(chunk, final=False)
                except UnicodeDecodeError:
                    utf8_ok = False
        if utf8_ok and decoder is not None:
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                utf8_ok = False
    return digest.hexdigest(), ("utf-8-sig" if utf8_ok else "cp1252")


def _sql_type(column: ColumnSpec) -> str:
    return "INTEGER" if column.dol_type.upper() in INTEGER_TYPES else "TEXT"


def create_audit_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS olms_meta (
          key TEXT PRIMARY KEY,
          value TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS import_runs (
          import_run_id INTEGER PRIMARY KEY,
          started_at TEXT NOT NULL,
          completed_at TEXT,
          input_directory TEXT NOT NULL,
          requested_years TEXT NOT NULL,
          mode TEXT NOT NULL,
          status TEXT NOT NULL,
          rows_attempted INTEGER NOT NULL DEFAULT 0,
          rows_loaded INTEGER NOT NULL DEFAULT 0,
          rows_repaired INTEGER NOT NULL DEFAULT 0,
          rows_quarantined INTEGER NOT NULL DEFAULT 0,
          duplicate_rows INTEGER NOT NULL DEFAULT 0,
          conflicting_duplicates INTEGER NOT NULL DEFAULT 0,
          orphan_rows INTEGER NOT NULL DEFAULT 0,
          notes TEXT
        );
        CREATE TABLE IF NOT EXISTS import_sources (
          import_source_id INTEGER PRIMARY KEY,
          import_run_id INTEGER NOT NULL,
          source_year INTEGER NOT NULL,
          logical_table TEXT NOT NULL,
          canonical_table TEXT NOT NULL,
          data_filename TEXT NOT NULL,
          meta_filename TEXT NOT NULL,
          data_sha256 TEXT NOT NULL,
          meta_sha256 TEXT NOT NULL,
          encoding TEXT NOT NULL,
          schema_hash TEXT NOT NULL,
          rows_attempted INTEGER NOT NULL DEFAULT 0,
          rows_loaded INTEGER NOT NULL DEFAULT 0,
          rows_repaired INTEGER NOT NULL DEFAULT 0,
          rows_quarantined INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          FOREIGN KEY(import_run_id) REFERENCES import_runs(import_run_id)
        );
        CREATE TABLE IF NOT EXISTS import_years (
          import_run_id INTEGER NOT NULL,
          source_year INTEGER NOT NULL,
          source_path TEXT NOT NULL,
          combined_sha256 TEXT NOT NULL,
          PRIMARY KEY(import_run_id, source_year)
        );
        CREATE TABLE IF NOT EXISTS import_table_stats (
          import_run_id INTEGER NOT NULL,
          import_source_id INTEGER NOT NULL,
          source_year INTEGER NOT NULL,
          logical_table TEXT NOT NULL,
          rows_attempted INTEGER NOT NULL,
          rows_loaded INTEGER NOT NULL,
          rows_repaired INTEGER NOT NULL,
          rows_quarantined INTEGER NOT NULL,
          orphan_rows INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(import_run_id, import_source_id)
        );
        CREATE TABLE IF NOT EXISTS import_repairs (
          repair_id INTEGER PRIMARY KEY,
          import_run_id INTEGER NOT NULL,
          import_source_id INTEGER NOT NULL,
          source_year INTEGER NOT NULL,
          logical_table TEXT NOT NULL,
          source_start_line INTEGER NOT NULL,
          source_end_line INTEGER NOT NULL,
          repair_type TEXT NOT NULL,
          reason TEXT NOT NULL,
          raw_record TEXT NOT NULL,
          repaired_values_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS import_errors (
          error_id INTEGER PRIMARY KEY,
          import_run_id INTEGER NOT NULL,
          import_source_id INTEGER NOT NULL,
          source_year INTEGER NOT NULL,
          logical_table TEXT NOT NULL,
          source_start_line INTEGER NOT NULL,
          source_end_line INTEGER NOT NULL,
          severity TEXT NOT NULL,
          error_type TEXT NOT NULL,
          message TEXT NOT NULL,
          raw_record TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS import_duplicate_conflicts (
          duplicate_id INTEGER PRIMARY KEY,
          import_run_id INTEGER NOT NULL,
          canonical_table TEXT NOT NULL,
          key_json TEXT NOT NULL,
          conflict_type TEXT NOT NULL,
          occurrence_count INTEGER NOT NULL,
          occurrences_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS import_orphans (
          orphan_id INTEGER PRIMARY KEY,
          import_run_id INTEGER NOT NULL,
          canonical_table TEXT NOT NULL,
          row_id INTEGER NOT NULL,
          rpt_id INTEGER,
          source_year INTEGER,
          source_file TEXT,
          source_row INTEGER
        );
        CREATE TABLE IF NOT EXISTS olms_schema_versions (
          import_run_id INTEGER NOT NULL,
          source_year INTEGER NOT NULL,
          logical_table TEXT NOT NULL,
          canonical_table TEXT NOT NULL,
          source_filename TEXT NOT NULL,
          schema_hash TEXT NOT NULL,
          position INTEGER NOT NULL,
          column_name TEXT NOT NULL,
          dol_type TEXT NOT NULL,
          nullable INTEGER NOT NULL,
          PRIMARY KEY(import_run_id, source_year, logical_table, position)
        );
        """
    )


def ensure_source_table(conn: sqlite3.Connection, table_name: str, columns: Sequence[ColumnSpec]) -> None:
    existing = {
        row[1].lower(): row for row in conn.execute(f"PRAGMA table_info({quote_ident(table_name)})")
    }
    if not existing:
        definitions = ["row_id INTEGER PRIMARY KEY"]
        definitions.extend(f"{quote_ident(c.sql_name)} {_sql_type(c)}" for c in columns)
        definitions.extend(
            [
                "_source_year INTEGER NOT NULL",
                "_source_file TEXT NOT NULL",
                "_source_row INTEGER NOT NULL",
                "_import_run_id INTEGER NOT NULL",
                "_raw_hash TEXT NOT NULL",
            ]
        )
        conn.execute(f"CREATE TABLE {quote_ident(table_name)} ({', '.join(definitions)})")
        return
    for column in columns:
        if column.sql_name not in existing:
            conn.execute(
                f"ALTER TABLE {quote_ident(table_name)} ADD COLUMN {quote_ident(column.sql_name)} {_sql_type(column)}"
            )


def _record_hash(values: Sequence[object]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def import_source(conn: sqlite3.Connection, run_id: int, source: SourceSpec) -> Dict[str, int]:
    data_hash, encoding = _file_hash_and_encoding(source.data_path)
    meta_hash = hashlib.sha256(source.meta_path.read_bytes()).hexdigest()
    cur = conn.execute(
        """
        INSERT INTO import_sources (
          import_run_id, source_year, logical_table, canonical_table,
          data_filename, meta_filename, data_sha256, meta_sha256,
          encoding, schema_hash, status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, source.year, source.logical_name, source.table_name,
            source.data_path.name, source.meta_path.name, data_hash, meta_hash,
            encoding, source.schema_hash, "RUNNING",
        ),
    )
    source_id = int(cur.lastrowid)
    ensure_source_table(conn, source.table_name, source.columns)
    conn.executemany(
        """
        INSERT OR REPLACE INTO olms_schema_versions (
          import_run_id, source_year, logical_table, canonical_table, source_filename,
          schema_hash, position, column_name, dol_type, nullable
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                run_id, source.year, source.logical_name, source.table_name,
                source.data_path.name, source.schema_hash, column.position,
                column.name, column.dol_type, int(column.nullable),
            )
            for column in source.columns
        ],
    )

    stats = {"attempted": 0, "loaded": 0, "repaired": 0, "quarantined": 0}
    names = [column.sql_name for column in source.columns]
    insert_columns = names + ["_source_year", "_source_file", "_source_row", "_import_run_id", "_raw_hash"]
    insert_sql = (
        f"INSERT INTO {quote_ident(source.table_name)} "
        f"({','.join(quote_ident(c) for c in insert_columns)}) "
        f"VALUES ({','.join('?' for _ in insert_columns)})"
    )
    batch: List[Tuple[object, ...]] = []

    with source.data_path.open("r", encoding=encoding, errors="strict", newline="") as handle:
        header = handle.readline().rstrip("\r\n").split("|")
        expected_header = [column.name for column in source.columns]
        if [h.upper() for h in header] != [h.upper() for h in expected_header]:
            raise ValueError(
                f"Header does not match metadata for {source.data_path}: "
                f"expected {expected_header}, found {header}"
            )
        for logical in iter_logical_records(handle, source.columns):
            stats["attempted"] += 1
            if not logical.result.ok:
                severity = "ERROR" if source.table_name == "filings" else "WARNING"
                conn.execute(
                    """
                    INSERT INTO import_errors (
                      import_run_id, import_source_id, source_year, logical_table,
                      source_start_line, source_end_line, severity, error_type, message, raw_record
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id, source_id, source.year, source.table_name,
                        logical.start_line, logical.end_line, severity, "MALFORMED_RECORD",
                        logical.result.error or "unknown parser error", logical.raw,
                    ),
                )
                stats["quarantined"] += 1
                continue

            values = logical.result.values or []
            batch.append(
                tuple(values)
                + (
                    source.year,
                    source.data_path.name,
                    logical.start_line,
                    run_id,
                    _record_hash(values),
                )
            )
            stats["loaded"] += 1
            if logical.result.repair_type:
                stats["repaired"] += 1
                conn.execute(
                    """
                    INSERT INTO import_repairs (
                      import_run_id, import_source_id, source_year, logical_table,
                      source_start_line, source_end_line, repair_type, reason,
                      raw_record, repaired_values_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id, source_id, source.year, source.table_name,
                        logical.start_line, logical.end_line, logical.result.repair_type,
                        "Schema-guided repair produced one valid interpretation.",
                        logical.raw,
                        json.dumps(values, ensure_ascii=False),
                    ),
                )
            if len(batch) >= 5000:
                conn.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)

    status = "COMPLETED_WITH_WARNINGS" if stats["quarantined"] else "COMPLETED"
    conn.execute(
        """
        UPDATE import_sources
        SET rows_attempted=?, rows_loaded=?, rows_repaired=?, rows_quarantined=?, status=?
        WHERE import_source_id=?
        """,
        (
            stats["attempted"], stats["loaded"], stats["repaired"],
            stats["quarantined"], status, source_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO import_table_stats (
          import_run_id, import_source_id, source_year, logical_table,
          rows_attempted, rows_loaded, rows_repaired, rows_quarantined
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            run_id, source_id, source.year, source.table_name,
            stats["attempted"], stats["loaded"], stats["repaired"], stats["quarantined"],
        ),
    )
    stats["source_id"] = source_id
    stats["data_hash"] = data_hash  # type: ignore[assignment]
    stats["meta_hash"] = meta_hash  # type: ignore[assignment]
    return stats


def _source_tables(conn: sqlite3.Connection) -> List[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT canonical_table FROM olms_schema_versions ORDER BY canonical_table"
        )
    ]


def _table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({quote_ident(table_name)})")]


def _key_for_table(conn: sqlite3.Connection, table_name: str) -> Tuple[str, ...]:
    columns = {column.lower() for column in _table_columns(conn, table_name)}
    if table_name in TABLE_KEYS and all(key in columns for key in TABLE_KEYS[table_name]):
        return TABLE_KEYS[table_name]
    if "rpt_id" in columns and "oid" in columns:
        return ("rpt_id", "oid")
    return ()


def drop_source_indexes(conn: sqlite3.Connection) -> None:
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND (name LIKE 'idx_olms_%' OR name LIKE 'uq_olms_%')"
    ).fetchall():
        conn.execute(f"DROP INDEX IF EXISTS {quote_ident(name)}")


def deduplicate_sources(conn: sqlite3.Connection, run_id: int) -> Tuple[int, int]:
    identical_removed = 0
    conflicting_removed = 0
    for table_name in _source_tables(conn):
        key_columns = _key_for_table(conn, table_name)
        if not key_columns:
            continue
        key_sql = ",".join(quote_ident(key) for key in key_columns)
        not_null = " AND ".join(f"{quote_ident(key)} IS NOT NULL" for key in key_columns)
        groups = conn.execute(
            f"""
            SELECT {key_sql}, COUNT(*) AS occurrence_count,
                   COUNT(DISTINCT _raw_hash) AS distinct_rows
            FROM {quote_ident(table_name)}
            WHERE {not_null}
            GROUP BY {key_sql}
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for group in groups:
            key_values = tuple(group[:len(key_columns)])
            occurrence_count = int(group[len(key_columns)])
            distinct_count = int(group[len(key_columns) + 1])
            where = " AND ".join(f"{quote_ident(key)}=?" for key in key_columns)
            rows = conn.execute(
                f"SELECT * FROM {quote_ident(table_name)} WHERE {where} ORDER BY row_id",
                key_values,
            ).fetchall()
            columns = _table_columns(conn, table_name)
            occurrence_payload = [dict(zip(columns, row)) for row in rows]
            conflict_type = "IDENTICAL_DUPLICATE" if distinct_count == 1 else "CONFLICTING_DUPLICATE"
            conn.execute(
                """
                INSERT INTO import_duplicate_conflicts (
                  import_run_id, canonical_table, key_json, conflict_type,
                  occurrence_count, occurrences_json
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    run_id,
                    table_name,
                    json.dumps(dict(zip(key_columns, key_values)), ensure_ascii=False),
                    conflict_type,
                    occurrence_count,
                    json.dumps(occurrence_payload, ensure_ascii=False, default=str),
                ),
            )
            if distinct_count == 1:
                keep_id = min(int(row[0]) for row in rows)
                conn.execute(
                    f"DELETE FROM {quote_ident(table_name)} WHERE {where} AND row_id<>?",
                    key_values + (keep_id,),
                )
                identical_removed += occurrence_count - 1
            else:
                # A conflicting natural key has no defensible authoritative row.
                # Preserve all versions in the audit table and quarantine the key.
                conn.execute(f"DELETE FROM {quote_ident(table_name)} WHERE {where}", key_values)
                conflicting_removed += occurrence_count
    return identical_removed, conflicting_removed


def record_orphans(conn: sqlite3.Connection, run_id: int) -> int:
    conn.execute("DELETE FROM import_orphans")
    total = 0
    for table_name in _source_tables(conn):
        if table_name == "filings" or "rpt_id" not in {c.lower() for c in _table_columns(conn, table_name)}:
            continue
        rows = conn.execute(
            f"""
            SELECT d.row_id, d.rpt_id, d._source_year, d._source_file, d._source_row
            FROM {quote_ident(table_name)} d
            LEFT JOIN filings f ON f.rpt_id=d.rpt_id
            WHERE d.rpt_id IS NOT NULL AND f.rpt_id IS NULL
            """
        ).fetchall()
        if rows:
            conn.executemany(
                """
                INSERT INTO import_orphans (
                  import_run_id, canonical_table, row_id, rpt_id,
                  source_year, source_file, source_row
                ) VALUES (?,?,?,?,?,?,?)
                """,
                [(run_id, table_name, *row) for row in rows],
            )
            conn.execute(
                """
                UPDATE import_table_stats
                SET orphan_rows = orphan_rows + ?
                WHERE import_run_id=? AND logical_table=?
                """,
                (len(rows), run_id, table_name),
            )
            total += len(rows)
    return total


def create_source_indexes(conn: sqlite3.Connection) -> None:
    for table_name in _source_tables(conn):
        columns = {c.lower() for c in _table_columns(conn, table_name)}
        key_columns = _key_for_table(conn, table_name)
        if key_columns:
            index_name = "uq_olms_" + table_name + "_key"
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {quote_ident(index_name)} "
                f"ON {quote_ident(table_name)} ({','.join(quote_ident(c) for c in key_columns)})"
            )
        for column in ("rpt_id", "f_num", "payer_payee_id", "name", "state", "receive_date"):
            if column in columns:
                index_name = f"idx_olms_{table_name}_{column}"
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {quote_ident(index_name)} "
                    f"ON {quote_ident(table_name)} ({quote_ident(column)})"
                )
    filing_columns = {c.lower() for c in _table_columns(conn, "filings")}
    if {"f_num", "pd_covered_to"}.issubset(filing_columns):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_olms_filings_fnum_period ON filings(f_num,pd_covered_to)"
        )
    if {"rpt_id", "payer_payee_id"}.issubset(
        {c.lower() for c in _table_columns(conn, "disbursements_general")}
    ):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_olms_disb_general_payee ON disbursements_general(rpt_id,payer_payee_id)"
        )


def _scope_classification(affiliation: object, *names: object) -> Tuple[str, str]:
    aff = normalize_name(affiliation)
    text = " ".join(normalize_name(value) for value in names if value)
    if aff == "NEA" or "NATIONAL EDUCATION ASSOCIATION" in text:
        return "likely_education", "NEA affiliation or name"
    if aff == "AFT" or "AMERICAN FEDERATION OF TEACHERS" in text:
        health_terms = ("NURSE", "HEALTH", "HOSPITAL", "PHYSICIAN", "MEDICAL")
        if any(term in text for term in health_terms) and not any(
            term in text for term in ("TEACHER", "SCHOOL", "EDUCATION", "FACULTY", "PROFESSOR")
        ):
            return "uncertain", "AFT affiliate with health-care terms"
        return "education_or_mixed", "AFT affiliation or name"
    if any(
        term in text
        for term in (
            "TEACHER", "EDUCATION", "SCHOOL", "EDUCATOR", "FACULTY",
            "PROFESSOR", "INSTRUCTOR", "ACADEMIC", "CLASSROOM",
        )
    ):
        return "likely_education", "Education-related organization-name terms"
    return "uncertain", "No deterministic education indicator"


def _read_csv_rows(path: Optional[Path]) -> List[Dict[str, str]]:
    if path is None or not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def build_organizations(conn: sqlite3.Connection, scope_overrides: Optional[Path] = None) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS organizations;
        CREATE TABLE organizations (
          f_num INTEGER PRIMARY KEY,
          display_name TEXT,
          union_name TEXT,
          unit_name TEXT,
          affiliation TEXT,
          designation TEXT,
          designation_number INTEGER,
          city TEXT,
          state TEXT,
          zip TEXT,
          first_period_end TEXT,
          latest_period_end TEXT,
          latest_report_received TEXT,
          latest_form_type TEXT,
          establishment_date TEXT,
          termination_date TEXT,
          terminated INTEGER NOT NULL DEFAULT 0,
          latest_rpt_id INTEGER,
          filing_count INTEGER NOT NULL,
          education_scope TEXT NOT NULL,
          education_scope_reason TEXT,
          scope_manual_override INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    rows = conn.execute(
        """
        WITH ranked AS (
          SELECT f.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY f_num
                   ORDER BY COALESCE(pd_covered_to,'') DESC,
                            COALESCE(receive_date,'') DESC, rpt_id DESC
                 ) AS rn
          FROM filings f
        ), agg AS (
          SELECT f_num, MIN(pd_covered_to) AS first_period_end,
                 MAX(pd_covered_to) AS latest_period_end,
                 MAX(receive_date) AS latest_report_received,
                 COUNT(*) AS filing_count
          FROM filings GROUP BY f_num
        )
        SELECT r.f_num, r.union_name, r.unit_name, r.aff_abbr,
               TRIM(COALESCE(r.desiq_pre,'') || ' ' || COALESCE(CAST(r.desig_num AS TEXT),'') ||
                    ' ' || COALESCE(r.desig_suf,'') || ' ' || COALESCE(r.desig_name,'')) AS designation,
               r.desig_num, r.city, r.state, r.zip,
               a.first_period_end, a.latest_period_end, a.latest_report_received,
               r.form_type, r.est_date, r.term_date,
               CASE WHEN UPPER(COALESCE(r.terminate,'')) IN ('T','Y','TRUE','1')
                          OR r.term_date IS NOT NULL THEN 1 ELSE 0 END AS terminated,
               r.rpt_id, a.filing_count, r.desig_name
        FROM ranked r JOIN agg a USING(f_num)
        WHERE r.rn=1
        """
    ).fetchall()
    inserts = []
    for row in rows:
        (
            f_num, union_name, unit_name, aff, designation, desig_num, city, state, postal,
            first_period, latest_period, latest_received, form_type, est_date, term_date,
            terminated, latest_rpt_id, filing_count, desig_name,
        ) = row
        display_name = normalize_text(unit_name) or normalize_text(union_name)
        scope, reason = _scope_classification(aff, union_name, unit_name, desig_name)
        inserts.append(
            (
                f_num, display_name, union_name, unit_name, aff, designation, desig_num,
                city, state, postal, first_period, latest_period, latest_received, form_type,
                est_date, term_date, terminated, latest_rpt_id, filing_count, scope, reason,
            )
        )
    conn.executemany(
        """
        INSERT INTO organizations (
          f_num, display_name, union_name, unit_name, affiliation, designation,
          designation_number, city, state, zip, first_period_end, latest_period_end,
          latest_report_received, latest_form_type, establishment_date, termination_date,
          terminated, latest_rpt_id, filing_count, education_scope, education_scope_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        inserts,
    )
    for override in _read_csv_rows(scope_overrides):
        try:
            f_num = int((override.get("f_num") or "").strip())
        except ValueError:
            continue
        action = (override.get("action") or "").strip().lower()
        scope = "manual_include" if action in {"include", "manual_include"} else (
            "manual_exclude" if action in {"exclude", "manual_exclude"} else ""
        )
        if scope:
            conn.execute(
                """
                UPDATE organizations
                SET education_scope=?, education_scope_reason=?, scope_manual_override=1
                WHERE f_num=?
                """,
                (scope, normalize_text(override.get("note")) or f"Manual {action}", f_num),
            )
    conn.executescript(
        """
        CREATE INDEX idx_olms_organizations_name ON organizations(display_name);
        CREATE INDEX idx_olms_organizations_state ON organizations(state);
        CREATE INDEX idx_olms_organizations_affiliation ON organizations(affiliation);
        CREATE INDEX idx_olms_organizations_scope ON organizations(education_scope);
        """
    )


def build_filing_periods(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS filing_periods;
        CREATE TABLE filing_periods AS
        WITH base AS (
          SELECT f.*,
                 CAST(f_num AS TEXT) || '|' || COALESCE(pd_covered_from,'') || '|' ||
                 COALESCE(pd_covered_to,'') || '|' ||
                 CASE WHEN pd_covered_from IS NULL AND pd_covered_to IS NULL
                      THEN COALESCE(CAST(yr_covered AS TEXT), 'RPT:' || CAST(rpt_id AS TEXT))
                      ELSE '' END AS period_key
          FROM filings f
        ), ranked AS (
          SELECT b.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY period_key
                   ORDER BY CASE WHEN amendment=0 THEN 0 ELSE 1 END,
                            COALESCE(receive_date,''), rpt_id
                 ) AS original_rank,
                 ROW_NUMBER() OVER (
                   PARTITION BY period_key
                   ORDER BY COALESCE(receive_date,'') DESC,
                            COALESCE(amendment,0) DESC, rpt_id DESC
                 ) AS latest_rank
          FROM base b
        )
        SELECT period_key, f_num,
               MIN(pd_covered_from) AS period_start,
               MAX(pd_covered_to) AS period_end,
               MAX(yr_covered) AS year_covered,
               MAX(CASE WHEN original_rank=1 THEN rpt_id END) AS original_rpt_id,
               MAX(CASE WHEN latest_rank=1 THEN rpt_id END) AS latest_rpt_id,
               MAX(CASE WHEN original_rank=1 THEN receive_date END) AS initial_receive_date,
               MAX(CASE WHEN latest_rank=1 THEN receive_date END) AS latest_receive_date,
               MAX(CASE WHEN original_rank=1 THEN form_type END) AS original_form_type,
               MAX(CASE WHEN latest_rank=1 THEN form_type END) AS latest_form_type,
               SUM(CASE WHEN COALESCE(amendment,0)<>0 THEN 1 ELSE 0 END) AS amendment_count,
               MAX(CASE WHEN amendment=0 THEN 1 ELSE 0 END) AS original_observed,
               MAX(CASE WHEN original_rank=1 AND UPPER(COALESCE(hardship,'')) IN ('T','Y','TRUE','1')
                        THEN 1 ELSE 0 END) AS hardship,
               MAX(CASE WHEN UPPER(COALESCE(terminate,'')) IN ('T','Y','TRUE','1')
                             OR term_date IS NOT NULL THEN 1 ELSE 0 END) AS terminal,
               date(MAX(pd_covered_to), '+90 days') AS due_date,
               CASE
                 WHEN MAX(CASE WHEN amendment=0 THEN 1 ELSE 0 END)=0 THEN 'ORIGINAL_NOT_OBSERVED'
                 WHEN MAX(pd_covered_to) IS NULL OR
                      MAX(CASE WHEN original_rank=1 THEN receive_date END) IS NULL
                      THEN 'ORIGINAL_NOT_OBSERVED'
                 WHEN MAX(CASE WHEN original_rank=1 AND UPPER(COALESCE(hardship,'')) IN ('T','Y','TRUE','1')
                              THEN 1 ELSE 0 END)=1
                      AND julianday(MAX(CASE WHEN original_rank=1 THEN receive_date END)) >
                          julianday(date(MAX(pd_covered_to), '+90 days')) THEN 'HARDSHIP_REVIEW'
                 WHEN julianday(MAX(CASE WHEN original_rank=1 THEN receive_date END)) <=
                      julianday(date(MAX(pd_covered_to), '+90 days')) THEN 'FILED_ON_TIME'
                 ELSE 'FILED_LATE'
               END AS filing_status,
               CASE
                 WHEN MAX(CASE WHEN amendment=0 THEN 1 ELSE 0 END)=0 OR
                      MAX(pd_covered_to) IS NULL OR
                      MAX(CASE WHEN original_rank=1 THEN receive_date END) IS NULL THEN NULL
                 WHEN MAX(CASE WHEN original_rank=1 AND UPPER(COALESCE(hardship,'')) IN ('T','Y','TRUE','1')
                              THEN 1 ELSE 0 END)=1
                      AND julianday(MAX(CASE WHEN original_rank=1 THEN receive_date END)) >
                          julianday(date(MAX(pd_covered_to), '+90 days')) THEN NULL
                 ELSE MAX(0, CAST(julianday(MAX(CASE WHEN original_rank=1 THEN receive_date END)) -
                                      julianday(date(MAX(pd_covered_to), '+90 days')) AS INTEGER))
               END AS days_late,
               '""" + RULE_VERSION + """' AS rule_version
        FROM ranked
        GROUP BY period_key, f_num;

        CREATE UNIQUE INDEX idx_olms_period_key ON filing_periods(period_key);
        CREATE INDEX idx_olms_period_fnum_end ON filing_periods(f_num,period_end);
        CREATE INDEX idx_olms_period_status ON filing_periods(filing_status);
        CREATE INDEX idx_olms_period_latest_rpt ON filing_periods(latest_rpt_id);
        """
    )


def _safe_anniversary(value: date, year: int) -> date:
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, day=28)


def build_compliance_results(conn: sqlite3.Connection, as_of_date: Optional[str] = None) -> str:
    if as_of_date:
        as_of = date.fromisoformat(as_of_date)
    else:
        row = conn.execute("SELECT MAX(date(receive_date)) FROM filings").fetchone()
        as_of = date.fromisoformat(row[0]) if row and row[0] else date.today()
    conn.executescript(
        """
        DROP TABLE IF EXISTS compliance_results;
        CREATE TABLE compliance_results (
          compliance_id INTEGER PRIMARY KEY,
          f_num INTEGER NOT NULL,
          period_start TEXT,
          period_end TEXT,
          form_type TEXT,
          due_date TEXT,
          initial_receive_date TEXT,
          status TEXT NOT NULL,
          days_late_or_overdue INTEGER,
          amendment_count INTEGER NOT NULL DEFAULT 0,
          hardship INTEGER NOT NULL DEFAULT 0,
          terminated INTEGER NOT NULL DEFAULT 0,
          latest_rpt_id INTEGER,
          reason TEXT NOT NULL,
          data_as_of TEXT NOT NULL,
          rule_version TEXT NOT NULL,
          result_kind TEXT NOT NULL
        );
        """
    )
    observed = conn.execute(
        """
        SELECT p.f_num,p.period_start,p.period_end,p.latest_form_type,p.due_date,
               p.initial_receive_date,p.filing_status,p.days_late,p.amendment_count,
               p.hardship,p.terminal,p.latest_rpt_id
        FROM filing_periods p
        ORDER BY p.f_num,p.period_end
        """
    ).fetchall()
    observed_rows = []
    by_fnum: Dict[int, List[date]] = defaultdict(list)
    for row in observed:
        (
            f_num, period_start, period_end, form_type, due, received, status,
            days, amendments, hardship, terminal, latest_rpt_id,
        ) = row
        if period_end:
            try:
                by_fnum[int(f_num)].append(date.fromisoformat(period_end))
            except ValueError:
                pass
        if status == "FILED_ON_TIME":
            reason = "Original annual report was received on or before the 90-day due date."
        elif status == "FILED_LATE":
            reason = f"Original annual report was received {int(days or 0)} calendar day(s) after the 90-day due date."
        elif status == "HARDSHIP_REVIEW":
            reason = "The electronic receive date is after the normal due date, but the filing is marked hardship; the timely paper date is not available."
        else:
            reason = "An original filing or the dates required for timeliness were not observed; no automatic lateness conclusion is made."
        observed_rows.append(
            (
                f_num, period_start, period_end, form_type, due, received, status, days,
                amendments, hardship, terminal, latest_rpt_id, reason, as_of.isoformat(),
                RULE_VERSION, "OBSERVED",
            )
        )
    conn.executemany(
        """
        INSERT INTO compliance_results (
          f_num,period_start,period_end,form_type,due_date,initial_receive_date,status,
          days_late_or_overdue,amendment_count,hardship,terminated,latest_rpt_id,
          reason,data_as_of,rule_version,result_kind
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        observed_rows,
    )

    org_rows = conn.execute("SELECT f_num,terminated,termination_date FROM organizations").fetchall()
    derived_rows = []
    for f_num, terminated, termination_date in org_rows:
        periods = sorted(set(by_fnum.get(int(f_num), [])))
        period_set = set(periods)
        if terminated:
            derived_rows.append(
                (
                    f_num, None, periods[-1].isoformat() if periods else None, None, None, None,
                    "TERMINATED", None, 0, 0, 1, None,
                    "Organization is marked terminated; no ordinary next annual filing is inferred.",
                    as_of.isoformat(), RULE_VERSION, "EXPECTATION",
                )
            )
            continue
        if len(periods) < 2:
            derived_rows.append(
                (
                    f_num, None, periods[-1].isoformat() if periods else None, None, None, None,
                    "INSUFFICIENT_HISTORY", None, 0, 0, 0, None,
                    "At least two consistent annual periods are required before inferring a missing filing.",
                    as_of.isoformat(), RULE_VERSION, "EXPECTATION",
                )
            )
            continue

        # Historical gaps only use surrounding reports with the same FYE month/day.
        for previous, following in zip(periods, periods[1:]):
            if previous.month == following.month and previous.day == following.day and following.year - previous.year > 1:
                for missing_year in range(previous.year + 1, following.year):
                    expected = _safe_anniversary(previous, missing_year)
                    due = expected + timedelta(days=90)
                    if due > as_of or expected in period_set:
                        continue
                    derived_rows.append(
                        (
                            f_num, None, expected.isoformat(), None, due.isoformat(), None,
                            "POTENTIAL_MISSING_FILING", (as_of - due).days, 0, 0, 0, None,
                            f"Annual periods ending {previous.isoformat()} and {following.isoformat()} were observed, "
                            f"but no report for the expected period ending {expected.isoformat()} was found. "
                            f"Expected due date: {due.isoformat()}.",
                            as_of.isoformat(), RULE_VERSION, "HISTORICAL_GAP",
                        )
                    )

        prior, latest = periods[-2], periods[-1]
        consistent = (
            prior.month == latest.month
            and prior.day == latest.day
            and latest.year - prior.year == 1
        )
        if not consistent:
            derived_rows.append(
                (
                    f_num, None, latest.isoformat(), None, None, None,
                    "FYE_CHANGED_REVIEW", None, 0, 0, 0, None,
                    "The two latest observed periods do not form a consecutive annual pattern with a consistent fiscal-year end.",
                    as_of.isoformat(), RULE_VERSION, "EXPECTATION",
                )
            )
            continue
        expected = _safe_anniversary(latest, latest.year + 1)
        due = expected + timedelta(days=90)
        if due <= as_of and expected not in period_set:
            derived_rows.append(
                (
                    f_num, None, expected.isoformat(), None, due.isoformat(), None,
                    "POTENTIAL_MISSING_FILING", (as_of - due).days, 0, 0, 0, None,
                    f"Last observed annual period ended {latest.isoformat()}. Previous periods consistently ended "
                    f"{latest.strftime('%B %d')}. No annual LM financial report for the expected period ending "
                    f"{expected.isoformat()} was found. Expected due date: {due.isoformat()}.",
                    as_of.isoformat(), RULE_VERSION, "CURRENT_EXPECTATION",
                )
            )
    conn.executemany(
        """
        INSERT INTO compliance_results (
          f_num,period_start,period_end,form_type,due_date,initial_receive_date,status,
          days_late_or_overdue,amendment_count,hardship,terminated,latest_rpt_id,
          reason,data_as_of,rule_version,result_kind
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        derived_rows,
    )
    conn.executescript(
        """
        CREATE INDEX idx_olms_compliance_status ON compliance_results(status);
        CREATE INDEX idx_olms_compliance_fnum_end ON compliance_results(f_num,period_end);
        CREATE INDEX idx_olms_compliance_days ON compliance_results(days_late_or_overdue);
        """
    )
    return as_of.isoformat()


def _counterparty_signature(name: object, city: object, state: object, postal: object) -> Tuple[str, str, str]:
    name_norm = normalize_name(name)
    city_norm = normalize_name(city)
    state_norm = normalize_name(state)
    postal5 = zip5(postal)
    if postal5:
        signature = f"NAME_ZIP|{name_norm}|{postal5}"
        strength = "EXACT_NAME_ZIP5"
    elif city_norm and state_norm:
        signature = f"NAME_CITY_STATE|{name_norm}|{city_norm}|{state_norm}"
        strength = "EXACT_NAME_CITY_STATE"
    else:
        signature = f"NAME_ONLY|{name_norm}"
        strength = "NAME_ONLY"
    counterparty_id = "cp_" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
    return counterparty_id, signature, strength


def build_counterparties(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS counterparty_aliases;
        DROP TABLE IF EXISTS counterparty_assignments;
        DROP TABLE IF EXISTS counterparties;
        CREATE TABLE counterparties (
          counterparty_id TEXT PRIMARY KEY,
          canonical_name TEXT NOT NULL,
          canonical_name_norm TEXT NOT NULL,
          city TEXT,
          state TEXT,
          zip5 TEXT,
          signature TEXT NOT NULL,
          identity_strength TEXT NOT NULL,
          first_year INTEGER,
          last_year INTEGER,
          occurrence_count INTEGER NOT NULL DEFAULT 0,
          matched_ein TEXT,
          match_status TEXT NOT NULL DEFAULT 'UNMATCHED',
          match_method TEXT,
          match_confidence REAL
        );
        CREATE TABLE counterparty_assignments (
          payer_payee_row_id INTEGER PRIMARY KEY,
          counterparty_id TEXT NOT NULL,
          FOREIGN KEY(counterparty_id) REFERENCES counterparties(counterparty_id)
        );
        CREATE TABLE counterparty_aliases (
          counterparty_id TEXT NOT NULL,
          alias TEXT NOT NULL,
          alias_norm TEXT NOT NULL,
          city TEXT,
          state TEXT,
          zip5 TEXT,
          occurrence_count INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(counterparty_id,alias,city,state,zip5)
        );
        """
    )
    counterparties: Dict[str, Dict[str, object]] = {}
    aliases: Dict[Tuple[str, str, str, str, str, str], int] = defaultdict(int)
    assignments: List[Tuple[int, str]] = []
    cursor = conn.execute(
        """
        SELECT row_id,name,city,state,zip,_source_year
        FROM payer_payee
        WHERE payer_payee_type=1002 AND TRIM(COALESCE(name,''))<>''
        """
    )
    for row_id, name, city, state, postal, source_year in cursor:
        cp_id, signature, strength = _counterparty_signature(name, city, state, postal)
        name_clean = normalize_text(name)
        name_norm = normalize_name(name)
        city_clean = normalize_text(city)
        state_clean = normalize_text(state).upper()
        postal5 = zip5(postal)
        current = counterparties.get(cp_id)
        if current is None:
            counterparties[cp_id] = {
                "counterparty_id": cp_id,
                "canonical_name": name_clean,
                "canonical_name_norm": name_norm,
                "city": city_clean,
                "state": state_clean,
                "zip5": postal5,
                "signature": signature,
                "identity_strength": strength,
                "first_year": int(source_year),
                "last_year": int(source_year),
                "occurrence_count": 1,
            }
        else:
            current["first_year"] = min(int(current["first_year"]), int(source_year))
            current["last_year"] = max(int(current["last_year"]), int(source_year))
            current["occurrence_count"] = int(current["occurrence_count"]) + 1
            if len(name_clean) > len(str(current["canonical_name"])):
                current["canonical_name"] = name_clean
        aliases[(cp_id, name_clean, name_norm, city_clean, state_clean, postal5)] += 1
        assignments.append((int(row_id), cp_id))

    conn.executemany(
        """
        INSERT INTO counterparties (
          counterparty_id,canonical_name,canonical_name_norm,city,state,zip5,
          signature,identity_strength,first_year,last_year,occurrence_count
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                item["counterparty_id"], item["canonical_name"], item["canonical_name_norm"],
                item["city"], item["state"], item["zip5"], item["signature"],
                item["identity_strength"], item["first_year"], item["last_year"],
                item["occurrence_count"],
            )
            for item in counterparties.values()
        ],
    )
    conn.executemany(
        "INSERT INTO counterparty_assignments(payer_payee_row_id,counterparty_id) VALUES (?,?)",
        assignments,
    )
    alias_rows = []
    for (cp_id, alias, alias_norm, city, state, postal5), count in aliases.items():
        alias_rows.append((cp_id, alias, alias_norm, city, state, postal5, count))
    conn.executemany(
        """
        INSERT INTO counterparty_aliases (
          counterparty_id,alias,alias_norm,city,state,zip5,occurrence_count
        ) VALUES (?,?,?,?,?,?,?)
        """,
        alias_rows,
    )
    conn.executescript(
        """
        CREATE INDEX idx_olms_counterparty_name ON counterparties(canonical_name_norm);
        CREATE INDEX idx_olms_counterparty_ein ON counterparties(matched_ein);
        CREATE INDEX idx_olms_counterparty_assignment ON counterparty_assignments(counterparty_id);
        CREATE INDEX idx_olms_counterparty_alias ON counterparty_aliases(alias_norm);
        """
    )


def _organization_name_variants(row: sqlite3.Row) -> List[str]:
    names = [row["display_name"], row["union_name"], row["unit_name"]]
    if row["union_name"] and row["unit_name"]:
        names.append(f"{row['union_name']} {row['unit_name']}")
    return sorted({normalize_name(name) for name in names if normalize_name(name)})


def build_irs_matches(
    conn: sqlite3.Connection,
    irs_db_path: Optional[Path],
    overrides_path: Optional[Path] = None,
    match_counterparties: bool = True,
) -> None:
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_accepted_irs_matches;
        DROP TABLE IF EXISTS irs_matches;
        DROP TABLE IF EXISTS irs_identity_candidates;
        CREATE TABLE irs_identity_candidates (
          name_norm TEXT NOT NULL,
          ein TEXT NOT NULL,
          irs_name TEXT NOT NULL,
          irs_city TEXT,
          irs_state TEXT,
          irs_zip TEXT,
          source_kind TEXT NOT NULL,
          UNIQUE(name_norm,ein,irs_name,irs_city,irs_state,irs_zip,source_kind)
        );
        CREATE TABLE irs_matches (
          match_id INTEGER PRIMARY KEY,
          f_num INTEGER NOT NULL,
          candidate_ein TEXT,
          olms_name TEXT,
          irs_name TEXT,
          olms_city TEXT,
          olms_state TEXT,
          olms_zip TEXT,
          irs_city TEXT,
          irs_state TEXT,
          irs_zip TEXT,
          name_score REAL,
          address_score REAL,
          match_method TEXT,
          confidence REAL,
          match_status TEXT NOT NULL,
          match_version TEXT NOT NULL,
          reason TEXT NOT NULL,
          manual_override_status TEXT
        );
        """
    )
    conn.row_factory = sqlite3.Row
    org_rows = conn.execute("SELECT * FROM organizations").fetchall()
    union_targets: Dict[str, set] = defaultdict(set)
    for row in org_rows:
        for variant in _organization_name_variants(row):
            union_targets[variant].add(int(row["f_num"]))
    target_names = set(union_targets)
    if match_counterparties:
        target_names.update(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT canonical_name_norm FROM counterparties WHERE canonical_name_norm<>''"
            )
        )

    if irs_db_path is not None and Path(irs_db_path).exists() and target_names:
        path = Path(irs_db_path).expanduser().resolve()
        irs = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
        try:
            batch: List[Tuple[str, str, str, str, str, str, str]] = []
            cursor = irs.execute("SELECT ein,org_name,dba_name,city,state,zip FROM returns")
            for ein, org_name, dba_name, city, state, postal in cursor:
                for source_kind, candidate_name in (("ORG_NAME", org_name), ("DBA_NAME", dba_name)):
                    name_norm = normalize_name(candidate_name)
                    if not name_norm or name_norm not in target_names or not ein:
                        continue
                    batch.append(
                        (
                            name_norm, str(ein), normalize_text(candidate_name),
                            normalize_text(city), normalize_text(state).upper(), zip5(postal), source_kind,
                        )
                    )
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR IGNORE INTO irs_identity_candidates VALUES (?,?,?,?,?,?,?)",
                        batch,
                    )
                    batch.clear()
            if batch:
                conn.executemany(
                    "INSERT OR IGNORE INTO irs_identity_candidates VALUES (?,?,?,?,?,?,?)",
                    batch,
                )
        finally:
            irs.close()
    conn.execute("CREATE INDEX idx_olms_irs_candidate_name ON irs_identity_candidates(name_norm)")
    conn.execute("CREATE INDEX idx_olms_irs_candidate_ein ON irs_identity_candidates(ein)")

    rejected: Dict[int, set] = defaultdict(set)
    manual_rows = _read_csv_rows(overrides_path)
    for row in manual_rows:
        try:
            f_num = int((row.get("f_num") or "").strip())
        except ValueError:
            continue
        if (row.get("action") or "").strip().lower() == "reject":
            rejected[f_num].add(re.sub(r"\D", "", row.get("ein") or ""))

    for org in org_rows:
        f_num = int(org["f_num"])
        variants = _organization_name_variants(org)
        candidates: Dict[Tuple[str, str], sqlite3.Row] = {}
        for variant in variants:
            for candidate in conn.execute(
                "SELECT * FROM irs_identity_candidates WHERE name_norm=?", (variant,)
            ):
                candidates[(candidate["ein"], candidate["irs_name"])] = candidate
        scored = []
        for candidate in candidates.values():
            same_zip = bool(zip5(org["zip"]) and zip5(org["zip"]) == candidate["irs_zip"])
            same_city_state = bool(
                normalize_name(org["city"]) and normalize_name(org["city"]) == normalize_name(candidate["irs_city"])
                and normalize_name(org["state"]) == normalize_name(candidate["irs_state"])
            )
            same_state = bool(
                normalize_name(org["state"]) and normalize_name(org["state"]) == normalize_name(candidate["irs_state"])
            )
            if same_zip:
                score, method, confidence = 1.0, "EXACT_NAME_ZIP5", 0.99
            elif same_city_state:
                score, method, confidence = 0.9, "EXACT_NAME_CITY_STATE", 0.97
            elif same_state:
                score, method, confidence = 0.5, "EXACT_NAME_STATE_REVIEW", 0.80
            else:
                score, method, confidence = 0.0, "EXACT_NAME_LOCATION_MISMATCH", 0.55
            scored.append((candidate, score, method, confidence))
        strong_eins = {item[0]["ein"] for item in scored if item[1] >= 0.9 and item[0]["ein"] not in rejected[f_num]}
        accepted_ein = next(iter(strong_eins)) if len(strong_eins) == 1 else None
        if not scored:
            conn.execute(
                """
                INSERT INTO irs_matches (
                  f_num,olms_name,olms_city,olms_state,olms_zip,name_score,address_score,
                  match_method,confidence,match_status,match_version,reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f_num, org["display_name"], org["city"], org["state"], org["zip"],
                    0.0, 0.0, "NO_EXACT_NAME_CANDIDATE", 0.0, "UNMATCHED", SCHEMA_VERSION,
                    "No IRS return identity shared an exact normalized OLMS name variant.",
                ),
            )
            continue
        for candidate, address_score, method, confidence in scored:
            if candidate["ein"] in rejected[f_num]:
                status = "REJECTED_MANUAL"
                reason = "Candidate was rejected by the durable manual override file."
                manual_status = "reject"
            elif candidate["ein"] == accepted_ein and address_score >= 0.9:
                status = "MATCHED_HIGH_CONFIDENCE"
                reason = "Unique EIN candidate with exact normalized name and matching ZIP5 or city/state."
                manual_status = None
            else:
                status = "CANDIDATE_REVIEW"
                reason = "Exact normalized name candidate was not uniquely supported by strong location evidence."
                manual_status = None
            conn.execute(
                """
                INSERT INTO irs_matches (
                  f_num,candidate_ein,olms_name,irs_name,olms_city,olms_state,olms_zip,
                  irs_city,irs_state,irs_zip,name_score,address_score,match_method,confidence,
                  match_status,match_version,reason,manual_override_status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f_num, candidate["ein"], org["display_name"], candidate["irs_name"],
                    org["city"], org["state"], org["zip"], candidate["irs_city"],
                    candidate["irs_state"], candidate["irs_zip"], 1.0, address_score,
                    method, confidence, status, SCHEMA_VERSION, reason, manual_status,
                ),
            )

    # Manual accept/unmatch decisions take precedence after automated candidates.
    for override in manual_rows:
        try:
            f_num = int((override.get("f_num") or "").strip())
        except ValueError:
            continue
        action = (override.get("action") or "").strip().lower()
        ein = re.sub(r"\D", "", override.get("ein") or "")
        if action not in {"accept", "unmatch"}:
            continue
        conn.execute(
            "UPDATE irs_matches SET match_status='CANDIDATE_REVIEW' WHERE f_num=? AND match_status='MATCHED_HIGH_CONFIDENCE'",
            (f_num,),
        )
        org = conn.execute("SELECT * FROM organizations WHERE f_num=?", (f_num,)).fetchone()
        if not org:
            continue
        if action == "unmatch":
            conn.execute(
                """
                INSERT INTO irs_matches (
                  f_num,olms_name,olms_city,olms_state,olms_zip,name_score,address_score,
                  match_method,confidence,match_status,match_version,reason,manual_override_status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f_num, org["display_name"], org["city"], org["state"], org["zip"],
                    0.0, 0.0, "MANUAL_UNMATCH", 1.0, "UNMATCHED", SCHEMA_VERSION,
                    normalize_text(override.get("note")) or "Manual unmatch override.", "unmatch",
                ),
            )
            continue
        candidate = conn.execute(
            "SELECT * FROM irs_identity_candidates WHERE ein=? ORDER BY name_norm LIMIT 1", (ein,)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO irs_matches (
              f_num,candidate_ein,olms_name,irs_name,olms_city,olms_state,olms_zip,
              irs_city,irs_state,irs_zip,name_score,address_score,match_method,confidence,
              match_status,match_version,reason,manual_override_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f_num, ein, org["display_name"], candidate["irs_name"] if candidate else None,
                org["city"], org["state"], org["zip"],
                candidate["irs_city"] if candidate else None,
                candidate["irs_state"] if candidate else None,
                candidate["irs_zip"] if candidate else None,
                1.0 if candidate else None, 1.0, "MANUAL_ACCEPT", 1.0,
                "MATCHED_MANUAL", SCHEMA_VERSION,
                normalize_text(override.get("note")) or "Manual accept override.", "accept",
            ),
        )

    if match_counterparties:
        conn.executescript(
            """
            DROP TABLE IF EXISTS temp_counterparty_irs_matches;
            CREATE TEMP TABLE temp_counterparty_irs_matches AS
            SELECT cp.counterparty_id, MIN(c.ein) AS ein,
                   CASE WHEN cp.zip5<>'' AND cp.zip5=c.irs_zip THEN 'EXACT_NAME_ZIP5'
                        ELSE 'EXACT_NAME_CITY_STATE' END AS method,
                   CASE WHEN cp.zip5<>'' AND cp.zip5=c.irs_zip THEN 0.99 ELSE 0.97 END AS confidence
            FROM counterparties cp
            JOIN irs_identity_candidates c ON c.name_norm=cp.canonical_name_norm
            WHERE (cp.zip5<>'' AND cp.zip5=c.irs_zip)
               OR (cp.city<>'' AND cp.state<>'' AND UPPER(cp.city)=UPPER(c.irs_city) AND UPPER(cp.state)=UPPER(c.irs_state))
            GROUP BY cp.counterparty_id
            HAVING COUNT(DISTINCT c.ein)=1;

            UPDATE counterparties
            SET matched_ein=(SELECT m.ein FROM temp_counterparty_irs_matches m WHERE m.counterparty_id=counterparties.counterparty_id),
                match_status='MATCHED_HIGH_CONFIDENCE',
                match_method=(SELECT m.method FROM temp_counterparty_irs_matches m WHERE m.counterparty_id=counterparties.counterparty_id),
                match_confidence=(SELECT m.confidence FROM temp_counterparty_irs_matches m WHERE m.counterparty_id=counterparties.counterparty_id)
            WHERE counterparty_id IN (SELECT counterparty_id FROM temp_counterparty_irs_matches);
            DROP TABLE temp_counterparty_irs_matches;
            """
        )
    conn.executescript(
        """
        CREATE INDEX idx_olms_irs_matches_fnum ON irs_matches(f_num);
        CREATE INDEX idx_olms_irs_matches_ein ON irs_matches(candidate_ein);
        CREATE INDEX idx_olms_irs_matches_status ON irs_matches(match_status);
        CREATE VIEW v_accepted_irs_matches AS
        WITH ranked AS (
          SELECT m.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY f_num
                   ORDER BY CASE match_status WHEN 'MATCHED_MANUAL' THEN 0 ELSE 1 END,
                            confidence DESC, match_id DESC
                 ) AS rn
          FROM irs_matches m
          WHERE match_status IN ('MATCHED_HIGH_CONFIDENCE','MATCHED_MANUAL')
        )
        SELECT * FROM ranked WHERE rn=1;
        """
    )
    conn.row_factory = None


def create_payment_views(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_vendor_transactions;
        DROP VIEW IF EXISTS v_vendor_payments_summary;
        DROP VIEW IF EXISTS v_grant_transactions;
        DROP VIEW IF EXISTS v_grants_paid_summary;
        DROP VIEW IF EXISTS v_payment_transactions;
        DROP VIEW IF EXISTS v_payment_payees;

        CREATE VIEW v_payment_payees AS
        SELECT pp.row_id AS payer_payee_row_id,
               f.f_num, o.display_name AS union_name, o.affiliation, o.state AS union_state,
               fp.period_start, fp.period_end, fp.latest_form_type AS form_type,
               pp.rpt_id, pp.payer_payee_id, pp.payer_payee_type,
               pp.rcpt_disb_type AS disbursement_code,
               COALESCE(ec.code_description, ec.code_name,
                        CASE pp.rcpt_disb_type
                          WHEN 501 THEN 'REPRESENTATIONAL' WHEN 502 THEN 'POLITICAL'
                          WHEN 503 THEN 'CONTRIBUTIONS, GIFTS AND GRANTS'
                          WHEN 504 THEN 'GENERAL OVERHEAD' WHEN 505 THEN 'UNION ADMINISTRATION'
                          WHEN 506 THEN 'GENERAL DISBURSEMENTS' END) AS disbursement_category,
               ca.counterparty_id, cp.matched_ein, cp.match_status AS counterparty_match_status,
               pp.name AS payee_name, pp.po_box, pp.street AS payee_address,
               pp.city AS payee_city, pp.state AS payee_state, pp.zip AS payee_zip,
               pp.type_or_class, pp.itemized AS itemized_amount,
               pp.non_itemized AS non_itemized_amount, pp.total AS total_amount
        FROM payer_payee pp
        JOIN filing_periods fp ON fp.latest_rpt_id=pp.rpt_id
        JOIN filings f ON f.rpt_id=fp.latest_rpt_id
        JOIN organizations o ON o.f_num=f.f_num
        LEFT JOIN counterparty_assignments ca ON ca.payer_payee_row_id=pp.row_id
        LEFT JOIN counterparties cp ON cp.counterparty_id=ca.counterparty_id
        LEFT JOIN erds_codes ec ON ec.code_type='DISBURSEMENT_CODE' AND ec.code=pp.rcpt_disb_type
        WHERE pp.payer_payee_type=1002;

        CREATE VIEW v_payment_transactions AS
        SELECT p.*, d.oid AS transaction_oid, d.date AS transaction_date,
               d.amount AS transaction_amount, d.purpose
        FROM v_payment_payees p
        JOIN disbursements_general d
          ON d.rpt_id=p.rpt_id AND d.payer_payee_id=p.payer_payee_id;

        CREATE VIEW v_grants_paid_summary AS
        SELECT * FROM v_payment_payees WHERE disbursement_code=503;
        CREATE VIEW v_grant_transactions AS
        SELECT * FROM v_payment_transactions WHERE disbursement_code=503;
        CREATE VIEW v_vendor_payments_summary AS
        SELECT * FROM v_payment_payees WHERE disbursement_code<>503;
        CREATE VIEW v_vendor_transactions AS
        SELECT * FROM v_payment_transactions WHERE disbursement_code<>503;
        """
    )


def build_stats_cache(conn: sqlite3.Connection, as_of_date: str) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS olms_stats_cache;
        CREATE TABLE olms_stats_cache (
          metric TEXT NOT NULL,
          bucket TEXT NOT NULL DEFAULT '',
          value INTEGER,
          notes TEXT,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(metric,bucket)
        );
        """
    )
    metrics = [
        ("years_loaded", "", "SELECT COUNT(DISTINCT _source_year) FROM filings", "Distinct source-year directories"),
        ("unique_labor_organizations", "", "SELECT COUNT(*) FROM organizations", "Unique OLMS F_NUM values"),
        ("total_reports", "", "SELECT COUNT(*) FROM filings", "Imported filing records including amendments"),
        ("form_count", "LM-2", "SELECT COUNT(*) FROM filings WHERE UPPER(form_type)='LM-2'", ""),
        ("form_count", "LM-3", "SELECT COUNT(*) FROM filings WHERE UPPER(form_type)='LM-3'", ""),
        ("form_count", "LM-4", "SELECT COUNT(*) FROM filings WHERE UPPER(form_type)='LM-4'", ""),
        ("affiliation_count", "NEA", "SELECT COUNT(*) FROM organizations WHERE UPPER(affiliation)='NEA'", "Unique F_NUMs"),
        ("affiliation_count", "AFT", "SELECT COUNT(*) FROM organizations WHERE UPPER(affiliation)='AFT'", "Unique F_NUMs"),
        ("likely_education", "", "SELECT COUNT(*) FROM organizations WHERE education_scope IN ('likely_education','education_or_mixed','manual_include')", ""),
        ("irs_high_confidence_matches", "", "SELECT COUNT(DISTINCT f_num) FROM irs_matches WHERE match_status IN ('MATCHED_HIGH_CONFIDENCE','MATCHED_MANUAL')", ""),
        ("irs_unmatched", "", "SELECT COUNT(DISTINCT f_num) FROM irs_matches WHERE match_status='UNMATCHED'", ""),
        ("grant_payee_rows", "", "SELECT COUNT(*) FROM v_grants_paid_summary", "Payee summary rows; not added to transactions"),
        ("counterparty_identities", "", "SELECT COUNT(*) FROM counterparties", ""),
        ("potential_missing_filings", "", "SELECT COUNT(*) FROM compliance_results WHERE status='POTENTIAL_MISSING_FILING'", ""),
        ("historically_late_filings", "", "SELECT COUNT(*) FROM compliance_results WHERE status='FILED_LATE'", ""),
        ("repaired_records", "", "SELECT COUNT(*) FROM import_repairs", ""),
        ("quarantined_records", "", "SELECT COUNT(*) FROM import_errors", ""),
        ("orphan_detail_rows", "", "SELECT COUNT(*) FROM import_orphans", ""),
        ("duplicate_conflicts", "", "SELECT COUNT(*) FROM import_duplicate_conflicts WHERE conflict_type='CONFLICTING_DUPLICATE'", ""),
    ]
    updated = now_stamp()
    rows = []
    for metric, bucket, sql, notes in metrics:
        value = conn.execute(sql).fetchone()[0]
        rows.append((metric, bucket, int(value or 0), notes, updated))
    rows.append(("data_as_of", as_of_date, None, "Maximum loaded OLMS receive date used for compliance calculations", updated))
    conn.executemany("INSERT INTO olms_stats_cache VALUES (?,?,?,?,?)", rows)


def rebuild_derived(
    conn: sqlite3.Connection,
    *,
    as_of_date: Optional[str] = None,
    scope_overrides: Optional[Path] = None,
    irs_db_path: Optional[Path] = None,
    irs_match_overrides: Optional[Path] = None,
    skip_irs_matching: bool = False,
    match_counterparties: bool = True,
) -> str:
    build_organizations(conn, scope_overrides)
    build_filing_periods(conn)
    data_as_of = build_compliance_results(conn, as_of_date)
    build_counterparties(conn)
    build_irs_matches(
        conn,
        None if skip_irs_matching else irs_db_path,
        irs_match_overrides,
        match_counterparties=match_counterparties and not skip_irs_matching,
    )
    create_payment_views(conn)
    build_stats_cache(conn, data_as_of)
    conn.execute(
        "INSERT OR REPLACE INTO olms_meta(key,value,updated_at) VALUES ('data_as_of',?,?)",
        (data_as_of, now_stamp()),
    )
    return data_as_of


def _delete_refreshed_years(conn: sqlite3.Connection, years: Sequence[int]) -> None:
    placeholders = ",".join("?" for _ in years)
    for table_name in _source_tables(conn):
        if "_source_year" in _table_columns(conn, table_name):
            conn.execute(
                f"DELETE FROM {quote_ident(table_name)} WHERE _source_year IN ({placeholders})",
                tuple(years),
            )
    source_ids = [
        row[0]
        for row in conn.execute(
            f"SELECT import_source_id FROM import_sources WHERE source_year IN ({placeholders})",
            tuple(years),
        )
    ]
    if source_ids:
        source_ph = ",".join("?" for _ in source_ids)
        for table in ("import_repairs", "import_errors", "import_table_stats"):
            conn.execute(f"DELETE FROM {table} WHERE import_source_id IN ({source_ph})", source_ids)


def _copy_database(source: Path, destination: Path) -> None:
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def build_database(
    input_root: Path,
    db_path: Path,
    *,
    years: Optional[Sequence[int]] = None,
    rebuild: bool = True,
    as_of_date: Optional[str] = None,
    allow_filing_errors: bool = False,
    exports_dir: Optional[Path] = None,
    scope_overrides: Optional[Path] = None,
    irs_db_path: Optional[Path] = None,
    irs_match_overrides: Optional[Path] = None,
    skip_irs_matching: bool = False,
    match_counterparties: bool = True,
) -> Dict[str, object]:
    input_root = Path(input_root).expanduser().resolve()
    db_path = Path(db_path).expanduser().resolve()
    sources = discover_sources(input_root, years)
    selected_years = sorted({source.year for source in sources})
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = db_path.parent / f".{db_path.stem}.build-{uuid.uuid4().hex}.db"
    if not rebuild:
        if not db_path.exists():
            raise FileNotFoundError(f"Cannot refresh missing OLMS database: {db_path}")
        _copy_database(db_path, temp_path)

    conn = sqlite3.connect(temp_path)
    build_ready = False
    run_id = None
    summary: Dict[str, object] = {}
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-500000")
        create_audit_schema(conn)
        drop_source_indexes(conn)
        if not rebuild:
            _delete_refreshed_years(conn, selected_years)
        run_id = int(
            conn.execute(
                """
                INSERT INTO import_runs(started_at,input_directory,requested_years,mode,status)
                VALUES (?,?,?,?,?)
                """,
                (now_stamp(), str(input_root), ",".join(map(str, selected_years)), "REBUILD" if rebuild else "REFRESH", "RUNNING"),
            ).lastrowid
        )
        year_hashes: Dict[int, List[Tuple[str, str]]] = defaultdict(list)
        for source in sources:
            stats = import_source(conn, run_id, source)
            year_hashes[source.year].extend(
                [
                    (source.data_path.name, str(stats["data_hash"])),
                    (source.meta_path.name, str(stats["meta_hash"])),
                ]
            )
            conn.commit()
        for year, hashes in year_hashes.items():
            combined = hashlib.sha256(
                json.dumps(sorted(hashes), separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            conn.execute(
                "INSERT INTO import_years VALUES (?,?,?,?)",
                (run_id, year, str(input_root / str(year)), combined),
            )

        conn.execute("DELETE FROM import_duplicate_conflicts")
        identical, conflicting = deduplicate_sources(conn, run_id)
        orphan_count = record_orphans(conn, run_id)
        create_source_indexes(conn)
        data_as_of = rebuild_derived(
            conn,
            as_of_date=as_of_date,
            scope_overrides=scope_overrides,
            irs_db_path=irs_db_path,
            irs_match_overrides=irs_match_overrides,
            skip_irs_matching=skip_irs_matching,
            match_counterparties=match_counterparties,
        )
        totals = conn.execute(
            """
            SELECT COALESCE(SUM(rows_attempted),0),COALESCE(SUM(rows_loaded),0),
                   COALESCE(SUM(rows_repaired),0),COALESCE(SUM(rows_quarantined),0)
            FROM import_sources WHERE import_run_id=?
            """,
            (run_id,),
        ).fetchone()
        filing_errors = int(
            conn.execute(
                "SELECT COUNT(*) FROM import_errors WHERE import_run_id=? AND logical_table='filings'",
                (run_id,),
            ).fetchone()[0]
        )
        central_conflicts = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM import_duplicate_conflicts
                WHERE import_run_id=? AND canonical_table='filings' AND conflict_type='CONFLICTING_DUPLICATE'
                """,
                (run_id,),
            ).fetchone()[0]
        )
        if (filing_errors or central_conflicts) and not allow_filing_errors:
            raise RuntimeError(
                f"Central filings validation failed: {filing_errors} malformed record(s), "
                f"{central_conflicts} conflicting duplicate key(s). Use --allow-filing-errors only after audit review."
            )
        status = "COMPLETED_WITH_WARNINGS" if int(totals[3]) or conflicting or orphan_count else "COMPLETED"
        conn.execute(
            """
            UPDATE import_runs SET completed_at=?,status=?,rows_attempted=?,rows_loaded=?,
              rows_repaired=?,rows_quarantined=?,duplicate_rows=?,conflicting_duplicates=?,orphan_rows=?
            WHERE import_run_id=?
            """,
            (
                now_stamp(), status, *map(int, totals), identical, conflicting, orphan_count, run_id,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO olms_meta(key,value,updated_at) VALUES ('schema_version',?,?)",
            (SCHEMA_VERSION, now_stamp()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO olms_meta(key,value,updated_at) VALUES ('latest_build_at',?,?)",
            (now_stamp(), now_stamp()),
        )
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"OLMS SQLite integrity_check failed: {integrity}")
        summary = {
            "import_run_id": run_id,
            "status": status,
            "years": selected_years,
            "rows_attempted": int(totals[0]),
            "rows_loaded": int(totals[1]),
            "rows_repaired": int(totals[2]),
            "rows_quarantined": int(totals[3]),
            "duplicate_rows": identical,
            "conflicting_duplicates": conflicting,
            "orphan_rows": orphan_count,
            "data_as_of": data_as_of,
            "temporary_database": str(temp_path),
        }
        build_ready = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if not build_ready:
            temp_path.unlink(missing_ok=True)

    os.replace(temp_path, db_path)
    summary["database"] = str(db_path)
    summary.pop("temporary_database", None)
    if exports_dir is not None:
        export_audit_reports(db_path, Path(exports_dir), int(summary["import_run_id"]))
    return summary


def export_audit_reports(db_path: Path, exports_dir: Path, run_id: Optional[int] = None) -> List[Path]:
    exports_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        if run_id is None:
            row = conn.execute("SELECT MAX(import_run_id) FROM import_runs").fetchone()
            run_id = int(row[0]) if row and row[0] else 0
        reports = [
            (
                "olms_import_summary.csv",
                """
                SELECT r.*,s.source_year,s.logical_table,s.canonical_table,s.data_filename,
                       s.data_sha256,s.schema_hash,s.rows_attempted AS source_rows_attempted,
                       s.rows_loaded AS source_rows_loaded,s.rows_repaired AS source_rows_repaired,
                       s.rows_quarantined AS source_rows_quarantined,s.status AS source_status
                FROM import_runs r JOIN import_sources s USING(import_run_id)
                WHERE r.import_run_id=? ORDER BY s.source_year,s.logical_table
                """,
            ),
            ("olms_import_repairs.csv", "SELECT * FROM import_repairs WHERE import_run_id=? ORDER BY repair_id"),
            ("olms_import_errors.csv", "SELECT * FROM import_errors WHERE import_run_id=? ORDER BY error_id"),
            (
                "olms_duplicate_conflicts.csv",
                "SELECT * FROM import_duplicate_conflicts WHERE import_run_id=? ORDER BY duplicate_id",
            ),
        ]
        written = []
        for filename, sql in reports:
            cursor = conn.execute(sql, (run_id,))
            path = exports_dir / filename
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow([item[0] for item in cursor.description])
                writer.writerows(cursor)
            written.append(path)
        return written
    finally:
        conn.close()
