#!/usr/bin/env python3
"""Build a read-optimized fraud/risk relationship-network sidecar.

The IRS source database is always opened read-only.  Rebuilds are written to a
temporary SQLite file and atomically installed only after a successful build;
incremental refreshes replace edges for an explicitly bounded filing set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import quote


SCHEMA_VERSION = "1"
DEFAULT_SIDECAR_NAME = "risk_network.db"
MAX_INCREMENTAL_FILINGS = 100_000
ENHANCED_GRANT_VIEW = "grant_recipient_resolved_plus_ai_v1"
DETERMINISTIC_GRANT_TABLE = "grant_recipient_resolved"
RAW_GRANT_TABLE = "grants"
APPLIED_GRANT_TABLE = "grant_recipient_ai_applied"
ENHANCED_GRANT_SCOREABLE_SOURCES = frozenset({
    "ai_assisted",
    "reported_ein_address_location",
    "reported_ein_identity_lookup",
    "reported_ein_rule",
})
ESTIMATE_TABLES = (
    "returns", "canonical_by_ein_year", "grant_recipient_resolved", "officers", "highest_comp_employees",
    "former_key_people", "irs990_ez_officer_director_trustee_empl_grp",
    "irs990_pf_officer_dir_trst_key_empl_info_grp",
    "irs990_schedule_j_rltd_org_officer_trst_key_empl_grp",
    "irs990_contractor_compensation_grp",
    "irs990_schedule_r_id_related_tax_exempt_org_grp",
    "irs990_schedule_r_id_related_org_txbl_corp_tr_grp",
    "irs990_schedule_r_id_related_org_txbl_partnership_grp",
    "irs990_schedule_r_unrelated_org_txbl_partnership_grp",
    "irs990_schedule_r_id_disregarded_entities_grp",
    "irs990_schedule_r_transactions_related_org_grp",
)
FULL_REQUIRED_FILING_INDEX_TABLES = (
    "returns",
    "canonical_by_ein_year",
    "grants",
    "officers",
    "highest_comp_employees",
    "former_key_people",
    "irs990_ez_officer_director_trustee_empl_grp",
    "irs990_pf_officer_dir_trst_key_empl_info_grp",
    "irs990_schedule_j_rltd_org_officer_trst_key_empl_grp",
    "irs990_contractor_compensation_grp",
    "irs990_schedule_r_id_related_tax_exempt_org_grp",
    "irs990_schedule_r_id_related_org_txbl_corp_tr_grp",
    "irs990_schedule_r_id_related_org_txbl_partnership_grp",
    "irs990_schedule_r_unrelated_org_txbl_partnership_grp",
    "irs990_schedule_r_id_disregarded_entities_grp",
    "irs990_schedule_r_transactions_related_org_grp",
)
INSERT_COLUMNS = (
    "edge_id", "source_ein", "source_name", "target_key", "target_type",
    "target_ein", "target_name", "edge_type", "direction", "filing_id",
    "tax_year", "period_end", "amount", "cash_amount", "noncash_amount",
    "amount_kind", "provenance_table", "provenance_row_id", "confidence",
    "confidence_basis", "is_scored", "hub_degree", "hub_suppressed",
    "attributes_json", "built_at",
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS risk_network_edge (
  edge_id TEXT PRIMARY KEY,
  source_ein TEXT NOT NULL,
  source_name TEXT,
  target_key TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_ein TEXT,
  target_name TEXT,
  edge_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  filing_id TEXT NOT NULL,
  tax_year INTEGER,
  period_end TEXT,
  amount NUMERIC,
  cash_amount NUMERIC,
  noncash_amount NUMERIC,
  amount_kind TEXT,
  provenance_table TEXT NOT NULL,
  provenance_row_id TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  confidence_basis TEXT NOT NULL,
  is_scored INTEGER NOT NULL CHECK(is_scored IN (0,1)),
  hub_degree INTEGER NOT NULL DEFAULT 0,
  hub_suppressed INTEGER NOT NULL DEFAULT 0 CHECK(hub_suppressed IN (0,1)),
  attributes_json TEXT NOT NULL DEFAULT '{}',
  built_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_network_node_stats (
  target_key TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  distinct_org_count INTEGER NOT NULL,
  edge_count INTEGER NOT NULL,
  filing_count INTEGER NOT NULL,
  first_tax_year INTEGER,
  last_tax_year INTEGER,
  hub_threshold INTEGER,
  is_hub INTEGER NOT NULL CHECK(is_hub IN (0,1)),
  computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_network_filing_state (
  filing_id TEXT PRIMARY KEY,
  source_ein TEXT NOT NULL,
  tax_year INTEGER,
  period_end TEXT,
  return_ts TEXT,
  built_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_network_build_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_network_source_status (
  source_name TEXT PRIMARY KEY,
  object_name TEXT NOT NULL,
  available INTEGER NOT NULL CHECK(available IN (0,1)),
  rows_written INTEGER NOT NULL,
  note TEXT,
  built_at TEXT NOT NULL
);
"""


PRE_HUB_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_risk_edge_source_year
  ON risk_network_edge(source_ein, tax_year, edge_type);
CREATE INDEX IF NOT EXISTS idx_risk_edge_target_year
  ON risk_network_edge(target_key, tax_year, source_ein);
CREATE INDEX IF NOT EXISTS idx_risk_edge_target_ein
  ON risk_network_edge(target_ein, tax_year);
CREATE INDEX IF NOT EXISTS idx_risk_edge_filing
  ON risk_network_edge(filing_id);
CREATE INDEX IF NOT EXISTS idx_risk_edge_provenance
  ON risk_network_edge(provenance_table);
CREATE INDEX IF NOT EXISTS idx_risk_node_type_hub
  ON risk_network_node_stats(target_type, is_hub, distinct_org_count);
CREATE INDEX IF NOT EXISTS idx_risk_filing_source_year
  ON risk_network_filing_state(source_ein, tax_year);
"""


POST_HUB_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_risk_edge_type_scored
  ON risk_network_edge(edge_type, is_scored, hub_suppressed, tax_year);
"""


INDEX_SQL = PRE_HUB_INDEX_SQL + POST_HUB_INDEX_SQL


PERSON_SOURCES: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    ("officers", "person_name", "title_txt", ("comp_from_org", "comp_from_related", "other_compensation")),
    ("highest_comp_employees", "person_name", "title_txt", ("comp_from_org", "comp_from_related", "other_compensation")),
    ("former_key_people", "person_name", "title_txt", ("comp_from_org", "comp_from_related", "other_compensation")),
    ("irs990_ez_officer_director_trustee_empl_grp", "person_nm", "title_txt", ("compensation_amt", "employee_benefit_program_amt", "expense_account_other_allwnc_amt")),
    ("irs990_pf_officer_dir_trst_key_empl_info_grp", "person_nm", "title_txt", ("compensation_amt", "employee_benefits_amt", "expense_account_amt")),
    ("irs990_schedule_j_rltd_org_officer_trst_key_empl_grp", "person_nm", "title_txt", ("total_compensation_filing_org_amt", "total_compensation_rltd_orgs_amt")),
)


SCHEDULE_R_SOURCES: Tuple[Tuple[str, str, bool], ...] = (
    ("irs990_schedule_r_id_related_tax_exempt_org_grp", "schedule_r_related_tax_exempt", True),
    ("irs990_schedule_r_id_related_org_txbl_corp_tr_grp", "schedule_r_related_taxable_corp_trust", True),
    ("irs990_schedule_r_id_related_org_txbl_partnership_grp", "schedule_r_related_taxable_partnership", True),
    ("irs990_schedule_r_unrelated_org_txbl_partnership_grp", "schedule_r_unrelated_taxable_partnership", False),
    ("irs990_schedule_r_id_disregarded_entities_grp", "schedule_r_disregarded_entity", False),
    ("irs990_schedule_r_transactions_related_org_grp", "schedule_r_related_transaction", False),
)


FULL_REQUIRED_SOURCE_LABELS = (
    "addresses",
    "grants",
    *("people:" + table for table, _name, _title, _amounts in PERSON_SOURCES),
    "contractors",
    *("schedule_r:" + table for table, _edge_type, _scored in SCHEDULE_R_SOURCES),
)


@dataclass(frozen=True)
class Filing:
    filing_id: str
    ein: str
    org_name: str
    tax_year: Optional[int]
    period_end: str
    return_ts: str
    address1: str
    address2: str
    city: str
    region: str
    postal_code: str
    country: str


@dataclass(frozen=True)
class BuildConfig:
    min_grant_confidence: float = 0.85
    person_hub_threshold: int = 25
    address_hub_threshold: int = 50
    contractor_hub_threshold: int = 50
    batch_size: int = 500
    canonical_only: bool = True


@dataclass(frozen=True)
class AuxiliaryFileState:
    """Stable state for a SQLite journal/WAL file.

    Missing and zero-length files are intentionally equivalent. SQLite may
    remove an empty checkpointed WAL without changing database contents.
    """

    populated: bool
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class SourceSnapshot:
    lineage_id: str
    file_size: int
    file_mtime_ns: int
    data_version: int
    journal_mode: str
    wal: AuxiliaryFileState
    rollback_journal: AuxiliaryFileState


@dataclass(frozen=True)
class FullSourcePreflight:
    grant_count: int
    resolver_count: int
    enhanced_count: int
    filing_indexes: Tuple[Tuple[str, str], ...]
    expected_filing_count: int = 0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _file_uri(path: Path, mode: str) -> str:
    return "file:" + quote(path.resolve().as_posix(), safe="/:" ) + f"?mode={mode}"


def connect_source_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_file_uri(path, "ro"), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def connect_sidecar(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def normalize_ein(value: Any) -> str:
    """Normalize only a complete, conventionally formatted nine-digit EIN."""

    text = str(value or "").strip()
    if re.fullmatch(r"\d{9}", text):
        return text
    if re.fullmatch(r"\d{2}-\d{7}", text):
        return text.replace("-", "")
    return ""


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").upper()
    return " ".join(re.findall(r"[A-Z0-9]+", text))


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_numbers(row: Mapping[str, Any], columns: Sequence[str]) -> Optional[float]:
    values = [_number(row[col]) for col in columns if col in row.keys()]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _json(values: Mapping[str, Any]) -> str:
    clean = {key: value for key, value in values.items() if value not in (None, "")}
    return json.dumps(clean, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _object_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE name=? AND type IN ('table','view')", (name,)
    ).fetchone() is not None


def _object_type(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT type FROM sqlite_schema WHERE name=? AND type IN ('table','view')",
        (name,),
    ).fetchone()
    return str(row[0]).lower() if row is not None else ""


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE name=? AND type='index'", (name,)
    ).fetchone() is not None


def sqlite_row_estimates(conn: sqlite3.Connection, tables: Sequence[str]) -> Dict[str, int]:
    """Return ANALYZE estimates without scanning large production tables."""
    estimates: Dict[str, int] = {}
    if not _object_exists(conn, "sqlite_stat1"):
        return estimates
    for table in tables:
        row = conn.execute(
            "SELECT stat FROM sqlite_stat1 WHERE tbl=? ORDER BY CASE WHEN idx IS NULL THEN 0 ELSE 1 END LIMIT 1",
            (table,),
        ).fetchone()
        if not row or not row[0]:
            continue
        try:
            estimates[table] = int(str(row[0]).split()[0])
        except (TypeError, ValueError):
            continue
    return estimates


def _columns(conn: sqlite3.Connection, name: str) -> Set[str]:
    safe = name.replace("'", "''")
    return {row["name"] for row in conn.execute(f"PRAGMA table_info('{safe}')")}


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _index_key_terms(conn: sqlite3.Connection, index_name: str) -> List[sqlite3.Row]:
    """Return every key term, including expression terms, in ordinal order."""

    safe_index = index_name.replace("'", "''")
    return sorted(
        (
            row
            for row in conn.execute(f"PRAGMA index_xinfo('{safe_index}')")
            if "key" not in row.keys() or int(row["key"] or 0) == 1
        ),
        key=lambda row: int(row["seqno"]),
    )


def _binary_lookup_uses_search(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> bool:
    if not columns:
        return False
    predicates = " AND ".join(
        f"{_quote_identifier(column)}=?" for column in columns
    )
    details = [
        str(row[3]).upper()
        for row in conn.execute(
            f"EXPLAIN QUERY PLAN SELECT 1 FROM {_quote_identifier(table)} WHERE {predicates}",
            tuple(None for _ in columns),
        )
    ]
    return any("SEARCH" in detail for detail in details)


def _index_terms_have_binary_prefix(
    terms: Sequence[sqlite3.Row],
    columns: Sequence[str],
) -> bool:
    if len(terms) < len(columns):
        return False
    for term, column in zip(terms, columns):
        # cid=-2/name=NULL is an expression.  Dropping it would incorrectly
        # promote a later ordinary column into the leading position.
        if int(term["cid"]) < 0 or term["name"] != column:
            return False
        if str(term["coll"] or "BINARY").upper() != "BINARY":
            return False
    return True


def _index_with_prefix(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> str:
    """Return an index/primary-key name whose leading columns match exactly."""

    wanted = tuple(columns)
    safe_table = table.replace("'", "''")
    table_info = list(conn.execute(f"PRAGMA table_info('{safe_table}')"))
    primary = tuple(
        row["name"]
        for row in sorted(table_info, key=lambda item: int(item["pk"] or 0))
        if int(row["pk"] or 0) > 0
    )
    if (
        primary[: len(wanted)] == wanted
        and _binary_lookup_uses_search(conn, table, wanted)
    ):
        return "PRIMARY KEY"
    for index_row in conn.execute(f"PRAGMA index_list('{safe_table}')"):
        if "partial" in index_row.keys() and int(index_row["partial"] or 0) != 0:
            continue
        index_name = str(index_row["name"])
        terms = _index_key_terms(conn, index_name)
        if (
            _index_terms_have_binary_prefix(terms, wanted)
            and _binary_lookup_uses_search(conn, table, wanted)
        ):
            return index_name
    return ""


def _unique_index_for_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> str:
    """Return a non-partial uniqueness guarantee for exactly ``columns``."""

    wanted = tuple(columns)
    safe_table = table.replace("'", "''")
    table_info = list(conn.execute(f"PRAGMA table_info('{safe_table}')"))
    primary = tuple(
        row["name"]
        for row in sorted(table_info, key=lambda item: int(item["pk"] or 0))
        if int(row["pk"] or 0) > 0
    )
    table_list_row = next(
        (
            row
            for row in conn.execute("PRAGMA table_list")
            if row["name"] == table
        ),
        None,
    )
    without_rowid = bool(
        table_list_row is not None
        and "wr" in table_list_row.keys()
        and int(table_list_row["wr"] or 0) == 1
    )
    has_primary_key_index = any(
        "origin" in row.keys() and str(row["origin"]).lower() == "pk"
        for row in conn.execute(f"PRAGMA index_list('{safe_table}')")
    )
    column_by_name = {row["name"]: row for row in table_info}
    exact_primary_is_nonnull = primary == wanted and all(
        int(column_by_name[column]["notnull"] or 0) == 1
        or without_rowid
        or (
            len(primary) == 1
            and str(column_by_name[column]["type"] or "").strip().upper() == "INTEGER"
            and not has_primary_key_index
        )
        for column in wanted
    )
    if exact_primary_is_nonnull:
        return "PRIMARY KEY"
    table_columns = column_by_name
    for index_row in conn.execute(f"PRAGMA index_list('{safe_table}')"):
        if int(index_row["unique"] or 0) != 1:
            continue
        if "partial" in index_row.keys() and int(index_row["partial"] or 0) != 0:
            continue
        index_name = str(index_row["name"])
        terms = _index_key_terms(conn, index_name)
        if len(terms) != len(wanted):
            continue
        if not _index_terms_have_binary_prefix(terms, wanted):
            continue
        if not all(
            column in table_columns and int(table_columns[column]["notnull"] or 0) == 1
            for column in wanted
        ):
            continue
        if tuple(term["name"] for term in terms) == wanted:
            return index_name
    return ""


def _model_derived_enhanced_source(model: Any) -> str:
    value = _clean(model)
    if value == "rule:reported_ein_identity_lookup":
        return "reported_ein_identity_lookup"
    if value == "rule:reported_ein_address_location":
        return "reported_ein_address_location"
    if value == "rule:reported_ein_from_filing_unverified":
        return "reported_ein_from_filing_unverified"
    if value.startswith("rule:"):
        return "reported_ein_rule"
    return "ai_assisted"


def _artifact_value_matches(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    return left == right


def _enhanced_row_artifact_problem(row: sqlite3.Row) -> str:
    """Independently prove one enhanced row from resolver/application artifacts."""

    base_pairs = (
        ("__enhanced_grant_id", "__base_grant_id"),
        ("__enhanced_filing_id", "__base_filing_id"),
        ("__enhanced_recipient_reported_ein", "__base_recipient_reported_ein"),
        ("__enhanced_recipient_reported_name", "__base_recipient_reported_name"),
        ("__enhanced_cash_amount", "__base_cash_amount"),
        ("__enhanced_noncash_amount", "__base_noncash_amount"),
        ("__enhanced_total_amount", "__base_total_amount"),
        ("__enhanced_purpose", "__base_purpose"),
        ("__enhanced_match_status", "__base_match_status"),
        ("__enhanced_warning_flags", "__base_warning_flags"),
    )
    for enhanced_column, artifact_column in base_pairs:
        enhanced_value = row[enhanced_column]
        artifact_value = row[artifact_column]
        if enhanced_column in {"__enhanced_grant_id", "__enhanced_filing_id"}:
            if str(enhanced_value) != str(artifact_value):
                return f"{enhanced_column.removeprefix('__enhanced_')}_mismatch"
        elif not _artifact_value_matches(enhanced_value, artifact_value):
            return f"{enhanced_column.removeprefix('__enhanced_')}_mismatch"

    applied_grant_id = row["__applied_grant_id"]
    if applied_grant_id is None:
        expected_ein = row["__base_resolved_ein"]
        expected_name = row["__base_resolved_org_name"]
        expected_confidence = row["__base_confidence"]
        expected_source = "deterministic"
    else:
        if str(applied_grant_id) != str(row["__base_grant_id"]):
            return "applied_grant_id_mismatch"
        signature_hash = row["__applied_signature_hash"]
        selected_ein = str(row["__applied_selected_ein"] or "")
        confidence = _number(row["__applied_confidence"])
        model = _clean(row["__applied_model"])
        if not _clean(signature_hash):
            return "blank_applied_signature_hash"
        if re.fullmatch(r"\d{9}", selected_ein) is None:
            return "invalid_applied_selected_ein"
        if confidence is None or confidence < 0.0 or confidence > 1.0:
            return "invalid_applied_confidence"
        if _clean(row["__applied_decision"]) not in {
            "SELECT_CANDIDATE",
            "KEEP_REPORTED_EIN",
        }:
            return "invalid_applied_decision"
        if not model:
            return "blank_applied_model"
        expected_ein = row["__applied_selected_ein"]
        expected_name = row["__applied_selected_name"]
        expected_confidence = row["__applied_confidence"]
        expected_source = _model_derived_enhanced_source(model)

    if not _artifact_value_matches(row["__enhanced_final_resolved_ein"], expected_ein):
        return "final_resolved_ein_mismatch"
    if not _artifact_value_matches(row["__enhanced_final_resolved_org_name"], expected_name):
        return "final_resolved_org_name_mismatch"
    if not _artifact_value_matches(row["__enhanced_final_confidence"], expected_confidence):
        return "final_confidence_mismatch"
    if _clean(row["__enhanced_final_match_source"]).lower() != expected_source:
        return "final_match_source_mismatch"
    return ""


def _enhanced_artifact_projection(enhanced_alias: str = "enhanced") -> str:
    base_columns = (
        "grant_id", "filing_id", "recipient_reported_ein", "recipient_reported_name",
        "cash_amount", "noncash_amount", "total_amount", "purpose", "match_status",
        "warning_flags", "resolved_ein", "resolved_org_name", "confidence",
    )
    applied_columns = (
        "grant_id", "signature_hash", "selected_ein", "selected_name",
        "ai_confidence", "ai_decision", "model",
    )
    applied_aliases = {
        "ai_confidence": "confidence",
        "ai_decision": "decision",
    }
    enhanced_columns = (
        "grant_id", "filing_id", "recipient_reported_ein", "recipient_reported_name",
        "cash_amount", "noncash_amount", "total_amount", "purpose", "match_status",
        "warning_flags", "final_resolved_ein", "final_resolved_org_name",
        "final_match_source", "final_confidence",
    )
    pieces = [f"base.{column} AS __base_{column}" for column in base_columns]
    pieces.extend(
        f"applied.{column} AS __applied_{applied_aliases.get(column, column)}"
        for column in applied_columns
    )
    pieces.extend(
        f"{enhanced_alias}.{column} AS __enhanced_{column}"
        for column in enhanced_columns
    )
    pieces.append(f"{enhanced_alias}.*")
    return ", ".join(pieces)


def validate_full_source(
    conn: sqlite3.Connection,
    *,
    check_row_parity: bool,
) -> FullSourcePreflight:
    """Fail closed unless a full build has every indexed, enhanced source."""

    missing_objects = [
        name
        for name in (
            *FULL_REQUIRED_FILING_INDEX_TABLES,
            DETERMINISTIC_GRANT_TABLE,
            APPLIED_GRANT_TABLE,
            ENHANCED_GRANT_VIEW,
        )
        if not _object_exists(conn, name)
    ]
    if missing_objects:
        raise RuntimeError(
            "Full risk-network build is missing required source objects: "
            + ", ".join(sorted(missing_objects))
        )

    wrong_object_types = [
        f"{name}={_object_type(conn, name)} (expected {expected})"
        for name, expected in (
            (RAW_GRANT_TABLE, "table"),
            (DETERMINISTIC_GRANT_TABLE, "table"),
            (APPLIED_GRANT_TABLE, "table"),
            (ENHANCED_GRANT_VIEW, "view"),
        )
        if _object_type(conn, name) != expected
    ]
    if wrong_object_types:
        raise RuntimeError(
            "Full risk-network grant objects have the wrong type: "
            + ", ".join(wrong_object_types)
        )

    required_columns_by_object: Dict[str, Set[str]] = {
        "returns": {
            "filing_id", "ein", "org_name", "tax_year", "period_end", "return_ts",
            "us_address_line1", "us_address_line2", "city", "state", "zip",
            "foreign_address_line1", "foreign_city", "foreign_province",
            "foreign_country", "foreign_postal_code",
        },
        "canonical_by_ein_year": {"filing_id"},
        RAW_GRANT_TABLE: {"id", "filing_id"},
        DETERMINISTIC_GRANT_TABLE: {
            "grant_id", "filing_id", "recipient_reported_ein",
            "recipient_reported_name", "cash_amount", "noncash_amount",
            "total_amount", "purpose", "match_status", "warning_flags",
            "resolved_ein", "resolved_org_name", "confidence",
        },
        APPLIED_GRANT_TABLE: {
            "grant_id", "signature_hash", "selected_ein", "selected_name",
            "ai_confidence", "ai_decision", "model",
        },
        "officers": {"id", "filing_id", "person_name"},
        "highest_comp_employees": {"id", "filing_id", "person_name"},
        "former_key_people": {"id", "filing_id", "person_name"},
        "irs990_ez_officer_director_trustee_empl_grp": {"id", "filing_id", "person_nm"},
        "irs990_pf_officer_dir_trst_key_empl_info_grp": {"id", "filing_id", "person_nm"},
        "irs990_schedule_j_rltd_org_officer_trst_key_empl_grp": {"id", "filing_id", "person_nm"},
        "irs990_contractor_compensation_grp": {
            "id", "filing_id", "business_name_line1_txt", "person_nm", "compensation_amt",
            "usaddress_address_line1_txt", "address_line1_txt", "usaddress_city_nm",
            "city_nm", "state_abbreviation_cd", "province_or_state_nm", "zipcd",
            "foreign_postal_cd", "services_desc",
        },
        "irs990_schedule_r_id_related_tax_exempt_org_grp": {
            "id", "filing_id", "ein", "business_name_line1_txt",
        },
        "irs990_schedule_r_id_related_org_txbl_corp_tr_grp": {
            "id", "filing_id", "ein", "related_organization_name_business_name_line1_txt",
        },
        "irs990_schedule_r_id_related_org_txbl_partnership_grp": {
            "id", "filing_id", "ein", "related_organization_name_business_name_line1_txt",
        },
        "irs990_schedule_r_unrelated_org_txbl_partnership_grp": {
            "id", "filing_id", "ein", "business_name_line1_txt",
        },
        "irs990_schedule_r_id_disregarded_entities_grp": {
            "id", "filing_id", "disregarded_entity_name_business_name_line1_txt",
        },
        "irs990_schedule_r_transactions_related_org_grp": {
            "id", "filing_id", "business_name_line1_txt", "involved_amt",
            "transaction_type_txt", "method_of_amount_determination_txt",
        },
    }
    missing_source_columns: List[str] = []
    for object_name, required_columns in required_columns_by_object.items():
        absent = sorted(required_columns - _columns(conn, object_name))
        if absent:
            missing_source_columns.append(
                f"{object_name}({', '.join(absent)})"
            )
    if missing_source_columns:
        raise RuntimeError(
            "Full risk-network build is missing required source columns: "
            + "; ".join(missing_source_columns)
        )

    filing_indexes: List[Tuple[str, str]] = []
    missing_indexes: List[str] = []
    for table in FULL_REQUIRED_FILING_INDEX_TABLES:
        if "filing_id" not in _columns(conn, table):
            missing_indexes.append(f"{table}(filing_id column missing)")
            continue
        index_name = _index_with_prefix(conn, table, ("filing_id",))
        if not index_name:
            missing_indexes.append(f"{table}(filing_id)")
        else:
            filing_indexes.append((table, index_name))
    if missing_indexes:
        raise RuntimeError(
            "Full risk-network build requires leading filing_id indexes; missing: "
            + ", ".join(missing_indexes)
        )
    missing_lookup_indexes = [
        f"{table}({column})"
        for table, column in (
            (RAW_GRANT_TABLE, "id"),
            (DETERMINISTIC_GRANT_TABLE, "grant_id"),
        )
        if not _index_with_prefix(conn, table, (column,))
    ]
    if missing_lookup_indexes:
        raise RuntimeError(
            "Full risk-network build requires indexed grant ID lookups; missing: "
            + ", ".join(missing_lookup_indexes)
        )
    missing_unique_grant_ids = [
        f"{table}({column})"
        for table, column in (
            (RAW_GRANT_TABLE, "id"),
            (DETERMINISTIC_GRANT_TABLE, "grant_id"),
            (APPLIED_GRANT_TABLE, "grant_id"),
        )
        if not _unique_index_for_columns(conn, table, (column,))
    ]
    if missing_unique_grant_ids:
        raise RuntimeError(
            "Full risk-network build requires exact unique grant-ID keys; missing: "
            + ", ".join(missing_unique_grant_ids)
        )

    required_enhanced_columns = {
        "grant_id", "filing_id", "recipient_reported_ein", "recipient_reported_name",
        "cash_amount", "noncash_amount", "total_amount", "purpose", "match_status",
        "warning_flags", "final_resolved_ein", "final_resolved_org_name",
        "final_match_source", "final_confidence",
    }
    enhanced_columns = _columns(conn, ENHANCED_GRANT_VIEW)
    missing_columns = sorted(required_enhanced_columns - enhanced_columns)
    if missing_columns:
        raise RuntimeError(
            f"Enhanced grant view {ENHANCED_GRANT_VIEW} is missing required columns: "
            + ", ".join(missing_columns)
        )

    if not check_row_parity:
        return FullSourcePreflight(0, 0, 0, tuple(filing_indexes))

    print("Validating exact enhanced-grant row parity...", flush=True)
    grant_count = int(conn.execute(f"SELECT COUNT(*) FROM {RAW_GRANT_TABLE}").fetchone()[0])
    resolver_count = int(conn.execute(
        f"SELECT COUNT(*) FROM {DETERMINISTIC_GRANT_TABLE}"
    ).fetchone()[0])
    enhanced_count = int(conn.execute(f"SELECT COUNT(*) FROM {ENHANCED_GRANT_VIEW}").fetchone()[0])
    if not (grant_count == resolver_count == enhanced_count):
        raise RuntimeError(
            "Full risk-network enhanced-grant row parity failed: "
            f"grants={grant_count:,}, {DETERMINISTIC_GRANT_TABLE}={resolver_count:,}, "
            f"{ENHANCED_GRANT_VIEW}={enhanced_count:,}"
        )

    resolver_problem = conn.execute(
        f"""SELECT g.id,
                   CASE
                     WHEN rr.grant_id IS NULL THEN 'missing_resolver'
                     WHEN CAST(rr.grant_id AS TEXT) IS NOT CAST(g.id AS TEXT)
                       THEN 'grant_id_mismatch'
                     WHEN NULLIF(TRIM(CAST(g.filing_id AS TEXT)),'') IS NULL
                       OR NULLIF(TRIM(CAST(rr.filing_id AS TEXT)),'') IS NULL
                       THEN 'blank_filing_id'
                     ELSE 'filing_id_mismatch'
                   END AS problem
            FROM {RAW_GRANT_TABLE} AS g
            LEFT JOIN {DETERMINISTIC_GRANT_TABLE} AS rr ON rr.grant_id=g.id
            WHERE rr.grant_id IS NULL
               OR CAST(rr.grant_id AS TEXT) IS NOT CAST(g.id AS TEXT)
               OR NULLIF(TRIM(CAST(g.filing_id AS TEXT)),'') IS NULL
               OR NULLIF(TRIM(CAST(rr.filing_id AS TEXT)),'') IS NULL
               OR CAST(rr.filing_id AS TEXT) IS NOT CAST(g.filing_id AS TEXT)
            LIMIT 1"""
    ).fetchone()
    if resolver_problem is not None:
        raise RuntimeError(
            "Full risk-network grant filing parity failed between raw grants and "
            f"the deterministic resolver: grant_id={resolver_problem[0]!r}, "
            f"problem={resolver_problem[1]}."
        )
    # Keep this as a correlated existence probe. A direct LEFT JOIN against
    # the view makes SQLite materialize all enhanced rows (and their wide
    # decision columns) before the real multi-million-row build even starts.
    enhanced_problem = conn.execute(
        f"""SELECT outer_rr.grant_id
            FROM {DETERMINISTIC_GRANT_TABLE} AS outer_rr
            WHERE NOT EXISTS (
              SELECT 1
              FROM {ENHANCED_GRANT_VIEW} AS enhanced
              WHERE enhanced.grant_id=outer_rr.grant_id
                AND CAST(enhanced.grant_id AS TEXT) IS CAST(outer_rr.grant_id AS TEXT)
                AND NULLIF(TRIM(CAST(enhanced.filing_id AS TEXT)),'') IS NOT NULL
                AND CAST(enhanced.filing_id AS TEXT) IS CAST(outer_rr.filing_id AS TEXT)
            )
            LIMIT 1"""
    ).fetchone()
    if enhanced_problem is not None:
        raise RuntimeError(
            "Full risk-network enhanced-grant filing parity failed: "
            f"grant_id={enhanced_problem[0]!r} is missing or has a different/blank filing_id."
        )

    applied_problem = conn.execute(
        f"""SELECT applied.grant_id,
                   CASE
                     WHEN base.grant_id IS NULL THEN 'orphan_applied_grant'
                     WHEN CAST(applied.grant_id AS TEXT) IS NOT CAST(base.grant_id AS TEXT)
                       THEN 'applied_grant_id_mismatch'
                     WHEN NULLIF(TRIM(CAST(applied.signature_hash AS TEXT)),'') IS NULL
                       THEN 'blank_applied_signature_hash'
                     WHEN LENGTH(CAST(applied.selected_ein AS TEXT))<>9
                       OR CAST(applied.selected_ein AS TEXT) GLOB '*[^0-9]*'
                       THEN 'invalid_applied_selected_ein'
                     WHEN applied.ai_confidence IS NULL OR applied.ai_confidence<0
                       OR applied.ai_confidence>1 THEN 'invalid_applied_confidence'
                     WHEN COALESCE(applied.ai_decision,'') NOT IN
                       ('SELECT_CANDIDATE','KEEP_REPORTED_EIN')
                       THEN 'invalid_applied_decision'
                     ELSE 'blank_applied_model'
                   END AS problem
            FROM {APPLIED_GRANT_TABLE} AS applied
            LEFT JOIN {DETERMINISTIC_GRANT_TABLE} AS base
              ON base.grant_id=applied.grant_id
            WHERE base.grant_id IS NULL
               OR CAST(applied.grant_id AS TEXT) IS NOT CAST(base.grant_id AS TEXT)
               OR NULLIF(TRIM(CAST(applied.signature_hash AS TEXT)),'') IS NULL
               OR LENGTH(CAST(applied.selected_ein AS TEXT))<>9
               OR CAST(applied.selected_ein AS TEXT) GLOB '*[^0-9]*'
               OR applied.ai_confidence IS NULL OR applied.ai_confidence<0
               OR applied.ai_confidence>1
               OR COALESCE(applied.ai_decision,'') NOT IN
                 ('SELECT_CANDIDATE','KEEP_REPORTED_EIN')
               OR NULLIF(TRIM(CAST(applied.model AS TEXT)),'') IS NULL
            LIMIT 1"""
    ).fetchone()
    if applied_problem is not None:
        raise RuntimeError(
            "Full risk-network applied-grant provenance failed: "
            f"grant_id={applied_problem[0]!r}, problem={applied_problem[1]}."
        )

    enhanced_artifact_problem = conn.execute(
        f"""SELECT enhanced.grant_id
            FROM {ENHANCED_GRANT_VIEW} AS enhanced
            JOIN {DETERMINISTIC_GRANT_TABLE} AS base
              ON base.grant_id=enhanced.grant_id
            LEFT JOIN {APPLIED_GRANT_TABLE} AS applied
              ON applied.grant_id=base.grant_id
            WHERE CAST(enhanced.grant_id AS TEXT) IS NOT CAST(base.grant_id AS TEXT)
               OR enhanced.filing_id IS NOT base.filing_id
               OR enhanced.recipient_reported_ein IS NOT base.recipient_reported_ein
               OR enhanced.recipient_reported_name IS NOT base.recipient_reported_name
               OR enhanced.cash_amount IS NOT base.cash_amount
               OR enhanced.noncash_amount IS NOT base.noncash_amount
               OR enhanced.total_amount IS NOT base.total_amount
               OR enhanced.purpose IS NOT base.purpose
               OR enhanced.match_status IS NOT base.match_status
               OR enhanced.warning_flags IS NOT base.warning_flags
               OR enhanced.final_resolved_ein IS NOT
                    CASE WHEN applied.grant_id IS NULL THEN base.resolved_ein
                         ELSE applied.selected_ein END
               OR enhanced.final_resolved_org_name IS NOT
                    CASE WHEN applied.grant_id IS NULL THEN base.resolved_org_name
                         ELSE applied.selected_name END
               OR enhanced.final_confidence IS NOT
                    CASE WHEN applied.grant_id IS NULL THEN base.confidence
                         ELSE applied.ai_confidence END
               OR LOWER(TRIM(CAST(enhanced.final_match_source AS TEXT))) IS NOT
                    CASE
                      WHEN applied.grant_id IS NULL THEN 'deterministic'
                      WHEN applied.model='rule:reported_ein_identity_lookup'
                        THEN 'reported_ein_identity_lookup'
                      WHEN applied.model='rule:reported_ein_address_location'
                        THEN 'reported_ein_address_location'
                      WHEN applied.model='rule:reported_ein_from_filing_unverified'
                        THEN 'reported_ein_from_filing_unverified'
                      WHEN applied.model LIKE 'rule:%' THEN 'reported_ein_rule'
                      ELSE 'ai_assisted'
                    END
            LIMIT 1"""
    ).fetchone()
    if enhanced_artifact_problem is not None:
        raise RuntimeError(
            "Full risk-network enhanced-grant provenance is not exactly backed by "
            "the deterministic resolver and applied-decision artifacts: "
            f"grant_id={enhanced_artifact_problem[0]!r}."
        )
    return FullSourcePreflight(
        grant_count, resolver_count, enhanced_count, tuple(filing_indexes)
    )


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _in_query(table: str, filing_ids: Sequence[str]) -> Tuple[str, Tuple[str, ...]]:
    placeholders = ",".join("?" for _ in filing_ids)
    return f"SELECT * FROM {table} WHERE filing_id IN ({placeholders}) ORDER BY filing_id", tuple(filing_ids)


def _row_id(row: Mapping[str, Any]) -> str:
    for key in ("id", "grant_id", "rowid"):
        if key in row.keys() and row[key] not in (None, ""):
            return str(row[key])
    return "unknown"


def _edge_id(provenance_table: str, provenance_row_id: str, edge_type: str,
             source_ein: str, target_key: str) -> str:
    raw = "\x1f".join((provenance_table, provenance_row_id, edge_type, source_ein, target_key))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_edge(
    filing: Filing,
    *,
    target_key: str,
    target_type: str,
    target_ein: str,
    target_name: str,
    edge_type: str,
    direction: str,
    amount: Optional[float],
    cash_amount: Optional[float],
    noncash_amount: Optional[float],
    amount_kind: str,
    provenance_table: str,
    provenance_row_id: str,
    confidence: float,
    confidence_basis: str,
    is_scored: bool,
    attributes: Mapping[str, Any],
    built_at: str,
) -> Tuple[Any, ...]:
    return (
        _edge_id(provenance_table, provenance_row_id, edge_type, filing.ein, target_key),
        filing.ein, filing.org_name, target_key, target_type, target_ein or None,
        target_name, edge_type, direction, filing.filing_id, filing.tax_year,
        filing.period_end, amount, cash_amount, noncash_amount, amount_kind,
        provenance_table, provenance_row_id, max(0.0, min(1.0, float(confidence))),
        confidence_basis, 1 if is_scored else 0, 0, 0, _json(attributes), built_at,
    )


def _validated_ein_selectors(eins: Sequence[str]) -> List[str]:
    normalized: Set[str] = set()
    invalid: List[Any] = []
    for value in eins:
        raw = str(value or "")
        ein = normalize_ein(raw)
        if not ein or raw != raw.strip():
            invalid.append(value)
        else:
            normalized.add(ein)
    if invalid:
        raise RuntimeError(
            "Invalid --ein selector(s); use exactly nine digits or NN-NNNNNNN: "
            f"{invalid[:10]!r}."
        )
    return sorted(normalized)


def _validated_filing_id_selectors(filing_ids: Sequence[str]) -> List[str]:
    canonical: Set[str] = set()
    invalid: List[Any] = []
    for value in filing_ids:
        raw = str(value or "")
        if not raw or raw != raw.strip() or any(character.isspace() for character in raw):
            invalid.append(value)
        else:
            canonical.add(raw)
    if invalid:
        raise RuntimeError(
            "Invalid --filing-id selector(s); IDs must be nonblank canonical values "
            f"without whitespace: {invalid[:10]!r}."
        )
    return sorted(canonical)


def _validated_tax_year_bounds(
    min_tax_year: Optional[int],
    max_tax_year: Optional[int],
) -> Tuple[Optional[int], Optional[int]]:
    def parse(value: Optional[int], label: str) -> Optional[int]:
        if value is None:
            return None
        raw = str(value)
        if isinstance(value, bool) or re.fullmatch(r"\d{4}", raw) is None:
            raise RuntimeError(f"{label} must be a four-digit tax year.")
        return int(raw)

    minimum = parse(min_tax_year, "--min-tax-year")
    maximum = parse(max_tax_year, "--max-tax-year")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise RuntimeError("--min-tax-year cannot be greater than --max-tax-year.")
    return minimum, maximum


def _require_requested_filing_ids_exist(
    conn: sqlite3.Connection,
    filing_ids: Sequence[str],
    canonical_join: str,
) -> None:
    if not filing_ids:
        return
    found: Set[str] = set()
    for filing_id_batch in _chunks(filing_ids, 800):
        placeholders = ",".join("?" for _ in filing_id_batch)
        found.update(
            str(row[0])
            for row in conn.execute(
                f"SELECT r.filing_id FROM returns AS r{canonical_join} "
                f"WHERE r.filing_id IN ({placeholders})",
                tuple(filing_id_batch),
            )
        )
    missing = [filing_id for filing_id in filing_ids if filing_id not in found]
    if missing:
        scope = "canonical source selection" if canonical_join else "source returns"
        raise RuntimeError(
            "Requested --filing-id selector(s) were not found in the "
            f"{scope}: {missing[:10]!r}."
        )


def _filing_selection_parts(
    conn: sqlite3.Connection,
    *,
    eins: Sequence[str] = (),
    filing_ids: Sequence[str] = (),
    min_tax_year: Optional[int] = None,
    max_tax_year: Optional[int] = None,
    after_filing_id: str = "",
    canonical_only: bool = True,
) -> Tuple[str, str, List[Any]]:
    if not _object_exists(conn, "returns"):
        raise RuntimeError("Source database is missing required table: returns")
    clean_eins = _validated_ein_selectors(eins)
    unique_ids = _validated_filing_id_selectors(filing_ids)
    minimum_year, maximum_year = _validated_tax_year_bounds(
        min_tax_year,
        max_tax_year,
    )
    canonical_join = ""
    if canonical_only and _object_exists(conn, "canonical_by_ein_year"):
        canonical_join = " JOIN canonical_by_ein_year AS c ON c.filing_id=r.filing_id"
    _require_requested_filing_ids_exist(conn, unique_ids, canonical_join)

    clauses: List[str] = []
    params: List[Any] = []
    if clean_eins:
        forms: List[str] = []
        for ein in clean_eins:
            forms.extend((ein, ein[:2] + "-" + ein[2:]))
        clauses.append("r.ein IN (" + ",".join("?" for _ in forms) + ")")
        params.extend(forms)
    if unique_ids:
        clauses.append("r.filing_id IN (" + ",".join("?" for _ in unique_ids) + ")")
        params.extend(unique_ids)
    if minimum_year is not None:
        clauses.append("r.tax_year >= ?")
        params.append(minimum_year)
    if maximum_year is not None:
        clauses.append("r.tax_year <= ?")
        params.append(maximum_year)
    if after_filing_id:
        clauses.append("r.filing_id > ?")
        params.append(after_filing_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return canonical_join, where, params


def count_selected_filings(
    conn: sqlite3.Connection,
    *,
    eins: Sequence[str] = (),
    filing_ids: Sequence[str] = (),
    min_tax_year: Optional[int] = None,
    max_tax_year: Optional[int] = None,
    canonical_only: bool = True,
) -> int:
    """Return the independent source-row count for one filing selection."""

    canonical_join, where, params = _filing_selection_parts(
        conn,
        eins=eins,
        filing_ids=filing_ids,
        min_tax_year=min_tax_year,
        max_tax_year=max_tax_year,
        canonical_only=canonical_only,
    )
    return int(conn.execute(
        f"SELECT COUNT(*) FROM returns AS r{canonical_join}{where}",
        params,
    ).fetchone()[0])


def select_filings(
    conn: sqlite3.Connection,
    *,
    eins: Sequence[str] = (),
    filing_ids: Sequence[str] = (),
    min_tax_year: Optional[int] = None,
    max_tax_year: Optional[int] = None,
    max_filings: Optional[int] = None,
    after_filing_id: str = "",
    overflow_check: bool = True,
    canonical_only: bool = True,
) -> List[Filing]:
    canonical_join, where, params = _filing_selection_parts(
        conn,
        eins=eins,
        filing_ids=filing_ids,
        min_tax_year=min_tax_year,
        max_tax_year=max_tax_year,
        after_filing_id=after_filing_id,
        canonical_only=canonical_only,
    )
    limit = ""
    if max_filings is not None:
        limit = " LIMIT ?"
        params.append(int(max_filings) + (1 if overflow_check else 0))
    sql = f"""
    SELECT r.filing_id, r.ein, r.org_name, r.tax_year, r.period_end, r.return_ts,
           COALESCE(r.us_address_line1, r.foreign_address_line1, '') AS address1,
           COALESCE(r.us_address_line2, '') AS address2,
           COALESCE(r.city, r.foreign_city, '') AS city,
           COALESCE(r.state, r.foreign_province, '') AS region,
           COALESCE(r.zip, r.foreign_postal_code, '') AS postal_code,
           CASE WHEN NULLIF(TRIM(r.state),'') IS NOT NULL OR NULLIF(TRIM(r.us_address_line1),'') IS NOT NULL
                THEN 'US' ELSE COALESCE(r.foreign_country, '') END AS country
    FROM returns AS r{canonical_join}{where}
    ORDER BY r.filing_id{limit}
    """
    filings: List[Filing] = []
    for row in conn.execute(sql, params):
        ein = normalize_ein(row["ein"])
        raw_filing_id = str(row["filing_id"] or "")
        filing_id = _clean(raw_filing_id)
        if not ein or not filing_id or filing_id != raw_filing_id:
            raise RuntimeError(
                "Selected source filing has a blank/invalid filing_id or EIN, or a noncanonical filing_id; "
                f"filing_id={filing_id!r}. Refusing a silently incomplete network build."
            )
        filings.append(Filing(
            filing_id=filing_id,
            ein=ein,
            org_name=_clean(row["org_name"]),
            tax_year=int(row["tax_year"]) if row["tax_year"] is not None else None,
            period_end=_clean(row["period_end"]),
            return_ts=_clean(row["return_ts"]),
            address1=_clean(row["address1"]),
            address2=_clean(row["address2"]),
            city=_clean(row["city"]),
            region=_clean(row["region"]),
            postal_code=_clean(row["postal_code"]),
            country=_clean(row["country"]),
        ))
    if overflow_check and max_filings is not None and len(filings) > max_filings:
        raise RuntimeError(
            f"Selection exceeds the safety cap of {max_filings:,} filings. "
            "Narrow --ein/--filing-id/year bounds or explicitly raise --max-filings."
        )
    return filings


def verify_selected_filings_in_snapshot(
    conn: sqlite3.Connection,
    filings: Sequence[Filing],
    *,
    canonical_only: bool,
) -> None:
    """Fail if caller-selected filing metadata is not exact in this snapshot."""

    expected = list(filings)
    expected_ids = [filing.filing_id for filing in expected]
    if len(expected_ids) != len(set(expected_ids)):
        raise RuntimeError("Selected filings contain duplicate filing IDs.")
    current_rows: List[Filing] = []
    for filing_id_batch in _chunks(expected_ids, 800):
        current_rows.extend(select_filings(
            conn,
            filing_ids=filing_id_batch,
            max_filings=None,
            overflow_check=False,
            canonical_only=canonical_only,
        ))
    current_by_id = {filing.filing_id: filing for filing in current_rows}
    if len(current_rows) != len(current_by_id):
        raise RuntimeError(
            "Selected filing IDs are not unique in the current source snapshot."
        )
    missing = sorted(set(expected_ids) - set(current_by_id))
    unexpected = sorted(set(current_by_id) - set(expected_ids))
    if missing or unexpected:
        raise RuntimeError(
            "Selected filings changed before the risk-network snapshot; "
            f"missing={missing[:10]!r}, unexpected={unexpected[:10]!r}. Rerun the selection."
        )
    changed = [
        filing.filing_id
        for filing in expected
        if current_by_id.get(filing.filing_id) != filing
    ]
    if changed:
        raise RuntimeError(
            "Selected filing metadata changed before the risk-network snapshot; "
            f"filing_ids={changed[:10]!r}. Rerun the selection."
        )


class NetworkBuilder:
    def __init__(self, source: sqlite3.Connection, output: sqlite3.Connection,
                 filings: Sequence[Filing], config: BuildConfig):
        self.source = source
        self.output = output
        self.filings = list(filings)
        self.by_id = {filing.filing_id: filing for filing in filings}
        self.config = config
        self.built_at = utc_now()
        self.rows_by_source: Dict[str, int] = {}
        self.source_notes: Dict[str, Tuple[str, bool, str]] = {}

    def _record_status(self, label: str, object_name: str, available: bool, note: str = "") -> None:
        self.source_notes[label] = (object_name, available, note)

    def _insert(self, rows: Iterable[Tuple[Any, ...]], source_label: str) -> int:
        placeholders = ",".join("?" for _ in INSERT_COLUMNS)
        sql = f"INSERT INTO risk_network_edge ({','.join(INSERT_COLUMNS)}) VALUES ({placeholders})"
        count = 0
        batch: List[Tuple[Any, ...]] = []
        for row in rows:
            batch.append(row)
            if len(batch) >= 2_000:
                self.output.executemany(sql, batch)
                count += len(batch)
                batch.clear()
        if batch:
            self.output.executemany(sql, batch)
            count += len(batch)
        self.rows_by_source[source_label] = self.rows_by_source.get(source_label, 0) + count
        return count

    def _filing_batches(self) -> Iterator[Sequence[str]]:
        ids = [filing.filing_id for filing in self.filings]
        yield from _chunks(ids, min(max(1, self.config.batch_size), 900))

    def address_edges(self) -> Iterator[Tuple[Any, ...]]:
        for filing in self.filings:
            address1 = normalize_text(" ".join((filing.address1, filing.address2)))
            city = normalize_text(filing.city)
            region = normalize_text(filing.region)
            postal = normalize_text(filing.postal_code)
            country = normalize_text(filing.country or "US")
            if not address1 or not (postal or (city and region)):
                continue
            is_po_box = bool(re.match(r"^(?:PO|P O) BOX\b", address1))
            normalized = "|".join((country, address1, city, region, postal))
            target_key = "address:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
            display = ", ".join(part for part in (filing.address1, filing.address2, filing.city, filing.region, filing.postal_code, filing.country) if part)
            yield make_edge(
                filing, target_key=target_key, target_type="address", target_ein="",
                target_name=display, edge_type="filed_address", direction="affiliation",
                amount=None, cash_amount=None, noncash_amount=None, amount_kind="",
                provenance_table="returns", provenance_row_id=filing.filing_id,
                confidence=0.35 if is_po_box else 1.0,
                confidence_basis=(
                    "po_box_address_candidate_only" if is_po_box
                    else "exact_normalized_filed_address"
                ),
                is_scored=not is_po_box,
                attributes={"normalized_address": normalized, "po_box": is_po_box},
                built_at=self.built_at,
            )

    def grant_edges(self) -> Iterator[Tuple[Any, ...]]:
        enhanced = _object_exists(self.source, ENHANCED_GRANT_VIEW)
        table = ENHANCED_GRANT_VIEW if enhanced else DETERMINISTIC_GRANT_TABLE
        if not _object_exists(self.source, table):
            self._record_status("grants", table, False, "Run resolve_grant_recipients.py first")
            return
        if enhanced:
            required_artifacts = {
                DETERMINISTIC_GRANT_TABLE: {
                    "grant_id", "filing_id", "recipient_reported_ein",
                    "recipient_reported_name", "cash_amount", "noncash_amount",
                    "total_amount", "purpose", "match_status", "warning_flags",
                    "resolved_ein", "resolved_org_name", "confidence",
                },
                APPLIED_GRANT_TABLE: {
                    "grant_id", "signature_hash", "selected_ein", "selected_name",
                    "ai_confidence", "ai_decision", "model",
                },
            }
            missing = [
                object_name
                for object_name in required_artifacts
                if _object_type(self.source, object_name) != "table"
            ]
            missing_columns = [
                f"{object_name}({', '.join(sorted(columns - _columns(self.source, object_name)))})"
                for object_name, columns in required_artifacts.items()
                if _object_type(self.source, object_name) == "table"
                and columns - _columns(self.source, object_name)
            ]
            invalid_keys = [
                f"{object_name}(grant_id exact non-null unique key)"
                for object_name in (
                    DETERMINISTIC_GRANT_TABLE,
                    APPLIED_GRANT_TABLE,
                )
                if _object_type(self.source, object_name) == "table"
                and "grant_id" in _columns(self.source, object_name)
                and not _unique_index_for_columns(
                    self.source, object_name, ("grant_id",)
                )
            ]
            if missing or missing_columns or invalid_keys:
                details = ", ".join(missing + missing_columns + invalid_keys)
                raise RuntimeError(
                    "Enhanced grant rows cannot be attributed to required resolver/applied "
                    f"artifacts: {details}."
                )
        self._record_status("grants", table, True, "enhanced" if enhanced else "deterministic fallback")
        grant_filing_index = (
            _index_with_prefix(self.source, RAW_GRANT_TABLE, ("filing_id",))
            if _object_exists(self.source, RAW_GRANT_TABLE)
            else ""
        )
        grant_index_hint = (
            ""
            if grant_filing_index in {"", "PRIMARY KEY"}
            else " INDEXED BY " + _quote_identifier(grant_filing_index)
        )
        for ids in self._filing_batches():
            if grant_filing_index:
                placeholders = ",".join("?" for _ in ids)
                # grant_recipient_resolved intentionally has no filing_id index in
                # production. Drive through the indexed raw-grant table, then use
                # its row ID to reach the resolver row; this keeps one-EIN and
                # incremental builds from scanning ~24 million resolution rows.
                projection = _enhanced_artifact_projection("rr") if enhanced else "rr.*"
                artifact_joins = (
                    f"LEFT JOIN {DETERMINISTIC_GRANT_TABLE} AS base ON base.grant_id=rr.grant_id "
                    f"LEFT JOIN {APPLIED_GRANT_TABLE} AS applied ON applied.grant_id=base.grant_id"
                    if enhanced else ""
                )
                sql = f"""
                  SELECT {projection}
                  FROM {RAW_GRANT_TABLE} AS g{grant_index_hint}
                  JOIN {table} AS rr ON rr.grant_id=g.id
                  {artifact_joins}
                  WHERE g.filing_id IN ({placeholders})
                  ORDER BY g.filing_id,g.id
                """
                params = tuple(ids)
            else:
                placeholders = ",".join("?" for _ in ids)
                if enhanced:
                    sql = f"""
                      SELECT {_enhanced_artifact_projection('rr')}
                      FROM {table} AS rr
                      LEFT JOIN {DETERMINISTIC_GRANT_TABLE} AS base
                        ON base.grant_id=rr.grant_id
                      LEFT JOIN {APPLIED_GRANT_TABLE} AS applied
                        ON applied.grant_id=base.grant_id
                      WHERE rr.filing_id IN ({placeholders})
                      ORDER BY rr.filing_id,rr.grant_id
                    """
                    params = tuple(ids)
                else:
                    sql, params = _in_query(table, ids)
            for row in self.source.execute(sql, params):
                filing = self.by_id.get(_clean(row["filing_id"]))
                if not filing:
                    continue
                if enhanced:
                    artifact_problem = _enhanced_row_artifact_problem(row)
                    if artifact_problem:
                        raise RuntimeError(
                            "Enhanced grant row is not exactly backed by resolver/applied "
                            f"artifacts: grant_id={row['grant_id']!r}, "
                            f"problem={artifact_problem}."
                        )
                    target_ein = normalize_ein(row["final_resolved_ein"])
                    target_name = _clean(row["final_resolved_org_name"] or row["recipient_reported_name"])
                    confidence = _number(row["final_confidence"]) or 0.0
                    match_source = _clean(row["final_match_source"]).lower()
                else:
                    target_ein = normalize_ein(row["resolved_ein"])
                    target_name = _clean(row["resolved_org_name"] or row["recipient_reported_name"])
                    confidence = _number(row["confidence"]) or 0.0
                    match_source = _clean(row["match_method"]).lower()
                name_norm = normalize_text(target_name)
                if target_ein:
                    target_key, target_type = "ein:" + target_ein, "organization"
                elif name_norm:
                    target_key, target_type = "orgname:" + name_norm, "organization_name"
                else:
                    continue
                status = _clean(row["match_status"]).lower()
                status_is_safe = bool(status) and "unresolved" not in status and "conflict" not in status
                status_gate = status_is_safe
                if enhanced:
                    if match_source == "deterministic":
                        status_gate = status_is_safe
                    else:
                        status_gate = match_source in ENHANCED_GRANT_SCOREABLE_SOURCES
                trusted = bool(
                    target_ein and confidence >= self.config.min_grant_confidence
                    and status_gate
                )
                cash = _number(row["cash_amount"])
                noncash = _number(row["noncash_amount"])
                amount = _number(row["total_amount"])
                if amount is None and (cash is not None or noncash is not None):
                    amount = (cash or 0.0) + (noncash or 0.0)
                yield make_edge(
                    filing, target_key=target_key, target_type=target_type,
                    target_ein=target_ein, target_name=target_name, edge_type="grant_paid",
                    direction="outgoing", amount=amount, cash_amount=cash,
                    noncash_amount=noncash, amount_kind="grant_cash_plus_noncash",
                    provenance_table=table, provenance_row_id=_row_id(row),
                    confidence=confidence,
                    confidence_basis=("enhanced_grant:" if enhanced else "deterministic_grant:") + (match_source or "unknown"),
                    is_scored=trusted,
                    attributes={
                        "match_status": row["match_status"], "warning_flags": row["warning_flags"],
                        "recipient_reported_ein": row["recipient_reported_ein"],
                        "recipient_reported_name": row["recipient_reported_name"],
                        "purpose": row["purpose"],
                    },
                    built_at=self.built_at,
                )

    def person_edges(self, table: str, name_col: str, title_col: str,
                     amount_cols: Sequence[str]) -> Iterator[Tuple[Any, ...]]:
        if not _object_exists(self.source, table):
            self._record_status("people:" + table, table, False, "source object absent")
            return
        columns = _columns(self.source, table)
        required = {"filing_id", name_col}
        if not required.issubset(columns):
            self._record_status("people:" + table, table, False, "required columns absent")
            return
        self._record_status("people:" + table, table, True)
        placeholders = {"", "N A", "NA", "NONE", "UNKNOWN", "VARIOUS", "SEE SCHEDULE", "NOT APPLICABLE"}
        for ids in self._filing_batches():
            sql, params = _in_query(table, ids)
            for row in self.source.execute(sql, params):
                filing = self.by_id.get(_clean(row["filing_id"]))
                name = _clean(row[name_col])
                norm = normalize_text(name)
                if not filing or not norm:
                    continue
                strong_name = norm not in placeholders and len(norm) >= 5 and len(norm.split()) >= 2
                amount = _sum_numbers(row, amount_cols)
                yield make_edge(
                    filing, target_key="person:" + norm, target_type="person",
                    target_ein="", target_name=name, edge_type="person_role",
                    direction="affiliation", amount=amount, cash_amount=None,
                    noncash_amount=None, amount_kind="reported_compensation_total",
                    provenance_table=table, provenance_row_id=_row_id(row),
                    confidence=0.90 if strong_name else 0.55,
                    confidence_basis="exact_normalized_person_name" if strong_name else "weak_or_single_token_person_name",
                    is_scored=strong_name,
                    attributes={"title": row[title_col] if title_col in columns else "", "compensation_columns": list(amount_cols)},
                    built_at=self.built_at,
                )

    def contractor_edges(self) -> Iterator[Tuple[Any, ...]]:
        table = "irs990_contractor_compensation_grp"
        if not _object_exists(self.source, table):
            self._record_status("contractors", table, False, "source object absent")
            return
        self._record_status("contractors", table, True)
        placeholders = {"", "N A", "NA", "NONE", "UNKNOWN", "VARIOUS", "SEE SCHEDULE"}
        for ids in self._filing_batches():
            sql, params = _in_query(table, ids)
            for row in self.source.execute(sql, params):
                filing = self.by_id.get(_clean(row["filing_id"]))
                name = _clean(row["business_name_line1_txt"] or row["person_nm"])
                norm = normalize_text(name)
                if not filing or not norm:
                    continue
                strong = norm not in placeholders and len(norm) >= 4
                address1 = _clean(row["usaddress_address_line1_txt"] or row["address_line1_txt"])
                city = _clean(row["usaddress_city_nm"] or row["city_nm"])
                region = _clean(row["state_abbreviation_cd"] or row["province_or_state_nm"])
                postal = _clean(row["zipcd"] or row["foreign_postal_cd"])
                yield make_edge(
                    filing, target_key="contractor:" + norm, target_type="contractor",
                    target_ein="", target_name=name, edge_type="contractor_payment",
                    direction="outgoing", amount=_number(row["compensation_amt"]),
                    cash_amount=None, noncash_amount=None,
                    amount_kind="contractor_compensation",
                    provenance_table=table, provenance_row_id=_row_id(row),
                    confidence=0.88 if strong else 0.50,
                    confidence_basis="exact_normalized_contractor_name" if strong else "weak_contractor_name",
                    is_scored=strong,
                    attributes={
                        "services": row["services_desc"], "address1": address1,
                        "city": city, "region": region, "postal_code": postal,
                    }, built_at=self.built_at,
                )

    def schedule_r_edges(self, table: str, edge_type: str,
                         scored_when_exact_ein: bool) -> Iterator[Tuple[Any, ...]]:
        if not _object_exists(self.source, table):
            self._record_status("schedule_r:" + table, table, False, "source object absent")
            return
        columns = _columns(self.source, table)
        self._record_status("schedule_r:" + table, table, True)
        for ids in self._filing_batches():
            sql, params = _in_query(table, ids)
            for row in self.source.execute(sql, params):
                filing = self.by_id.get(_clean(row["filing_id"]))
                if not filing:
                    continue
                target_ein = normalize_ein(row["ein"]) if "ein" in columns else ""
                name = ""
                for column in (
                    "related_organization_name_business_name_line1_txt",
                    "business_name_line1_txt",
                    "disregarded_entity_name_business_name_line1_txt",
                ):
                    if column in columns and _clean(row[column]):
                        name = _clean(row[column])
                        break
                name2 = ""
                for column in (
                    "related_organization_name_business_name_line2_txt",
                    "business_name_line2_txt",
                    "disregarded_entity_name_business_name_line2_txt",
                ):
                    if column in columns and _clean(row[column]):
                        name2 = _clean(row[column])
                        break
                target_name = " ".join(part for part in (name, name2) if part)
                norm = normalize_text(target_name)
                if target_ein:
                    target_key, target_type = "ein:" + target_ein, "organization"
                elif norm:
                    target_key, target_type = "schedr-name:" + norm, "organization_name"
                else:
                    continue
                exact = bool(target_ein)
                amount = _number(row["involved_amt"]) if "involved_amt" in columns else None
                attributes = {
                    key: row[key] for key in (
                        "controlled_organization_ind", "direct_controlling_nacd",
                        "general_or_managing_partner_ind", "ownership_pct",
                        "share_of_total_income_amt", "share_of_eoyassets_amt",
                        "ubicode_vamt", "primary_activities_txt", "transaction_type_txt",
                        "method_of_amount_determination_txt",
                    ) if key in columns
                }
                yield make_edge(
                    filing, target_key=target_key, target_type=target_type,
                    target_ein=target_ein, target_name=target_name, edge_type=edge_type,
                    direction="outgoing", amount=amount, cash_amount=None,
                    noncash_amount=None,
                    amount_kind="schedule_r_involved_amount" if amount is not None else "",
                    provenance_table=table, provenance_row_id=_row_id(row),
                    confidence=1.0 if exact else 0.60,
                    confidence_basis="schedule_r_exact_reported_ein" if exact else "schedule_r_name_only_unverified",
                    is_scored=bool(exact and scored_when_exact_ein),
                    attributes=attributes, built_at=self.built_at,
                )

    def build_edges(self) -> int:
        total = self._insert(self.address_edges(), "addresses")
        self._record_status("addresses", "returns", True, "exact filing-year address")
        total += self._insert(self.grant_edges(), "grants")
        for table, name_col, title_col, amounts in PERSON_SOURCES:
            total += self._insert(self.person_edges(table, name_col, title_col, amounts), "people:" + table)
        total += self._insert(self.contractor_edges(), "contractors")
        for table, edge_type, scored in SCHEDULE_R_SOURCES:
            total += self._insert(self.schedule_r_edges(table, edge_type, scored), "schedule_r:" + table)
        return total

    def write_filing_state(self) -> None:
        self.output.executemany(
            """
            INSERT OR REPLACE INTO risk_network_filing_state
              (filing_id,source_ein,tax_year,period_end,return_ts,built_at)
            VALUES (?,?,?,?,?,?)
            """,
            [(f.filing_id, f.ein, f.tax_year, f.period_end, f.return_ts, self.built_at) for f in self.filings],
        )

    def write_source_status(self) -> None:
        for label, (object_name, available, note) in self.source_notes.items():
            row_count = int(self.output.execute(
                "SELECT COUNT(*) FROM risk_network_edge WHERE provenance_table=?", (object_name,)
            ).fetchone()[0]) if available else 0
            self.output.execute(
                """
                INSERT OR REPLACE INTO risk_network_source_status
                  (source_name,object_name,available,rows_written,note,built_at)
                VALUES (?,?,?,?,?,?)
                """,
                (label, object_name, 1 if available else 0, row_count, note, self.built_at),
            )


def initialize_sidecar(conn: sqlite3.Connection, *, with_indexes: bool = True) -> None:
    conn.executescript(SCHEMA_SQL)
    if with_indexes:
        conn.executescript(INDEX_SQL)


def _hub_threshold_sql(config: BuildConfig) -> str:
    return f"""CASE target_type
      WHEN 'person' THEN {int(config.person_hub_threshold)}
      WHEN 'address' THEN {int(config.address_hub_threshold)}
      WHEN 'contractor' THEN {int(config.contractor_hub_threshold)}
      ELSE NULL END"""


def refresh_hub_metadata(conn: sqlite3.Connection, config: BuildConfig,
                         target_keys: Optional[Set[str]] = None) -> None:
    now = utc_now()
    if target_keys is None:
        conn.execute("DELETE FROM risk_network_node_stats")
        where = ""
        params: List[Any] = []
    else:
        if not target_keys:
            return
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_risk_affected_key(target_key TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM tmp_risk_affected_key")
        conn.executemany("INSERT INTO tmp_risk_affected_key(target_key) VALUES (?)", [(key,) for key in target_keys])
        conn.execute("DELETE FROM risk_network_node_stats WHERE target_key IN (SELECT target_key FROM tmp_risk_affected_key)")
        where = " WHERE target_key IN (SELECT target_key FROM tmp_risk_affected_key)"
        params = []
    threshold = _hub_threshold_sql(config)
    conn.execute(f"""
      INSERT INTO risk_network_node_stats
        (target_key,target_type,distinct_org_count,edge_count,filing_count,
         first_tax_year,last_tax_year,hub_threshold,is_hub,computed_at)
      SELECT target_key, target_type, COUNT(DISTINCT source_ein), COUNT(*),
             COUNT(DISTINCT filing_id), MIN(tax_year), MAX(tax_year),
             {threshold} AS hub_threshold,
             CASE WHEN ({threshold}) IS NOT NULL
                        AND COUNT(DISTINCT source_ein) > ({threshold}) THEN 1 ELSE 0 END,
             ?
      FROM risk_network_edge{where}
      GROUP BY target_key,target_type
    """, [now] + params)
    if target_keys is None:
        edge_where = ""
    else:
        edge_where = " WHERE target_key IN (SELECT target_key FROM tmp_risk_affected_key)"
    conn.execute(f"""
      UPDATE risk_network_edge
      SET hub_degree=COALESCE((SELECT distinct_org_count FROM risk_network_node_stats n
                               WHERE n.target_key=risk_network_edge.target_key),0),
          hub_suppressed=COALESCE((SELECT is_hub FROM risk_network_node_stats n
                                   WHERE n.target_key=risk_network_edge.target_key),0)
      {edge_where}
    """)


def write_meta(conn: sqlite3.Connection, source_path: Path, mode: str,
               filing_count: int, config: BuildConfig, row_count: int,
               *, build_scope: str, source_snapshot: SourceSnapshot,
               full_preflight: Optional[FullSourcePreflight] = None) -> None:
    coverage = conn.execute(
        """SELECT COUNT(DISTINCT source_ein), MIN(tax_year), MAX(tax_year)
           FROM risk_network_filing_state"""
    ).fetchone()
    values = {
        "schema_version": SCHEMA_VERSION,
        "build_status": "complete",
        "build_mode": mode,
        "build_scope": build_scope,
        "built_at": utc_now(),
        "source_file_name": source_path.name,
        "source_lineage_id": source_snapshot.lineage_id,
        "source_file_size": str(source_snapshot.file_size),
        "source_file_mtime_ns": str(source_snapshot.file_mtime_ns),
        "source_data_version_at_snapshot": str(source_snapshot.data_version),
        "source_journal_mode": source_snapshot.journal_mode,
        "source_wal_size_at_snapshot": str(source_snapshot.wal.size),
        "source_rollback_journal_size_at_snapshot": str(source_snapshot.rollback_journal.size),
        "source_checkpoint_condition": (
            "wal_absent_or_empty" if not source_snapshot.wal.populated else "wal_present"
        ),
        "selected_filing_count": str(filing_count),
        "edge_count_written": str(row_count),
        "min_grant_confidence": str(config.min_grant_confidence),
        "person_hub_threshold": str(config.person_hub_threshold),
        "address_hub_threshold": str(config.address_hub_threshold),
        "contractor_hub_threshold": str(config.contractor_hub_threshold),
        "canonical_filings_only": "1" if config.canonical_only else "0",
        "covered_ein_count": str(int(coverage[0] or 0)),
        "covered_min_tax_year": "" if coverage[1] is None else str(coverage[1]),
        "covered_max_tax_year": "" if coverage[2] is None else str(coverage[2]),
    }
    if full_preflight is not None:
        values.update({
            "full_source_preflight": "complete",
            "enhanced_grant_view_required": "1",
            "enhanced_grant_view_name": ENHANCED_GRANT_VIEW,
            "source_grant_count": str(full_preflight.grant_count),
            "source_resolver_count": str(full_preflight.resolver_count),
            "source_enhanced_grant_count": str(full_preflight.enhanced_count),
            "source_filing_index_count": str(len(full_preflight.filing_indexes)),
            "source_selected_filing_count": str(full_preflight.expected_filing_count),
        })
    conn.executemany(
        "INSERT OR REPLACE INTO risk_network_build_meta(key,value) VALUES (?,?)",
        sorted(values.items()),
    )


def validate_completed_sidecar(
    sidecar_path: Path,
    *,
    expected_filings: int,
    expected_edges: int,
    expected_scope: str,
    source_snapshot: SourceSnapshot,
    full_preflight: Optional[FullSourcePreflight] = None,
) -> None:
    """Reopen and validate the completed temporary database before publication."""

    required_indexes = {
        "idx_risk_edge_source_year",
        "idx_risk_edge_target_year",
        "idx_risk_edge_target_ein",
        "idx_risk_edge_filing",
        "idx_risk_edge_provenance",
        "idx_risk_edge_type_scored",
        "idx_risk_node_type_hub",
        "idx_risk_filing_source_year",
    }
    with closing(connect_source_readonly(sidecar_path)) as conn:
        print("Validating temporary sidecar with PRAGMA quick_check...", flush=True)
        quick_check = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
        if quick_check != ["ok"]:
            raise RuntimeError(
                "Temporary risk-network sidecar failed PRAGMA quick_check: "
                + "; ".join(quick_check[:20])
            )
        meta = {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key,value FROM risk_network_build_meta")
        }
        expected_meta = {
            "schema_version": SCHEMA_VERSION,
            "build_status": "complete",
            "build_scope": expected_scope,
            "selected_filing_count": str(expected_filings),
            "edge_count_written": str(expected_edges),
            "source_lineage_id": source_snapshot.lineage_id,
            "source_file_size": str(source_snapshot.file_size),
            "source_file_mtime_ns": str(source_snapshot.file_mtime_ns),
        }
        mismatched_meta = [
            f"{key}={meta.get(key)!r} (expected {value!r})"
            for key, value in expected_meta.items()
            if meta.get(key) != value
        ]
        if mismatched_meta:
            raise RuntimeError(
                "Temporary risk-network sidecar metadata validation failed: "
                + ", ".join(mismatched_meta)
            )

        actual_filings = int(conn.execute(
            "SELECT COUNT(*) FROM risk_network_filing_state"
        ).fetchone()[0])
        actual_edges = int(conn.execute(
            "SELECT COUNT(*) FROM risk_network_edge"
        ).fetchone()[0])
        if actual_filings != expected_filings or actual_edges != expected_edges:
            raise RuntimeError(
                "Temporary risk-network sidecar count validation failed: "
                f"filings={actual_filings:,}/{expected_filings:,}, "
                f"edges={actual_edges:,}/{expected_edges:,}"
            )

        indexes = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='index'")
        }
        missing_indexes = sorted(required_indexes - indexes)
        if missing_indexes:
            raise RuntimeError(
                "Temporary risk-network sidecar is missing required indexes: "
                + ", ".join(missing_indexes)
            )

        orphan_edge = conn.execute(
            """SELECT e.edge_id
               FROM risk_network_edge AS e
               LEFT JOIN risk_network_filing_state AS state
                 ON state.filing_id=e.filing_id
               WHERE state.filing_id IS NULL
               LIMIT 1"""
        ).fetchone()
        if orphan_edge is not None:
            raise RuntimeError(
                f"Temporary risk-network sidecar has an edge without filing state: {orphan_edge[0]!r}"
            )
        hub_mismatch = conn.execute(
            """SELECT e.edge_id
               FROM risk_network_edge AS e
               LEFT JOIN risk_network_node_stats AS node
                 ON node.target_key=e.target_key
               WHERE node.target_key IS NULL
                  OR e.hub_degree<>node.distinct_org_count
                  OR e.hub_suppressed<>node.is_hub
               LIMIT 1"""
        ).fetchone()
        if hub_mismatch is not None:
            raise RuntimeError(
                f"Temporary risk-network sidecar has inconsistent hub metadata: {hub_mismatch[0]!r}"
            )

        status_total = int(conn.execute(
            "SELECT COALESCE(SUM(rows_written),0) FROM risk_network_source_status"
        ).fetchone()[0])
        if status_total != actual_edges:
            raise RuntimeError(
                "Temporary risk-network source-status totals do not equal edge count: "
                f"status_rows={status_total:,}, edges={actual_edges:,}"
            )
        scoreable_grant_bases = tuple(sorted(
            "enhanced_grant:" + source
            for source in ENHANCED_GRANT_SCOREABLE_SOURCES | {"deterministic"}
        ))
        scoreable_placeholders = ",".join("?" for _ in scoreable_grant_bases)
        unsafe_scored_grant = conn.execute(
            f"""SELECT edge_id
                FROM risk_network_edge
                WHERE edge_type='grant_paid'
                  AND provenance_table=?
                  AND is_scored=1
                  AND (
                    target_ein IS NULL
                    OR LENGTH(target_ein)<>9
                    OR target_ein GLOB '*[^0-9]*'
                    OR confidence < ?
                    OR LOWER(confidence_basis) NOT IN ({scoreable_placeholders})
                    OR (
                      LOWER(confidence_basis)='enhanced_grant:deterministic'
                      AND (
                        NULLIF(TRIM(CAST(json_extract(attributes_json,'$.match_status') AS TEXT)),'') IS NULL
                        OR LOWER(CAST(json_extract(attributes_json,'$.match_status') AS TEXT)) LIKE '%unresolved%'
                        OR LOWER(CAST(json_extract(attributes_json,'$.match_status') AS TEXT)) LIKE '%conflict%'
                      )
                    )
                  )
                LIMIT 1""",
            (
                ENHANCED_GRANT_VIEW,
                float(meta.get("min_grant_confidence", "1")),
                *scoreable_grant_bases,
            ),
        ).fetchone()
        if unsafe_scored_grant is not None:
            raise RuntimeError(
                "Temporary risk-network sidecar contains a scored grant edge without "
                "approved enhanced provenance."
            )
        if full_preflight is not None:
            if full_preflight.expected_filing_count != expected_filings:
                raise RuntimeError(
                    "Temporary risk-network filing count disagrees with the independent source selection count: "
                    f"sidecar={expected_filings:,}, source={full_preflight.expected_filing_count:,}."
                )
            if meta.get("source_selected_filing_count") != str(full_preflight.expected_filing_count):
                raise RuntimeError(
                    "Temporary risk-network metadata is missing the independently counted source filing total."
                )
            statuses = {
                str(row["source_name"]): row
                for row in conn.execute("SELECT * FROM risk_network_source_status")
            }
            missing_labels = [
                label
                for label in FULL_REQUIRED_SOURCE_LABELS
                if label not in statuses or int(statuses[label]["available"] or 0) != 1
            ]
            if missing_labels:
                raise RuntimeError(
                    "Full risk-network sidecar lacks required available source groups: "
                    + ", ".join(missing_labels)
                )
            wrong_grant_source = conn.execute(
                """SELECT edge_id
                   FROM risk_network_edge
                   WHERE edge_type='grant_paid' AND provenance_table<>?
                   LIMIT 1""",
                (ENHANCED_GRANT_VIEW,),
            ).fetchone()
            if wrong_grant_source is not None:
                raise RuntimeError(
                    "Full risk-network sidecar contains a grant edge outside the required enhanced view."
                )
            unsafe_scored_edge = conn.execute(
                """SELECT edge_id
                   FROM risk_network_edge
                   WHERE is_scored=1
                      AND (
                        (edge_type LIKE 'schedule_r_%' AND target_ein IS NULL)
                        OR edge_type IN (
                          'schedule_r_unrelated_taxable_partnership',
                          'schedule_r_disregarded_entity',
                         'schedule_r_related_transaction'
                       )
                       OR confidence_basis='po_box_address_candidate_only'
                     )
                   LIMIT 1"""
            ).fetchone()
            if unsafe_scored_edge is not None:
                raise RuntimeError(
                    "Full risk-network sidecar contains source evidence that should have been retained as unscored."
                )
        print(
            f"Temporary sidecar validation complete: filings={actual_filings:,}, edges={actual_edges:,}.",
            flush=True,
        )


def ensure_distinct_database_paths(source_path: Path, sidecar_path: Path) -> None:
    """Refuse any build that could modify or replace the source database."""
    source = source_path.expanduser().resolve()
    destination = sidecar_path.expanduser().resolve()
    same_file = source == destination
    if not same_file and source.exists() and destination.exists():
        try:
            same_file = os.path.samefile(source, destination)
        except OSError:
            same_file = False
    if same_file:
        raise RuntimeError(
            "Risk-network destination must be a separate file from the read-only source database."
        )


def source_lineage_id(source_path: Path) -> str:
    """Identify the physical source file without hashing mutable database rows.

    ``st_dev`` plus ``st_ino`` is the volume/file-index identity on supported
    Windows Python builds and the ordinary device/inode identity on POSIX.  It
    stays stable for writes to the existing SQLite file but changes when an
    atomically published replacement takes over the same path.  The creation-
    time fallback is only for platforms/filesystems that report no file index.
    Size, modification time, and return contents are deliberately excluded.
    """
    resolved = source_path.expanduser().resolve()
    stat = resolved.stat()
    device = int(getattr(stat, "st_dev", 0))
    file_index = int(getattr(stat, "st_ino", 0))
    if file_index:
        identity_kind = "device_file_index"
        file_identity = [device, file_index]
    else:
        # st_birthtime_ns is explicit where available.  On older Windows
        # Python builds st_ctime_ns represents file creation time and remains
        # stable across in-place writes; POSIX filesystems normally take the
        # inode branch above.
        creation_time = int(
            getattr(stat, "st_birthtime_ns", 0)
            or getattr(stat, "st_ctime_ns", 0)
        )
        identity_kind = "device_creation_time_fallback"
        file_identity = [device, creation_time]
    payload = {
        "resolved_path": str(resolved).casefold(),
        "identity_kind": identity_kind,
        "file_identity": file_identity,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _auxiliary_file_state(path: Path) -> AuxiliaryFileState:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return AuxiliaryFileState(False, 0, 0)
    size = int(stat.st_size)
    if size <= 0:
        return AuxiliaryFileState(False, 0, 0)
    return AuxiliaryFileState(True, size, int(stat.st_mtime_ns))


def _source_path_state(source_path: Path) -> Tuple[str, int, int, AuxiliaryFileState, AuxiliaryFileState]:
    resolved = source_path.expanduser().resolve()
    stat = resolved.stat()
    return (
        source_lineage_id(resolved),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        _auxiliary_file_state(Path(str(resolved) + "-wal")),
        _auxiliary_file_state(Path(str(resolved) + "-journal")),
    )


def begin_source_snapshot(
    conn: sqlite3.Connection,
    source_path: Path,
    *,
    require_checkpointed: bool,
) -> SourceSnapshot:
    """Begin one read snapshot and record the file/version state it represents."""

    before = _source_path_state(source_path)
    if require_checkpointed and before[3].populated:
        raise RuntimeError(
            "Full risk-network build requires a checkpointed source database; "
            f"non-empty WAL remains ({before[3].size:,} bytes). Stop writers and run "
            "PRAGMA wal_checkpoint(TRUNCATE) before retrying."
        )
    if before[4].populated:
        raise RuntimeError(
            "Source database has a non-empty rollback journal; stop the active writer "
            "and verify database recovery before building the risk network."
        )
    conn.execute("BEGIN")
    # Force the deferred transaction to acquire its read snapshot before the
    # data-version and filesystem stamps are recorded.
    conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
    data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
    after = _source_path_state(source_path)
    if before != after:
        raise RuntimeError(
            "Source database changed while the risk-network read snapshot was starting; retry in a maintenance window."
        )
    return SourceSnapshot(
        lineage_id=after[0],
        file_size=after[1],
        file_mtime_ns=after[2],
        data_version=data_version,
        journal_mode=journal_mode,
        wal=after[3],
        rollback_journal=after[4],
    )


def assert_source_snapshot_unchanged(
    conn: sqlite3.Connection,
    source_path: Path,
    snapshot: SourceSnapshot,
    *,
    require_checkpointed: bool,
) -> None:
    """Refuse publication when another connection committed during the build."""

    current_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    current = _source_path_state(source_path)
    expected = (
        snapshot.lineage_id,
        snapshot.file_size,
        snapshot.file_mtime_ns,
        snapshot.wal,
        snapshot.rollback_journal,
    )
    if current_version != snapshot.data_version or current != expected:
        raise RuntimeError(
            "Source database changed during the risk-network build; the temporary sidecar will not be published. "
            "Stop writers, checkpoint the source, and rebuild."
        )
    if require_checkpointed and current[3].populated:
        raise RuntimeError(
            "Source WAL became non-empty during the full risk-network build; the temporary sidecar will not be published."
        )


def assert_source_path_matches_snapshot(source_path: Path, snapshot: SourceSnapshot) -> None:
    """Repeat the filesystem guard immediately before the atomic rename."""

    current = _source_path_state(source_path)
    expected = (
        snapshot.lineage_id,
        snapshot.file_size,
        snapshot.file_mtime_ns,
        snapshot.wal,
        snapshot.rollback_journal,
    )
    if current != expected:
        raise RuntimeError(
            "Source database changed after sidecar validation; refusing atomic publication."
        )


def assert_destination_auxiliaries_clear(sidecar_path: Path) -> None:
    """Prevent an old WAL/journal from being replayed over a new main file."""

    resolved = sidecar_path.expanduser().resolve()
    populated = [
        (label, state)
        for label, state in (
            ("WAL", _auxiliary_file_state(Path(str(resolved) + "-wal"))),
            ("rollback journal", _auxiliary_file_state(Path(str(resolved) + "-journal"))),
        )
        if state.populated
    ]
    if populated:
        details = ", ".join(f"{label}={state.size:,} bytes" for label, state in populated)
        raise RuntimeError(
            "Risk-network destination has populated SQLite auxiliary files "
            f"({details}); refusing atomic replacement because stale pages could be "
            "replayed over the validated sidecar. Stop users of the destination, "
            "checkpoint/recover it, or choose a new destination path."
        )


def rebuild_sidecar(source_path: Path, sidecar_path: Path, filings: Sequence[Filing],
                    config: BuildConfig) -> Dict[str, int]:
    ensure_distinct_database_paths(source_path, sidecar_path)
    assert_destination_auxiliaries_clear(sidecar_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=sidecar_path.name + ".building-", suffix=".db", dir=str(sidecar_path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    snapshot: Optional[SourceSnapshot] = None
    try:
        with closing(connect_source_readonly(source_path)) as source, closing(connect_sidecar(temp_path)) as output:
            snapshot = begin_source_snapshot(
                source, source_path, require_checkpointed=False
            )
            verify_selected_filings_in_snapshot(
                source, filings, canonical_only=config.canonical_only
            )
            initialize_sidecar(output, with_indexes=False)
            output.execute("BEGIN IMMEDIATE")
            builder = NetworkBuilder(source, output, filings, config)
            edge_count = builder.build_edges()
            builder.write_filing_state()
            output.commit()
            output.executescript(PRE_HUB_INDEX_SQL)
            output.execute("BEGIN IMMEDIATE")
            builder.write_source_status()
            refresh_hub_metadata(output, config)
            output.commit()
            output.executescript(POST_HUB_INDEX_SQL)
            output.execute("BEGIN IMMEDIATE")
            write_meta(
                output, source_path, "rebuild", len(filings), config, edge_count,
                build_scope="selected", source_snapshot=snapshot,
            )
            output.commit()
            output.execute("ANALYZE")
            output.commit()
            assert_source_snapshot_unchanged(
                source, source_path, snapshot, require_checkpointed=False
            )
        if snapshot is None:
            raise RuntimeError("Risk-network source snapshot was not established.")
        validate_completed_sidecar(
            temp_path,
            expected_filings=len(filings),
            expected_edges=edge_count,
            expected_scope="selected",
            source_snapshot=snapshot,
        )
        assert_source_path_matches_snapshot(source_path, snapshot)
        assert_destination_auxiliaries_clear(sidecar_path)
        os.replace(temp_path, sidecar_path)
        return {"filings": len(filings), "edges": edge_count}
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def rebuild_full_sidecar(
    source_path: Path,
    sidecar_path: Path,
    config: BuildConfig,
    *,
    eins: Sequence[str] = (),
    filing_ids: Sequence[str] = (),
    min_tax_year: Optional[int] = None,
    max_tax_year: Optional[int] = None,
    page_size: int = 10_000,
) -> Dict[str, int]:
    """Stream an explicitly confirmed full selection into a temporary sidecar."""
    ensure_distinct_database_paths(source_path, sidecar_path)
    assert_destination_auxiliaries_clear(sidecar_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=sidecar_path.name + ".building-", suffix=".db", dir=str(sidecar_path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    edge_count = filing_count = 0
    snapshot: Optional[SourceSnapshot] = None
    full_preflight: Optional[FullSourcePreflight] = None
    scope = "full" if not (eins or filing_ids or min_tax_year is not None or max_tax_year is not None) else "selected_streaming"
    try:
        with closing(connect_source_readonly(source_path)) as source, closing(connect_sidecar(temp_path)) as output:
            # Hold one explicit read snapshot across every page. Writers should
            # still be stopped for a full build so their WAL cannot grow for hours.
            snapshot = begin_source_snapshot(
                source, source_path, require_checkpointed=True
            )
            structural_preflight = validate_full_source(source, check_row_parity=True)
            expected_filing_count = count_selected_filings(
                source,
                eins=eins,
                filing_ids=filing_ids,
                min_tax_year=min_tax_year,
                max_tax_year=max_tax_year,
                canonical_only=config.canonical_only,
            )
            if expected_filing_count == 0:
                raise RuntimeError(
                    "Full risk-network rebuild selected zero filings; refusing to replace "
                    "the destination with an empty sidecar. Verify every selector and the "
                    "canonical filing scope."
                )
            full_preflight = FullSourcePreflight(
                grant_count=structural_preflight.grant_count,
                resolver_count=structural_preflight.resolver_count,
                enhanced_count=structural_preflight.enhanced_count,
                filing_indexes=structural_preflight.filing_indexes,
                expected_filing_count=expected_filing_count,
            )
            print(
                "Full source preflight complete: "
                f"grants=resolver=enhanced={full_preflight.grant_count:,}; "
                f"selected filings={expected_filing_count:,}; "
                f"filing indexes={len(full_preflight.filing_indexes):,}.",
                flush=True,
            )
            initialize_sidecar(output, with_indexes=False)
            after_filing_id = ""
            last_builder: Optional[NetworkBuilder] = None
            while True:
                page = select_filings(
                    source, eins=eins, filing_ids=filing_ids,
                    min_tax_year=min_tax_year, max_tax_year=max_tax_year,
                    max_filings=page_size, after_filing_id=after_filing_id,
                    overflow_check=False, canonical_only=config.canonical_only,
                )
                if not page:
                    break
                output.execute("BEGIN IMMEDIATE")
                builder = NetworkBuilder(source, output, page, config)
                edge_count += builder.build_edges()
                builder.write_filing_state()
                output.commit()
                filing_count += len(page)
                after_filing_id = page[-1].filing_id
                last_builder = builder
                print(f"Streamed {filing_count:,} filings and {edge_count:,} edges...", flush=True)
                if len(page) < page_size:
                    break
            if filing_count != expected_filing_count:
                raise RuntimeError(
                    "Full risk-network pagination was incomplete; "
                    f"streamed={filing_count:,}, independently selected={expected_filing_count:,}."
                )
            if last_builder is None:
                last_builder = NetworkBuilder(source, output, (), config)
                last_builder.build_edges()
            print("Creating pre-hub risk-network indexes...", flush=True)
            output.executescript(PRE_HUB_INDEX_SQL)
            output.execute("BEGIN IMMEDIATE")
            last_builder.write_source_status()
            print("Computing global hub-suppression metadata...", flush=True)
            refresh_hub_metadata(output, config)
            output.commit()
            print("Creating final hub-aware risk-network index...", flush=True)
            output.executescript(POST_HUB_INDEX_SQL)
            output.execute("BEGIN IMMEDIATE")
            write_meta(
                output, source_path, "rebuild", filing_count, config, edge_count,
                build_scope=scope, source_snapshot=snapshot,
                full_preflight=full_preflight,
            )
            output.commit()
            output.execute("ANALYZE")
            output.commit()
            assert_source_snapshot_unchanged(
                source, source_path, snapshot, require_checkpointed=True
            )
        if snapshot is None or full_preflight is None:
            raise RuntimeError("Full risk-network source preflight did not complete.")
        validate_completed_sidecar(
            temp_path,
            expected_filings=full_preflight.expected_filing_count,
            expected_edges=edge_count,
            expected_scope=scope,
            source_snapshot=snapshot,
            full_preflight=full_preflight,
        )
        assert_source_path_matches_snapshot(source_path, snapshot)
        assert_destination_auxiliaries_clear(sidecar_path)
        os.replace(temp_path, sidecar_path)
        return {"filings": full_preflight.expected_filing_count, "edges": edge_count}
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def incremental_sidecar(source_path: Path, sidecar_path: Path, filings: Sequence[Filing],
                        config: BuildConfig) -> Dict[str, int]:
    ensure_distinct_database_paths(source_path, sidecar_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_source_readonly(source_path)) as source, closing(connect_sidecar(sidecar_path)) as output:
        snapshot = begin_source_snapshot(
            source, source_path, require_checkpointed=False
        )
        verify_selected_filings_in_snapshot(
            source, filings, canonical_only=config.canonical_only
        )
        current_lineage = snapshot.lineage_id
        previous_scope = ""
        try:
            previous_meta = {
                row[0]: str(row[1] or "")
                for row in output.execute("SELECT key,value FROM risk_network_build_meta")
            }
        except sqlite3.Error:
            previous_meta = {}
        if previous_meta:
            if previous_meta.get("schema_version") != SCHEMA_VERSION:
                raise RuntimeError(
                    "Risk-network sidecar schema is incompatible; run a full rebuild before incremental refresh."
                )
            if previous_meta.get("source_lineage_id") != current_lineage:
                raise RuntimeError(
                    "Risk-network sidecar belongs to a different source database; run a full rebuild."
                )
        previous_scope = previous_meta.get("build_scope", "")
        initialize_sidecar(output)
        output.execute("BEGIN IMMEDIATE")
        filing_ids = [filing.filing_id for filing in filings]
        affected: Set[str] = set()
        stale_filing_ids: List[str] = []
        if config.canonical_only and filings:
            output.execute(
                """CREATE TEMP TABLE IF NOT EXISTS selected_canonical_key (
                     source_ein TEXT NOT NULL, tax_year INTEGER NOT NULL,
                     filing_id TEXT NOT NULL, PRIMARY KEY(source_ein,tax_year)
                   ) WITHOUT ROWID"""
            )
            output.execute("DELETE FROM selected_canonical_key")
            output.executemany(
                "INSERT OR REPLACE INTO selected_canonical_key VALUES (?,?,?)",
                [(filing.ein, filing.tax_year, filing.filing_id) for filing in filings],
            )
            stale_filing_ids = [
                row[0] for row in output.execute(
                    """SELECT state.filing_id
                       FROM risk_network_filing_state AS state
                       JOIN selected_canonical_key AS selected
                         ON selected.source_ein=state.source_ein
                        AND selected.tax_year=state.tax_year
                       WHERE state.filing_id<>selected.filing_id"""
                )
            ]
        replacement_ids = list(dict.fromkeys([*filing_ids, *stale_filing_ids]))
        for ids in _chunks(replacement_ids, 900):
            placeholders = ",".join("?" for _ in ids)
            affected.update(row[0] for row in output.execute(
                f"SELECT DISTINCT target_key FROM risk_network_edge WHERE filing_id IN ({placeholders})", ids
            ))
            output.execute(f"DELETE FROM risk_network_edge WHERE filing_id IN ({placeholders})", ids)
            output.execute(f"DELETE FROM risk_network_filing_state WHERE filing_id IN ({placeholders})", ids)
        builder = NetworkBuilder(source, output, filings, config)
        edge_count = builder.build_edges()
        builder.write_filing_state()
        builder.write_source_status()
        for ids in _chunks(filing_ids, 900):
            placeholders = ",".join("?" for _ in ids)
            affected.update(row[0] for row in output.execute(
                f"SELECT DISTINCT target_key FROM risk_network_edge WHERE filing_id IN ({placeholders})", ids
            ))
        refresh_hub_metadata(output, config, affected)
        build_scope = "full_plus_incremental" if previous_scope in {"full", "full_plus_incremental"} else "incremental"
        write_meta(
            output, source_path, "incremental", len(filings), config, edge_count,
            build_scope=build_scope, source_snapshot=snapshot,
        )
        assert_source_snapshot_unchanged(
            source, source_path, snapshot, require_checkpointed=False
        )
        output.commit()
        return {
            "filings": len(filings),
            "edges": edge_count,
            "affected_nodes": len(affected),
            "stale_canonical_filings_removed": len(stale_filing_ids),
        }


def default_sidecar_path(source_path: Path) -> Path:
    env_path = os.environ.get("IRS_RISK_NETWORK_DB_PATH", "").strip()
    return Path(env_path) if env_path else source_path.resolve().parent / DEFAULT_SIDECAR_NAME


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the fraud/risk relationship-network SQLite sidecar")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "rebuild", "incremental"):
        p = sub.add_parser(command)
        p.add_argument("--db", default=os.environ.get("IRS_DB_PATH", r"C:\Projects\irs990-tool\db\irs990.db"))
        p.add_argument("--sidecar", default=os.environ.get("IRS_RISK_NETWORK_DB_PATH", ""))
        p.add_argument("--ein", action="append", default=[], help="Repeatable organization EIN selector")
        p.add_argument("--filing-id", action="append", default=[], help="Repeatable exact filing ID selector")
        p.add_argument("--min-tax-year", type=int)
        p.add_argument("--max-tax-year", type=int)
        p.add_argument("--max-filings", type=int, default=10_000,
                       help="Safety cap for plan/incremental selections; maximum allowed is 100000")
        p.add_argument("--batch-size", type=int, default=500)
        p.add_argument("--min-grant-confidence", type=float, default=0.85)
        p.add_argument("--person-hub-threshold", type=int, default=25)
        p.add_argument("--address-hub-threshold", type=int, default=50)
        p.add_argument("--contractor-hub-threshold", type=int, default=50)
        p.add_argument("--include-noncanonical", action="store_true",
                       help="Include amended/superseded filings; canonical filings are the default")
        if command in ("plan", "rebuild"):
            p.add_argument("--full", action="store_true", help="Plan or run an unbounded, streaming full-database rebuild")
        if command == "rebuild":
            p.add_argument("--yes", action="store_true", help="Confirm replacement of the destination sidecar")
    return parser


def _has_selector(args: argparse.Namespace) -> bool:
    return bool(args.ein or args.filing_id or args.min_tax_year is not None or args.max_tax_year is not None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    source_path = Path(args.db).resolve()
    if not source_path.is_file():
        raise RuntimeError(f"Source database not found: {source_path}")
    sidecar_path = Path(args.sidecar).resolve() if args.sidecar else default_sidecar_path(source_path)
    ensure_distinct_database_paths(source_path, sidecar_path)
    if args.max_filings < 1 or args.max_filings > MAX_INCREMENTAL_FILINGS:
        raise RuntimeError(f"--max-filings must be between 1 and {MAX_INCREMENTAL_FILINGS:,}")
    # Validate every supplied selector before command routing.  In particular,
    # --full must never turn a malformed bounded selector into an unbounded build.
    _validated_ein_selectors(args.ein)
    _validated_filing_id_selectors(args.filing_id)
    _validated_tax_year_bounds(args.min_tax_year, args.max_tax_year)
    if args.command == "incremental" and not _has_selector(args):
        raise RuntimeError("incremental requires --ein, --filing-id, or a tax-year bound")
    if args.command == "rebuild" and not args.full and not _has_selector(args):
        raise RuntimeError("Unbounded rebuild requires --full and --yes; run plan first")
    config = BuildConfig(
        min_grant_confidence=float(args.min_grant_confidence),
        person_hub_threshold=max(1, int(args.person_hub_threshold)),
        address_hub_threshold=max(1, int(args.address_hub_threshold)),
        contractor_hub_threshold=max(1, int(args.contractor_hub_threshold)),
        batch_size=min(900, max(1, int(args.batch_size))),
        canonical_only=not bool(args.include_noncanonical),
    )
    if args.command == "plan" and args.full:
        selected_count: Optional[int] = None
        with closing(connect_source_readonly(source_path)) as source:
            snapshot = begin_source_snapshot(
                source, source_path, require_checkpointed=True
            )
            preflight = validate_full_source(source, check_row_parity=False)
            estimates = sqlite_row_estimates(source, ESTIMATE_TABLES)
            if _has_selector(args):
                selected_count = count_selected_filings(
                    source,
                    eins=args.ein,
                    filing_ids=args.filing_id,
                    min_tax_year=args.min_tax_year,
                    max_tax_year=args.max_tax_year,
                    canonical_only=config.canonical_only,
                )
                if selected_count == 0:
                    raise RuntimeError(
                        "Full risk-network plan selected zero filings; the corresponding "
                        "rebuild would refuse an empty replacement. Verify the selectors."
                    )
            assert_source_snapshot_unchanged(
                source, source_path, snapshot, require_checkpointed=True
            )
        filing_estimate = selected_count if selected_count is not None else (
            estimates.get("canonical_by_ein_year", estimates.get("returns", 0))
            if config.canonical_only else estimates.get("returns", 0)
        )
        source_upper_bound = sum(value for table, value in estimates.items() if table != "canonical_by_ein_year")
        rough_gib_low = source_upper_bound * 300 / (1024 ** 3)
        rough_gib_high = source_upper_bound * 650 / (1024 ** 3)
        print(f"Source (read-only): {source_path}")
        print(f"Destination:        {sidecar_path}")
        if selected_count is None:
            print(f"Estimated filings:  {filing_estimate:,} (SQLite ANALYZE statistics)")
        else:
            print(f"Selected filings:   {filing_estimate:,} (exact selector count)")
        print(f"Source-row ceiling: {source_upper_bound:,} before blank/untrusted filtering")
        print(f"Rough sidecar disk: {rough_gib_low:,.1f}-{rough_gib_high:,.1f} GiB; verify free space")
        print(f"Enhanced grants:    {ENHANCED_GRANT_VIEW} is present")
        print(f"Filing indexes:     {len(preflight.filing_indexes):,} required indexes/primary keys present")
        print("Checkpoint state:   source WAL absent or empty")
        if selected_count is None:
            print("Plan only: no source data tables were scanned and no files were changed; exact grant row parity runs at rebuild start.")
        else:
            print("Plan only: the filtered filing selection was counted exactly and no files were changed; exact grant row parity runs at rebuild start.")
        return 0
    if args.command == "rebuild" and args.full:
        print(f"Source (read-only): {source_path}")
        print(f"Destination:        {sidecar_path}")
        print("Selection:          all matching filings, streamed in bounded pages")
        if not args.yes:
            raise RuntimeError("rebuild requires --yes after reviewing the plan")
        result = rebuild_full_sidecar(
            source_path, sidecar_path, config, eins=args.ein,
            filing_ids=args.filing_id, min_tax_year=args.min_tax_year,
            max_tax_year=args.max_tax_year, page_size=int(args.max_filings),
        )
        print("Completed: " + ", ".join(f"{key}={value:,}" for key, value in result.items()))
        return 0
    with closing(connect_source_readonly(source_path)) as source:
        filings = select_filings(
            source, eins=args.ein, filing_ids=args.filing_id,
            min_tax_year=args.min_tax_year, max_tax_year=args.max_tax_year,
            max_filings=int(args.max_filings), canonical_only=config.canonical_only,
        )
    years = [f.tax_year for f in filings if f.tax_year is not None]
    print(f"Source (read-only): {source_path}")
    print(f"Destination:        {sidecar_path}")
    print(f"Selected filings:   {len(filings):,}")
    if years:
        print(f"Tax-year range:     {min(years)}-{max(years)}")
    if args.command == "plan":
        print("Plan only: no files were changed.")
        return 0
    if args.command == "rebuild" and not args.yes:
        raise RuntimeError("rebuild requires --yes after reviewing the plan")
    if not filings:
        print("No matching filings; no files were changed.")
        return 0
    if args.command == "rebuild":
        result = rebuild_sidecar(source_path, sidecar_path, filings, config)
    else:
        result = incremental_sidecar(source_path, sidecar_path, filings, config)
    print("Completed: " + ", ".join(f"{key}={value:,}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
