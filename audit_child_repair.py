#!/usr/bin/env python3
"""Compare repeated IRS child rows before and after a clean database rebuild.

The source and repaired databases are opened with SQLite ``mode=ro`` and
``query_only=ON``.  The exact returns/provenance population and a typed,
order-independent payload digest for every child filing are streamed through
filing-id indexes.  Full payload multisets are fetched only for mismatches.

The audit is intentionally fail-closed for missing repaired rows, changed
payload sets, orphan/unexplained rows, schema differences, and missing filing
indexes.  A repaired database may contain a genuinely new filing child set;
use ``--fail-on-new`` when an exact source-equivalent rebuild is required.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from rebuild_irs990_slim_clean import MULTIROW_CHILD_TABLES, object_id_from_filing_id


FORMAT_VERSION = 2
HARD_FAILURE_CLASSES = {"missing_in_rebuild", "content_changed", "unexplained"}
SQLITE_COMPANION_SUFFIXES = ("-wal", "-shm", "-journal")
MULTISET_MASK = (1 << 256) - 1

DETAIL_FIELDS = [
    "table_name",
    "filing_id",
    "ein",
    "tax_year",
    "return_type",
    "source_file",
    "repaired_source_file",
    "source_object_id",
    "repaired_object_id",
    "source_count",
    "repaired_count",
    "row_delta_source_minus_repaired",
    "classification",
    "gate_failure",
    "source_payload_digest",
    "repaired_payload_digest",
    "source_unique_payloads",
    "repaired_unique_payloads",
    "exact_extra_rows",
    "new_payload_rows",
    "whole_set_replay_factor",
    "source_grant_core_total",
    "source_grant_detail_total",
    "source_grant_difference",
    "source_grant_material_mismatch",
    "source_grant_inflated",
    "repaired_grant_core_total",
    "repaired_grant_detail_total",
    "repaired_grant_difference",
    "repaired_grant_material_mismatch",
    "repaired_grant_inflated",
    "notes",
]

SUMMARY_FIELDS = [
    "table_name",
    "status",
    "source_index",
    "repaired_index",
    "source_rows",
    "repaired_rows",
    "source_filing_groups",
    "repaired_filing_groups",
    "source_distinct_files",
    "repaired_distinct_files",
    "source_distinct_objects",
    "repaired_distinct_objects",
    "source_file_covered_rows",
    "repaired_source_file_covered_rows",
    "source_object_covered_filings",
    "repaired_object_covered_filings",
    "mismatched_filings",
    "expected_exact_replay_cleanup",
    "missing_in_rebuild",
    "new_in_rebuild",
    "content_changed",
    "unexplained",
    "source_extra_rows",
    "repaired_extra_rows",
    "whole_set_replays",
    "grant_source_inflated",
    "grant_repaired_inflated",
    "gate_failures",
    "notes",
]


class AuditInvariantError(RuntimeError):
    """Raised when a database cannot be compared safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def connect_readonly(path: Path) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    conn = sqlite3.connect(readonly_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise AuditInvariantError(f"query_only could not be enabled for {path}")
    # Hold one consistent read snapshot for the duration of the audit. Writers
    # must still be stopped so a source WAL cannot grow for hours.
    conn.execute("BEGIN")
    return conn


@lru_cache(maxsize=None)
def object_exists(conn: sqlite3.Connection, name: str, kind: str = "table") -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type=? AND name=? LIMIT 1",
        (kind, name),
    ).fetchone() is not None


@lru_cache(maxsize=None)
def table_columns(conn: sqlite3.Connection, table: str) -> Tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({quote_identifier(table)})")
    )


def leading_filing_index(conn: sqlite3.Connection, table: str) -> str:
    if not object_exists(conn, table):
        raise AuditInvariantError(f"missing table: {table}")
    columns = table_columns(conn, table)
    if "filing_id" not in columns:
        raise AuditInvariantError(f"{table} has no filing_id column")

    candidates: List[Tuple[int, str]] = []
    for row in conn.execute(f"PRAGMA index_list({quote_identifier(table)})"):
        index_name = str(row[1])
        key_columns = [
            str(info[2]) if info[2] is not None else ""
            for info in conn.execute(f"PRAGMA index_xinfo({quote_identifier(index_name)})")
            if int(info[5]) == 1
        ]
        if key_columns and key_columns[0].lower() == "filing_id":
            candidates.append((len(key_columns), index_name))
    if not candidates:
        raise AuditInvariantError(f"{table} has no index led by filing_id")
    candidates.sort(key=lambda item: (item[0], item[1].casefold()))
    return candidates[0][1]


def filing_sort_key(filing_id: Any) -> Tuple[int, str]:
    if filing_id is None:
        return (0, "")
    return (1, str(filing_id))


def scalar_token(value: Any) -> List[Any]:
    if value is None:
        return ["null", None]
    if isinstance(value, bytes):
        return ["blob", base64.b64encode(value).decode("ascii")]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, float):
        if math.isnan(value):
            return ["float", "nan"]
        if math.isinf(value):
            return ["float", "+inf" if value > 0 else "-inf"]
        return ["float", value.hex()]
    return ["text", str(value)]


def payload_key(row: sqlite3.Row, payload_columns: Sequence[str]) -> str:
    values = [[column, scalar_token(row[column])] for column in payload_columns]
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


class StreamingMultisetDigest:
    """Order-independent, multiplicity-sensitive digest with constant memory.

    Each canonical payload is cryptographically hashed, then two independently
    domain-separated 256-bit values are added modulo 2**256.  The row count and
    accumulators are hashed again for the published digest.  Addition makes the
    result independent of row order while retaining duplicate multiplicity.
    """

    __slots__ = ("row_count", "_sum_primary", "_sum_secondary")

    def __init__(self) -> None:
        self.row_count = 0
        self._sum_primary = 0
        self._sum_secondary = 0

    def add(self, payload: str, multiplicity: int = 1) -> None:
        multiplicity = int(multiplicity)
        if multiplicity < 0:
            raise ValueError("multiset multiplicity cannot be negative")
        if not multiplicity:
            return
        encoded = payload.encode("utf-8")
        primary_bytes = hashlib.sha256(b"payload-v2\x00" + encoded).digest()
        secondary_bytes = hashlib.sha256(b"payload-v2\x01" + encoded).digest()
        self._sum_primary = (
            self._sum_primary + int.from_bytes(primary_bytes, "big") * multiplicity
        ) & MULTISET_MASK
        self._sum_secondary = (
            self._sum_secondary + int.from_bytes(secondary_bytes, "big") * multiplicity
        ) & MULTISET_MASK
        self.row_count += multiplicity

    def hexdigest(self) -> str:
        digest = hashlib.sha256()
        digest.update(b"irs990-child-multiset-v2\x00")
        digest.update(int(self.row_count).to_bytes(16, "big"))
        digest.update(int(self._sum_primary).to_bytes(32, "big"))
        digest.update(int(self._sum_secondary).to_bytes(32, "big"))
        return digest.hexdigest()


def multiset_digest(counter: Counter[str]) -> str:
    digest = StreamingMultisetDigest()
    for payload, count in counter.items():
        digest.add(payload, count)
    return digest.hexdigest()


@dataclass(frozen=True)
class FilingDigest:
    filing_id: Any
    row_count: int
    payload_digest: str


def payload_digest_stream(
    conn: sqlite3.Connection,
    table: str,
    index_name: str,
    payload_columns: Sequence[str],
) -> Iterator[FilingDigest]:
    """Stream one bounded-memory payload multiset digest per filing."""

    selected_columns = ["filing_id", *payload_columns]
    selected_sql = ",".join(quote_identifier(column) for column in selected_columns)
    sql = (
        f"SELECT {selected_sql} FROM {quote_identifier(table)} "
        f"INDEXED BY {quote_identifier(index_name)} ORDER BY filing_id"
    )
    sentinel = object()
    current_filing: Any = sentinel
    digest = StreamingMultisetDigest()
    for row in conn.execute(sql):
        filing_id = row["filing_id"]
        if current_filing is not sentinel and filing_id != current_filing:
            yield FilingDigest(current_filing, digest.row_count, digest.hexdigest())
            digest = StreamingMultisetDigest()
        if current_filing is sentinel or filing_id != current_filing:
            current_filing = filing_id
        digest.add(payload_key(row, payload_columns))
    if current_filing is not sentinel:
        yield FilingDigest(current_filing, digest.row_count, digest.hexdigest())


def decimal_value(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


@dataclass
class PayloadResult:
    counter: Counter[str]
    row_count: int
    grant_detail_total: Optional[Decimal]
    invalid_grant_numbers: int = 0


def fetch_payload(
    conn: sqlite3.Connection,
    table: str,
    index_name: str,
    filing_id: Any,
    payload_columns: Sequence[str],
) -> PayloadResult:
    selected_columns = ["filing_id", *payload_columns]
    selected_sql = ",".join(quote_identifier(column) for column in selected_columns)
    sql = (
        f"SELECT {selected_sql} FROM {quote_identifier(table)} "
        f"INDEXED BY {quote_identifier(index_name)} WHERE filing_id IS ?"
    )
    counter: Counter[str] = Counter()
    row_count = 0
    grant_total = Decimal(0) if table == "grants" else None
    invalid_grant_numbers = 0
    for row in conn.execute(sql, (filing_id,)):
        row_count += 1
        counter[payload_key(row, payload_columns)] += 1
        if table == "grants":
            for column in ("cash_grant_amt", "non_cash_assistance_amt"):
                value = row[column]
                if value in (None, ""):
                    continue
                number = decimal_value(value)
                if number is None:
                    invalid_grant_numbers += 1
                else:
                    grant_total += number
    return PayloadResult(counter, row_count, grant_total, invalid_grant_numbers)


def source_file_object_id(source_file: Any) -> str:
    value = str(source_file or "").strip().replace("\\", "/")
    filename = value.rsplit("/", 1)[-1]
    if filename.lower().endswith(".xml"):
        filename = filename[:-4]
    return object_id_from_filing_id(filename)


@dataclass
class ReturnPopulationGroup:
    filing_id: Any
    row_count: int
    mapping_counter: Counter[str]
    source_file_covered_rows: int
    object_ids: Tuple[str, ...]
    source_file: Optional[str]
    problems: List[str]

    @property
    def payload_digest(self) -> str:
        return multiset_digest(self.mapping_counter)

    @property
    def object_id(self) -> Optional[str]:
        return self.object_ids[0] if len(self.object_ids) == 1 else None

    @property
    def object_covered(self) -> int:
        return int(
            self.row_count == 1
            and self.source_file_covered_rows == 1
            and len(self.object_ids) == 1
            and not self.problems
        )


def build_return_group(filing_id: Any, source_files: Sequence[Any]) -> ReturnPopulationGroup:
    filing_text = str(filing_id or "").strip()
    filing_object = object_id_from_filing_id(filing_text)
    counter: Counter[str] = Counter()
    covered = 0
    object_ids = set()
    problems: List[str] = []
    exemplar: Optional[str] = None
    if not filing_text:
        problems.append("blank_or_null_filing_id")
    if len(source_files) != 1:
        problems.append(f"duplicate_returns_filing_id:rows={len(source_files)}")
    for raw_source in source_files:
        raw_text = "" if raw_source is None else str(raw_source)
        source_file = raw_text.strip()
        if exemplar is None and source_file:
            exemplar = raw_text
        source_object = source_file_object_id(source_file)
        counter[
            json.dumps(scalar_token(raw_source), ensure_ascii=False, separators=(",", ":"))
        ] += 1
        if not source_file:
            problems.append("blank_returns_source_file")
            continue
        covered += 1
        if not source_object:
            problems.append("blank_source_file_object_id")
            continue
        object_ids.add(source_object)
        if not filing_object or source_object != filing_object:
            problems.append(
                f"source_file_object_mismatch:filing={filing_object!r},source={source_object!r}"
            )
    return ReturnPopulationGroup(
        filing_id=filing_id,
        row_count=len(source_files),
        mapping_counter=counter,
        source_file_covered_rows=covered,
        object_ids=tuple(sorted(object_ids)),
        source_file=exemplar,
        problems=list(dict.fromkeys(problems)),
    )


def returns_population_stream(
    conn: sqlite3.Connection,
    index_name: str,
) -> Iterator[ReturnPopulationGroup]:
    required = {"filing_id", "source_file"}
    missing = required.difference(table_columns(conn, "returns"))
    if missing:
        raise AuditInvariantError(
            f"returns is missing required column(s): {', '.join(sorted(missing))}"
        )
    sql = (
        "SELECT filing_id,source_file FROM returns "
        f"INDEXED BY {quote_identifier(index_name)} ORDER BY filing_id"
    )
    sentinel = object()
    current_filing: Any = sentinel
    source_files: List[Any] = []
    for row in conn.execute(sql):
        filing_id = row["filing_id"]
        if current_filing is not sentinel and filing_id != current_filing:
            yield build_return_group(current_filing, source_files)
            source_files = []
        if current_filing is sentinel or filing_id != current_filing:
            current_filing = filing_id
        source_files.append(row["source_file"])
    if current_filing is not sentinel:
        yield build_return_group(current_filing, source_files)


def returns_coverage_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Return the same exact one-to-one coverage counts used by clean rebuilds."""

    if not object_exists(conn, "returns"):
        raise AuditInvariantError("missing table: returns")
    required = {"filing_id", "source_file"}
    missing = required.difference(table_columns(conn, "returns"))
    if missing:
        raise AuditInvariantError(
            f"returns is missing required column(s): {', '.join(sorted(missing))}"
        )
    conn.create_function(
        "audit_object_id",
        1,
        object_id_from_filing_id,
        deterministic=True,
    )
    row = conn.execute(
        """
        SELECT COUNT(*) AS return_count,
               COUNT(DISTINCT filing_id) AS filing_count,
               COUNT(DISTINCT source_file) AS source_count,
               COUNT(DISTINCT audit_object_id(filing_id)) AS object_count
        FROM returns
        """
    ).fetchone()
    return {
        "returns": int(row[0]),
        "filings": int(row[1]),
        "sources": int(row[2]),
        "objects": int(row[3]),
    }


def selected_metadata_columns(conn: sqlite3.Connection) -> List[str]:
    if not object_exists(conn, "returns"):
        return []
    available = set(table_columns(conn, "returns"))
    return [
        column
        for column in ("filing_id", "source_file", "ein", "tax_year", "return_type", "org_name")
        if column in available
    ]


def filing_metadata(conn: sqlite3.Connection, filing_id: Any) -> Optional[Dict[str, Any]]:
    columns = selected_metadata_columns(conn)
    if "filing_id" not in columns:
        return None
    sql = (
        f"SELECT {','.join(quote_identifier(column) for column in columns)} "
        "FROM returns WHERE filing_id IS ? LIMIT 1"
    )
    row = conn.execute(sql, (filing_id,)).fetchone()
    if row is None:
        return None
    result = {column: row[column] for column in columns}
    for column in ("source_file", "ein", "tax_year", "return_type", "org_name"):
        result.setdefault(column, None)
    return result


def table_scalar(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    filing_id: Any,
) -> Any:
    if not object_exists(conn, table) or column not in table_columns(conn, table):
        return None
    row = conn.execute(
        f"SELECT {quote_identifier(column)} FROM {quote_identifier(table)} "
        "WHERE filing_id IS ? LIMIT 1",
        (filing_id,),
    ).fetchone()
    return None if row is None else row[0]


def grant_core_total(
    conn: sqlite3.Connection,
    filing_id: Any,
    metadata: Optional[Mapping[str, Any]],
) -> Optional[Decimal]:
    return_type = str((metadata or {}).get("return_type") or "").upper()
    candidates: List[Tuple[str, str]] = []
    if return_type.startswith("990PF"):
        candidates.append(("irs990_pf_root", "total_grant_or_contri_pd_dur_yr_amt"))
    elif return_type.startswith("990EZ"):
        candidates.append(("irs990_ez_root", "grants_and_similar_amounts_paid_amt"))
    elif return_type.startswith("990") and not return_type.startswith("990T"):
        candidates.append(("irs990_root", "cygrants_and_similar_paid_amt"))
    candidates.append(("core_hot", "grants_paid"))
    for table, column in candidates:
        value = decimal_value(table_scalar(conn, table, column, filing_id))
        if value is not None:
            return value
    return None


def decimal_text(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value, "f")


def grant_reconciliation(
    core_total: Optional[Decimal],
    detail_total: Optional[Decimal],
) -> Dict[str, Any]:
    if detail_total is None:
        return {
            "core_total": decimal_text(core_total),
            "detail_total": None,
            "difference": None,
            "material_mismatch": None,
            "inflated": None,
        }
    difference = None if core_total is None else detail_total - core_total
    material = False
    inflated = False
    if core_total is not None and core_total > 0 and difference is not None:
        material = abs(difference) >= max(Decimal("10000"), core_total * Decimal("0.20"))
        inflated = material and detail_total > core_total * Decimal("1.25")
    return {
        "core_total": decimal_text(core_total),
        "detail_total": decimal_text(detail_total),
        "difference": decimal_text(difference),
        "material_mismatch": int(material),
        "inflated": int(inflated),
    }


def whole_set_replay_factor(source: Counter[str], repaired: Counter[str]) -> Optional[int]:
    if not source or set(source) != set(repaired):
        return None
    ratios = set()
    for payload, repaired_count in repaired.items():
        if repaired_count <= 0 or source[payload] % repaired_count:
            return None
        ratios.add(source[payload] // repaired_count)
    if len(ratios) == 1:
        factor = next(iter(ratios))
        return factor if factor > 1 else None
    return None


def classify_payloads(
    source: PayloadResult,
    repaired: PayloadResult,
) -> Tuple[str, int, int, Optional[int]]:
    if source.row_count and not repaired.row_count:
        return "missing_in_rebuild", source.row_count, 0, None
    if repaired.row_count and not source.row_count:
        return "new_in_rebuild", 0, repaired.row_count, None

    source_keys = set(source.counter)
    repaired_keys = set(repaired.counter)
    exact_extra = sum((source.counter - repaired.counter).values())
    new_rows = sum((repaired.counter - source.counter).values())
    if (
        source.row_count > repaired.row_count
        and source_keys == repaired_keys
        and new_rows == 0
        and exact_extra == source.row_count - repaired.row_count
    ):
        return (
            "expected_exact_replay_cleanup",
            exact_extra,
            0,
            whole_set_replay_factor(source.counter, repaired.counter),
        )
    return "content_changed", exact_extra, new_rows, None


def blank_detail(table: str, note: str) -> Dict[str, Any]:
    row = {field: None for field in DETAIL_FIELDS}
    row.update(
        {
            "table_name": table,
            "classification": "unexplained",
            "gate_failure": 1,
            "notes": note,
        }
    )
    return row


def new_summary(table: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {field: 0 for field in SUMMARY_FIELDS}
    row.update(
        {
            "table_name": table,
            "status": "ok",
            "source_index": "",
            "repaired_index": "",
            "notes": "",
        }
    )
    return row


def next_or_none(iterator: Iterator[Any]) -> Optional[Any]:
    try:
        return next(iterator)
    except StopIteration:
        return None


def metadata_problem(
    filing_id: Any,
    source_count: int,
    repaired_count: int,
    source_meta: Optional[Mapping[str, Any]],
    repaired_meta: Optional[Mapping[str, Any]],
) -> Optional[str]:
    if filing_id is None or str(filing_id).strip() == "":
        return "blank_or_null_filing_id"
    if source_count and source_meta is None:
        return "source_child_has_no_returns_row"
    if repaired_count and repaired_meta is None:
        return "repaired_child_has_no_returns_row"
    if source_meta and repaired_meta:
        for key in ("ein", "tax_year", "return_type"):
            if str(source_meta.get(key) or "") != str(repaired_meta.get(key) or ""):
                return f"filing_metadata_changed:{key}"
    return None


def build_detail(
    table: str,
    filing_id: Any,
    source_count: int,
    repaired_count: int,
    source_conn: sqlite3.Connection,
    repaired_conn: sqlite3.Connection,
    source_index: str,
    repaired_index: str,
    payload_columns: Sequence[str],
    fail_on_new: bool,
) -> Dict[str, Any]:
    source_payload = fetch_payload(
        source_conn, table, source_index, filing_id, payload_columns
    )
    repaired_payload = fetch_payload(
        repaired_conn, table, repaired_index, filing_id, payload_columns
    )
    classification, exact_extra, new_rows, replay_factor = classify_payloads(
        source_payload, repaired_payload
    )
    notes: List[str] = []
    if source_payload.row_count != source_count:
        notes.append(
            f"source_payload_count_changed:{source_count}->{source_payload.row_count}"
        )
        classification = "unexplained"
    if repaired_payload.row_count != repaired_count:
        notes.append(
            f"repaired_payload_count_changed:{repaired_count}->{repaired_payload.row_count}"
        )
        classification = "unexplained"

    source_meta = filing_metadata(source_conn, filing_id)
    repaired_meta = filing_metadata(repaired_conn, filing_id)
    metadata_issue = metadata_problem(
        filing_id,
        source_count,
        repaired_count,
        source_meta,
        repaired_meta,
    )
    if metadata_issue:
        notes.append(metadata_issue)
        classification = "unexplained"
    meta = repaired_meta or source_meta or {}
    source_file = (source_meta or {}).get("source_file")
    repaired_source_file = (repaired_meta or {}).get("source_file")

    row: Dict[str, Any] = {field: None for field in DETAIL_FIELDS}
    row.update(
        {
            "table_name": table,
            "filing_id": filing_id,
            "ein": meta.get("ein"),
            "tax_year": meta.get("tax_year"),
            "return_type": meta.get("return_type"),
            "source_file": source_file,
            "repaired_source_file": repaired_source_file,
            "source_object_id": source_file_object_id(source_file),
            "repaired_object_id": source_file_object_id(repaired_source_file),
            "source_count": source_count,
            "repaired_count": repaired_count,
            "row_delta_source_minus_repaired": source_count - repaired_count,
            "classification": classification,
            "source_payload_digest": multiset_digest(source_payload.counter),
            "repaired_payload_digest": multiset_digest(repaired_payload.counter),
            "source_unique_payloads": len(source_payload.counter),
            "repaired_unique_payloads": len(repaired_payload.counter),
            "exact_extra_rows": exact_extra,
            "new_payload_rows": new_rows,
            "whole_set_replay_factor": replay_factor,
        }
    )

    if table == "grants":
        if source_payload.invalid_grant_numbers or repaired_payload.invalid_grant_numbers:
            notes.append(
                "invalid_grant_numbers:"
                f"source={source_payload.invalid_grant_numbers},"
                f"repaired={repaired_payload.invalid_grant_numbers}"
            )
            classification = "unexplained"
            row["classification"] = classification
        source_core = grant_core_total(source_conn, filing_id, source_meta)
        repaired_core = grant_core_total(repaired_conn, filing_id, repaired_meta)
        if source_count and source_core is None:
            notes.append("source_grant_core_total_unavailable")
            classification = "unexplained"
            row["classification"] = classification
        if repaired_count and repaired_core is None:
            notes.append("repaired_grant_core_total_unavailable")
            classification = "unexplained"
            row["classification"] = classification
        source_recon = grant_reconciliation(source_core, source_payload.grant_detail_total)
        repaired_recon = grant_reconciliation(repaired_core, repaired_payload.grant_detail_total)
        for prefix, reconciliation in (
            ("source", source_recon),
            ("repaired", repaired_recon),
        ):
            row[f"{prefix}_grant_core_total"] = reconciliation["core_total"]
            row[f"{prefix}_grant_detail_total"] = reconciliation["detail_total"]
            row[f"{prefix}_grant_difference"] = reconciliation["difference"]
            row[f"{prefix}_grant_material_mismatch"] = reconciliation["material_mismatch"]
            row[f"{prefix}_grant_inflated"] = reconciliation["inflated"]

    gate_failure = classification in HARD_FAILURE_CLASSES or (
        fail_on_new and classification == "new_in_rebuild"
    )
    row["classification"] = classification
    row["gate_failure"] = int(gate_failure)
    row["notes"] = ";".join(notes)
    return row


class DetailReportWriter:
    def __init__(
        self,
        detail_csv: Path,
        detail_json: Path,
        metadata: Mapping[str, Any],
    ) -> None:
        self.detail_csv = detail_csv.resolve()
        self.detail_json = detail_json.resolve()
        self.metadata = dict(metadata)
        self._csv_tmp: Optional[Path] = None
        self._json_tmp: Optional[Path] = None
        self._csv_fh = None
        self._json_fh = None
        self._csv_writer = None
        self._first_json = True

    @staticmethod
    def _temporary_path(target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        os.close(fd)
        return Path(raw)

    def __enter__(self) -> "DetailReportWriter":
        self._csv_tmp = self._temporary_path(self.detail_csv)
        self._json_tmp = self._temporary_path(self.detail_json)
        self._csv_fh = self._csv_tmp.open("w", newline="", encoding="utf-8")
        self._csv_writer = csv.DictWriter(
            self._csv_fh, fieldnames=DETAIL_FIELDS, extrasaction="ignore"
        )
        self._csv_writer.writeheader()
        self._json_fh = self._json_tmp.open("w", encoding="utf-8")
        self._json_fh.write('{"format_version":')
        self._json_fh.write(str(FORMAT_VERSION))
        self._json_fh.write(',"metadata":')
        json.dump(self.metadata, self._json_fh, ensure_ascii=False, sort_keys=True)
        self._json_fh.write(',"details":[')
        return self

    def write(self, row: Mapping[str, Any]) -> None:
        assert self._csv_writer is not None and self._json_fh is not None
        self._csv_writer.writerow(dict(row))
        if not self._first_json:
            self._json_fh.write(",")
        json.dump(dict(row), self._json_fh, ensure_ascii=False, sort_keys=True)
        self._first_json = False

    def finish(
        self,
        summary: Sequence[Mapping[str, Any]],
        gates: Mapping[str, Any],
    ) -> None:
        assert self._csv_fh is not None and self._json_fh is not None
        self._json_fh.write('],"summary":')
        json.dump(list(summary), self._json_fh, ensure_ascii=False, sort_keys=True)
        self._json_fh.write(',"gates":')
        json.dump(dict(gates), self._json_fh, ensure_ascii=False, sort_keys=True)
        self._json_fh.write("}\n")
        self._csv_fh.flush()
        self._json_fh.flush()
        self._csv_fh.close()
        self._json_fh.close()
        self._csv_fh = None
        self._json_fh = None
        assert self._csv_tmp is not None and self._json_tmp is not None
        os.replace(self._csv_tmp, self.detail_csv)
        os.replace(self._json_tmp, self.detail_json)
        self._csv_tmp = None
        self._json_tmp = None

    def abort(self) -> None:
        for handle_name in ("_csv_fh", "_json_fh"):
            handle = getattr(self, handle_name)
            if handle is not None:
                handle.close()
                setattr(self, handle_name, None)
        for path_name in ("_csv_tmp", "_json_tmp"):
            path = getattr(self, path_name)
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                setattr(self, path_name, None)

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.abort()


def write_summary_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp = Path(raw)
    try:
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows([dict(row) for row in rows])
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def database_identity(conn: sqlite3.Connection, path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "page_size": int(conn.execute("PRAGMA page_size").fetchone()[0]),
        "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
        "schema_version": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
        "query_only": int(conn.execute("PRAGMA query_only").fetchone()[0]),
    }


def validate_output_paths(
    source_db: Path,
    repaired_db: Path,
    summary_csv: Path,
    detail_csv: Path,
    detail_json: Path,
) -> None:
    databases = {source_db.resolve(), repaired_db.resolve()}
    protected_paths = set(databases)
    for database in databases:
        protected_paths.update(
            Path(str(database) + suffix).resolve()
            for suffix in SQLITE_COMPANION_SUFFIXES
        )
    outputs = [summary_csv.resolve(), detail_csv.resolve(), detail_json.resolve()]
    if source_db.resolve() == repaired_db.resolve():
        raise ValueError("source and repaired database paths must differ")
    if len(set(outputs)) != len(outputs):
        raise ValueError("summary/detail report paths must be distinct")
    overlap = protected_paths.intersection(outputs)
    if overlap:
        raise ValueError(
            "report path would overwrite a database or SQLite companion: "
            f"{next(iter(overlap))}"
        )


def build_returns_detail(
    source_group: Optional[ReturnPopulationGroup],
    repaired_group: Optional[ReturnPopulationGroup],
    source_conn: sqlite3.Connection,
    repaired_conn: sqlite3.Connection,
) -> Dict[str, Any]:
    filing_id = (
        source_group.filing_id if source_group is not None else repaired_group.filing_id
    )
    source_count = source_group.row_count if source_group is not None else 0
    repaired_count = repaired_group.row_count if repaired_group is not None else 0
    source_counter = source_group.mapping_counter if source_group else Counter()
    repaired_counter = repaired_group.mapping_counter if repaired_group else Counter()
    notes: List[str] = []

    if source_group is None:
        classification = "new_in_rebuild"
    elif repaired_group is None:
        classification = "missing_in_rebuild"
    else:
        notes.extend(f"source:{note}" for note in source_group.problems)
        notes.extend(f"repaired:{note}" for note in repaired_group.problems)
        classification = (
            "content_changed"
            if source_counter != repaired_counter
            else "unexplained"
        )
    if source_group is not None and repaired_group is None:
        notes.extend(f"source:{note}" for note in source_group.problems)
    if repaired_group is not None and source_group is None:
        notes.extend(f"repaired:{note}" for note in repaired_group.problems)
    if notes:
        classification = "unexplained"

    source_meta = filing_metadata(source_conn, filing_id)
    repaired_meta = filing_metadata(repaired_conn, filing_id)
    meta = repaired_meta or source_meta or {}
    source_file = source_group.source_file if source_group else None
    repaired_source_file = repaired_group.source_file if repaired_group else None
    exact_extra = sum((source_counter - repaired_counter).values())
    new_rows = sum((repaired_counter - source_counter).values())
    row: Dict[str, Any] = {field: None for field in DETAIL_FIELDS}
    row.update(
        {
            "table_name": "returns",
            "filing_id": filing_id,
            "ein": meta.get("ein"),
            "tax_year": meta.get("tax_year"),
            "return_type": meta.get("return_type"),
            "source_file": source_file,
            "repaired_source_file": repaired_source_file,
            "source_object_id": source_group.object_id if source_group else None,
            "repaired_object_id": repaired_group.object_id if repaired_group else None,
            "source_count": source_count,
            "repaired_count": repaired_count,
            "row_delta_source_minus_repaired": source_count - repaired_count,
            "classification": classification,
            # Returns population/provenance is exact, so even a newly present
            # repaired filing gates independently of --fail-on-new.
            "gate_failure": 1,
            "source_payload_digest": multiset_digest(source_counter),
            "repaired_payload_digest": multiset_digest(repaired_counter),
            "source_unique_payloads": len(source_counter),
            "repaired_unique_payloads": len(repaired_counter),
            "exact_extra_rows": exact_extra,
            "new_payload_rows": new_rows,
            "notes": ";".join(notes),
        }
    )
    return row


def audit_returns_population(
    source_conn: sqlite3.Connection,
    repaired_conn: sqlite3.Connection,
    report: DetailReportWriter,
) -> Dict[str, Any]:
    """Compare exact filing, source-file, and normalized object coverage."""

    summary = new_summary("returns")
    try:
        source_index = leading_filing_index(source_conn, "returns")
        repaired_index = leading_filing_index(repaired_conn, "returns")
        summary["source_index"] = source_index
        summary["repaired_index"] = repaired_index
        source_coverage = returns_coverage_counts(source_conn)
        repaired_coverage = returns_coverage_counts(repaired_conn)
        summary["source_distinct_files"] = source_coverage["sources"]
        summary["repaired_distinct_files"] = repaired_coverage["sources"]
        summary["source_distinct_objects"] = source_coverage["objects"]
        summary["repaired_distinct_objects"] = repaired_coverage["objects"]
        for side, coverage in (
            ("source", source_coverage),
            ("repaired", repaired_coverage),
        ):
            if len(set(coverage.values())) != 1:
                note = f"{side}_returns_coverage_not_one_to_one:{coverage}"
                report.write(blank_detail("returns", note))
                summary["unexplained"] += 1
                summary["gate_failures"] += 1
                summary["notes"] = ";".join(
                    part for part in (summary["notes"], note) if part
                )
        source_iter = returns_population_stream(source_conn, source_index)
        repaired_iter = returns_population_stream(repaired_conn, repaired_index)
        source_group = next_or_none(source_iter)
        repaired_group = next_or_none(repaired_iter)

        while source_group is not None or repaired_group is not None:
            source_key = (
                filing_sort_key(source_group.filing_id)
                if source_group is not None
                else None
            )
            repaired_key = (
                filing_sort_key(repaired_group.filing_id)
                if repaired_group is not None
                else None
            )
            current_source: Optional[ReturnPopulationGroup] = None
            current_repaired: Optional[ReturnPopulationGroup] = None
            if repaired_group is None or (
                source_group is not None and source_key < repaired_key
            ):
                current_source = source_group
                source_group = next_or_none(source_iter)
            elif source_group is None or repaired_key < source_key:
                current_repaired = repaired_group
                repaired_group = next_or_none(repaired_iter)
            else:
                current_source = source_group
                current_repaired = repaired_group
                source_group = next_or_none(source_iter)
                repaired_group = next_or_none(repaired_iter)

            if current_source is not None:
                summary["source_rows"] += current_source.row_count
                summary["source_filing_groups"] += 1
                summary["source_file_covered_rows"] += (
                    current_source.source_file_covered_rows
                )
                summary["source_object_covered_filings"] += current_source.object_covered
            if current_repaired is not None:
                summary["repaired_rows"] += current_repaired.row_count
                summary["repaired_filing_groups"] += 1
                summary["repaired_source_file_covered_rows"] += (
                    current_repaired.source_file_covered_rows
                )
                summary["repaired_object_covered_filings"] += (
                    current_repaired.object_covered
                )

            matches = bool(
                current_source is not None
                and current_repaired is not None
                and not current_source.problems
                and not current_repaired.problems
                and current_source.mapping_counter == current_repaired.mapping_counter
            )
            if matches:
                continue
            detail = build_returns_detail(
                current_source,
                current_repaired,
                source_conn,
                repaired_conn,
            )
            report.write(detail)
            summary["mismatched_filings"] += 1
            classification = str(detail["classification"])
            if classification in summary:
                summary[classification] += 1
            else:
                summary["unexplained"] += 1
            source_count = int(detail.get("source_count") or 0)
            repaired_count = int(detail.get("repaired_count") or 0)
            if source_count > repaired_count:
                summary["source_extra_rows"] += source_count - repaired_count
            elif repaired_count > source_count:
                summary["repaired_extra_rows"] += repaired_count - source_count
            summary["gate_failures"] += 1
        for side, coverage in (
            ("source", source_coverage),
            ("repaired", repaired_coverage),
        ):
            streamed = {
                "returns": int(summary[f"{side}_rows"]),
                "filings": int(summary[f"{side}_filing_groups"]),
            }
            expected = {
                "returns": coverage["returns"],
                "filings": coverage["filings"],
            }
            if streamed != expected:
                note = (
                    f"{side}_returns_stream_count_changed:"
                    f"aggregate={expected},stream={streamed}"
                )
                report.write(blank_detail("returns", note))
                summary["unexplained"] += 1
                summary["gate_failures"] += 1
                summary["notes"] = ";".join(
                    part for part in (summary["notes"], note) if part
                )
    except Exception as exc:
        note = f"{type(exc).__name__}: {exc}"
        summary["status"] = "error"
        summary["unexplained"] += 1
        summary["gate_failures"] += 1
        summary["notes"] = note
        report.write(blank_detail("returns", note))
    if summary["gate_failures"]:
        summary["status"] = "failed"
    elif not summary["notes"]:
        summary["notes"] = "exact filing/source_file/object coverage"
    return summary


def aggregate_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    total = new_summary("__TOTAL__")
    total["source_index"] = ""
    total["repaired_index"] = ""
    for row in rows:
        for field in SUMMARY_FIELDS:
            if field in {"table_name", "status", "source_index", "repaired_index", "notes"}:
                continue
            total[field] += int(row.get(field) or 0)
    total["status"] = "failed" if total["gate_failures"] else "ok"
    total["notes"] = f"aggregate across {len(rows)} audit scopes"
    return total


def audit_table(
    table: str,
    source_conn: sqlite3.Connection,
    repaired_conn: sqlite3.Connection,
    report: DetailReportWriter,
    fail_on_new: bool,
) -> Dict[str, Any]:
    summary = new_summary(table)
    try:
        source_index = leading_filing_index(source_conn, table)
        repaired_index = leading_filing_index(repaired_conn, table)
        summary["source_index"] = source_index
        summary["repaired_index"] = repaired_index
        source_columns = [column for column in table_columns(source_conn, table) if column != "id"]
        repaired_columns = [column for column in table_columns(repaired_conn, table) if column != "id"]
        if source_columns != repaired_columns:
            raise AuditInvariantError(
                f"schema mismatch for {table}: source={source_columns!r}; repaired={repaired_columns!r}"
            )
        payload_columns = [column for column in source_columns if column != "filing_id"]

        source_iter = payload_digest_stream(
            source_conn, table, source_index, payload_columns
        )
        repaired_iter = payload_digest_stream(
            repaired_conn, table, repaired_index, payload_columns
        )
        source_row = next_or_none(source_iter)
        repaired_row = next_or_none(repaired_iter)
        while source_row is not None or repaired_row is not None:
            source_key = (
                filing_sort_key(source_row.filing_id)
                if source_row is not None
                else None
            )
            repaired_key = (
                filing_sort_key(repaired_row.filing_id)
                if repaired_row is not None
                else None
            )
            source_digest = None
            repaired_digest = None
            if repaired_row is None or (source_row is not None and source_key < repaired_key):
                filing_id = source_row.filing_id
                source_count = source_row.row_count
                source_digest = source_row.payload_digest
                repaired_count = 0
                summary["source_rows"] += source_count
                summary["source_filing_groups"] += 1
                source_row = next_or_none(source_iter)
            elif source_row is None or repaired_key < source_key:
                filing_id = repaired_row.filing_id
                repaired_count = repaired_row.row_count
                repaired_digest = repaired_row.payload_digest
                source_count = 0
                summary["repaired_rows"] += repaired_count
                summary["repaired_filing_groups"] += 1
                repaired_row = next_or_none(repaired_iter)
            else:
                filing_id = source_row.filing_id
                source_count = source_row.row_count
                repaired_count = repaired_row.row_count
                source_digest = source_row.payload_digest
                repaired_digest = repaired_row.payload_digest
                summary["source_rows"] += source_count
                summary["repaired_rows"] += repaired_count
                summary["source_filing_groups"] += 1
                summary["repaired_filing_groups"] += 1
                source_row = next_or_none(source_iter)
                repaired_row = next_or_none(repaired_iter)

            if (
                source_count == repaired_count
                and source_digest == repaired_digest
            ):
                continue
            detail = build_detail(
                table,
                filing_id,
                source_count,
                repaired_count,
                source_conn,
                repaired_conn,
                source_index,
                repaired_index,
                payload_columns,
                fail_on_new,
            )
            report.write(detail)
            summary["mismatched_filings"] += 1
            classification = str(detail["classification"])
            if classification in summary:
                summary[classification] += 1
            else:
                summary["unexplained"] += 1
            if source_count > repaired_count:
                summary["source_extra_rows"] += source_count - repaired_count
            else:
                summary["repaired_extra_rows"] += repaired_count - source_count
            if detail.get("whole_set_replay_factor"):
                summary["whole_set_replays"] += 1
            if table == "grants":
                summary["grant_source_inflated"] += int(detail.get("source_grant_inflated") or 0)
                summary["grant_repaired_inflated"] += int(detail.get("repaired_grant_inflated") or 0)
            summary["gate_failures"] += int(detail.get("gate_failure") or 0)
    except Exception as exc:
        note = f"{type(exc).__name__}: {exc}"
        summary["status"] = "error"
        summary["unexplained"] += 1
        summary["gate_failures"] += 1
        summary["notes"] = note
        report.write(blank_detail(table, note))
    if summary["gate_failures"]:
        summary["status"] = "failed"
    return summary


def run_audit(
    source_db: Path,
    repaired_db: Path,
    summary_csv: Path,
    detail_csv: Path,
    detail_json: Path,
    fail_on_new: bool = False,
) -> int:
    source_db = source_db.expanduser().resolve()
    repaired_db = repaired_db.expanduser().resolve()
    summary_csv = summary_csv.expanduser().resolve()
    detail_csv = detail_csv.expanduser().resolve()
    detail_json = detail_json.expanduser().resolve()
    validate_output_paths(
        source_db, repaired_db, summary_csv, detail_csv, detail_json
    )

    source_conn = connect_readonly(source_db)
    repaired_conn = None
    try:
        repaired_conn = connect_readonly(repaired_db)
        metadata = {
            "started_at": utc_now(),
            "source": database_identity(source_conn, source_db),
            "repaired": database_identity(repaired_conn, repaired_db),
            "tables": list(MULTIROW_CHILD_TABLES),
            "audit_scopes": ["returns", *MULTIROW_CHILD_TABLES],
            "population_strategy": "exact indexed filing/source_file/object coverage",
            "count_strategy": "streamed through filing_id leading index",
            "payload_strategy": (
                "order-independent constant-memory digest for every filing; "
                "full payload multiset only for mismatches"
            ),
            "fail_on_new": bool(fail_on_new),
        }
        summaries: List[Dict[str, Any]] = []
        with DetailReportWriter(detail_csv, detail_json, metadata) as report:
            print("[audit] comparing returns population/provenance...", flush=True)
            summaries.append(
                audit_returns_population(source_conn, repaired_conn, report)
            )
            for table in MULTIROW_CHILD_TABLES:
                print(f"[audit] comparing {table}...", flush=True)
                summaries.append(
                    audit_table(
                        table,
                        source_conn,
                        repaired_conn,
                        report,
                        fail_on_new,
                    )
                )
            total = aggregate_summary(summaries)
            all_summaries = [*summaries, total]
            gates = {
                "passed": not bool(total["gate_failures"]),
                "gate_failures": int(total["gate_failures"]),
                "hard_failure_classes": sorted(HARD_FAILURE_CLASSES),
                "exact_returns_population_required": True,
                "new_rows_fail_when_requested": bool(fail_on_new),
                "completed_at": utc_now(),
            }
            report.finish(all_summaries, gates)
        write_summary_csv(summary_csv, all_summaries)
    finally:
        if repaired_conn is not None:
            repaired_conn.close()
        source_conn.close()
        object_exists.cache_clear()
        table_columns.cache_clear()

    print(f"[audit] summary CSV: {summary_csv}")
    print(f"[audit] detail CSV: {detail_csv}")
    print(f"[audit] detail JSON: {detail_json}")
    if total["gate_failures"]:
        print(f"[audit] FAILED: {total['gate_failures']:,} gating issue(s)", file=sys.stderr)
        return 2
    print("[audit] PASSED")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only, indexed comparison of exact returns provenance and "
            "repeated IRS child rows before and after a clean rebuild."
        )
    )
    parser.add_argument("--source-db", required=True, help="Backed-up pre-repair SQLite DB")
    parser.add_argument("--repaired-db", required=True, help="Clean repaired staging SQLite DB")
    parser.add_argument(
        "--summary-csv",
        default="exports/child_repair_summary.csv",
        help="Per-table summary CSV output",
    )
    parser.add_argument(
        "--detail-csv",
        default="exports/child_repair_detail.csv",
        help="Filing/table mismatch detail CSV output",
    )
    parser.add_argument(
        "--detail-json",
        default="exports/child_repair_detail.json",
        help="Mismatch detail, metadata, summary, and gates JSON output",
    )
    parser.add_argument(
        "--fail-on-new",
        action="store_true",
        help=(
            "Also fail when a child filing exists only in the repaired DB. "
            "Recommended when the extractor and source inventory are expected to be identical."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_audit(
            Path(args.source_db),
            Path(args.repaired_db),
            Path(args.summary_csv),
            Path(args.detail_csv),
            Path(args.detail_json),
            fail_on_new=bool(args.fail_on_new),
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
