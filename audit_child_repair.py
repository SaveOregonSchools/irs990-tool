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
import xml.etree.ElementTree as ET
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from rebuild_irs990_slim_clean import (
    ManifestSelectionError,
    MULTIROW_CHILD_TABLES,
    _choose_manifest_source,
    _loaded_path_matches,
    _manifest_relative_path,
    _require_manifest_schema,
    _resolve_manifest_source,
    descendants_first_by_col,
    extract_file,
    extract_schedule_c_supplemental,
    find_groups,
    form_nodes,
    header_extract,
    local,
    object_id_from_filing_id,
)


FORMAT_VERSION = 5
HARD_FAILURE_CLASSES = {"missing_in_rebuild", "content_changed", "unexplained"}
SQLITE_COMPANION_SUFFIXES = ("-wal", "-shm", "-journal")
MULTISET_MASK = (1 << 256) - 1
DEFAULT_DETAIL_LIMIT_PER_TABLE = 1_000
DEFAULT_DETAIL_LIMIT_TOTAL = 25_000
MIN_RELOCATED_SOURCE_SUFFIX_PARTS = 2
GRANT_PAYLOAD_COLUMNS = (
    "filer_ein",
    "filer_name",
    "recipient_ein",
    "business_name_line1_txt",
    "business_name_line2_txt",
    "us_address_line1_txt",
    "us_address_line2_txt",
    "us_city_nm",
    "us_state_abbreviation_cd",
    "us_zip_cd",
    "foreign_address_line1_txt",
    "foreign_city_nm",
    "foreign_province_or_state_nm",
    "foreign_postal_cd",
    "foreign_country_cd",
    "ircsection_desc",
    "cash_grant_amt",
    "non_cash_assistance_amt",
    "non_cash_assistance_desc",
    "valuation_method_used_desc",
    "purpose_of_grant_txt",
)
GRANT_NUMERIC_COLUMNS = {"cash_grant_amt", "non_cash_assistance_amt"}
SCHEDULE_C_SUPPLEMENTAL_PAYLOAD_COLUMNS = (
    "form_and_line_reference_desc",
    "explanation_txt",
)
PF_OFFICER_PAYLOAD_COLUMNS = (
    "person_nm",
    "title_txt",
    "average_hrs_per_wk_devoted_to_pos_rt",
    "compensation_amt",
    "employee_benefits_amt",
    "expense_account_amt",
)
PF_OFFICER_NUMERIC_COLUMNS = {
    "compensation_amt",
    "employee_benefits_amt",
    "expense_account_amt",
}
PF_EMPLOYEE_ORIGIN_COLUMN = "__employee_benefits_xml_origin"
PF_EXPENSE_ORIGIN_COLUMN = "__expense_account_xml_origin"
PF_ALLOWED_ALTERNATE_ORIGINS = {
    "employee_benefits_amt": "employeebenefitprogramamt",
    "expense_account_amt": "expenseaccountotherallwncamt",
}
VERIFIED_EXTRACTOR_ENRICHMENT_TABLES = {
    "grants",
    "irs990_pf_officer_dir_trst_key_empl_info_grp",
    "irs990_schedule_c_supplemental_info",
}

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
    "manifest_provenance_matches",
    "mismatched_filings",
    "expected_exact_replay_cleanup",
    "verified_extractor_enrichment",
    "verified_zero_only_placeholder_removal",
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
    "detail_evidence_rows",
    "detail_rows_written",
    "detail_rows_suppressed",
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


def mapping_payload_key(
    row: Mapping[str, Any],
    payload_columns: Sequence[str],
    *,
    numeric_columns: Sequence[str] = (),
) -> str:
    numeric = set(numeric_columns)
    values = [
        [
            column,
            scalar_token(
                sqlite_numeric_affinity_value(row.get(column))
                if column in numeric
                else row.get(column)
            ),
        ]
        for column in payload_columns
    ]
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def sqlite_numeric_affinity_value(value: Any) -> Any:
    """Model SQLite NUMERIC affinity for well-formed IRS numeric XML text."""

    if value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and -(1 << 63) <= value < (1 << 63):
            return int(value)
        return value
    text = str(value).strip()
    if not text:
        return value
    try:
        number = Decimal(text)
    except InvalidOperation:
        return value
    if not number.is_finite():
        return value
    integral = number.to_integral_value()
    if number == integral and -(1 << 63) <= integral < (1 << 63):
        return int(integral)
    try:
        return float(text)
    except (ValueError, OverflowError):
        return value


PayloadToken = Tuple[str, Any]


def decode_payload_key(value: str) -> Dict[str, PayloadToken]:
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise AuditInvariantError("payload signature is not a column list")
    result: Dict[str, PayloadToken] = {}
    for item in decoded:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], list)
            or len(item[1]) != 2
            or not isinstance(item[1][0], str)
        ):
            raise AuditInvariantError("payload signature has an invalid token")
        column = item[0]
        if column in result:
            raise AuditInvariantError(f"payload signature repeats column {column!r}")
        result[column] = (item[1][0], item[1][1])
    return result


def token_is_null(token: Optional[PayloadToken]) -> bool:
    return token == ("null", None)


def token_numeric_value(token: Optional[PayloadToken]) -> Optional[Decimal]:
    if token is None:
        return None
    kind, value = token
    try:
        if kind == "integer":
            number = Decimal(str(value))
        elif kind == "float" and value not in {"nan", "+inf", "-inf"}:
            number = Decimal.from_float(float.fromhex(str(value)))
        else:
            return None
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return number if number.is_finite() else None


def token_is_numeric_zero(token: Optional[PayloadToken]) -> bool:
    number = token_numeric_value(token)
    return number is not None and number == 0


def token_has_meaningful_grant_value(token: Optional[PayloadToken]) -> bool:
    if token is None or token_is_null(token):
        return False
    kind, value = token
    if kind in {"integer", "float"}:
        return token_numeric_value(token) is not None
    if kind == "text":
        return bool(str(value or "").strip())
    if kind == "blob":
        return bool(value)
    return True


@dataclass
class _FlowEdge:
    to: int
    reverse_index: int
    capacity: int
    original_capacity: int

    @property
    def flow(self) -> int:
        return self.original_capacity - self.capacity


class _Dinic:
    def __init__(self, node_count: int) -> None:
        self.graph: List[List[_FlowEdge]] = [[] for _ in range(node_count)]

    def add_edge(self, source: int, target: int, capacity: int) -> _FlowEdge:
        capacity = int(capacity)
        if capacity < 0:
            raise ValueError("flow capacity cannot be negative")
        forward = _FlowEdge(target, len(self.graph[target]), capacity, capacity)
        reverse = _FlowEdge(source, len(self.graph[source]), 0, 0)
        self.graph[source].append(forward)
        self.graph[target].append(reverse)
        return forward

    def max_flow(self, source: int, sink: int) -> int:
        total = 0
        node_count = len(self.graph)
        while True:
            level = [-1] * node_count
            level[source] = 0
            queue = deque([source])
            while queue:
                node = queue.popleft()
                for edge in self.graph[node]:
                    if edge.capacity and level[edge.to] < 0:
                        level[edge.to] = level[node] + 1
                        queue.append(edge.to)
            if level[sink] < 0:
                return total
            offsets = [0] * node_count

            def send(node: int, available: int) -> int:
                if node == sink:
                    return available
                while offsets[node] < len(self.graph[node]):
                    edge = self.graph[node][offsets[node]]
                    if edge.capacity and level[edge.to] == level[node] + 1:
                        pushed = send(edge.to, min(available, edge.capacity))
                        if pushed:
                            edge.capacity -= pushed
                            reverse = self.graph[edge.to][edge.reverse_index]
                            reverse.capacity += pushed
                            return pushed
                    offsets[node] += 1
                return 0

            while True:
                pushed = send(source, 1 << 60)
                if not pushed:
                    break
                total += pushed


@dataclass(frozen=True)
class TransformVerification:
    verified: bool
    reason: str
    enriched_rows: int = 0
    kind: str = ""
    source_extra_rows: int = 0
    new_payload_rows: int = 0
    classification: str = "verified_extractor_enrichment"


TransformCompatibility = Callable[
    [Mapping[str, PayloadToken], Mapping[str, PayloadToken]], Tuple[bool, bool]
]


def verify_directional_payload_transform(
    source_counter: Counter[str],
    repaired_counter: Counter[str],
    compatibility: TransformCompatibility,
) -> TransformVerification:
    """Prove repaired rows derive directionally from retained source signatures.

    Every repaired row must be supplied by a compatible source row, and every
    distinct source signature must supply at least one repaired row.  Remaining
    source multiplicity is therefore demonstrably duplicate replay cleanup,
    never deletion of an entire source payload signature.
    """

    if not source_counter or not repaired_counter:
        return TransformVerification(False, "both source and repaired payloads are required")
    source_items = [(key, int(count)) for key, count in source_counter.items() if count]
    repaired_items = [(key, int(count)) for key, count in repaired_counter.items() if count]
    source_total = sum(count for _key, count in source_items)
    repaired_total = sum(count for _key, count in repaired_items)
    if repaired_total > source_total:
        return TransformVerification(False, "repaired multiplicity exceeds source")
    if len(source_items) > repaired_total:
        return TransformVerification(
            False,
            "repaired rows cannot retain every distinct source payload signature",
        )

    source_tokens = [decode_payload_key(key) for key, _count in source_items]
    repaired_tokens = [decode_payload_key(key) for key, _count in repaired_items]
    source_node = 0
    source_offset = 1
    repaired_offset = source_offset + len(source_items)
    sink_node = repaired_offset + len(repaired_items)
    super_source = sink_node + 1
    super_sink = sink_node + 2
    network = _Dinic(super_sink + 1)
    demand = [0] * (super_sink + 1)
    enrichment_edges: List[_FlowEdge] = []

    def add_lower_edge(
        start: int,
        end: int,
        lower: int,
        upper: int,
    ) -> _FlowEdge:
        if lower < 0 or upper < lower:
            raise ValueError("invalid lower/upper flow capacity")
        demand[start] -= lower
        demand[end] += lower
        return network.add_edge(start, end, upper - lower)

    for index, (_key, count) in enumerate(source_items):
        add_lower_edge(source_node, source_offset + index, 1, count)
    for source_index, source_values in enumerate(source_tokens):
        compatible_count = 0
        for repaired_index, repaired_values in enumerate(repaired_tokens):
            compatible, enrichment = compatibility(source_values, repaired_values)
            if not compatible:
                continue
            compatible_count += 1
            edge = add_lower_edge(
                source_offset + source_index,
                repaired_offset + repaired_index,
                0,
                min(source_items[source_index][1], repaired_items[repaired_index][1]),
            )
            if enrichment:
                enrichment_edges.append(edge)
        if not compatible_count:
            return TransformVerification(
                False,
                "a source payload signature has no allowed repaired counterpart",
            )
    for index, (_key, count) in enumerate(repaired_items):
        add_lower_edge(repaired_offset + index, sink_node, count, count)
    add_lower_edge(sink_node, source_node, 0, repaired_total)

    required = 0
    for node, balance in enumerate(demand[: sink_node + 1]):
        if balance > 0:
            network.add_edge(super_source, node, balance)
            required += balance
        elif balance < 0:
            network.add_edge(node, super_sink, -balance)
    if network.max_flow(super_source, super_sink) != required:
        return TransformVerification(False, "directional payload multiset is infeasible")
    enriched_rows = sum(max(0, edge.flow) for edge in enrichment_edges)
    if not enriched_rows:
        return TransformVerification(False, "no allowed extractor enrichment was required")
    return TransformVerification(True, "directional payload multiset verified", enriched_rows)


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


def _unchanged_except(
    source_values: Mapping[str, PayloadToken],
    repaired_values: Mapping[str, PayloadToken],
    allowed_columns: Sequence[str],
) -> bool:
    allowed = set(allowed_columns)
    columns = set(source_values).union(repaired_values)
    return all(
        source_values.get(column) == repaired_values.get(column)
        for column in columns
        if column not in allowed
    )


def grant_null_to_zero_compatibility(
    source_values: Mapping[str, PayloadToken],
    repaired_values: Mapping[str, PayloadToken],
) -> Tuple[bool, bool]:
    if not _unchanged_except(source_values, repaired_values, ("cash_grant_amt",)):
        return False, False
    source_cash = source_values.get("cash_grant_amt")
    repaired_cash = repaired_values.get("cash_grant_amt")
    if source_cash == repaired_cash:
        return True, False
    enrichment = token_is_null(source_cash) and token_is_numeric_zero(repaired_cash)
    return enrichment, enrichment


def pf_officer_enrichment_compatibility(
    source_values: Mapping[str, PayloadToken],
    repaired_values: Mapping[str, PayloadToken],
) -> Tuple[bool, bool]:
    amount_columns = (
        "employee_benefits_amt",
        "expense_account_amt",
    )
    ignored_columns = (
        *amount_columns,
        PF_EMPLOYEE_ORIGIN_COLUMN,
        PF_EXPENSE_ORIGIN_COLUMN,
    )
    if not _unchanged_except(source_values, repaired_values, ignored_columns):
        return False, False
    enriched = False
    for column in amount_columns:
        source_value = source_values.get(column)
        repaired_value = repaired_values.get(column)
        if source_value == repaired_value:
            continue
        if not token_is_null(source_value) or token_numeric_value(repaired_value) is None:
            return False, False
        origin_column = (
            PF_EMPLOYEE_ORIGIN_COLUMN
            if column == "employee_benefits_amt"
            else PF_EXPENSE_ORIGIN_COLUMN
        )
        origin_token = repaired_values.get(origin_column)
        expected_origin = PF_ALLOWED_ALTERNATE_ORIGINS[column]
        if (
            origin_token is None
            or origin_token[0] != "text"
            or str(origin_token[1] or "").casefold() != expected_origin
        ):
            return False, False
        enriched = True
    return True, enriched


def is_zero_only_blank_grant(values: Mapping[str, PayloadToken]) -> bool:
    if not token_is_numeric_zero(values.get("cash_grant_amt")):
        return False
    ignored = {"cash_grant_amt", "filer_ein", "filer_name"}
    return not any(
        token_has_meaningful_grant_value(token)
        for column, token in values.items()
        if column not in ignored
    )


def strict_zero_only_recipient_placeholder_counter(
    root: ET.Element,
    header: Mapping[str, Any],
) -> Counter[str]:
    """Map only structurally blank Schedule I zero-cash placeholders.

    The allowlist is deliberately narrower than general grant extraction: the
    group must be ``RecipientTable`` and its sole nonblank XML value must be a
    numeric-zero ``CashGrantAmt``.  Attributes or mixed-content tails make the
    group ineligible.  The resulting signature includes every audited grant
    field, including filer metadata from the selected XML header, so it can be
    compared exactly with the source-only payload counter.
    """

    placeholders: Counter[str] = Counter()
    for group in find_groups(root, ["RecipientTable"]):
        nodes = list(group.iter())
        if any(
            str(value or "").strip()
            for node in nodes
            for value in node.attrib.values()
        ):
            continue
        if any(
            str(node.tail or "").strip()
            for node in nodes
            if node is not group
        ):
            continue
        populated = [
            (local(node.tag).casefold(), str(node.text or "").strip())
            for node in nodes
            if str(node.text or "").strip()
        ]
        if len(populated) != 1:
            continue
        tag, value = populated[0]
        if tag != "cashgrantamt" or decimal_value(value) != Decimal(0):
            continue
        row: Dict[str, Any] = {column: None for column in GRANT_PAYLOAD_COLUMNS}
        row.update(
            {
                "filer_ein": header.get("ein"),
                "filer_name": header.get("org_name"),
                "cash_grant_amt": value,
            }
        )
        placeholders[
            mapping_payload_key(
                row,
                GRANT_PAYLOAD_COLUMNS,
                numeric_columns=GRANT_NUMERIC_COLUMNS,
            )
        ] += 1
    return placeholders


def verify_grant_extractor_enrichment(
    filing_id: Any,
    source_counter: Counter[str],
    repaired_counter: Counter[str],
    payload_columns: Sequence[str],
    repaired_meta: Optional[Mapping[str, Any]],
    xml_root: Path,
) -> TransformVerification:
    for payload in repaired_counter:
        if is_zero_only_blank_grant(decode_payload_key(payload)):
            return TransformVerification(False, "zero-only blank grant row is not allowed")
    if tuple(payload_columns) != GRANT_PAYLOAD_COLUMNS:
        return TransformVerification(False, "grant payload schema differs from current extractor")

    source_only = source_counter - repaired_counter
    repaired_only = repaired_counter - source_counter
    placeholder_removal = bool(source_only) and not repaired_only and all(
        is_zero_only_blank_grant(decode_payload_key(payload))
        for payload in source_only
    )
    placeholder_removal = bool(
        placeholder_removal
        and source_counter == repaired_counter + source_only
    )
    directional_result: Optional[TransformVerification] = None
    if not placeholder_removal:
        directional_result = verify_directional_payload_transform(
            source_counter,
            repaired_counter,
            grant_null_to_zero_compatibility,
        )
        if not directional_result.verified:
            return directional_result
    if not repaired_meta:
        return TransformVerification(False, "repaired return metadata is unavailable")
    candidate, path_error = resolve_selected_xml_path(
        repaired_meta.get("source_file"),
        filing_id,
        xml_root,
    )
    if candidate is None:
        return TransformVerification(False, path_error)
    extracted = extract_file(str(candidate))
    if extracted.get("error"):
        return TransformVerification(
            False,
            "current extractor rejected selected XML: " + str(extracted["error"]),
        )
    header = extracted.get("header") or {}
    if str(header.get("filing_id") or "") != str(filing_id or ""):
        return TransformVerification(False, "current extractor filing_id differs from audit filing")
    for column in ("ein", "tax_year", "return_type"):
        if scalar_token(header.get(column)) != scalar_token(repaired_meta.get(column)):
            return TransformVerification(
                False,
                f"selected XML header {column} differs from repaired return",
            )
    extracted_rows = extracted.get("grants")
    if not isinstance(extracted_rows, list):
        return TransformVerification(False, "current extractor returned no grant row list")
    extracted_counter: Counter[str] = Counter(
        mapping_payload_key(
            row,
            payload_columns,
            numeric_columns=GRANT_NUMERIC_COLUMNS,
        )
        for row in extracted_rows
        if isinstance(row, dict)
    )
    if extracted_counter != repaired_counter:
        return TransformVerification(
            False,
            "full repaired grant multiset differs from current selected-XML extraction",
        )
    if placeholder_removal:
        try:
            root = ET.parse(candidate).getroot()
        except (OSError, ET.ParseError) as exc:
            return TransformVerification(
                False,
                "selected XML could not be parsed for strict grant placeholders: "
                f"{type(exc).__name__}",
            )
        xml_placeholders = strict_zero_only_recipient_placeholder_counter(root, header)
        if xml_placeholders != source_only:
            return TransformVerification(
                False,
                "source-only grant payloads do not exactly match strict zero-only "
                "RecipientTable placeholders in selected XML",
            )
        return TransformVerification(
            verified=True,
            reason=(
                "strict selected-XML zero-only RecipientTable removal and current "
                "extractor exactly verified"
            ),
            kind="grants_zero_only_recipient_placeholder_removal",
            source_extra_rows=sum(source_only.values()),
            classification="verified_zero_only_placeholder_removal",
        )
    assert directional_result is not None
    return TransformVerification(
        True,
        "directional transform and selected XML/current extractor exactly verified",
        directional_result.enriched_rows,
        "grants_cash_null_to_zero",
        sum(source_counter.values()) - sum(repaired_counter.values()),
        0,
    )


def first_pf_amount_origin(group: ET.Element, column: str) -> str:
    candidates = {
        "employee_benefits_amt": {
            "employeebenefitsamt",
            "employeebenefitprogramamt",
        },
        "expense_account_amt": {
            "expenseaccountamt",
            "expenseaccountotherallwncamt",
        },
    }[column]
    for node in group.iter():
        tag = local(node.tag).casefold()
        if tag in candidates and str(node.text or "").strip():
            return tag
    return ""


def extract_pf_officer_rows_with_origins(
    xml_document: ET.Element,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in find_groups(xml_document, ("OfficerDirTrstKeyEmplGrp",)):
        row = {
            column: descendants_first_by_col(group, column)
            for column in PF_OFFICER_PAYLOAD_COLUMNS
        }
        if not any(value not in (None, "") for value in row.values()):
            continue
        row[PF_EMPLOYEE_ORIGIN_COLUMN] = first_pf_amount_origin(
            group, "employee_benefits_amt"
        )
        row[PF_EXPENSE_ORIGIN_COLUMN] = first_pf_amount_origin(
            group, "expense_account_amt"
        )
        rows.append(row)
    return rows


def verify_pf_officer_extractor_enrichment(
    filing_id: Any,
    source_counter: Counter[str],
    repaired_counter: Counter[str],
    payload_columns: Sequence[str],
    repaired_meta: Optional[Mapping[str, Any]],
    xml_root: Path,
) -> TransformVerification:
    if tuple(payload_columns) != PF_OFFICER_PAYLOAD_COLUMNS:
        return TransformVerification(False, "PF officer payload schema differs from current extractor")
    if not repaired_meta:
        return TransformVerification(False, "repaired return metadata is unavailable")
    candidate, path_error = resolve_selected_xml_path(
        repaired_meta.get("source_file"),
        filing_id,
        xml_root,
    )
    if candidate is None:
        return TransformVerification(False, path_error)
    xml_document, extract_error = parse_selected_xml_for_verification(
        candidate,
        filing_id,
        repaired_meta,
    )
    if xml_document is None:
        return TransformVerification(False, extract_error)
    extracted_rows = extract_pf_officer_rows_with_origins(xml_document)
    extracted_counter: Counter[str] = Counter(
        mapping_payload_key(
            row,
            payload_columns,
            numeric_columns=PF_OFFICER_NUMERIC_COLUMNS,
        )
        for row in extracted_rows
    )
    if extracted_counter != repaired_counter:
        return TransformVerification(
            False,
            "full repaired PF officer multiset differs from current selected-XML extraction",
        )
    origin_payload_columns = (
        *payload_columns,
        PF_EMPLOYEE_ORIGIN_COLUMN,
        PF_EXPENSE_ORIGIN_COLUMN,
    )
    origin_counter: Counter[str] = Counter(
        mapping_payload_key(
            row,
            origin_payload_columns,
            numeric_columns=PF_OFFICER_NUMERIC_COLUMNS,
        )
        for row in extracted_rows
    )
    result = verify_directional_payload_transform(
        source_counter,
        origin_counter,
        pf_officer_enrichment_compatibility,
    )
    if not result.verified:
        return TransformVerification(
            False,
            "PF directional/origin proof failed: " + result.reason,
        )
    return TransformVerification(
        True,
        "directional transform and selected XML/current extractor exactly verified",
        result.enriched_rows,
        "pf_officer_benefit_expense_selected_xml_enrichment",
        sum(source_counter.values()) - sum(repaired_counter.values()),
        0,
    )


def resolve_selected_xml_path(
    source_file: Any,
    filing_id: Any,
    xml_root: Path,
) -> Tuple[Optional[Path], str]:
    raw = str(source_file or "").strip()
    if not raw:
        return None, "repaired return has no source_file"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = xml_root / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return None, f"selected XML cannot be resolved: {type(exc).__name__}"
    if xml_root != candidate and xml_root not in candidate.parents:
        return None, "selected XML resolves outside the verified XML root"
    if not candidate.is_file() or candidate.suffix.casefold() != ".xml":
        return None, "selected XML is not a readable XML file"
    filing_object = object_id_from_filing_id(str(filing_id or "").strip())
    candidate_object = object_id_from_filing_id(candidate.stem)
    if not filing_object or candidate_object != filing_object:
        return None, "selected XML object does not match filing_id"
    return candidate, ""


def parse_selected_xml_for_verification(
    candidate: Path,
    filing_id: Any,
    repaired_meta: Mapping[str, Any],
) -> Tuple[Optional[ET.Element], str]:
    try:
        xml_document = ET.parse(str(candidate)).getroot()
    except (ET.ParseError, OSError) as exc:
        return None, f"selected XML parse failed: {type(exc).__name__}"
    header = header_extract(xml_document, candidate)
    if header is None:
        return None, "current extractor rejected selected XML header"
    if str(header.get("filing_id") or "") != str(filing_id or ""):
        return None, "current extractor filing_id differs from audit filing"
    for column in ("ein", "tax_year", "return_type"):
        if scalar_token(header.get(column)) != scalar_token(repaired_meta.get(column)):
            return None, f"selected XML header {column} differs from repaired return"
    return xml_document, ""


def verify_schedule_c_extractor_enrichment(
    filing_id: Any,
    source_counter: Counter[str],
    repaired_counter: Counter[str],
    payload_columns: Sequence[str],
    repaired_meta: Optional[Mapping[str, Any]],
    xml_root: Path,
) -> TransformVerification:
    if tuple(payload_columns) != SCHEDULE_C_SUPPLEMENTAL_PAYLOAD_COLUMNS:
        return TransformVerification(False, "Schedule C payload schema differs from current extractor")
    if not repaired_meta:
        return TransformVerification(False, "repaired return metadata is unavailable")
    if source_counter - repaired_counter:
        return TransformVerification(False, "a source Schedule C payload was removed or changed")
    new_rows = sum((repaired_counter - source_counter).values())
    if not new_rows:
        return TransformVerification(False, "no new Schedule C payload is present")
    candidate, path_error = resolve_selected_xml_path(
        repaired_meta.get("source_file"),
        filing_id,
        xml_root,
    )
    if candidate is None:
        return TransformVerification(False, path_error)
    xml_document, extract_error = parse_selected_xml_for_verification(
        candidate,
        filing_id,
        repaired_meta,
    )
    if xml_document is None:
        return TransformVerification(False, extract_error)
    extracted_rows = extract_schedule_c_supplemental(
        form_nodes(xml_document)["SCHC"],
        str(filing_id),
    )
    extracted_counter: Counter[str] = Counter(
        mapping_payload_key(row, payload_columns)
        for row in extracted_rows
    )
    if extracted_counter != repaired_counter:
        return TransformVerification(
            False,
            "full repaired Schedule C multiset differs from current selected-XML extraction",
        )
    return TransformVerification(
        True,
        "selected XML/current extractor exactly verified repaired Schedule C multiset",
        new_rows,
        "schedule_c_selected_xml_enrichment",
        0,
        new_rows,
    )


def source_file_object_id(source_file: Any) -> str:
    value = str(source_file or "").strip().replace("\\", "/")
    filename = value.rsplit("/", 1)[-1]
    if filename.lower().endswith(".xml"):
        filename = filename[:-4]
    return object_id_from_filing_id(filename)


def normalized_source_path_parts(source_file: Any) -> Tuple[str, ...]:
    """Return comparison-only path parts without assuming either archive root.

    IRS XML databases have historically been rebuilt after moving the archive
    between Windows roots.  The full absolute path is therefore not portable,
    but the archive directory suffix and XML object filename are.  Traversal
    components are rejected so a malformed path can never compare equal merely
    because its tail resembles a selected source.
    """

    value = str(source_file or "").strip().replace("\\", "/")
    if not value:
        return ()
    raw_parts = tuple(part for part in value.split("/") if part not in ("", "."))
    if not raw_parts or any(part == ".." for part in raw_parts):
        return ()
    # These inventories and databases are Windows-produced.  Case-folding
    # preserves the filesystem's comparison semantics across drive/root moves.
    return tuple(part.casefold() for part in raw_parts)


def source_files_equivalent(source_file: Any, repaired_source_file: Any) -> bool:
    """Compare source provenance while allowing only a relocated root prefix.

    Exact normalized paths compare equal.  Otherwise both values must identify
    the same XML object and share at least the final archive-directory component
    plus the object filename.  A basename-only match is deliberately rejected:
    different archive directories remain a hard provenance mismatch.
    """

    source_parts = normalized_source_path_parts(source_file)
    repaired_parts = normalized_source_path_parts(repaired_source_file)
    if not source_parts or not repaired_parts:
        return False
    if source_parts == repaired_parts:
        return True
    if not source_parts[-1].endswith(".xml") or not repaired_parts[-1].endswith(".xml"):
        return False
    source_object = source_file_object_id(source_file)
    repaired_object = source_file_object_id(repaired_source_file)
    if not source_object or source_object != repaired_object:
        return False

    common_suffix_parts = 0
    for source_part, repaired_part in zip(
        reversed(source_parts), reversed(repaired_parts)
    ):
        if source_part != repaired_part:
            break
        common_suffix_parts += 1
    return common_suffix_parts >= MIN_RELOCATED_SOURCE_SUFFIX_PARTS


@dataclass(frozen=True)
class ManifestProvenanceResult:
    verified: bool
    reason: str
    selection_strategy: str = ""
    selected_relative_path: str = ""


class ManifestProvenanceVerifier:
    """Verify historical-to-current source relocation against one completed scan."""

    def __init__(self, manifest_db: Path, xml_root: Path) -> None:
        self.manifest_db = manifest_db.expanduser().resolve()
        self.xml_root = xml_root.expanduser().resolve()
        if not self.xml_root.is_dir():
            raise ValueError(f"manifest XML root is not a directory: {self.xml_root}")
        self.conn = connect_readonly(self.manifest_db)
        try:
            self.conn.execute("PRAGMA trusted_schema=OFF")
            self.scan_id, self.scanned_at = _require_manifest_schema(
                self.conn,
                self.manifest_db,
            )
            loaded_columns = set(table_columns(self.conn, "loaded_filings"))
            missing_metadata = {
                "ein",
                "tax_year",
                "return_type",
            }.difference(loaded_columns)
            if missing_metadata:
                raise AuditInvariantError(
                    "manifest loaded_filings lacks provenance metadata column(s): "
                    + ", ".join(sorted(missing_metadata))
                )
        except Exception:
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    def verify(
        self,
        filing_id: Any,
        historical_source_file: Any,
        repaired_source_file: Any,
        source_meta: Optional[Mapping[str, Any]],
        repaired_meta: Optional[Mapping[str, Any]],
    ) -> ManifestProvenanceResult:
        try:
            filing_text = str(filing_id or "").strip()
            object_id = object_id_from_filing_id(filing_text)
            if not filing_text or not object_id:
                raise ManifestSelectionError("blank filing/object ID")
            if not source_meta or not repaired_meta:
                raise ManifestSelectionError("returns metadata is unavailable")
            if (
                str(source_meta.get("filing_id") or "") != filing_text
                or str(repaired_meta.get("filing_id") or "") != filing_text
            ):
                raise ManifestSelectionError("returns metadata filing_id differs")

            loaded_rows = self.conn.execute(
                "SELECT * FROM loaded_filings WHERE object_id=?",
                (object_id,),
            ).fetchall()
            source_rows = self.conn.execute(
                "SELECT * FROM source_files WHERE object_id=? ORDER BY relative_path",
                (object_id,),
            ).fetchall()
            if len(loaded_rows) != 1:
                raise ManifestSelectionError(
                    f"manifest has {len(loaded_rows)} loaded rows for object; expected one"
                )
            if not source_rows:
                raise ManifestSelectionError("manifest has no source row for object")
            loaded_row = loaded_rows[0]
            if str(loaded_row["imported_at"] or "") != self.scanned_at:
                raise ManifestSelectionError(
                    "loaded row is not from the completed manifest scan"
                )
            if any(str(row["scan_id"] or "") != self.scan_id for row in source_rows):
                raise ManifestSelectionError(
                    "source row is not from the completed manifest scan"
                )
            for row in source_rows:
                if _manifest_relative_path(row["source_file"]) != _manifest_relative_path(
                    row["relative_path"]
                ):
                    raise ManifestSelectionError(
                        "manifest source_file and relative_path differ"
                    )

            selected, strategy = _choose_manifest_source(
                object_id,
                source_rows,
                loaded_row,
            )
            if (
                str(loaded_row["filing_id"] or "") != filing_text
                or str(selected["filing_id"] or "") != filing_text
                or str(loaded_row["object_id"] or "") != object_id
                or str(selected["object_id"] or "") != object_id
            ):
                raise ManifestSelectionError(
                    "manifest filing/object identity differs from returns"
                )

            loaded_parts = normalized_source_path_parts(loaded_row["source_file"])
            if len(loaded_parts) < MIN_RELOCATED_SOURCE_SUFFIX_PARTS:
                raise ManifestSelectionError(
                    "manifest loaded source lacks an archive directory"
                )
            if not normalized_source_path_parts(historical_source_file):
                raise ManifestSelectionError(
                    "historical returns source path is invalid"
                )
            if not _loaded_path_matches(
                historical_source_file,
                loaded_row["source_file"],
            ):
                raise ManifestSelectionError(
                    "historical returns source does not match manifest loaded source"
                )

            for column in ("ein", "tax_year", "return_type"):
                source_token = scalar_token(source_meta.get(column))
                if (
                    source_token != scalar_token(repaired_meta.get(column))
                    or source_token != scalar_token(loaded_row[column])
                ):
                    raise ManifestSelectionError(
                        f"typed {column} differs across source/repaired/manifest"
                    )

            authoritative = _resolve_manifest_source(
                self.xml_root,
                object_id,
                selected,
            )
            repaired_raw = str(repaired_source_file or "").strip()
            repaired_recorded = Path(repaired_raw)
            if not repaired_raw or not repaired_recorded.is_absolute():
                raise ManifestSelectionError(
                    "repaired source path must be an absolute selected-XML path"
                )
            try:
                repaired_resolved = repaired_recorded.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ManifestSelectionError(
                    f"repaired source path cannot be resolved: {type(exc).__name__}"
                ) from exc
            if self.xml_root != repaired_resolved and self.xml_root not in repaired_resolved.parents:
                raise ManifestSelectionError(
                    "repaired source path resolves outside the manifest XML root"
                )
            if os.path.normcase(str(repaired_resolved)) != os.path.normcase(
                str(authoritative)
            ):
                raise ManifestSelectionError(
                    "repaired source path is not the exact manifest-selected file"
                )

            xml_document, header_error = parse_selected_xml_for_verification(
                authoritative,
                filing_text,
                repaired_meta,
            )
            if xml_document is None:
                raise ManifestSelectionError(header_error)
            return ManifestProvenanceResult(
                True,
                "completed-scan loaded path and exact selected XML verified",
                strategy,
                _manifest_relative_path(selected["relative_path"]),
            )
        except (AuditInvariantError, ManifestSelectionError, OSError, sqlite3.Error, ValueError) as exc:
            return ManifestProvenanceResult(False, str(exc))


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
            if scalar_token(source_meta.get(key)) != scalar_token(repaired_meta.get(key)):
                return f"filing_metadata_changed:{key}"
    return None


def verify_known_extractor_enrichment(
    table: str,
    filing_id: Any,
    source_payload: PayloadResult,
    repaired_payload: PayloadResult,
    payload_columns: Sequence[str],
    repaired_meta: Optional[Mapping[str, Any]],
    xml_root: Path,
) -> Optional[TransformVerification]:
    if table == "grants":
        return verify_grant_extractor_enrichment(
            filing_id,
            source_payload.counter,
            repaired_payload.counter,
            payload_columns,
            repaired_meta,
            xml_root,
        )
    if table == "irs990_pf_officer_dir_trst_key_empl_info_grp":
        return verify_pf_officer_extractor_enrichment(
            filing_id,
            source_payload.counter,
            repaired_payload.counter,
            payload_columns,
            repaired_meta,
            xml_root,
        )
    if table == "irs990_schedule_c_supplemental_info":
        return verify_schedule_c_extractor_enrichment(
            filing_id,
            source_payload.counter,
            repaired_payload.counter,
            payload_columns,
            repaired_meta,
            xml_root,
        )
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
    allow_verified_extractor_enrichments: bool,
    extractor_enrichment_xml_root: Optional[Path],
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

    if (
        allow_verified_extractor_enrichments
        and extractor_enrichment_xml_root is not None
        and not notes
        and classification != "expected_exact_replay_cleanup"
    ):
        verification = verify_known_extractor_enrichment(
            table,
            filing_id,
            source_payload,
            repaired_payload,
            payload_columns,
            repaired_meta,
            extractor_enrichment_xml_root,
        )
        if verification is not None:
            if verification.verified:
                classification = verification.classification
                exact_extra = verification.source_extra_rows
                new_rows = verification.new_payload_rows
                replay_factor = None
                notes.append(
                    f"{classification}:"
                    f"{verification.kind};enriched_rows={verification.enriched_rows};"
                    f"source_rows_removed={verification.source_extra_rows};"
                    f"new_rows={verification.new_payload_rows}"
                )
            else:
                notes.append(
                    f"extractor_enrichment_not_verified:{verification.reason}"
                )
                # Opt-in never relaxes a supported-table mismatch unless the
                # complete directional/XML proof succeeds.  In particular, a
                # failed Schedule C proof must not inherit the normally
                # non-gating new_in_rebuild classification.
                classification = "unexplained"

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

    unverified_allowlisted_new = bool(
        not allow_verified_extractor_enrichments
        and table in VERIFIED_EXTRACTOR_ENRICHMENT_TABLES
        and classification == "new_in_rebuild"
    )
    if unverified_allowlisted_new:
        notes.append("allowlisted_new_rows_require_explicit_verified_enrichment_opt_in")
    gate_failure = (
        classification in HARD_FAILURE_CLASSES
        or (fail_on_new and classification == "new_in_rebuild")
        or unverified_allowlisted_new
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
        *,
        detail_limit_per_table: int = DEFAULT_DETAIL_LIMIT_PER_TABLE,
        detail_limit_total: int = DEFAULT_DETAIL_LIMIT_TOTAL,
    ) -> None:
        if detail_limit_per_table < 1:
            raise ValueError("detail_limit_per_table must be at least 1")
        if detail_limit_total < 1:
            raise ValueError("detail_limit_total must be at least 1")
        self.detail_csv = detail_csv.resolve()
        self.detail_json = detail_json.resolve()
        self.metadata = dict(metadata)
        self.detail_limit_per_table = int(detail_limit_per_table)
        self.detail_limit_total = int(detail_limit_total)
        self._evidence_by_table: Counter[str] = Counter()
        self._written_by_table: Counter[str] = Counter()
        self._suppressed_by_table: Counter[str] = Counter()
        self._written_total = 0
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

    def write(self, row: Mapping[str, Any]) -> bool:
        assert self._csv_writer is not None and self._json_fh is not None
        table = str(row.get("table_name") or "__UNKNOWN__")
        self._evidence_by_table[table] += 1
        if (
            self._written_by_table[table] >= self.detail_limit_per_table
            or self._written_total >= self.detail_limit_total
        ):
            self._suppressed_by_table[table] += 1
            return False
        self._csv_writer.writerow(dict(row))
        if not self._first_json:
            self._json_fh.write(",")
        json.dump(dict(row), self._json_fh, ensure_ascii=False, sort_keys=True)
        self._first_json = False
        self._written_by_table[table] += 1
        self._written_total += 1
        return True

    def detail_counts(self, table: str) -> Dict[str, int]:
        return {
            "detail_evidence_rows": int(self._evidence_by_table[table]),
            "detail_rows_written": int(self._written_by_table[table]),
            "detail_rows_suppressed": int(self._suppressed_by_table[table]),
        }

    def detail_reporting(self) -> Dict[str, Any]:
        tables = sorted(self._evidence_by_table)
        return {
            "limit_per_table": self.detail_limit_per_table,
            "limit_total": self.detail_limit_total,
            "evidence_rows": sum(self._evidence_by_table.values()),
            "rows_written": self._written_total,
            "rows_suppressed": sum(self._suppressed_by_table.values()),
            "by_table": {
                table: self.detail_counts(table)
                for table in tables
            },
        }

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
        self._json_fh.write(',"detail_reporting":')
        json.dump(self.detail_reporting(), self._json_fh, ensure_ascii=False, sort_keys=True)
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
    additional_databases: Sequence[Path] = (),
) -> None:
    resolved_databases = [
        source_db.resolve(),
        repaired_db.resolve(),
        *(Path(path).resolve() for path in additional_databases),
    ]
    databases = set(resolved_databases)
    protected_paths = set(databases)
    for database in databases:
        protected_paths.update(
            Path(str(database) + suffix).resolve()
            for suffix in SQLITE_COMPANION_SUFFIXES
        )
    outputs = [summary_csv.resolve(), detail_csv.resolve(), detail_json.resolve()]
    if len(databases) != len(resolved_databases):
        raise ValueError("source, repaired, and manifest database paths must differ")
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
    manifest_provenance_failure: str = "",
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
    if manifest_provenance_failure:
        notes.append(f"manifest_provenance:{manifest_provenance_failure}")
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
    manifest_verifier: Optional[ManifestProvenanceVerifier] = None,
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
                and current_source.row_count == current_repaired.row_count == 1
                and source_files_equivalent(
                    current_source.source_file,
                    current_repaired.source_file,
                )
            )
            if matches:
                continue
            manifest_failure = ""
            manifest_candidate = bool(
                manifest_verifier is not None
                and current_source is not None
                and current_repaired is not None
                and not current_source.problems
                and not current_repaired.problems
                and current_source.row_count == current_repaired.row_count == 1
            )
            if manifest_candidate:
                source_meta = filing_metadata(
                    source_conn,
                    current_source.filing_id,
                )
                repaired_meta = filing_metadata(
                    repaired_conn,
                    current_repaired.filing_id,
                )
                verification = manifest_verifier.verify(
                    current_source.filing_id,
                    current_source.source_file,
                    current_repaired.source_file,
                    source_meta,
                    repaired_meta,
                )
                if verification.verified:
                    summary["manifest_provenance_matches"] += 1
                    continue
                manifest_failure = verification.reason
            detail = build_returns_detail(
                current_source,
                current_repaired,
                source_conn,
                repaired_conn,
                manifest_failure,
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
        summary["notes"] = "exact filing/portable source provenance/object coverage"
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
    allow_verified_extractor_enrichments: bool,
    extractor_enrichment_xml_root: Optional[Path],
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
                allow_verified_extractor_enrichments,
                extractor_enrichment_xml_root,
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
    detail_limit_per_table: int = DEFAULT_DETAIL_LIMIT_PER_TABLE,
    detail_limit_total: int = DEFAULT_DETAIL_LIMIT_TOTAL,
    allow_verified_extractor_enrichments: bool = False,
    extractor_enrichment_xml_root: Optional[Path] = None,
    source_manifest_db: Optional[Path] = None,
    manifest_xml_root: Optional[Path] = None,
) -> int:
    source_db = source_db.expanduser().resolve()
    repaired_db = repaired_db.expanduser().resolve()
    summary_csv = summary_csv.expanduser().resolve()
    detail_csv = detail_csv.expanduser().resolve()
    detail_json = detail_json.expanduser().resolve()
    if (source_manifest_db is None) != (manifest_xml_root is None):
        raise ValueError(
            "--source-manifest-db and --manifest-xml-root must be supplied together"
        )
    if source_manifest_db is not None and manifest_xml_root is not None:
        source_manifest_db = Path(source_manifest_db).expanduser().resolve()
        manifest_xml_root = Path(manifest_xml_root).expanduser().resolve()
    if allow_verified_extractor_enrichments:
        if extractor_enrichment_xml_root is None:
            raise ValueError(
                "--allow-verified-extractor-enrichments requires "
                "--extractor-enrichment-xml-root"
            )
        extractor_enrichment_xml_root = (
            Path(extractor_enrichment_xml_root).expanduser().resolve()
        )
        if not extractor_enrichment_xml_root.is_dir():
            raise ValueError(
                "extractor enrichment XML root is not a directory: "
                f"{extractor_enrichment_xml_root}"
            )
    elif extractor_enrichment_xml_root is not None:
        raise ValueError(
            "--extractor-enrichment-xml-root requires "
            "--allow-verified-extractor-enrichments"
        )
    validate_output_paths(
        source_db,
        repaired_db,
        summary_csv,
        detail_csv,
        detail_json,
        additional_databases=(
            (source_manifest_db,) if source_manifest_db is not None else ()
        ),
    )

    source_conn = connect_readonly(source_db)
    repaired_conn = None
    manifest_verifier = None
    try:
        repaired_conn = connect_readonly(repaired_db)
        if source_manifest_db is not None and manifest_xml_root is not None:
            manifest_verifier = ManifestProvenanceVerifier(
                source_manifest_db,
                manifest_xml_root,
            )
        metadata = {
            "started_at": utc_now(),
            "source": database_identity(source_conn, source_db),
            "repaired": database_identity(repaired_conn, repaired_db),
            "tables": list(MULTIROW_CHILD_TABLES),
            "audit_scopes": ["returns", *MULTIROW_CHILD_TABLES],
            "population_strategy": (
                "exact indexed filing/portable source provenance/object coverage"
            ),
            "count_strategy": "streamed through filing_id leading index",
            "payload_strategy": (
                "order-independent constant-memory digest for every filing; "
                "full payload multiset only for mismatches"
            ),
            "source_file_strategy": (
                "exact normalized path or same XML object with at least one "
                "matching trailing archive directory; otherwise optional completed-scan "
                "manifest loaded-source and exact selected-XML verification"
            ),
            "manifest_provenance": {
                "enabled": manifest_verifier is not None,
                "manifest": (
                    database_identity(manifest_verifier.conn, source_manifest_db)
                    if manifest_verifier is not None and source_manifest_db is not None
                    else None
                ),
                "scan_id": (
                    manifest_verifier.scan_id
                    if manifest_verifier is not None
                    else None
                ),
                "scanned_at": (
                    manifest_verifier.scanned_at
                    if manifest_verifier is not None
                    else None
                ),
                "xml_root": (
                    str(manifest_verifier.xml_root)
                    if manifest_verifier is not None
                    else None
                ),
                "rule": (
                    "historical source equals manifest loaded source; repaired source is "
                    "the exact root-confined selected current file; typed return metadata "
                    "and current XML header agree"
                ),
            },
            "detail_reporting": {
                "limit_per_table": int(detail_limit_per_table),
                "limit_total": int(detail_limit_total),
                "summary_counts_remain_exact_when_details_are_suppressed": True,
            },
            "verified_extractor_enrichments": {
                "enabled": bool(allow_verified_extractor_enrichments),
                "xml_root": (
                    str(extractor_enrichment_xml_root)
                    if extractor_enrichment_xml_root is not None
                    else None
                ),
                "policies": [
                    "grants_cash_null_to_zero_with_directional_and_selected_xml_proof",
                    "grants_strict_zero_only_recipient_placeholder_removal_with_exact_source_delta_and_selected_xml_proof",
                    "pf_officer_alternate_tag_benefit_expense_with_directional_and_selected_xml_proof",
                    "schedule_c_full_selected_xml_current_extractor_counter_equality",
                ],
            },
            "fail_on_new": bool(fail_on_new),
        }
        summaries: List[Dict[str, Any]] = []
        with DetailReportWriter(
            detail_csv,
            detail_json,
            metadata,
            detail_limit_per_table=detail_limit_per_table,
            detail_limit_total=detail_limit_total,
        ) as report:
            print("[audit] comparing returns population/provenance...", flush=True)
            summaries.append(
                audit_returns_population(
                    source_conn,
                    repaired_conn,
                    report,
                    manifest_verifier,
                )
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
                        allow_verified_extractor_enrichments,
                        extractor_enrichment_xml_root,
                    )
                )
            for summary in summaries:
                summary.update(report.detail_counts(str(summary["table_name"])))
            total = aggregate_summary(summaries)
            all_summaries = [*summaries, total]
            detail_reporting = report.detail_reporting()
            gates = {
                "passed": not bool(total["gate_failures"]),
                "gate_failures": int(total["gate_failures"]),
                "hard_failure_classes": sorted(HARD_FAILURE_CLASSES),
                "exact_returns_population_required": True,
                "new_rows_fail_when_requested": bool(fail_on_new),
                "verified_extractor_enrichments_enabled": bool(
                    allow_verified_extractor_enrichments
                ),
                "manifest_provenance_enabled": manifest_verifier is not None,
                "detail_rows_written": int(detail_reporting["rows_written"]),
                "detail_rows_suppressed": int(detail_reporting["rows_suppressed"]),
                "completed_at": utc_now(),
            }
            report.finish(all_summaries, gates)
        write_summary_csv(summary_csv, all_summaries)
    finally:
        if manifest_verifier is not None:
            manifest_verifier.close()
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
    parser.add_argument(
        "--detail-limit-per-table",
        type=int,
        default=DEFAULT_DETAIL_LIMIT_PER_TABLE,
        help=(
            "Maximum mismatch evidence rows written for each audit scope "
            f"(default: {DEFAULT_DETAIL_LIMIT_PER_TABLE:,}); exact summary counts are retained."
        ),
    )
    parser.add_argument(
        "--detail-limit-total",
        type=int,
        default=DEFAULT_DETAIL_LIMIT_TOTAL,
        help=(
            "Maximum mismatch evidence rows written across all scopes "
            f"(default: {DEFAULT_DETAIL_LIMIT_TOTAL:,}); exact summary counts are retained."
        ),
    )
    parser.add_argument(
        "--allow-verified-extractor-enrichments",
        action="store_true",
        help=(
            "Opt in to narrowly verified extractor enrichments and strict zero-only "
            "grant placeholder cleanup. All other payload changes still fail."
        ),
    )
    parser.add_argument(
        "--extractor-enrichment-xml-root",
        help=(
            "Explicit XML archive root used to root-confine and re-extract grant, PF, "
            "and Schedule C evidence; required with "
            "--allow-verified-extractor-enrichments."
        ),
    )
    parser.add_argument(
        "--source-manifest-db",
        help=(
            "Read-only completed XML source manifest used to verify authoritative "
            "historical-to-current returns provenance; requires --manifest-xml-root."
        ),
    )
    parser.add_argument(
        "--manifest-xml-root",
        help=(
            "Explicit current XML root for exact manifest-selected file, stat/hash, "
            "object, and header verification; requires --source-manifest-db."
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
            detail_limit_per_table=int(args.detail_limit_per_table),
            detail_limit_total=int(args.detail_limit_total),
            allow_verified_extractor_enrichments=bool(
                args.allow_verified_extractor_enrichments
            ),
            extractor_enrichment_xml_root=(
                Path(args.extractor_enrichment_xml_root)
                if args.extractor_enrichment_xml_root
                else None
            ),
            source_manifest_db=(
                Path(args.source_manifest_db)
                if args.source_manifest_db
                else None
            ),
            manifest_xml_root=(
                Path(args.manifest_xml_root)
                if args.manifest_xml_root
                else None
            ),
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
