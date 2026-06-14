#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
grant_ai_assist_v1.py

AI-assisted second-pass grant recipient matching for the IRS 990 SQLite database.

This script is intended to run AFTER resolve_grant_recipients_v2.py has created
or refreshed grant_recipient_resolved. It adds a materialized organization
identity layer that can include IRS EO BMF CSV files, builds one row per unique
hard-to-match grant recipient signature, generates a compact candidate EIN set,
and optionally asks a local Ollama model to adjudicate those candidates.

Design principle
----------------
Ollama is used as an adjudicator, not as a database search engine. The database
and deterministic Python code generate the candidate list; the model may choose
only among candidates it was given, or return NO_MATCH / AMBIGUOUS / HUMAN_REVIEW.

Expected project layout
-----------------------
  project_root/
    irs990.db or DB at C:\IRSDB\db\irs990.db
    eo-bmf/
      eo1.csv
      eo2.csv
      eo3.csv
      eo4.csv

Common commands
---------------
Verify BMF files:
  python grant_ai_assist_v1.py verify-bmf --project-dir C:\IRSDB

Build org_identity from returns + EO BMF:
  python grant_ai_assist_v1.py build-identity --db C:\IRSDB\db\irs990.db --project-dir C:\IRSDB --full-refresh

Build signatures for unresolved and low-confidence deterministic matches:
  python grant_ai_assist_v1.py build-signatures --db C:\IRSDB\db\irs990.db --full-refresh

Generate top candidates for those signatures:
  python grant_ai_assist_v1.py generate-candidates --db C:\IRSDB\db\irs990.db --limit 100000

Dry-run Ollama adjudication to CSV:
  python grant_ai_assist_v1.py adjudicate --db C:\IRSDB\db\irs990.db --model gemma4:12b --limit 100 --dry-run --csv-out ai_decisions_sample.csv

Store Ollama decisions:
  python grant_ai_assist_v1.py adjudicate --db C:\IRSDB\db\irs990.db --model gemma4:12b --limit 1000

Apply only auto-accepted AI decisions into a separate applied table and final view:
  python grant_ai_assist_v1.py apply-decisions --db C:\IRSDB\db\irs990.db
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

DEFAULT_DB = os.getenv("IRS_DB_PATH", r"C:\IRSDB\db\irs990.db")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:12b")
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")

BMF_FILES = {
    "eo1.csv": "Region 1: Northeast",
    "eo2.csv": "Region 2: Mid-Atlantic and Great Lakes",
    "eo3.csv": "Region 3: Gulf Coast and Pacific Coast",
    "eo4.csv": "Region 4: International and all others",
}

BMF_COLUMNS = [
    "EIN", "NAME", "ICO", "STREET", "CITY", "STATE", "ZIP", "GROUP",
    "SUBSECTION", "AFFILIATION", "CLASSIFICATION", "RULING", "DEDUCTIBILITY",
    "FOUNDATION", "ACTIVITY", "ORGANIZATION", "STATUS", "TAX_PERIOD",
    "ASSET_CD", "INCOME_CD", "FILING_REQ_CD", "PF_FILING_REQ_CD", "ACCT_PD",
    "ASSET_AMT", "INCOME_AMT", "REVENUE_AMT", "NTEE_CD", "SORT_NAME",
]

ORG_IDENTITY_TABLE = "org_identity"
ORG_TOKEN_TABLE = "org_identity_token"
SIG_TABLE = "grant_recipient_signature"
SIG_GRANT_TABLE = "grant_recipient_signature_grant"
CAND_TABLE = "grant_recipient_ai_candidate"
DECISION_TABLE = "grant_recipient_ai_decision"
APPLIED_TABLE = "grant_recipient_ai_applied"
FINAL_VIEW = "grant_recipient_resolved_plus_ai_v1"
RESOLVED_TABLE = "grant_recipient_resolved"

LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
    "LTD", "LIMITED", "LLC", "L.L.C", "PLC", "PLLC", "PC", "P.C",
}
LEADING_NOISE = {"THE"}
NAME_STOPWORDS = {
    "THE", "A", "AN", "OF", "AND", "FOR", "TO", "IN", "AT", "ON", "BY",
    "WITH", "FROM", "FOUNDATION", "FUND", "INC", "CORP", "LLC", "CO", "LTD",
}
USPS_STREET = {
    "STREET": "ST", "ST.": "ST", "AVENUE": "AVE", "AVE.": "AVE",
    "ROAD": "RD", "RD.": "RD", "BOULEVARD": "BLVD", "BLVD.": "BLVD",
    "DRIVE": "DR", "DR.": "DR", "LANE": "LN", "LN.": "LN",
    "COURT": "CT", "CT.": "CT", "PLACE": "PL", "PL.": "PL",
    "PARKWAY": "PKWY", "PKWY.": "PKWY", "HIGHWAY": "HWY", "HWY.": "HWY",
    "SUITE": "STE", "STE.": "STE", "FLOOR": "FL", "FL.": "FL",
    "APARTMENT": "APT", "APT.": "APT", "BUILDING": "BLDG", "BLDG.": "BLDG",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
ABBREVIATIONS = {
    "UNIV": "UNIVERSITY", "UNIVERS": "UNIVERSITY", "SCH": "SCHOOL", "SCHL": "SCHOOL",
    "CTR": "CENTER", "CNTR": "CENTER", "ASSN": "ASSOCIATION", "ASSOC": "ASSOCIATION",
    "FDN": "FOUNDATION", "FDTN": "FOUNDATION", "FDNTN": "FOUNDATION",
    "ORG": "ORGANIZATION", "INST": "INSTITUTE", "DEPT": "DEPARTMENT",
    "ST": "SAINT", "MT": "MOUNT", "INTL": "INTERNATIONAL", "NATL": "NATIONAL",
}


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-300000;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA mmap_size=2147483648;")
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def digits9(value: Optional[str]) -> str:
    d = re.sub(r"\D", "", value or "")
    return d if len(d) == 9 else ""


def zip5(value: Optional[str]) -> str:
    d = re.sub(r"\D", "", value or "")
    return d[:5] if len(d) >= 5 else ""


def clean_text(value: Optional[Any]) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def norm_upper(value: Optional[str]) -> str:
    return clean_text(value).upper()


def normalize_name(value: Optional[str]) -> str:
    s = (value or "").upper()
    s = s.replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    tokens: List[str] = []
    for token in s.split():
        token = ABBREVIATIONS.get(token, token)
        if token:
            tokens.append(token)
    while tokens and tokens[0] in LEADING_NOISE:
        tokens = tokens[1:]
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens = tokens[:-1]
    return " ".join(tokens)


def normalize_address(value: Optional[str]) -> str:
    s = (value or "").upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    tokens = [USPS_STREET.get(t, t) for t in s.split() if t]
    return " ".join(tokens)


def name_tokens(name_norm: str) -> List[str]:
    out = []
    seen = set()
    for t in (name_norm or "").split():
        if len(t) < 3 or t in NAME_STOPWORDS:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return round(SequenceMatcher(None, a, b).ratio(), 4)


def stable_hash(parts: Sequence[Any], prefix: str = "") -> str:
    raw = "\u241f".join(clean_text(p) for p in parts)
    h = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
    return f"{prefix}{h}" if prefix else h


def to_number(value: Optional[Any]) -> Optional[float]:
    s = clean_text(value).replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def project_bmf_dir(project_dir: Optional[str], bmf_dir: Optional[str]) -> Path:
    if bmf_dir:
        return Path(bmf_dir)
    if project_dir:
        return Path(project_dir) / "eo-bmf"
    # Default: eo-bmf directory next to this script, or current working directory.
    script_dir = Path(__file__).resolve().parent
    p = script_dir / "eo-bmf"
    return p if p.exists() else Path.cwd() / "eo-bmf"


# ---------------------------------------------------------------------------
# BMF verification / import
# ---------------------------------------------------------------------------


def verify_bmf_files(bmf_dir: Path, require: bool = True) -> List[Path]:
    paths: List[Path] = []
    missing: List[str] = []
    print(f"Checking EO BMF directory: {bmf_dir}", flush=True)
    for fname, desc in BMF_FILES.items():
        path = bmf_dir / fname
        if path.exists() and path.is_file():
            size_mb = path.stat().st_size / (1024 * 1024)
            print(f"  OK  {fname:7s} {size_mb:10.1f} MB  {desc}", flush=True)
            paths.append(path)
        else:
            print(f"  MISSING {fname:7s}          {desc}", flush=True)
            missing.append(fname)
    if missing and require:
        raise FileNotFoundError(
            f"Missing EO BMF CSV file(s) in {bmf_dir}: {', '.join(missing)}"
        )
    return paths


def canonical_bmf_key(key: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (key or "").strip().upper()).strip("_")


def iter_bmf_dicts(path: Path) -> Iterator[Dict[str, str]]:
    """Yield EO BMF rows as dicts, supporting both headered and headerless CSV."""
    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        reader = csv.reader(fh)
        try:
            first = next(reader)
        except StopIteration:
            return
        first_clean = [canonical_bmf_key(x) for x in first]
        has_header = "EIN" in first_clean and ("NAME" in first_clean or "STREET" in first_clean)
        if has_header:
            header = first_clean
        else:
            header = BMF_COLUMNS[:len(first)]
            # Emit the first row as data.
            yield {header[i]: first[i] if i < len(first) else "" for i in range(len(header))}
        for row in reader:
            if not row:
                continue
            # Pad short rows so missing trailing fields become blanks.
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            yield {header[i]: row[i] if i < len(row) else "" for i in range(len(header))}


def identity_key(ein: str, source: str, name_norm: str, street_norm: str, city: str, state: str, z5: str, filing_id: str = "", tax_year: Optional[int] = None) -> str:
    return stable_hash([ein, source, name_norm, street_norm, city, state, z5, filing_id, tax_year or ""], "ID_")


def create_identity_schema(conn: sqlite3.Connection, full_refresh: bool = False, create_fts: bool = True) -> None:
    if full_refresh:
        conn.executescript(f"""
        DROP VIEW IF EXISTS {FINAL_VIEW};
        DROP TABLE IF EXISTS org_identity_fts;
        DROP TABLE IF EXISTS {ORG_TOKEN_TABLE};
        DROP TABLE IF EXISTS {ORG_IDENTITY_TABLE};
        """)
        conn.commit()

    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS {ORG_IDENTITY_TABLE} (
      identity_id INTEGER PRIMARY KEY AUTOINCREMENT,
      identity_key TEXT NOT NULL UNIQUE,
      ein TEXT NOT NULL,
      source TEXT NOT NULL,
      source_detail TEXT,
      source_rank INTEGER NOT NULL,
      legal_name TEXT,
      alias_name TEXT,
      display_name TEXT,
      name_norm TEXT NOT NULL,
      street TEXT,
      street_norm TEXT,
      city TEXT,
      state TEXT,
      zip5 TEXT,
      filing_id TEXT,
      tax_year INTEGER,
      bmf_region INTEGER,
      subsection TEXT,
      foundation TEXT,
      deductibility TEXT,
      ntee_cd TEXT,
      status TEXT,
      tax_period TEXT,
      asset_amt NUMERIC,
      income_amt NUMERIC,
      revenue_amt NUMERIC,
      extra_json TEXT,
      created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS {ORG_TOKEN_TABLE} (
      identity_id INTEGER NOT NULL,
      token TEXT NOT NULL,
      state TEXT,
      zip5 TEXT,
      PRIMARY KEY(identity_id, token)
    );
    """)
    conn.commit()

    if create_fts:
        try:
            conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS org_identity_fts USING fts5(
              display_name,
              name_norm,
              content='{ORG_IDENTITY_TABLE}',
              content_rowid='identity_id'
            );
            """)
            conn.commit()
        except sqlite3.Error as e:
            print(f"FTS5 not available or failed to create: {e}. Continuing without FTS.", flush=True)


def create_identity_indexes(conn: sqlite3.Connection, include_fts_rebuild: bool = True) -> None:
    statements = [
        f"CREATE INDEX IF NOT EXISTS idx_org_identity_ein ON {ORG_IDENTITY_TABLE}(ein);",
        f"CREATE INDEX IF NOT EXISTS idx_org_identity_name ON {ORG_IDENTITY_TABLE}(name_norm);",
        f"CREATE INDEX IF NOT EXISTS idx_org_identity_name_zip ON {ORG_IDENTITY_TABLE}(name_norm, zip5);",
        f"CREATE INDEX IF NOT EXISTS idx_org_identity_name_state ON {ORG_IDENTITY_TABLE}(name_norm, state);",
        f"CREATE INDEX IF NOT EXISTS idx_org_identity_name_city_state ON {ORG_IDENTITY_TABLE}(name_norm, city, state);",
        f"CREATE INDEX IF NOT EXISTS idx_org_identity_addr_zip ON {ORG_IDENTITY_TABLE}(street_norm, zip5);",
        f"CREATE INDEX IF NOT EXISTS idx_org_identity_addr_city_state ON {ORG_IDENTITY_TABLE}(street_norm, city, state);",
        f"CREATE INDEX IF NOT EXISTS idx_org_identity_state_zip ON {ORG_IDENTITY_TABLE}(state, zip5);",
        f"CREATE INDEX IF NOT EXISTS idx_org_identity_source ON {ORG_IDENTITY_TABLE}(source);",
        f"CREATE INDEX IF NOT EXISTS idx_org_token_token ON {ORG_TOKEN_TABLE}(token);",
        f"CREATE INDEX IF NOT EXISTS idx_org_token_token_state ON {ORG_TOKEN_TABLE}(token, state);",
        f"CREATE INDEX IF NOT EXISTS idx_org_token_token_zip ON {ORG_TOKEN_TABLE}(token, zip5);",
    ]
    for stmt in statements:
        conn.execute(stmt)
    conn.commit()
    if include_fts_rebuild and table_exists(conn, "org_identity_fts"):
        try:
            conn.execute("INSERT INTO org_identity_fts(org_identity_fts) VALUES('rebuild')")
            conn.commit()
        except sqlite3.Error as e:
            print(f"FTS rebuild skipped: {e}", flush=True)


def insert_identity_batch(conn: sqlite3.Connection, rows: Sequence[Dict[str, Any]], build_tokens: bool = True) -> Tuple[int, int]:
    if not rows:
        return 0, 0
    cols = [
        "identity_key", "ein", "source", "source_detail", "source_rank",
        "legal_name", "alias_name", "display_name", "name_norm",
        "street", "street_norm", "city", "state", "zip5", "filing_id", "tax_year",
        "bmf_region", "subsection", "foundation", "deductibility", "ntee_cd", "status", "tax_period",
        "asset_amt", "income_amt", "revenue_amt", "extra_json", "created_at",
    ]
    before = conn.total_changes
    placeholders = ",".join("?" for _ in cols)
    conn.executemany(
        f"INSERT OR IGNORE INTO {ORG_IDENTITY_TABLE} ({','.join(cols)}) VALUES ({placeholders})",
        [tuple(r.get(c) for c in cols) for r in rows],
    )
    conn.commit()
    inserted = conn.total_changes - before

    token_rows: List[Tuple[int, str, str, str]] = []
    if build_tokens and inserted:
        # Fetch identity IDs for this batch by identity_key. This remains cheap because identity_key is UNIQUE.
        keys = [r["identity_key"] for r in rows]
        for i in range(0, len(keys), 500):
            chunk = keys[i:i+500]
            ph = ",".join("?" for _ in chunk)
            id_rows = conn.execute(
                f"SELECT identity_id, identity_key, name_norm, state, zip5 FROM {ORG_IDENTITY_TABLE} WHERE identity_key IN ({ph})",
                chunk,
            ).fetchall()
            for ir in id_rows:
                for tok in name_tokens(ir["name_norm"]):
                    token_rows.append((int(ir["identity_id"]), tok, clean_text(ir["state"]), clean_text(ir["zip5"])))
        if token_rows:
            conn.executemany(
                f"INSERT OR IGNORE INTO {ORG_TOKEN_TABLE} (identity_id, token, state, zip5) VALUES (?,?,?,?)",
                token_rows,
            )
            conn.commit()
    return inserted, len(token_rows)


def make_identity_row(
    *,
    ein: str,
    source: str,
    source_detail: str,
    source_rank: int,
    legal_name: str,
    alias_name: str = "",
    street: str = "",
    city: str = "",
    state: str = "",
    zip_value: str = "",
    filing_id: str = "",
    tax_year: Optional[int] = None,
    bmf_region: Optional[int] = None,
    subsection: str = "",
    foundation: str = "",
    deductibility: str = "",
    ntee_cd: str = "",
    status: str = "",
    tax_period: str = "",
    asset_amt: Optional[float] = None,
    income_amt: Optional[float] = None,
    revenue_amt: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    ein = digits9(ein)
    legal_name = clean_text(legal_name)
    alias_name = clean_text(alias_name)
    display_name = alias_name or legal_name
    name_norm = normalize_name(display_name)
    street = clean_text(street)
    street_norm = normalize_address(street)
    city = norm_upper(city)
    state = norm_upper(state)
    z5 = zip5(zip_value)
    if not ein or not display_name or not name_norm:
        return None
    ikey = identity_key(ein, source, name_norm, street_norm, city, state, z5, filing_id, tax_year)
    return {
        "identity_key": ikey,
        "ein": ein,
        "source": source,
        "source_detail": source_detail,
        "source_rank": source_rank,
        "legal_name": legal_name,
        "alias_name": alias_name,
        "display_name": display_name,
        "name_norm": name_norm,
        "street": street,
        "street_norm": street_norm,
        "city": city,
        "state": state,
        "zip5": z5,
        "filing_id": clean_text(filing_id),
        "tax_year": tax_year,
        "bmf_region": bmf_region,
        "subsection": clean_text(subsection),
        "foundation": clean_text(foundation),
        "deductibility": clean_text(deductibility),
        "ntee_cd": clean_text(ntee_cd),
        "status": clean_text(status),
        "tax_period": clean_text(tax_period),
        "asset_amt": asset_amt,
        "income_amt": income_amt,
        "revenue_amt": revenue_amt,
        "extra_json": json.dumps(extra or {}, ensure_ascii=False, sort_keys=True),
        "created_at": now_stamp(),
    }


def import_returns_identity(conn: sqlite3.Connection, batch_size: int, build_tokens: bool) -> Counter:
    if not table_exists(conn, "returns"):
        raise RuntimeError("Database is missing returns table")
    sql = """
    SELECT
      r.ein, r.org_name, r.dba_name, r.in_care_of_name,
      r.us_address_line1, r.city, r.state, r.zip,
      r.filing_id, COALESCE(c.tax_year, r.tax_year) AS tax_year
    FROM returns r
    LEFT JOIN canonical_by_ein_year c ON c.filing_id = r.filing_id
    WHERE r.ein IS NOT NULL AND TRIM(r.ein) <> ''
      AND r.org_name IS NOT NULL AND TRIM(r.org_name) <> ''
    """
    counts: Counter = Counter()
    batch: List[Dict[str, Any]] = []
    started = time.time()
    for r in conn.execute(sql):
        ein = digits9(r["ein"])
        if not ein:
            continue
        legal = clean_text(r["org_name"])
        base = make_identity_row(
            ein=ein,
            source="returns_org_name",
            source_detail="returns.org_name",
            source_rank=10,
            legal_name=legal,
            street=clean_text(r["us_address_line1"]),
            city=clean_text(r["city"]),
            state=clean_text(r["state"]),
            zip_value=clean_text(r["zip"]),
            filing_id=clean_text(r["filing_id"]),
            tax_year=r["tax_year"],
        )
        if base:
            batch.append(base)
        dba = clean_text(r["dba_name"])
        if dba and normalize_name(dba) != normalize_name(legal):
            drow = make_identity_row(
                ein=ein,
                source="returns_dba_name",
                source_detail="returns.dba_name",
                source_rank=12,
                legal_name=legal,
                alias_name=dba,
                street=clean_text(r["us_address_line1"]),
                city=clean_text(r["city"]),
                state=clean_text(r["state"]),
                zip_value=clean_text(r["zip"]),
                filing_id=clean_text(r["filing_id"]),
                tax_year=r["tax_year"],
            )
            if drow:
                batch.append(drow)
        if len(batch) >= batch_size:
            inserted, tokens = insert_identity_batch(conn, batch, build_tokens)
            counts["inserted"] += inserted
            counts["tokens"] += tokens
            counts["seen"] += len(batch)
            batch.clear()
            if counts["seen"] % 250_000 < batch_size:
                elapsed = max(1.0, time.time() - started)
                print(f"returns identities seen {counts['seen']:,}; inserted {counts['inserted']:,}; {counts['seen']/elapsed:,.0f}/sec", flush=True)
    if batch:
        inserted, tokens = insert_identity_batch(conn, batch, build_tokens)
        counts["inserted"] += inserted
        counts["tokens"] += tokens
        counts["seen"] += len(batch)
    print(f"returns identity import complete: seen {counts['seen']:,}, inserted {counts['inserted']:,}", flush=True)
    return counts


def import_bmf_identity(conn: sqlite3.Connection, bmf_dir: Path, batch_size: int, build_tokens: bool, include_ico: bool = False) -> Counter:
    paths = verify_bmf_files(bmf_dir, require=True)
    counts: Counter = Counter()
    batch: List[Dict[str, Any]] = []
    started = time.time()
    for path in paths:
        region_match = re.search(r"eo([1-4])\.csv$", path.name, flags=re.I)
        region = int(region_match.group(1)) if region_match else None
        print(f"Importing {path.name}...", flush=True)
        for row in iter_bmf_dicts(path):
            counts["bmf_rows_seen"] += 1
            ein = digits9(row.get("EIN"))
            name = clean_text(row.get("NAME"))
            if not ein or not name:
                counts["bmf_rows_skipped"] += 1
                continue
            common = dict(
                ein=ein,
                legal_name=name,
                street=clean_text(row.get("STREET")),
                city=clean_text(row.get("CITY")),
                state=clean_text(row.get("STATE")),
                zip_value=clean_text(row.get("ZIP")),
                bmf_region=region,
                subsection=clean_text(row.get("SUBSECTION")),
                foundation=clean_text(row.get("FOUNDATION")),
                deductibility=clean_text(row.get("DEDUCTIBILITY")),
                ntee_cd=clean_text(row.get("NTEE_CD")),
                status=clean_text(row.get("STATUS")),
                tax_period=clean_text(row.get("TAX_PERIOD")),
                asset_amt=to_number(row.get("ASSET_AMT")),
                income_amt=to_number(row.get("INCOME_AMT")),
                revenue_amt=to_number(row.get("REVENUE_AMT")),
                extra={
                    "group": clean_text(row.get("GROUP")),
                    "affiliation": clean_text(row.get("AFFILIATION")),
                    "classification": clean_text(row.get("CLASSIFICATION")),
                    "ruling": clean_text(row.get("RULING")),
                    "activity": clean_text(row.get("ACTIVITY")),
                    "organization": clean_text(row.get("ORGANIZATION")),
                    "filing_req_cd": clean_text(row.get("FILING_REQ_CD")),
                    "pf_filing_req_cd": clean_text(row.get("PF_FILING_REQ_CD")),
                    "acct_pd": clean_text(row.get("ACCT_PD")),
                    "source_file": path.name,
                },
            )
            base = make_identity_row(
                source="bmf_name",
                source_detail=path.name,
                source_rank=20,
                **common,
            )
            if base:
                batch.append(base)
            sort_name = clean_text(row.get("SORT_NAME"))
            if sort_name and normalize_name(sort_name) != normalize_name(name):
                srow = make_identity_row(
                    source="bmf_sort_name",
                    source_detail=path.name,
                    source_rank=22,
                    alias_name=sort_name,
                    **common,
                )
                if srow:
                    batch.append(srow)
            ico = clean_text(row.get("ICO"))
            if include_ico and ico and normalize_name(ico) != normalize_name(name):
                # ICO can be a person or unrelated mailing contact, so keep it lower priority and optional.
                irow = make_identity_row(
                    source="bmf_ico",
                    source_detail=path.name,
                    source_rank=35,
                    alias_name=ico,
                    **common,
                )
                if irow:
                    batch.append(irow)
            if len(batch) >= batch_size:
                inserted, tokens = insert_identity_batch(conn, batch, build_tokens)
                counts["inserted"] += inserted
                counts["tokens"] += tokens
                counts["identity_rows_seen"] += len(batch)
                batch.clear()
                if counts["bmf_rows_seen"] % 250_000 < batch_size:
                    elapsed = max(1.0, time.time() - started)
                    print(f"BMF rows {counts['bmf_rows_seen']:,}; identities inserted {counts['inserted']:,}; {counts['bmf_rows_seen']/elapsed:,.0f} rows/sec", flush=True)
    if batch:
        inserted, tokens = insert_identity_batch(conn, batch, build_tokens)
        counts["inserted"] += inserted
        counts["tokens"] += tokens
        counts["identity_rows_seen"] += len(batch)
    print(f"BMF identity import complete: BMF rows {counts['bmf_rows_seen']:,}, identities inserted {counts['inserted']:,}", flush=True)
    return counts


def cmd_verify_bmf(args: argparse.Namespace) -> None:
    bmf_dir = project_bmf_dir(args.project_dir, args.bmf_dir)
    verify_bmf_files(bmf_dir, require=True)


def cmd_build_identity(args: argparse.Namespace) -> None:
    bmf_dir = project_bmf_dir(args.project_dir, args.bmf_dir)
    verify_bmf_files(bmf_dir, require=not args.skip_bmf)
    conn = connect(args.db, readonly=False)
    create_identity_schema(conn, full_refresh=args.full_refresh, create_fts=not args.no_fts)
    if args.full_refresh:
        # Index creation after bulk loading is faster, but the UNIQUE index exists from the table definition.
        pass
    total = Counter()
    if not args.skip_returns:
        total.update({f"returns_{k}": v for k, v in import_returns_identity(conn, args.batch_size, not args.no_tokens).items()})
    if not args.skip_bmf:
        total.update({f"bmf_{k}": v for k, v in import_bmf_identity(conn, bmf_dir, args.batch_size, not args.no_tokens, args.include_bmf_ico).items()})
    print("Creating identity indexes / rebuilding FTS...", flush=True)
    create_identity_indexes(conn, include_fts_rebuild=not args.no_fts)
    distinct_eins = conn.execute(f"SELECT COUNT(DISTINCT ein) FROM {ORG_IDENTITY_TABLE}").fetchone()[0]
    rows = conn.execute(f"SELECT COUNT(*) FROM {ORG_IDENTITY_TABLE}").fetchone()[0]
    print(f"org_identity ready: {rows:,} identity rows, {distinct_eins:,} distinct EINs", flush=True)


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


def create_signature_schema(conn: sqlite3.Connection, full_refresh: bool = False) -> None:
    if full_refresh:
        conn.executescript(f"""
        DROP TABLE IF EXISTS {SIG_GRANT_TABLE};
        DROP TABLE IF EXISTS {SIG_TABLE};
        """)
        conn.commit()
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS {SIG_TABLE} (
      signature_hash TEXT PRIMARY KEY,
      reported_ein TEXT,
      recipient_name TEXT,
      recipient_name_norm TEXT,
      street TEXT,
      street_norm TEXT,
      city TEXT,
      state TEXT,
      zip5 TEXT,
      country TEXT,
      grant_count INTEGER NOT NULL DEFAULT 0,
      total_amount NUMERIC NOT NULL DEFAULT 0,
      first_grant_id INTEGER,
      last_grant_id INTEGER,
      sample_purpose TEXT,
      sample_grantor_ein TEXT,
      sample_grantor_name TEXT,
      first_pass_statuses_json TEXT,
      first_pass_methods_json TEXT,
      first_pass_warning_flags TEXT,
      first_pass_min_confidence NUMERIC,
      first_pass_max_confidence NUMERIC,
      first_pass_avg_confidence NUMERIC,
      queued_reason TEXT,
      candidate_count INTEGER DEFAULT 0,
      ai_queue_status TEXT DEFAULT 'new',
      created_at TEXT,
      updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS {SIG_GRANT_TABLE} (
      signature_hash TEXT NOT NULL,
      grant_id INTEGER NOT NULL,
      PRIMARY KEY(signature_hash, grant_id)
    );

    CREATE INDEX IF NOT EXISTS idx_sig_state_zip ON {SIG_TABLE}(state, zip5);
    CREATE INDEX IF NOT EXISTS idx_sig_name ON {SIG_TABLE}(recipient_name_norm);
    CREATE INDEX IF NOT EXISTS idx_sig_amount ON {SIG_TABLE}(total_amount DESC);
    CREATE INDEX IF NOT EXISTS idx_sig_queue ON {SIG_TABLE}(ai_queue_status, total_amount DESC);
    CREATE INDEX IF NOT EXISTS idx_sig_grant_grant ON {SIG_GRANT_TABLE}(grant_id);
    """)
    conn.commit()


@dataclass
class SigAgg:
    signature_hash: str
    reported_ein: str
    recipient_name: str
    recipient_name_norm: str
    street: str
    street_norm: str
    city: str
    state: str
    zip5: str
    country: str
    grant_count: int = 0
    total_amount: float = 0.0
    first_grant_id: int = 0
    last_grant_id: int = 0
    sample_purpose: str = ""
    sample_grantor_ein: str = ""
    sample_grantor_name: str = ""
    statuses: Counter = None  # type: ignore
    methods: Counter = None  # type: ignore
    warnings: Counter = None  # type: ignore
    min_confidence: float = 999.0
    max_confidence: float = 0.0
    sum_confidence: float = 0.0
    queued_reason: str = ""

    def __post_init__(self) -> None:
        if self.statuses is None:
            self.statuses = Counter()
        if self.methods is None:
            self.methods = Counter()
        if self.warnings is None:
            self.warnings = Counter()


def signature_from_parts(reported_ein: str, name_norm: str, street_norm: str, city: str, state: str, z5: str, country: str) -> str:
    return stable_hash([reported_ein, name_norm, street_norm, city, state, z5, country], "SIG_")


def target_where_for_signatures(args: argparse.Namespace) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    low = args.low_confidence_threshold
    statuses = [s.strip() for s in (args.statuses or "unresolved,conflicting_ein_match,reported_ein_not_found_name_matched,address_unique,address_narrowed_name_match,fuzzy_probable").split(",") if s.strip()]
    status_clause = ""
    if statuses:
        ph = ",".join("?" for _ in statuses)
        status_clause = f"rr.match_status IN ({ph})"
        params.extend(statuses)
    conf_clause = "rr.confidence <= ?"
    params.append(low)
    warn_clause = "(rr.warning_flags IS NOT NULL AND TRIM(rr.warning_flags) <> '')"
    clauses.append(f"(({status_clause}) OR ({conf_clause}) OR ({warn_clause}))" if status_clause else f"(({conf_clause}) OR ({warn_clause}))")
    if args.min_total_amount is not None:
        clauses.append("COALESCE(rr.total_amount,0) >= ?")
        params.append(args.min_total_amount)
    if args.state:
        clauses.append("UPPER(COALESCE(rr.recipient_state,'')) = ?")
        params.append(args.state.upper())
    if args.min_grant_id is not None:
        clauses.append("rr.grant_id >= ?")
        params.append(args.min_grant_id)
    if args.max_grant_id is not None:
        clauses.append("rr.grant_id <= ?")
        params.append(args.max_grant_id)
    return "WHERE " + " AND ".join(clauses), params


def iter_signature_source_rows(conn: sqlite3.Connection, args: argparse.Namespace) -> Iterator[sqlite3.Row]:
    if not table_exists(conn, RESOLVED_TABLE):
        raise RuntimeError(f"Missing {RESOLVED_TABLE}. Run resolve_grant_recipients_v2.py first.")
    where, params = target_where_for_signatures(args)
    limit = ""
    if args.limit:
        limit = f"LIMIT {int(args.limit)}"
    sql = f"""
    SELECT
      rr.grant_id, rr.filing_id, rr.grantor_ein, rr.grantor_name, rr.tax_year,
      rr.recipient_reported_ein, rr.recipient_reported_name, rr.recipient_city,
      rr.recipient_state, rr.recipient_zip, rr.total_amount, rr.purpose,
      rr.match_status, rr.match_method, rr.confidence, rr.warning_flags,
      COALESCE(g.us_address_line1_txt, g.foreign_address_line1_txt) AS recipient_street,
      CASE WHEN g.us_state_abbreviation_cd IS NOT NULL AND TRIM(g.us_state_abbreviation_cd) <> '' THEN 'US'
           ELSE COALESCE(g.foreign_country_cd, '') END AS recipient_country
    FROM {RESOLVED_TABLE} rr
    LEFT JOIN grants g ON g.id = rr.grant_id
    {where}
    ORDER BY rr.grant_id
    {limit}
    """
    yield from conn.execute(sql, params)


def signature_to_row(sig: SigAgg) -> Tuple[Any, ...]:
    avg = sig.sum_confidence / sig.grant_count if sig.grant_count else 0.0
    warn_flags = ";".join(k for k, _ in sig.warnings.most_common(20))
    return (
        sig.signature_hash, sig.reported_ein, sig.recipient_name, sig.recipient_name_norm,
        sig.street, sig.street_norm, sig.city, sig.state, sig.zip5, sig.country,
        sig.grant_count, round(sig.total_amount, 2), sig.first_grant_id, sig.last_grant_id,
        sig.sample_purpose, sig.sample_grantor_ein, sig.sample_grantor_name,
        json.dumps(dict(sig.statuses), sort_keys=True), json.dumps(dict(sig.methods), sort_keys=True),
        warn_flags, None if sig.min_confidence == 999.0 else round(sig.min_confidence, 4),
        round(sig.max_confidence, 4), round(avg, 4), sig.queued_reason,
        0, "new", now_stamp(), now_stamp(),
    )


def _merge_existing_signature(conn: sqlite3.Connection, sigs: Sequence[SigAgg]) -> List[SigAgg]:
    """Merge current in-memory signature aggregates with rows already flushed.

    The source grant rows are ordered by grant_id, not by signature. The same
    recipient signature can therefore appear in more than one flush batch. This
    helper preserves cumulative counts/totals instead of letting later batches
    overwrite earlier aggregates.
    """
    if not sigs:
        return []
    by_hash = {s.signature_hash: s for s in sigs}
    hashes = list(by_hash)
    for i in range(0, len(hashes), 500):
        chunk = hashes[i:i+500]
        ph = ",".join("?" for _ in chunk)
        for row in conn.execute(f"SELECT * FROM {SIG_TABLE} WHERE signature_hash IN ({ph})", chunk):
            sig = by_hash[row["signature_hash"]]
            old_count = int(row["grant_count"] or 0)
            old_total = float(row["total_amount"] or 0)
            old_avg = float(row["first_pass_avg_confidence"] or 0)
            sig.sum_confidence += old_avg * old_count
            sig.grant_count += old_count
            sig.total_amount += old_total
            if row["first_grant_id"] is not None:
                sig.first_grant_id = min(sig.first_grant_id or int(row["first_grant_id"]), int(row["first_grant_id"]))
            if row["last_grant_id"] is not None:
                sig.last_grant_id = max(sig.last_grant_id or int(row["last_grant_id"]), int(row["last_grant_id"]))
            if row["first_pass_min_confidence"] is not None:
                sig.min_confidence = min(sig.min_confidence, float(row["first_pass_min_confidence"]))
            if row["first_pass_max_confidence"] is not None:
                sig.max_confidence = max(sig.max_confidence, float(row["first_pass_max_confidence"]))
            try:
                sig.statuses.update(json.loads(row["first_pass_statuses_json"] or "{}"))
            except Exception:
                pass
            try:
                sig.methods.update(json.loads(row["first_pass_methods_json"] or "{}"))
            except Exception:
                pass
            for w in clean_text(row["first_pass_warning_flags"]).split(";"):
                if w:
                    sig.warnings[w] += 1
    return list(by_hash.values())


def insert_signature_batch(conn: sqlite3.Connection, sigs: Sequence[SigAgg], mappings: Sequence[Tuple[str, int]]) -> None:
    sig_cols = [
        "signature_hash", "reported_ein", "recipient_name", "recipient_name_norm", "street", "street_norm",
        "city", "state", "zip5", "country", "grant_count", "total_amount", "first_grant_id", "last_grant_id",
        "sample_purpose", "sample_grantor_ein", "sample_grantor_name", "first_pass_statuses_json",
        "first_pass_methods_json", "first_pass_warning_flags", "first_pass_min_confidence", "first_pass_max_confidence",
        "first_pass_avg_confidence", "queued_reason", "candidate_count", "ai_queue_status", "created_at", "updated_at",
    ]
    merged = _merge_existing_signature(conn, sigs)
    ph = ",".join("?" for _ in sig_cols)
    conn.executemany(
        f"INSERT OR REPLACE INTO {SIG_TABLE} ({','.join(sig_cols)}) VALUES ({ph})",
        [signature_to_row(s) for s in merged],
    )
    if mappings:
        conn.executemany(
            f"INSERT OR IGNORE INTO {SIG_GRANT_TABLE} (signature_hash, grant_id) VALUES (?,?)",
            mappings,
        )
    conn.commit()


def cmd_build_signatures(args: argparse.Namespace) -> None:
    conn = connect(args.db, readonly=False)
    create_signature_schema(conn, full_refresh=args.full_refresh)
    sigs: Dict[str, SigAgg] = {}
    mappings: List[Tuple[str, int]] = []
    processed = 0
    started = time.time()
    for r in iter_signature_source_rows(conn, args):
        reported_ein = digits9(r["recipient_reported_ein"])
        recipient_name = clean_text(r["recipient_reported_name"])
        name_norm = normalize_name(recipient_name)
        street = clean_text(r["recipient_street"])
        street_norm = normalize_address(street)
        city = norm_upper(r["recipient_city"])
        state = norm_upper(r["recipient_state"])
        z5 = zip5(r["recipient_zip"])
        country = norm_upper(r["recipient_country"])
        sig_hash = signature_from_parts(reported_ein, name_norm, street_norm, city, state, z5, country)
        sig = sigs.get(sig_hash)
        if sig is None:
            sig = SigAgg(
                signature_hash=sig_hash,
                reported_ein=reported_ein,
                recipient_name=recipient_name,
                recipient_name_norm=name_norm,
                street=street,
                street_norm=street_norm,
                city=city,
                state=state,
                zip5=z5,
                country=country,
                first_grant_id=int(r["grant_id"]),
                last_grant_id=int(r["grant_id"]),
                sample_purpose=clean_text(r["purpose"]),
                sample_grantor_ein=digits9(r["grantor_ein"]),
                sample_grantor_name=clean_text(r["grantor_name"]),
                queued_reason="ai_second_pass_target",
            )
            sigs[sig_hash] = sig
        sig.grant_count += 1
        sig.total_amount += float(r["total_amount"] or 0)
        sig.last_grant_id = max(sig.last_grant_id, int(r["grant_id"]))
        status = clean_text(r["match_status"])
        method = clean_text(r["match_method"])
        if status:
            sig.statuses[status] += 1
        if method:
            sig.methods[method] += 1
        conf = float(r["confidence"] or 0)
        sig.min_confidence = min(sig.min_confidence, conf)
        sig.max_confidence = max(sig.max_confidence, conf)
        sig.sum_confidence += conf
        for w in clean_text(r["warning_flags"]).split(";"):
            if w:
                sig.warnings[w] += 1
        mappings.append((sig_hash, int(r["grant_id"])))
        processed += 1
        if processed % args.flush_every == 0:
            insert_signature_batch(conn, list(sigs.values()), mappings)
            print(f"Processed {processed:,} grant rows into {len(sigs):,} signatures...", flush=True)
            sigs.clear()
            mappings.clear()
    if sigs or mappings:
        insert_signature_batch(conn, list(sigs.values()), mappings)
    elapsed = max(1.0, time.time() - started)
    sig_count = conn.execute(f"SELECT COUNT(*) FROM {SIG_TABLE}").fetchone()[0]
    map_count = conn.execute(f"SELECT COUNT(*) FROM {SIG_GRANT_TABLE}").fetchone()[0]
    print(f"Signatures ready: {sig_count:,} signatures, {map_count:,} grant mappings; processed {processed:,} rows at {processed/elapsed:,.0f}/sec", flush=True)


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------


def create_candidate_schema(conn: sqlite3.Connection, full_refresh: bool = False) -> None:
    if full_refresh:
        conn.executescript(f"DROP TABLE IF EXISTS {CAND_TABLE};")
        conn.commit()
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS {CAND_TABLE} (
      signature_hash TEXT NOT NULL,
      candidate_id TEXT NOT NULL,
      candidate_rank INTEGER NOT NULL,
      identity_id INTEGER,
      ein TEXT NOT NULL,
      candidate_name TEXT,
      source TEXT,
      source_rank INTEGER,
      street TEXT,
      city TEXT,
      state TEXT,
      zip5 TEXT,
      name_score NUMERIC,
      address_score NUMERIC,
      zip_match INTEGER,
      city_state_match INTEGER,
      state_match INTEGER,
      exact_name INTEGER,
      exact_address INTEGER,
      reported_ein_match INTEGER,
      candidate_score NUMERIC,
      candidate_reason TEXT,
      created_at TEXT,
      PRIMARY KEY(signature_hash, candidate_id)
    );
    CREATE INDEX IF NOT EXISTS idx_ai_cand_sig_rank ON {CAND_TABLE}(signature_hash, candidate_rank);
    CREATE INDEX IF NOT EXISTS idx_ai_cand_ein ON {CAND_TABLE}(ein);
    """)
    conn.commit()


def identity_rows_by_sql(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> List[sqlite3.Row]:
    return list(conn.execute(sql, params))


def get_candidate_identity_rows(conn: sqlite3.Connection, sig: sqlite3.Row, use_fts: bool, token_limit: int = 200) -> List[sqlite3.Row]:
    rows: Dict[int, sqlite3.Row] = {}
    reported_ein = digits9(sig["reported_ein"])
    name_norm = clean_text(sig["recipient_name_norm"])
    street_norm = clean_text(sig["street_norm"])
    city = clean_text(sig["city"])
    state = clean_text(sig["state"])
    z5 = clean_text(sig["zip5"])

    queries: List[Tuple[str, Sequence[Any]]] = []
    base_cols = f"SELECT * FROM {ORG_IDENTITY_TABLE} WHERE "
    if reported_ein:
        queries.append((base_cols + "ein=? ORDER BY source_rank, tax_year DESC LIMIT 100", [reported_ein]))
    if name_norm and street_norm and z5:
        queries.append((base_cols + "name_norm=? AND street_norm=? AND zip5=? ORDER BY source_rank, tax_year DESC LIMIT 100", [name_norm, street_norm, z5]))
    if name_norm and street_norm and city and state:
        queries.append((base_cols + "name_norm=? AND street_norm=? AND city=? AND state=? ORDER BY source_rank, tax_year DESC LIMIT 100", [name_norm, street_norm, city, state]))
    if name_norm and z5:
        queries.append((base_cols + "name_norm=? AND zip5=? ORDER BY source_rank, tax_year DESC LIMIT 100", [name_norm, z5]))
    if name_norm and city and state:
        queries.append((base_cols + "name_norm=? AND city=? AND state=? ORDER BY source_rank, tax_year DESC LIMIT 100", [name_norm, city, state]))
    if street_norm and z5:
        queries.append((base_cols + "street_norm=? AND zip5=? ORDER BY source_rank, tax_year DESC LIMIT 200", [street_norm, z5]))
    if street_norm and city and state:
        queries.append((base_cols + "street_norm=? AND city=? AND state=? ORDER BY source_rank, tax_year DESC LIMIT 200", [street_norm, city, state]))
    if name_norm and state:
        queries.append((base_cols + "name_norm=? AND state=? ORDER BY source_rank, tax_year DESC LIMIT 100", [name_norm, state]))
    if name_norm:
        queries.append((base_cols + "name_norm=? ORDER BY source_rank, tax_year DESC LIMIT 100", [name_norm]))

    for sql, params in queries:
        for r in identity_rows_by_sql(conn, sql, params):
            rows[int(r["identity_id"])] = r

    # Token overlap fallback: useful for abbreviations or partial names, constrained by geography when possible.
    toks = name_tokens(name_norm)
    if toks:
        ph = ",".join("?" for _ in toks[:8])
        params: List[Any] = list(toks[:8])
        geo_clause = ""
        if z5:
            geo_clause = " AND oi.zip5=?"
            params.append(z5)
        elif state:
            geo_clause = " AND oi.state=?"
            params.append(state)
        sql = f"""
        SELECT oi.*, COUNT(*) AS token_hits
        FROM {ORG_TOKEN_TABLE} tok
        JOIN {ORG_IDENTITY_TABLE} oi ON oi.identity_id = tok.identity_id
        WHERE tok.token IN ({ph}) {geo_clause}
        GROUP BY oi.identity_id
        ORDER BY token_hits DESC, oi.source_rank ASC, oi.tax_year DESC
        LIMIT {int(token_limit)}
        """
        try:
            for r in conn.execute(sql, params):
                rows[int(r["identity_id"])] = r
        except sqlite3.Error:
            pass

    # FTS fallback, if available. It is intentionally constrained after retrieval by score later.
    if use_fts and name_norm and table_exists(conn, "org_identity_fts"):
        toks = name_tokens(name_norm)[:6]
        if toks:
            # AND terms in FTS5 by whitespace. Use quoted terms for safety.
            match = " ".join('"' + t.replace('"', '') + '"' for t in toks)
            try:
                sql = f"""
                SELECT oi.*
                FROM org_identity_fts f
                JOIN {ORG_IDENTITY_TABLE} oi ON oi.identity_id = f.rowid
                WHERE org_identity_fts MATCH ?
                ORDER BY rank
                LIMIT 100
                """
                for r in conn.execute(sql, (match,)):
                    rows[int(r["identity_id"])] = r
            except sqlite3.Error:
                pass

    return list(rows.values())


@dataclass
class CandidateChoice:
    identity_id: int
    ein: str
    candidate_name: str
    source: str
    source_rank: int
    street: str
    city: str
    state: str
    zip5: str
    name_score: float
    address_score: float
    zip_match: int
    city_state_match: int
    state_match: int
    exact_name: int
    exact_address: int
    reported_ein_match: int
    candidate_score: float
    reasons: List[str]


def score_identity(sig: sqlite3.Row, row: sqlite3.Row) -> CandidateChoice:
    reported_ein = digits9(sig["reported_ein"])
    name_norm = clean_text(sig["recipient_name_norm"])
    street_norm = clean_text(sig["street_norm"])
    city = clean_text(sig["city"])
    state = clean_text(sig["state"])
    z5 = clean_text(sig["zip5"])

    cand_name_norm = clean_text(row["name_norm"])
    cand_street_norm = clean_text(row["street_norm"])
    cand_state = clean_text(row["state"])
    cand_city = clean_text(row["city"])
    cand_zip = clean_text(row["zip5"])
    ein = digits9(row["ein"])
    nscore = ratio(name_norm, cand_name_norm)
    ascore = ratio(street_norm, cand_street_norm) if street_norm and cand_street_norm else 0.0
    exact_name = 1 if name_norm and name_norm == cand_name_norm else 0
    exact_address = 1 if street_norm and street_norm == cand_street_norm else 0
    zip_match = 1 if z5 and z5 == cand_zip else 0
    city_state_match = 1 if city and state and city == cand_city and state == cand_state else 0
    state_match = 1 if state and state == cand_state else 0
    reported_ein_match = 1 if reported_ein and reported_ein == ein else 0
    reasons: List[str] = []
    score = 0.0
    if reported_ein_match:
        score += 65
        reasons.append("reported_ein_candidate")
    if exact_name:
        score += 45
        reasons.append("exact_normalized_name")
    else:
        score += 35 * nscore
        if nscore >= 0.90:
            reasons.append("very_high_name_similarity")
        elif nscore >= 0.80:
            reasons.append("high_name_similarity")
        elif nscore >= 0.70:
            reasons.append("moderate_name_similarity")
    if exact_address:
        score += 30
        reasons.append("exact_street_address")
    else:
        score += 18 * ascore
        if ascore >= 0.90:
            reasons.append("high_address_similarity")
    if zip_match:
        score += 16
        reasons.append("zip_match")
    if city_state_match:
        score += 12
        reasons.append("city_state_match")
    elif state_match:
        score += 6
        reasons.append("state_match")
    # Prefer returns slightly over BMF, but not enough to swamp evidence.
    source_rank = int(row["source_rank"] or 99)
    score += max(0, 12 - source_rank / 4)
    if clean_text(row["source"]).startswith("returns"):
        reasons.append("seen_in_990_returns")
    elif clean_text(row["source"]).startswith("bmf"):
        reasons.append("seen_in_irs_eo_bmf")
    return CandidateChoice(
        identity_id=int(row["identity_id"]),
        ein=ein,
        candidate_name=clean_text(row["display_name"] or row["legal_name"]),
        source=clean_text(row["source"]),
        source_rank=source_rank,
        street=clean_text(row["street"]),
        city=clean_text(row["city"]),
        state=clean_text(row["state"]),
        zip5=clean_text(row["zip5"]),
        name_score=round(nscore, 4),
        address_score=round(ascore, 4),
        zip_match=zip_match,
        city_state_match=city_state_match,
        state_match=state_match,
        exact_name=exact_name,
        exact_address=exact_address,
        reported_ein_match=reported_ein_match,
        candidate_score=round(score, 4),
        reasons=reasons,
    )


def best_candidates_by_ein(sig: sqlite3.Row, identity_rows: Sequence[sqlite3.Row], max_candidates: int, min_score: float) -> List[CandidateChoice]:
    best: Dict[str, CandidateChoice] = {}
    for row in identity_rows:
        c = score_identity(sig, row)
        if not c.ein:
            continue
        # Do not keep very weak candidates unless they are the reported EIN.
        if c.candidate_score < min_score and not c.reported_ein_match:
            continue
        old = best.get(c.ein)
        if old is None or (c.candidate_score, -c.source_rank) > (old.candidate_score, -old.source_rank):
            best[c.ein] = c
    return sorted(best.values(), key=lambda c: (c.candidate_score, c.name_score, c.address_score), reverse=True)[:max_candidates]


def insert_candidate_rows(conn: sqlite3.Connection, signature_hash: str, candidates: Sequence[CandidateChoice]) -> None:
    conn.execute(f"DELETE FROM {CAND_TABLE} WHERE signature_hash=?", (signature_hash,))
    rows = []
    for i, c in enumerate(candidates, 1):
        rows.append((
            signature_hash, f"C{i}", i, c.identity_id, c.ein, c.candidate_name, c.source, c.source_rank,
            c.street, c.city, c.state, c.zip5, c.name_score, c.address_score, c.zip_match,
            c.city_state_match, c.state_match, c.exact_name, c.exact_address, c.reported_ein_match,
            c.candidate_score, ";".join(c.reasons), now_stamp(),
        ))
    conn.executemany(f"""
        INSERT INTO {CAND_TABLE} (
          signature_hash, candidate_id, candidate_rank, identity_id, ein, candidate_name, source, source_rank,
          street, city, state, zip5, name_score, address_score, zip_match, city_state_match, state_match,
          exact_name, exact_address, reported_ein_match, candidate_score, candidate_reason, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.execute(f"UPDATE {SIG_TABLE} SET candidate_count=?, ai_queue_status=CASE WHEN ? > 0 THEN 'candidates_ready' ELSE 'no_candidates' END, updated_at=? WHERE signature_hash=?",
                 (len(candidates), len(candidates), now_stamp(), signature_hash))


def iter_signatures_for_candidates(conn: sqlite3.Connection, args: argparse.Namespace) -> Iterator[sqlite3.Row]:
    where = []
    params: List[Any] = []
    if not args.regenerate:
        where.append(f"NOT EXISTS (SELECT 1 FROM {CAND_TABLE} c WHERE c.signature_hash = s.signature_hash)")
    if args.state:
        where.append("s.state=?")
        params.append(args.state.upper())
    if args.min_total_amount is not None:
        where.append("s.total_amount >= ?")
        params.append(args.min_total_amount)
    if args.queue_status:
        where.append("s.ai_queue_status=?")
        params.append(args.queue_status)
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    limit = f"LIMIT {int(args.limit)}" if args.limit else ""
    sql = f"""
    SELECT * FROM {SIG_TABLE} s
    {sql_where}
    ORDER BY s.total_amount DESC, s.grant_count DESC
    {limit}
    """
    yield from conn.execute(sql, params)


def cmd_generate_candidates(args: argparse.Namespace) -> None:
    conn = connect(args.db, readonly=False)
    if not table_exists(conn, ORG_IDENTITY_TABLE):
        raise RuntimeError(f"Missing {ORG_IDENTITY_TABLE}. Run build-identity first.")
    if not table_exists(conn, SIG_TABLE):
        raise RuntimeError(f"Missing {SIG_TABLE}. Run build-signatures first.")
    create_candidate_schema(conn, full_refresh=args.full_refresh)
    processed = 0
    with_candidates = 0
    started = time.time()
    for sig in iter_signatures_for_candidates(conn, args):
        identity_rows = get_candidate_identity_rows(conn, sig, use_fts=not args.no_fts, token_limit=args.token_limit)
        candidates = best_candidates_by_ein(sig, identity_rows, args.max_candidates, args.min_candidate_score)
        insert_candidate_rows(conn, sig["signature_hash"], candidates)
        processed += 1
        if candidates:
            with_candidates += 1
        if processed % args.commit_every == 0:
            conn.commit()
            elapsed = max(1.0, time.time() - started)
            print(f"Generated candidates for {processed:,} signatures; {with_candidates:,} have candidates; {processed/elapsed:,.0f}/sec", flush=True)
    conn.commit()
    elapsed = max(1.0, time.time() - started)
    print(f"Candidate generation complete: {processed:,} signatures, {with_candidates:,} with candidates at {processed/elapsed:,.0f}/sec", flush=True)


# ---------------------------------------------------------------------------
# Ollama adjudication
# ---------------------------------------------------------------------------


def create_decision_schema(conn: sqlite3.Connection, full_refresh: bool = False) -> None:
    if full_refresh:
        conn.executescript(f"DROP TABLE IF EXISTS {DECISION_TABLE};")
        conn.commit()
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS {DECISION_TABLE} (
      signature_hash TEXT PRIMARY KEY,
      decision TEXT,
      selected_candidate_id TEXT,
      selected_ein TEXT,
      selected_name TEXT,
      confidence NUMERIC,
      confidence_label TEXT,
      reason_codes_json TEXT,
      explanation TEXT,
      needs_human_review INTEGER,
      auto_accept INTEGER,
      validation_status TEXT,
      validation_error TEXT,
      model TEXT,
      model_options_json TEXT,
      prompt_hash TEXT,
      candidate_set_hash TEXT,
      input_json TEXT,
      output_json TEXT,
      created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ai_decision_auto ON {DECISION_TABLE}(auto_accept, confidence);
    CREATE INDEX IF NOT EXISTS idx_ai_decision_selected_ein ON {DECISION_TABLE}(selected_ein);
    """)
    conn.commit()


AI_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["SELECT_CANDIDATE", "KEEP_REPORTED_EIN", "NO_MATCH", "AMBIGUOUS", "HUMAN_REVIEW"]},
        "candidate_id": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence_label": {"type": "string", "enum": ["high", "medium", "low", "none"]},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
        "needs_human_review": {"type": "boolean"},
    },
    "required": ["decision", "confidence", "confidence_label", "reason_codes", "explanation", "needs_human_review"],
    "additionalProperties": False,
}


def iter_signatures_for_adjudication(conn: sqlite3.Connection, args: argparse.Namespace) -> Iterator[sqlite3.Row]:
    clauses = [f"EXISTS (SELECT 1 FROM {CAND_TABLE} c WHERE c.signature_hash = s.signature_hash)"]
    params: List[Any] = []
    if not args.regenerate:
        clauses.append(f"NOT EXISTS (SELECT 1 FROM {DECISION_TABLE} d WHERE d.signature_hash = s.signature_hash)")
    if args.state:
        clauses.append("s.state=?")
        params.append(args.state.upper())
    if args.min_total_amount is not None:
        clauses.append("s.total_amount >= ?")
        params.append(args.min_total_amount)
    where = "WHERE " + " AND ".join(clauses)
    limit = f"LIMIT {int(args.limit)}" if args.limit else ""
    sql = f"""
    SELECT s.*
    FROM {SIG_TABLE} s
    {where}
    ORDER BY s.total_amount DESC, s.grant_count DESC
    {limit}
    """
    yield from conn.execute(sql, params)


def candidates_for_signature(conn: sqlite3.Connection, signature_hash: str, max_candidates: int) -> List[sqlite3.Row]:
    return list(conn.execute(
        f"SELECT * FROM {CAND_TABLE} WHERE signature_hash=? ORDER BY candidate_rank LIMIT ?",
        (signature_hash, max_candidates),
    ))


def build_ai_input(sig: sqlite3.Row, candidates: Sequence[sqlite3.Row]) -> Dict[str, Any]:
    cand_list = []
    for c in candidates:
        cand_list.append({
            "candidate_id": c["candidate_id"],
            "ein": c["ein"],
            "name": c["candidate_name"],
            "source": c["source"],
            "street": c["street"],
            "city": c["city"],
            "state": c["state"],
            "zip5": c["zip5"],
            "name_score": c["name_score"],
            "address_score": c["address_score"],
            "zip_match": bool(c["zip_match"]),
            "city_state_match": bool(c["city_state_match"]),
            "state_match": bool(c["state_match"]),
            "exact_name": bool(c["exact_name"]),
            "exact_address": bool(c["exact_address"]),
            "reported_ein_match": bool(c["reported_ein_match"]),
            "candidate_score": c["candidate_score"],
            "candidate_reason": clean_text(c["candidate_reason"]),
        })
    return {
        "task": "Choose the correct nonprofit EIN for the grant recipient, or return NO_MATCH, AMBIGUOUS, or HUMAN_REVIEW.",
        "rules": [
            "Choose only from the provided candidates.",
            "Do not invent EINs or candidate IDs.",
            "A known reported EIN should be kept unless name/address evidence strongly contradicts it.",
            "Prefer exact address plus ZIP and strong name evidence over broad name-only similarity.",
            "If multiple candidates are plausible and evidence does not clearly select one, return AMBIGUOUS or HUMAN_REVIEW.",
            "If no candidate appears to be the grant recipient, return NO_MATCH.",
        ],
        "grant_recipient_signature": {
            "signature_hash": sig["signature_hash"],
            "reported_ein": clean_text(sig["reported_ein"]),
            "name": clean_text(sig["recipient_name"]),
            "name_norm": clean_text(sig["recipient_name_norm"]),
            "street": clean_text(sig["street"]),
            "street_norm": clean_text(sig["street_norm"]),
            "city": clean_text(sig["city"]),
            "state": clean_text(sig["state"]),
            "zip5": clean_text(sig["zip5"]),
            "country": clean_text(sig["country"]),
            "grant_count": int(sig["grant_count"] or 0),
            "total_amount": float(sig["total_amount"] or 0),
            "sample_purpose": clean_text(sig["sample_purpose"]),
            "sample_grantor_ein": clean_text(sig["sample_grantor_ein"]),
            "sample_grantor_name": clean_text(sig["sample_grantor_name"]),
        },
        "first_pass": {
            "statuses": json.loads(sig["first_pass_statuses_json"] or "{}"),
            "methods": json.loads(sig["first_pass_methods_json"] or "{}"),
            "warning_flags": clean_text(sig["first_pass_warning_flags"]),
            "min_confidence": sig["first_pass_min_confidence"],
            "max_confidence": sig["first_pass_max_confidence"],
            "avg_confidence": sig["first_pass_avg_confidence"],
        },
        "candidates": cand_list,
    }


def call_ollama(input_obj: Dict[str, Any], model: str, url: str, timeout: int, num_ctx: int, num_predict: int) -> Dict[str, Any]:
    system_msg = """
You are a careful nonprofit identity matching adjudicator.
You receive one grant-recipient record and a candidate list generated by a database.
Return only JSON that follows the provided schema.
Never invent an EIN or candidate ID. If the right answer is unclear, return AMBIGUOUS or HUMAN_REVIEW.
Precision is more important than recall: a wrong EIN is worse than no match.
""".strip()
    user_msg = json.dumps(input_obj, ensure_ascii=False, sort_keys=True)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "format": AI_DECISION_SCHEMA,
        "keep_alive": "30m",
        "options": {
            "temperature": 0,
            "top_p": 0.9,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    content = ""
    if isinstance(data.get("message"), dict):
        content = data["message"].get("content") or ""
    if not content:
        content = data.get("response") or data.get("content") or ""
    content = clean_text(content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # tolerate accidental fenced JSON
        m = re.search(r"\{.*\}", content, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise


def validate_ai_output(output: Dict[str, Any], candidates: Sequence[sqlite3.Row], sig: sqlite3.Row, auto_accept_threshold: float) -> Dict[str, Any]:
    candidate_by_id = {clean_text(c["candidate_id"]): c for c in candidates}
    decision = clean_text(output.get("decision"))
    candidate_id = clean_text(output.get("candidate_id"))
    confidence = output.get("confidence")
    errors: List[str] = []
    selected: Optional[sqlite3.Row] = None
    try:
        confidence_f = float(confidence)
    except Exception:
        confidence_f = 0.0
        errors.append("invalid_confidence")
    if confidence_f < 0 or confidence_f > 1:
        errors.append("confidence_out_of_range")
    if decision not in {"SELECT_CANDIDATE", "KEEP_REPORTED_EIN", "NO_MATCH", "AMBIGUOUS", "HUMAN_REVIEW"}:
        errors.append("invalid_decision")
    if decision == "SELECT_CANDIDATE":
        if candidate_id not in candidate_by_id:
            errors.append("candidate_id_not_in_candidate_list")
        else:
            selected = candidate_by_id[candidate_id]
    elif decision == "KEEP_REPORTED_EIN":
        reported_ein = digits9(sig["reported_ein"])
        # If reported EIN appears in candidate list, treat that candidate as selected.
        for c in candidates:
            if digits9(c["ein"]) == reported_ein:
                selected = c
                candidate_id = clean_text(c["candidate_id"])
                break
        if not reported_ein:
            errors.append("keep_reported_ein_but_no_reported_ein")
    else:
        candidate_id = ""

    validation_status = "ok" if not errors else "invalid"
    needs_review = bool(output.get("needs_human_review", True))
    auto_accept = 0
    if validation_status == "ok" and selected is not None and decision in {"SELECT_CANDIDATE", "KEEP_REPORTED_EIN"}:
        strong_signal = bool(
            selected["reported_ein_match"]
            or (selected["exact_name"] and (selected["zip_match"] or selected["city_state_match"]))
            or (selected["exact_address"] and selected["zip_match"] and float(selected["name_score"] or 0) >= 0.72)
            or float(selected["candidate_score"] or 0) >= 92
        )
        if confidence_f >= auto_accept_threshold and strong_signal and not needs_review:
            auto_accept = 1
    return {
        "validation_status": validation_status,
        "validation_error": ";".join(errors),
        "selected_candidate_id": candidate_id,
        "selected_ein": clean_text(selected["ein"]) if selected is not None else "",
        "selected_name": clean_text(selected["candidate_name"]) if selected is not None else "",
        "confidence": round(confidence_f, 4),
        "auto_accept": auto_accept,
    }


def decision_row_tuple(sig_hash: str, input_obj: Dict[str, Any], candidates: Sequence[sqlite3.Row], output: Dict[str, Any], validation: Dict[str, Any], args: argparse.Namespace) -> Tuple[Any, ...]:
    input_json = json.dumps(input_obj, ensure_ascii=False, sort_keys=True)
    output_json = json.dumps(output, ensure_ascii=False, sort_keys=True)
    candidate_set_json = json.dumps([{"id": c["candidate_id"], "ein": c["ein"], "score": c["candidate_score"]} for c in candidates], sort_keys=True)
    return (
        sig_hash,
        clean_text(output.get("decision")),
        validation["selected_candidate_id"],
        validation["selected_ein"],
        validation["selected_name"],
        validation["confidence"],
        clean_text(output.get("confidence_label")),
        json.dumps(output.get("reason_codes") or [], ensure_ascii=False, sort_keys=True),
        clean_text(output.get("explanation")),
        1 if output.get("needs_human_review", True) else 0,
        validation["auto_accept"],
        validation["validation_status"],
        validation["validation_error"],
        args.model,
        json.dumps({"num_ctx": args.num_ctx, "num_predict": args.num_predict, "temperature": 0}, sort_keys=True),
        stable_hash([input_json], "PROMPT_"),
        stable_hash([candidate_set_json], "CANDS_"),
        input_json,
        output_json,
        now_stamp(),
    )


def insert_decision(conn: sqlite3.Connection, row: Tuple[Any, ...]) -> None:
    cols = [
        "signature_hash", "decision", "selected_candidate_id", "selected_ein", "selected_name", "confidence",
        "confidence_label", "reason_codes_json", "explanation", "needs_human_review", "auto_accept",
        "validation_status", "validation_error", "model", "model_options_json", "prompt_hash", "candidate_set_hash",
        "input_json", "output_json", "created_at",
    ]
    ph = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in {"signature_hash"})
    conn.execute(
        f"INSERT INTO {DECISION_TABLE} ({','.join(cols)}) VALUES ({ph}) ON CONFLICT(signature_hash) DO UPDATE SET {updates}",
        row,
    )
    conn.execute(f"UPDATE {SIG_TABLE} SET ai_queue_status='adjudicated', updated_at=? WHERE signature_hash=?", (now_stamp(), row[0]))


def cmd_adjudicate(args: argparse.Namespace) -> None:
    conn = connect(args.db, readonly=False)
    if not table_exists(conn, CAND_TABLE):
        raise RuntimeError(f"Missing {CAND_TABLE}. Run generate-candidates first.")
    create_decision_schema(conn, full_refresh=args.full_refresh)
    out_fh = None
    writer = None
    if args.dry_run:
        out_path = Path(args.csv_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_fh = out_path.open("w", newline="", encoding="utf-8-sig")
        writer = csv.writer(out_fh)
        writer.writerow([
            "signature_hash", "decision", "selected_candidate_id", "selected_ein", "selected_name", "confidence",
            "auto_accept", "validation_status", "validation_error", "explanation", "output_json",
        ])
        print(f"Dry run enabled; writing AI decisions CSV to {out_path}", flush=True)
    processed = 0
    started = time.time()
    try:
        for sig in iter_signatures_for_adjudication(conn, args):
            cands = candidates_for_signature(conn, sig["signature_hash"], args.max_candidates)
            if not cands:
                continue
            input_obj = build_ai_input(sig, cands)
            try:
                output = call_ollama(
                    input_obj,
                    model=args.model,
                    url=args.ollama_url,
                    timeout=args.timeout,
                    num_ctx=args.num_ctx,
                    num_predict=args.num_predict,
                )
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception) as e:
                output = {
                    "decision": "HUMAN_REVIEW",
                    "candidate_id": "",
                    "confidence": 0,
                    "confidence_label": "none",
                    "reason_codes": ["ollama_call_failed"],
                    "explanation": f"Ollama call failed: {type(e).__name__}: {e}",
                    "needs_human_review": True,
                }
            validation = validate_ai_output(output, cands, sig, args.auto_accept_threshold)
            row = decision_row_tuple(sig["signature_hash"], input_obj, cands, output, validation, args)
            if writer is not None:
                writer.writerow([
                    row[0], row[1], row[2], row[3], row[4], row[5], row[10], row[11], row[12], row[8], row[18]
                ])
                if processed and processed % args.flush_every == 0:
                    out_fh.flush()
            else:
                insert_decision(conn, row)
                if processed and processed % args.commit_every == 0:
                    conn.commit()
            processed += 1
            if processed % args.progress_every == 0:
                elapsed = max(1.0, time.time() - started)
                print(f"Adjudicated {processed:,} signatures at {processed/elapsed:,.2f}/sec", flush=True)
        if writer is None:
            conn.commit()
    finally:
        if out_fh is not None:
            out_fh.flush()
            out_fh.close()
    print(f"AI adjudication complete: {processed:,} signatures", flush=True)


# ---------------------------------------------------------------------------
# Apply decisions / final view
# ---------------------------------------------------------------------------


def create_applied_schema_and_view(conn: sqlite3.Connection, full_refresh: bool = False) -> None:
    if full_refresh:
        conn.executescript(f"DROP VIEW IF EXISTS {FINAL_VIEW}; DROP TABLE IF EXISTS {APPLIED_TABLE};")
        conn.commit()
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS {APPLIED_TABLE} (
      grant_id INTEGER PRIMARY KEY,
      signature_hash TEXT NOT NULL,
      selected_ein TEXT NOT NULL,
      selected_name TEXT,
      ai_confidence NUMERIC,
      ai_decision TEXT,
      model TEXT,
      applied_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ai_applied_selected_ein ON {APPLIED_TABLE}(selected_ein);
    CREATE INDEX IF NOT EXISTS idx_ai_applied_sig ON {APPLIED_TABLE}(signature_hash);
    """)
    conn.commit()
    conn.execute(f"DROP VIEW IF EXISTS {FINAL_VIEW}")
    conn.execute(f"""
    CREATE VIEW {FINAL_VIEW} AS
    SELECT
      rr.*,
      aa.selected_ein AS ai_resolved_ein,
      aa.selected_name AS ai_resolved_name,
      aa.ai_confidence AS ai_confidence,
      aa.ai_decision AS ai_decision,
      CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN aa.selected_ein ELSE rr.resolved_ein END AS final_resolved_ein,
      CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN aa.selected_name ELSE rr.resolved_org_name END AS final_resolved_org_name,
      CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN 'ai_assisted' ELSE 'deterministic' END AS final_match_source,
      CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN aa.ai_confidence ELSE rr.confidence END AS final_confidence
    FROM {RESOLVED_TABLE} rr
    LEFT JOIN {APPLIED_TABLE} aa ON aa.grant_id = rr.grant_id
    """)
    conn.commit()


def cmd_apply_decisions(args: argparse.Namespace) -> None:
    conn = connect(args.db, readonly=False)
    if not table_exists(conn, DECISION_TABLE):
        raise RuntimeError(f"Missing {DECISION_TABLE}. Run adjudicate first.")
    create_applied_schema_and_view(conn, full_refresh=args.full_refresh)
    where = "WHERE d.auto_accept=1 AND d.validation_status='ok' AND d.selected_ein IS NOT NULL AND d.selected_ein <> ''"
    params: List[Any] = []
    if args.min_confidence is not None:
        where += " AND d.confidence >= ?"
        params.append(args.min_confidence)
    sql = f"""
    SELECT sg.grant_id, d.signature_hash, d.selected_ein, d.selected_name, d.confidence, d.decision, d.model
    FROM {DECISION_TABLE} d
    JOIN {SIG_GRANT_TABLE} sg ON sg.signature_hash = d.signature_hash
    {where}
    """
    count = 0
    batch = []
    for r in conn.execute(sql, params):
        batch.append((
            r["grant_id"], r["signature_hash"], r["selected_ein"], r["selected_name"], r["confidence"], r["decision"], r["model"], now_stamp()
        ))
        if len(batch) >= args.batch_size:
            conn.executemany(
                f"INSERT OR REPLACE INTO {APPLIED_TABLE} (grant_id, signature_hash, selected_ein, selected_name, ai_confidence, ai_decision, model, applied_at) VALUES (?,?,?,?,?,?,?,?)",
                batch,
            )
            count += len(batch)
            batch.clear()
            conn.commit()
            print(f"Applied {count:,} grant-level AI matches...", flush=True)
    if batch:
        conn.executemany(
            f"INSERT OR REPLACE INTO {APPLIED_TABLE} (grant_id, signature_hash, selected_ein, selected_name, ai_confidence, ai_decision, model, applied_at) VALUES (?,?,?,?,?,?,?,?)",
            batch,
        )
        count += len(batch)
        conn.commit()
    print(f"Applied {count:,} grant-level AI matches into {APPLIED_TABLE}; final view is {FINAL_VIEW}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common_db(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-assisted second-pass grant recipient matcher")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("verify-bmf", help="Verify eo-bmf/eo1.csv ... eo4.csv exist")
    p.add_argument("--project-dir", default=None, help="Main project folder containing eo-bmf/")
    p.add_argument("--bmf-dir", default=None, help="Explicit EO BMF directory")
    p.set_defaults(func=cmd_verify_bmf)

    p = sub.add_parser("build-identity", help="Build org_identity from returns and EO BMF CSVs")
    add_common_db(p)
    p.add_argument("--project-dir", default=None, help="Main project folder containing eo-bmf/")
    p.add_argument("--bmf-dir", default=None, help="Explicit EO BMF directory")
    p.add_argument("--full-refresh", action="store_true", help="Drop and rebuild org_identity")
    p.add_argument("--skip-returns", action="store_true", help="Do not import identity rows from returns")
    p.add_argument("--skip-bmf", action="store_true", help="Do not import EO BMF files")
    p.add_argument("--include-bmf-ico", action="store_true", help="Also index BMF ICO as low-priority alias; off by default")
    p.add_argument("--no-tokens", action="store_true", help="Do not build org_identity_token")
    p.add_argument("--no-fts", action="store_true", help="Do not create/rebuild FTS5 table")
    p.add_argument("--batch-size", type=int, default=10000)
    p.set_defaults(func=cmd_build_identity)

    p = sub.add_parser("build-signatures", help="Build unique hard-case grant recipient signatures")
    add_common_db(p)
    p.add_argument("--full-refresh", action="store_true")
    p.add_argument("--statuses", default="unresolved,conflicting_ein_match,reported_ein_not_found_name_matched,address_unique,address_narrowed_name_match,fuzzy_probable", help="Comma-separated first-pass statuses to queue")
    p.add_argument("--low-confidence-threshold", type=float, default=0.90, help="Queue first-pass rows at or below this confidence")
    p.add_argument("--min-total-amount", type=float, default=None)
    p.add_argument("--state", default=None)
    p.add_argument("--min-grant-id", type=int, default=None)
    p.add_argument("--max-grant-id", type=int, default=None)
    p.add_argument("--limit", type=int, default=None, help="Limit source grant rows scanned")
    p.add_argument("--flush-every", type=int, default=250000)
    p.set_defaults(func=cmd_build_signatures)

    p = sub.add_parser("generate-candidates", help="Generate candidate EINs for signatures from org_identity")
    add_common_db(p)
    p.add_argument("--full-refresh", action="store_true", help="Drop/rebuild candidate table")
    p.add_argument("--regenerate", action="store_true", help="Regenerate candidates even if they already exist")
    p.add_argument("--state", default=None)
    p.add_argument("--min-total-amount", type=float, default=None)
    p.add_argument("--queue-status", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-candidates", type=int, default=20)
    p.add_argument("--min-candidate-score", type=float, default=45.0)
    p.add_argument("--token-limit", type=int, default=200)
    p.add_argument("--no-fts", action="store_true")
    p.add_argument("--commit-every", type=int, default=1000)
    p.set_defaults(func=cmd_generate_candidates)

    p = sub.add_parser("adjudicate", help="Ask local Ollama to adjudicate candidate lists")
    add_common_db(p)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument("--num-predict", type=int, default=500)
    p.add_argument("--full-refresh", action="store_true", help="Drop/rebuild decision table")
    p.add_argument("--regenerate", action="store_true", help="Regenerate decisions even if one exists")
    p.add_argument("--state", default=None)
    p.add_argument("--min-total-amount", type=float, default=None)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--max-candidates", type=int, default=20)
    p.add_argument("--auto-accept-threshold", type=float, default=0.92)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--csv-out", default="ai_grant_decisions.csv")
    p.add_argument("--commit-every", type=int, default=50)
    p.add_argument("--flush-every", type=int, default=10)
    p.add_argument("--progress-every", type=int, default=10)
    p.set_defaults(func=cmd_adjudicate)

    p = sub.add_parser("apply-decisions", help="Apply auto-accepted AI decisions to separate table and final view")
    add_common_db(p)
    p.add_argument("--full-refresh", action="store_true")
    p.add_argument("--min-confidence", type=float, default=0.92)
    p.add_argument("--batch-size", type=int, default=10000)
    p.set_defaults(func=cmd_apply_decisions)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
