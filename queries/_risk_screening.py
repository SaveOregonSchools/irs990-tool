"""Bounded, read-only lookups against the public-screening sidecar.

This module deliberately performs candidate retrieval, not identity resolution.
An exact normalized name (including an official alias) is still only a lead.
HHS-OIG explicitly requires online verification with an EIN or SSN before a
name-based LEIE result is treated as confirmed.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from build_screening_sidecar import normalize_ein, normalize_name


_MAX_IRS_RESULTS = 50
_MAX_NAME_RESULTS = 25
_MAX_ADDRESSES_PER_CANDIDATE = 10
_NAME_DATASETS = ("ofac_sdn", "ofac_consolidated", "hhs_leie")
_IRS_DATASETS = ("irs_pub78", "irs_auto_revocation")


def default_screening_db_path() -> Path:
    configured = os.getenv("IRS_SCREENING_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    main_db = os.getenv("IRS_DB_PATH")
    if main_db:
        return Path(main_db).expanduser().resolve().parent / "screening_data.db"
    return Path(__file__).resolve().parents[1] / "db" / "screening_data.db"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    uri = f"file:{quote(resolved.as_posix(), safe='/:')}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _limit(value: object, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = maximum
    return max(1, min(parsed, maximum))


def _base_response(path: Path, query: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": False,
        "candidate_only": True,
        "query": query,
        "results": [],
        "coverage": [],
        "sidecar_path": str(path),
        "error": "",
    }


def _coverage(conn: sqlite3.Connection, datasets: Iterable[str]) -> list[dict[str, Any]]:
    selected = tuple(datasets)
    placeholders = ",".join("?" for _ in selected)
    rows = conn.execute(
        f"""
        SELECT dataset_key, publisher, title, source_page_url, source_url,
               source_date, retrieved_at, content_sha256, record_count,
               complete_snapshot, access_note
        FROM screening_dataset
        WHERE dataset_key IN ({placeholders})
        ORDER BY dataset_key
        """,
        selected,
    ).fetchall()
    return [dict(row) for row in rows]


def _aliases(
    conn: sqlite3.Connection, dataset_key: str, source_record_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT alias_name, normalized_name, alias_type, alias_quality, remarks
        FROM screening_alias
        WHERE dataset_key=? AND source_record_id=?
        ORDER BY alias_type, alias_name
        LIMIT 50
        """,
        (dataset_key, source_record_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _addresses(
    conn: sqlite3.Connection, dataset_key: str, source_record_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT address_line, city, region, postal_code, country,
               normalized_address, remarks
        FROM screening_address
        WHERE dataset_key=? AND source_record_id=?
        ORDER BY source_address_id
        LIMIT ?
        """,
        (dataset_key, source_record_id, _MAX_ADDRESSES_PER_CANDIDATE),
    ).fetchall()
    return [dict(row) for row in rows]


def lookup_irs_status(
    ein: object,
    *,
    db_path: Optional[Path] = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Return exact-EIN Pub. 78 and automatic-revocation records."""

    path = (db_path or default_screening_db_path()).resolve()
    normalized_ein = normalize_ein(ein)
    response = _base_response(
        path,
        {
            "ein": normalized_ein or "",
            "match_rule": "exact_9_digit_ein",
        },
    )
    if not normalized_ein:
        response["error"] = "EIN must contain exactly nine digits."
        return response
    if not path.is_file():
        response["error"] = "Public-screening sidecar is not installed."
        return response

    try:
        conn = _connect_readonly(path)
        try:
            response["coverage"] = _coverage(conn, _IRS_DATASETS)
            rows = conn.execute(
                """
                SELECT
                  e.*,
                  d.publisher,
                  d.title AS dataset_title,
                  d.source_page_url,
                  d.source_url,
                  d.source_date,
                  d.retrieved_at,
                  d.content_sha256
                FROM screening_entity e
                JOIN screening_dataset d USING (dataset_key)
                WHERE e.dataset_key IN ('irs_pub78','irs_auto_revocation')
                  AND e.ein=?
                ORDER BY
                  CASE e.dataset_key WHEN 'irs_pub78' THEN 0 ELSE 1 END,
                  e.status_date DESC,
                  e.source_record_id
                LIMIT ?
                """,
                (normalized_ein, _limit(limit, _MAX_IRS_RESULTS)),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["match_evidence"] = {
                    "kind": "exact_ein",
                    "query_ein": normalized_ein,
                    "matched_ein": row["ein"],
                }
                item["aliases"] = _aliases(
                    conn, row["dataset_key"], row["source_record_id"]
                )
                addresses = _addresses(
                    conn, row["dataset_key"], row["source_record_id"]
                )
                if not addresses and any(
                    row[field]
                    for field in (
                        "address_line",
                        "city",
                        "region",
                        "postal_code",
                        "country",
                    )
                ):
                    addresses = [
                        {
                            "address_line": row["address_line"],
                            "city": row["city"],
                            "region": row["region"],
                            "postal_code": row["postal_code"],
                            "country": row["country"],
                            "normalized_address": "",
                            "remarks": "Address fields stored on the exact-EIN source record.",
                        }
                    ]
                item["addresses"] = addresses
                item["candidate_only"] = False
                results.append(item)
            response["results"] = results
            response["available"] = bool(response["coverage"])
            response["candidate_only"] = False
            return response
        finally:
            conn.close()
    except sqlite3.Error:
        response["error"] = "Public-screening sidecar could not be read."
        return response


def _normalize_country(value: object) -> str:
    normalized = normalize_name(value)
    if normalized in {
        "US",
        "USA",
        "UNITED STATES",
        "UNITED STATES OF AMERICA",
    }:
        return "US"
    return normalized


def _location_evidence(
    addresses: list[dict[str, Any]],
    *,
    city: str,
    region: str,
    country: str,
) -> dict[str, Any]:
    requested = {
        "city": normalize_name(city),
        "region": normalize_name(region),
        "country": _normalize_country(country),
    }
    requested = {key: value for key, value in requested.items() if value}
    if not requested:
        return {
            "kind": "not_requested",
            "requested": {},
            "matching_address_count": 0,
        }

    comparisons = []
    for address in addresses:
        actual = {
            "city": normalize_name(address.get("city")),
            "region": normalize_name(address.get("region")),
            "country": _normalize_country(address.get("country")),
        }
        field_matches = {
            key: bool(actual.get(key)) and actual.get(key) == expected
            for key, expected in requested.items()
        }
        comparisons.append(
            {
                "address": address,
                "field_matches": field_matches,
                "all_requested_fields_match": bool(field_matches)
                and all(field_matches.values()),
            }
        )

    exact_count = sum(
        int(item["all_requested_fields_match"]) for item in comparisons
    )
    any_field = any(
        any(item["field_matches"].values()) for item in comparisons
    )
    any_known = any(
        any(
            normalize_name(item["address"].get(field))
            for field in requested
        )
        for item in comparisons
    )
    if exact_count:
        kind = "exact"
    elif any_field:
        kind = "partial"
    elif any_known:
        kind = "conflict"
    else:
        kind = "unknown"
    return {
        "kind": kind,
        "requested": requested,
        "matching_address_count": exact_count,
        "comparisons": comparisons,
    }


def lookup_name_candidates(
    name: object,
    *,
    city: object = "",
    region: object = "",
    country: object = "",
    entity_type: object = "",
    db_path: Optional[Path] = None,
    limit: int = 15,
) -> dict[str, Any]:
    """Return conservative OFAC/HHS name candidates with location evidence.

    Only exact matches on the deterministic normalized primary or alias name
    are returned.  There is no substring, edit-distance, phonetic, or token
    match in this runtime boundary.
    """

    path = (db_path or default_screening_db_path()).resolve()
    normalized = normalize_name(name)
    requested_type = re.sub(r"[^a-z]", "", str(entity_type or "").lower())
    if requested_type not in {"", "organization", "individual", "entity"}:
        requested_type = ""
    response = _base_response(
        path,
        {
            "name": str(name or "").strip(),
            "normalized_name": normalized,
            "city": str(city or "").strip(),
            "region": str(region or "").strip(),
            "country": str(country or "").strip(),
            "entity_type": requested_type,
            "match_rule": "exact_conservative_normalized_primary_or_alias",
        },
    )
    response["verification_notice"] = (
        "Every result is a candidate only. Verify identifiers and source "
        "records before drawing conclusions. HHS-OIG LEIE name matches must "
        "be verified through OIG's online search with an EIN or SSN."
    )
    if not normalized:
        response["error"] = "A nonblank name is required."
        return response
    if not path.is_file():
        response["error"] = "Public-screening sidecar is not installed."
        return response

    result_limit = _limit(limit, _MAX_NAME_RESULTS)
    try:
        conn = _connect_readonly(path)
        try:
            response["coverage"] = _coverage(conn, _NAME_DATASETS)
            type_sql = ""
            parameters: list[Any] = [normalized]
            if requested_type:
                if requested_type == "organization":
                    type_sql = " AND e.entity_type IN ('organization','entity')"
                elif requested_type == "entity":
                    type_sql = " AND e.entity_type IN ('organization','entity')"
                else:
                    type_sql = " AND e.entity_type='individual'"
            parameters.append(min(result_limit * 4, _MAX_NAME_RESULTS * 4))
            rows = conn.execute(
                f"""
                SELECT
                  n.name_role,
                  n.display_name AS matched_name,
                  n.normalized_name AS matched_normalized_name,
                  n.alias_type AS matched_alias_type,
                  e.*,
                  d.publisher,
                  d.title AS dataset_title,
                  d.source_page_url,
                  d.source_url,
                  d.source_date,
                  d.retrieved_at,
                  d.content_sha256
                FROM screening_names_v1 n
                JOIN screening_entity e
                  USING (dataset_key, source_record_id)
                JOIN screening_dataset d USING (dataset_key)
                WHERE n.dataset_key IN ('ofac_sdn','ofac_consolidated','hhs_leie')
                  AND n.normalized_name=?
                  {type_sql}
                ORDER BY
                  CASE n.name_role WHEN 'primary' THEN 0 ELSE 1 END,
                  n.dataset_key,
                  n.source_record_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()

            results_by_entity: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                key = (row["dataset_key"], row["source_record_id"])
                if key in results_by_entity:
                    results_by_entity[key]["additional_name_matches"].append(
                        {
                            "name_role": row["name_role"],
                            "matched_name": row["matched_name"],
                            "alias_type": row["matched_alias_type"],
                        }
                    )
                    continue
                item = dict(row)
                addresses = _addresses(conn, key[0], key[1])
                item["addresses"] = addresses
                item["aliases"] = _aliases(conn, key[0], key[1])
                item["match_evidence"] = {
                    "kind": (
                        "exact_normalized_primary_name"
                        if row["name_role"] == "primary"
                        else "exact_normalized_source_alias"
                    ),
                    "query_normalized_name": normalized,
                    "matched_normalized_name": row["matched_normalized_name"],
                    "matched_name": row["matched_name"],
                    "name_role": row["name_role"],
                    "alias_type": row["matched_alias_type"],
                }
                item["location_evidence"] = _location_evidence(
                    addresses,
                    city=str(city or ""),
                    region=str(region or ""),
                    country=str(country or ""),
                )
                item["candidate_only"] = True
                item["verification_required"] = (
                    "OIG online EIN/SSN verification"
                    if row["dataset_key"] == "hhs_leie"
                    else "manual OFAC identity verification"
                )
                item["additional_name_matches"] = []
                results_by_entity[key] = item

            location_order = {
                "exact": 0,
                "partial": 1,
                "not_requested": 2,
                "unknown": 3,
                "conflict": 4,
            }
            results = sorted(
                results_by_entity.values(),
                key=lambda item: (
                    location_order.get(item["location_evidence"]["kind"], 9),
                    0 if item["name_role"] == "primary" else 1,
                    item["dataset_key"],
                    item["source_record_id"],
                ),
            )
            response["results"] = results[:result_limit]
            response["available"] = bool(response["coverage"])
            return response
        finally:
            conn.close()
    except sqlite3.Error:
        response["error"] = "Public-screening sidecar could not be read."
        return response
