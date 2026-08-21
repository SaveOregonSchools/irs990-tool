"""Read-only access helpers for the precomputed fraud/risk network sidecar."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote

from risk_source_identity import (
    RiskSourceIdentityError,
    parse_portable_metadata,
    validate_portable_source,
)


DEFAULT_NAME = "risk_network.db"
EXPECTED_SCHEMA_VERSION = "1"
DEFAULT_MAIN_DB_PATH = Path(__file__).resolve().parents[1] / "db" / "irs990.db"


def _normalize_ein(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(9) if digits and len(digits) <= 9 else ""


def risk_network_path(main_db_path: Optional[str] = None,
                      environ: Optional[Mapping[str, str]] = None) -> Path:
    env = environ if environ is not None else os.environ
    configured = str(env.get("IRS_RISK_NETWORK_DB_PATH", "") or "").strip()
    if configured:
        return Path(configured).resolve()
    main = _main_database_path(main_db_path, env)
    return main.resolve().parent / DEFAULT_NAME


def _main_database_path(
    main_db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    env = environ if environ is not None else os.environ
    return Path(main_db_path or env.get("IRS_DB_PATH", DEFAULT_MAIN_DB_PATH)).expanduser().resolve()


def _source_lineage_id(source_path: Path) -> str:
    """Mirror the builder's physical-file identity algorithm.

    Keep this implementation aligned with ``build_risk_network.source_lineage_id``.
    Importing the builder from a runtime query module would pull maintenance-only
    dependencies into the Flask process, so the small algorithm is duplicated here.
    """

    resolved = source_path.expanduser().resolve()
    stat = resolved.stat()
    device = int(getattr(stat, "st_dev", 0))
    file_index = int(getattr(stat, "st_ino", 0))
    if file_index:
        identity_kind = "device_file_index"
        file_identity = [device, file_index]
    else:
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
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()


def _validate_legacy_source_freshness(
    meta: Mapping[str, str], source_path: Path
) -> None:
    """Validate a wholly legacy sidecar against its original physical file."""

    required = {
        "source_lineage_id",
        "source_file_size",
        "source_file_mtime_ns",
    }
    if not required.issubset(meta) or any(meta.get(key) in (None, "") for key in required):
        raise RuntimeError(
            "Risk-network sidecar lacks source freshness metadata; run a full rebuild."
        )
    try:
        stat = source_path.stat()
        current = {
            "source_lineage_id": _source_lineage_id(source_path),
            "source_file_size": str(stat.st_size),
            "source_file_mtime_ns": str(stat.st_mtime_ns),
        }
    except OSError:
        raise RuntimeError(
            "Risk-network sidecar source database is unavailable; run a full rebuild."
        ) from None
    if any(str(meta.get(key)) != value for key, value in current.items()):
        raise RuntimeError(
            "Risk-network sidecar is stale for the current source database; run a full rebuild."
        )


def _validate_database_auxiliaries_clear(database_path: Path, label: str) -> None:
    populated = []
    for auxiliary_label, suffix in (("WAL", "-wal"), ("rollback journal", "-journal")):
        path = Path(str(database_path.expanduser().resolve()) + suffix)
        try:
            size = int(path.stat().st_size)
        except FileNotFoundError:
            size = 0
        if size > 0:
            populated.append(f"{auxiliary_label}={size:,} bytes")
    if populated:
        raise RuntimeError(
            f"Risk-network {label} has populated SQLite auxiliary files "
            f"({', '.join(populated)}); checkpoint or recover it before use."
        )


def _validate_source_auxiliaries_clear(source_path: Path) -> None:
    _validate_database_auxiliaries_clear(source_path, "source database")


def _validate_sidecar_auxiliaries_clear(sidecar_path: Path) -> None:
    _validate_database_auxiliaries_clear(sidecar_path, "sidecar database")


def _validate_source_freshness(meta: Mapping[str, str], source_path: Path) -> None:
    """Validate portable metadata first, with a complete legacy fallback."""

    _validate_source_auxiliaries_clear(source_path)
    try:
        portable = parse_portable_metadata(meta)
    except RiskSourceIdentityError as exc:
        raise RuntimeError(
            f"Risk-network sidecar has invalid portable source metadata: {exc}"
        ) from exc
    if portable is not None:
        try:
            validate_portable_source(meta, source_path)
        except (RiskSourceIdentityError, OSError, sqlite3.Error) as exc:
            raise RuntimeError(
                f"Risk-network sidecar is stale for the current portable source database: {exc}"
            ) from exc
    else:
        _validate_legacy_source_freshness(meta, source_path)
    # Close the race where a writer creates a WAL while identity/stat metadata
    # is being read. Any populated auxiliary invalidates the portable copy.
    _validate_source_auxiliaries_clear(source_path)


def connect_readonly(path: Path) -> sqlite3.Connection:
    _validate_sidecar_auxiliaries_clear(path)
    uri = "file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def available(main_db_path: Optional[str] = None,
              environ: Optional[Mapping[str, str]] = None) -> bool:
    path = risk_network_path(main_db_path, environ)
    source_path = _main_database_path(main_db_path, environ)
    if not path.is_file():
        return False
    try:
        with closing(connect_readonly(path)) as conn:
            meta = {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key,value FROM risk_network_build_meta"
                )
            }
            if (
                meta.get("build_status") != "complete"
                or meta.get("schema_version") != EXPECTED_SCHEMA_VERSION
            ):
                return False
            _validate_source_freshness(meta, source_path)
        _validate_source_freshness(meta, source_path)
        _validate_sidecar_auxiliaries_clear(path)
        return True
    except (sqlite3.Error, RuntimeError, OSError):
        return False


def edges_for_ein(path: Path, ein: str, *, min_tax_year: Optional[int] = None,
                  max_tax_year: Optional[int] = None, limit: int = 2_000) -> List[Dict[str, Any]]:
    normalized = _normalize_ein(ein)
    if not normalized:
        return []
    clauses = ["source_ein=?"]
    params: List[Any] = [normalized]
    if min_tax_year is not None:
        clauses.append("tax_year>=?")
        params.append(int(min_tax_year))
    if max_tax_year is not None:
        clauses.append("tax_year<=?")
        params.append(int(max_tax_year))
    bounded_limit = min(10_000, max(1, int(limit)))
    params.append(bounded_limit)
    with closing(connect_readonly(path)) as conn:
        rows = conn.execute(
            "SELECT * FROM risk_network_edge WHERE " + " AND ".join(clauses)
            + " ORDER BY tax_year DESC, edge_type, amount DESC LIMIT ?", params
        ).fetchall()
    _validate_sidecar_auxiliaries_clear(path)
    return [dict(row) for row in rows]


def _bounded(value: int, *, default: int, maximum: int) -> int:
    try:
        return min(maximum, max(1, int(value)))
    except (TypeError, ValueError):
        return default


def _edge_dict(row: sqlite3.Row) -> Dict[str, Any]:
    value = dict(row)
    raw_attributes = value.pop("attributes_json", "")
    try:
        value["attributes"] = json.loads(raw_attributes) if raw_attributes else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        value["attributes"] = {}
    return value


def build_metadata(
    path: Path,
    *,
    main_db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Return sidecar build and source-coverage metadata without opening it RW."""
    source_path = _main_database_path(main_db_path, environ)
    with closing(connect_readonly(path)) as conn:
        meta = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key,value FROM risk_network_build_meta ORDER BY key")
        }
        if meta.get("schema_version") != EXPECTED_SCHEMA_VERSION or meta.get("build_status") != "complete":
            raise RuntimeError("Risk-network sidecar is incomplete or schema-incompatible.")
        _validate_source_freshness(
            meta, source_path
        )
        sources = [
            dict(row) for row in conn.execute(
                """SELECT source_name,object_name,available,rows_written,note,built_at
                   FROM risk_network_source_status ORDER BY source_name"""
            )
        ]
    _validate_source_freshness(meta, source_path)
    _validate_sidecar_auxiliaries_clear(path)
    return {"meta": meta, "sources": sources}


def network_for_ein(
    path: Path,
    ein: str,
    *,
    main_db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    min_tax_year: Optional[int] = None,
    max_tax_year: Optional[int] = None,
    outgoing_limit: int = 1_000,
    incoming_limit: int = 1_000,
    shared_target_limit: int = 150,
    shared_edge_limit: int = 1_000,
    include_unscored: bool = False,
    suppress_hubs: bool = True,
) -> Dict[str, Any]:
    """Return a bounded dashboard-ready one-hop network for an EIN.

    Direct incoming relationships use the indexed ``target_ein`` column.
    Shared neighbors are limited to exact normalized people, filed addresses,
    and contractors; hub-suppressed targets are excluded by default. Original
    edge rows retain filing year, amounts, confidence, and provenance.
    """
    normalized = _normalize_ein(ein)
    empty = {
        "ein": normalized, "build": {}, "sources": [], "outgoing": [],
        "incoming": [], "shared_neighbors": [], "coverage": {"covered": False},
    }
    if not normalized:
        return empty

    outgoing_cap = _bounded(outgoing_limit, default=1_000, maximum=10_000)
    incoming_cap = _bounded(incoming_limit, default=1_000, maximum=10_000)
    shared_target_cap = _bounded(shared_target_limit, default=150, maximum=500)
    shared_edge_cap = _bounded(shared_edge_limit, default=1_000, maximum=10_000)

    year_clauses: List[str] = []
    year_params: List[Any] = []
    if min_tax_year is not None:
        year_clauses.append("tax_year>=?")
        year_params.append(int(min_tax_year))
    if max_tax_year is not None:
        year_clauses.append("tax_year<=?")
        year_params.append(int(max_tax_year))
    year_sql = (" AND " + " AND ".join(year_clauses)) if year_clauses else ""
    scored_sql = "" if include_unscored else " AND is_scored=1"
    source_path = _main_database_path(main_db_path, environ)

    with closing(connect_readonly(path)) as conn:
        meta = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key,value FROM risk_network_build_meta ORDER BY key")
        }
        if meta.get("schema_version") != EXPECTED_SCHEMA_VERSION or meta.get("build_status") != "complete":
            raise RuntimeError("Risk-network sidecar is incomplete or schema-incompatible.")
        _validate_source_freshness(
            meta, source_path
        )
        sources = [
            dict(row) for row in conn.execute(
                """SELECT source_name,object_name,available,rows_written,note,built_at
                   FROM risk_network_source_status ORDER BY source_name"""
            )
        ]
        coverage_rows = conn.execute(
            """SELECT tax_year,COUNT(*) AS filing_count
               FROM risk_network_filing_state
               WHERE source_ein=?
               GROUP BY tax_year ORDER BY tax_year""",
            (normalized,),
        ).fetchall()
        covered_years = [int(row["tax_year"]) for row in coverage_rows if row["tax_year"] is not None]
        requested_covered_years = [
            year for year in covered_years
            if (min_tax_year is None or year >= int(min_tax_year))
            and (max_tax_year is None or year <= int(max_tax_year))
        ]
        coverage = {
            "covered": bool(coverage_rows),
            "covered_tax_years": covered_years,
            "covered_filing_count": sum(int(row["filing_count"] or 0) for row in coverage_rows),
            "requested_window_has_coverage": bool(requested_covered_years),
            "requested_covered_tax_years": requested_covered_years,
            "min_requested_tax_year": min_tax_year,
            "max_requested_tax_year": max_tax_year,
            "build_scope": meta.get("build_scope", "unknown"),
        }

        outgoing_params: List[Any] = [normalized] + year_params + [outgoing_cap]
        outgoing = conn.execute(
            "SELECT * FROM risk_network_edge WHERE source_ein=?" + year_sql + scored_sql
            + " ORDER BY tax_year DESC,edge_type,amount DESC LIMIT ?",
            outgoing_params,
        ).fetchall()

        incoming_params: List[Any] = [normalized] + year_params + [incoming_cap]
        incoming = conn.execute(
            "SELECT * FROM risk_network_edge WHERE target_ein=?" + year_sql + scored_sql
            + " ORDER BY tax_year DESC,edge_type,amount DESC LIMIT ?",
            incoming_params,
        ).fetchall()

        focal_filters = [
            "source_ein=?", "target_type IN ('person','address','contractor')", "is_scored=1",
        ]
        focal_params: List[Any] = [normalized]
        if suppress_hubs:
            focal_filters.append("hub_suppressed=0")
        if min_tax_year is not None:
            focal_filters.append("tax_year>=?")
            focal_params.append(int(min_tax_year))
        if max_tax_year is not None:
            focal_filters.append("tax_year<=?")
            focal_params.append(int(max_tax_year))
        focal_params.extend([shared_target_cap, normalized])

        neighbor_filters = ["e.source_ein<>?", "e.is_scored=1"]
        if suppress_hubs:
            neighbor_filters.append("e.hub_suppressed=0")
        if min_tax_year is not None:
            neighbor_filters.append("e.tax_year>=?")
            focal_params.append(int(min_tax_year))
        if max_tax_year is not None:
            neighbor_filters.append("e.tax_year<=?")
            focal_params.append(int(max_tax_year))
        focal_params.append(shared_edge_cap)
        shared = conn.execute(
            f"""
            WITH focal_ranked AS (
              SELECT target_key,target_type,target_name AS shared_target_name,
                     filing_id AS focal_filing_id,tax_year AS focal_tax_year,
                     edge_type AS focal_edge_type,
                     provenance_table AS focal_provenance_table,
                     provenance_row_id AS focal_provenance_row_id,
                     confidence AS focal_confidence,hub_degree,
                     ROW_NUMBER() OVER (
                       PARTITION BY target_key
                       ORDER BY tax_year DESC,confidence DESC,edge_id
                     ) AS focal_rank
              FROM risk_network_edge
              WHERE {' AND '.join(focal_filters)}
            ),
            focal AS (
              SELECT * FROM focal_ranked
              WHERE focal_rank=1
              ORDER BY focal_tax_year DESC,target_key
              LIMIT ?
            )
            SELECT e.*,
                   focal.shared_target_name,
                   focal.focal_filing_id,
                   focal.focal_tax_year,
                   focal.focal_edge_type,
                   focal.focal_provenance_table,
                   focal.focal_provenance_row_id,
                   focal.focal_confidence,
                   focal.hub_degree AS shared_target_degree
            FROM focal
            JOIN risk_network_edge AS e ON e.target_key=focal.target_key
            WHERE {' AND '.join(neighbor_filters)}
            ORDER BY e.tax_year DESC,e.source_ein,e.edge_type,e.amount DESC
            LIMIT ?
            """,
            focal_params,
        ).fetchall()

    _validate_source_freshness(meta, source_path)
    _validate_sidecar_auxiliaries_clear(path)

    return {
        "ein": normalized,
        "build": meta,
        "sources": sources,
        "outgoing": [_edge_dict(row) for row in outgoing],
        "incoming": [_edge_dict(row) for row in incoming],
        "shared_neighbors": [_edge_dict(row) for row in shared],
        "coverage": coverage,
        "limits": {
            "outgoing": outgoing_cap,
            "incoming": incoming_cap,
            "shared_targets": shared_target_cap,
            "shared_edges": shared_edge_cap,
        },
    }
