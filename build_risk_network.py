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


INDEX_SQL = """
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
CREATE INDEX IF NOT EXISTS idx_risk_edge_type_scored
  ON risk_network_edge(edge_type, is_scored, hub_suppressed, tax_year);
CREATE INDEX IF NOT EXISTS idx_risk_node_type_hub
  ON risk_network_node_stats(target_type, is_hub, distinct_org_count);
CREATE INDEX IF NOT EXISTS idx_risk_filing_source_year
  ON risk_network_filing_state(source_ein, tax_year);
"""


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
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits or len(digits) > 9:
        return ""
    return digits.zfill(9)


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
    if not _object_exists(conn, "returns"):
        raise RuntimeError("Source database is missing required table: returns")
    clauses: List[str] = []
    params: List[Any] = []
    clean_eins = sorted({normalize_ein(value) for value in eins if normalize_ein(value)})
    if clean_eins:
        forms: List[str] = []
        for ein in clean_eins:
            forms.extend((ein, ein[:2] + "-" + ein[2:]))
        clauses.append("r.ein IN (" + ",".join("?" for _ in forms) + ")")
        params.extend(forms)
    if filing_ids:
        unique_ids = sorted({_clean(value) for value in filing_ids if _clean(value)})
        clauses.append("r.filing_id IN (" + ",".join("?" for _ in unique_ids) + ")")
        params.extend(unique_ids)
    if min_tax_year is not None:
        clauses.append("r.tax_year >= ?")
        params.append(int(min_tax_year))
    if max_tax_year is not None:
        clauses.append("r.tax_year <= ?")
        params.append(int(max_tax_year))
    if after_filing_id:
        clauses.append("r.filing_id > ?")
        params.append(after_filing_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    limit = ""
    if max_filings is not None:
        limit = " LIMIT ?"
        params.append(int(max_filings) + (1 if overflow_check else 0))
    canonical_join = ""
    if canonical_only and _object_exists(conn, "canonical_by_ein_year"):
        canonical_join = " JOIN canonical_by_ein_year AS c ON c.filing_id=r.filing_id"
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
        filing_id = _clean(row["filing_id"])
        if not ein or not filing_id:
            continue
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
        enhanced = _object_exists(self.source, "grant_recipient_resolved_plus_ai_v1")
        table = "grant_recipient_resolved_plus_ai_v1" if enhanced else "grant_recipient_resolved"
        if not _object_exists(self.source, table):
            self._record_status("grants", table, False, "Run resolve_grant_recipients.py first")
            return
        self._record_status("grants", table, True, "enhanced" if enhanced else "deterministic fallback")
        for ids in self._filing_batches():
            if _object_exists(self.source, "grants") and _index_exists(self.source, "idx_grants_filing_id"):
                placeholders = ",".join("?" for _ in ids)
                # grant_recipient_resolved intentionally has no filing_id index in
                # production. Drive through the indexed raw-grant table, then use
                # its row ID to reach the resolver row; this keeps one-EIN and
                # incremental builds from scanning ~24 million resolution rows.
                sql = f"""
                  SELECT rr.*
                  FROM grants AS g INDEXED BY idx_grants_filing_id
                  JOIN {table} AS rr ON rr.grant_id=g.id
                  WHERE g.filing_id IN ({placeholders})
                  ORDER BY g.filing_id,g.id
                """
                params = tuple(ids)
            else:
                sql, params = _in_query(table, ids)
            for row in self.source.execute(sql, params):
                filing = self.by_id.get(_clean(row["filing_id"]))
                if not filing:
                    continue
                if enhanced:
                    target_ein = normalize_ein(row["final_resolved_ein"])
                    target_name = _clean(row["final_resolved_org_name"] or row["recipient_reported_name"])
                    confidence = _number(row["final_confidence"]) or 0.0
                    match_source = _clean(row["final_match_source"])
                else:
                    target_ein = normalize_ein(row["resolved_ein"])
                    target_name = _clean(row["resolved_org_name"] or row["recipient_reported_name"])
                    confidence = _number(row["confidence"]) or 0.0
                    match_source = _clean(row["match_method"])
                name_norm = normalize_text(target_name)
                if target_ein:
                    target_key, target_type = "ein:" + target_ein, "organization"
                elif name_norm:
                    target_key, target_type = "orgname:" + name_norm, "organization_name"
                else:
                    continue
                status = _clean(row["match_status"]).lower()
                status_is_safe = "unresolved" not in status and "conflict" not in status
                status_gate = status_is_safe
                if enhanced:
                    status_gate = (
                        match_source != "reported_ein_from_filing_unverified"
                        and (match_source != "deterministic" or status_is_safe)
                    )
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
               *, build_scope: str, source_lineage: str) -> None:
    source_stat = source_path.stat()
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
        "source_lineage_id": source_lineage,
        "source_file_size": str(source_stat.st_size),
        "source_file_mtime_ns": str(source_stat.st_mtime_ns),
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
    conn.executemany(
        "INSERT OR REPLACE INTO risk_network_build_meta(key,value) VALUES (?,?)",
        sorted(values.items()),
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


def rebuild_sidecar(source_path: Path, sidecar_path: Path, filings: Sequence[Filing],
                    config: BuildConfig) -> Dict[str, int]:
    ensure_distinct_database_paths(source_path, sidecar_path)
    source_lineage = source_lineage_id(source_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=sidecar_path.name + ".building-", suffix=".db", dir=str(sidecar_path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with closing(connect_source_readonly(source_path)) as source, closing(connect_sidecar(temp_path)) as output:
            source.execute("BEGIN")
            initialize_sidecar(output, with_indexes=False)
            output.execute("BEGIN IMMEDIATE")
            builder = NetworkBuilder(source, output, filings, config)
            edge_count = builder.build_edges()
            builder.write_filing_state()
            output.commit()
            output.executescript(INDEX_SQL)
            output.execute("BEGIN IMMEDIATE")
            builder.write_source_status()
            refresh_hub_metadata(output, config)
            write_meta(
                output, source_path, "rebuild", len(filings), config, edge_count,
                build_scope="selected", source_lineage=source_lineage,
            )
            output.commit()
            output.execute("ANALYZE")
            output.commit()
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
    source_lineage = source_lineage_id(source_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=sidecar_path.name + ".building-", suffix=".db", dir=str(sidecar_path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    edge_count = filing_count = 0
    try:
        with closing(connect_source_readonly(source_path)) as source, closing(connect_sidecar(temp_path)) as output:
            # Hold one explicit read snapshot across every page. Writers should
            # still be stopped for a full build so their WAL cannot grow for hours.
            source.execute("BEGIN")
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
            output.executescript(INDEX_SQL)
            output.execute("BEGIN IMMEDIATE")
            if last_builder is not None:
                last_builder.write_source_status()
            refresh_hub_metadata(output, config)
            scope = "full" if not (eins or filing_ids or min_tax_year is not None or max_tax_year is not None) else "selected_streaming"
            write_meta(
                output, source_path, "rebuild", filing_count, config, edge_count,
                build_scope=scope, source_lineage=source_lineage,
            )
            output.commit()
            output.execute("ANALYZE")
            output.commit()
        os.replace(temp_path, sidecar_path)
        return {"filings": filing_count, "edges": edge_count}
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def incremental_sidecar(source_path: Path, sidecar_path: Path, filings: Sequence[Filing],
                        config: BuildConfig) -> Dict[str, int]:
    ensure_distinct_database_paths(source_path, sidecar_path)
    current_lineage = source_lineage_id(source_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_source_readonly(source_path)) as source, closing(connect_sidecar(sidecar_path)) as output:
        source.execute("BEGIN")
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
            build_scope=build_scope, source_lineage=current_lineage,
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
        with closing(connect_source_readonly(source_path)) as source:
            estimates = sqlite_row_estimates(source, ESTIMATE_TABLES)
        filing_estimate = (
            estimates.get("canonical_by_ein_year", estimates.get("returns", 0))
            if config.canonical_only else estimates.get("returns", 0)
        )
        source_upper_bound = sum(value for table, value in estimates.items() if table != "canonical_by_ein_year")
        rough_gib_low = source_upper_bound * 300 / (1024 ** 3)
        rough_gib_high = source_upper_bound * 650 / (1024 ** 3)
        print(f"Source (read-only): {source_path}")
        print(f"Destination:        {sidecar_path}")
        print(f"Estimated filings:  {filing_estimate:,} (SQLite ANALYZE statistics)")
        print(f"Source-row ceiling: {source_upper_bound:,} before blank/untrusted filtering")
        print(f"Rough sidecar disk: {rough_gib_low:,.1f}-{rough_gib_high:,.1f} GiB; verify free space")
        print("Plan only: no tables were scanned and no files were changed.")
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
