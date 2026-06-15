#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
grant_ai_assist_v1_6_prompt_tuned.py

Fast AI-assisted second-pass grant recipient matching for the IRS 990 SQLite database.

Fast v1.5 changes
-----------------
- Defers secondary indexes during full-refresh loads for signatures, candidates, and applied AI matches.
- Uses exclusive SQLite locking for bulk-write commands, but not during Ollama adjudication.
- Raises safe batch/commit defaults for a 32 GB RAM workstation.
- Skips unnecessary per-signature candidate deletes during full-refresh candidate generation.
- Makes candidate generation much faster by defaulting to high-signal exact/name/address/EIN lookups.
- Adds optional balanced/broad candidate modes for token/FTS fallback, with safer geo-constrained token queries.
- Adds a stats command for raw grants, deterministic resolver results, AI signatures/candidates/decisions, and final applied results.
- Optimizes candidate generation by staging candidate counts and bulk-updating signature status instead of updating one signature row at a time.
- Adds Ollama diagnostics, a test-ollama command, fail-fast behavior for repeated Ollama call failures, retries, and format-mode controls.
- v1.6 tunes the adjudication prompt so missing reported EINs and legal suffix differences do not make otherwise strong matches ambiguous.

This script is intended to run AFTER resolve_grant_recipients_v2_1_fast.py has created
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
    irs990.db or DB at C:\projects\irs990-tool\db\irs990.db
    eo-bmf/
      eo1.csv
      eo2.csv
      eo3.csv
      eo4.csv

Common commands
---------------
Verify BMF files:
  python grant_ai_assist_v1_6_prompt_tuned.py verify-bmf --project-dir C:\projects\irs990-tool

Build org_identity from returns + EO BMF:
  python grant_ai_assist_v1_6_prompt_tuned.py build-identity --db C:\projects\irs990-tool\db\irs990.db --project-dir C:\projects\irs990-tool --full-refresh

Build signatures for unresolved and low-confidence deterministic matches:
  python grant_ai_assist_v1_6_prompt_tuned.py build-signatures --db C:\projects\irs990-tool\db\irs990.db --full-refresh

Generate top candidates for those signatures:
  python grant_ai_assist_v1_6_prompt_tuned.py generate-candidates --db C:\projects\irs990-tool\db\irs990.db --limit 100000

Dry-run Ollama adjudication to CSV:
  python grant_ai_assist_v1_6_prompt_tuned.py adjudicate --db C:\projects\irs990-tool\db\irs990.db --model gemma4:12b --limit 100 --dry-run --csv-out ai_decisions_sample.csv

Store Ollama decisions:
  python grant_ai_assist_v1_6_prompt_tuned.py adjudicate --db C:\projects\irs990-tool\db\irs990.db --model gemma4:12b --limit 1000

Apply only auto-accepted AI decisions into a separate applied table and final view:
  python grant_ai_assist_v1_6_prompt_tuned.py apply-decisions --db C:\projects\irs990-tool\db\irs990.db
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

DEFAULT_PROJECT_DIR = os.getenv("IRS_PROJECT_DIR", r"C:\projects\irs990-tool")
DEFAULT_DB = os.getenv("IRS_DB_PATH", str(Path(DEFAULT_PROJECT_DIR) / "db" / "irs990.db"))
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


class OllamaCallError(RuntimeError):
    """Raised when the Ollama endpoint returns an unusable response."""


def _snippet(text: str, limit: int = 1000) -> str:
    text = text or ""
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text[:limit] + ("..." if len(text) > limit else "")

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


def connect(db_path: str, readonly: bool = False, exclusive: bool = False) -> sqlite3.Connection:
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
            if exclusive:
                conn.execute("PRAGMA locking_mode=EXCLUSIVE;")
    except Exception:
        pass
    return conn


def run_index_statements(conn: sqlite3.Connection, statements: Sequence[str], label: str) -> None:
    total = len(statements)
    for i, stmt in enumerate(statements, 1):
        print(f"Creating {label} index {i}/{total}...", flush=True)
        conn.execute(stmt)
        conn.commit()


def analyze_tables(conn: sqlite3.Connection, tables: Sequence[str]) -> None:
    for table in tables:
        try:
            print(f"Analyzing {table}...", flush=True)
            conn.execute(f"ANALYZE {table}")
        except sqlite3.Error as e:
            print(f"ANALYZE skipped for {table}: {e}", flush=True)
    conn.commit()


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
    run_index_statements(conn, statements, "org_identity")
    analyze_tables(conn, [ORG_IDENTITY_TABLE, ORG_TOKEN_TABLE])
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
    conn = connect(args.db, readonly=False, exclusive=True)
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


def create_signature_indexes(conn: sqlite3.Connection) -> None:
    statements = [
        f"CREATE INDEX IF NOT EXISTS idx_sig_state_zip ON {SIG_TABLE}(state, zip5);",
        f"CREATE INDEX IF NOT EXISTS idx_sig_name ON {SIG_TABLE}(recipient_name_norm);",
        f"CREATE INDEX IF NOT EXISTS idx_sig_amount ON {SIG_TABLE}(total_amount DESC);",
        f"CREATE INDEX IF NOT EXISTS idx_sig_queue ON {SIG_TABLE}(ai_queue_status, total_amount DESC);",
        f"CREATE INDEX IF NOT EXISTS idx_sig_grant_grant ON {SIG_GRANT_TABLE}(grant_id);",
    ]
    run_index_statements(conn, statements, "signature")
    analyze_tables(conn, [SIG_TABLE, SIG_GRANT_TABLE])


def create_signature_schema(conn: sqlite3.Connection, full_refresh: bool = False, create_indexes: bool = True) -> None:
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
    """)
    conn.commit()
    if create_indexes:
        create_signature_indexes(conn)


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
    conn = connect(args.db, readonly=False, exclusive=True)
    create_signature_schema(conn, full_refresh=args.full_refresh, create_indexes=not args.full_refresh)
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
    if args.full_refresh:
        print("Bulk signature build complete; creating signature indexes after load...", flush=True)
        create_signature_indexes(conn)
    elapsed = max(1.0, time.time() - started)
    sig_count = conn.execute(f"SELECT COUNT(*) FROM {SIG_TABLE}").fetchone()[0]
    map_count = conn.execute(f"SELECT COUNT(*) FROM {SIG_GRANT_TABLE}").fetchone()[0]
    print(f"Signatures ready: {sig_count:,} signatures, {map_count:,} grant mappings; processed {processed:,} rows at {processed/elapsed:,.0f}/sec", flush=True)


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------


def create_candidate_indexes(conn: sqlite3.Connection) -> None:
    statements = [
        f"CREATE INDEX IF NOT EXISTS idx_ai_cand_sig_rank ON {CAND_TABLE}(signature_hash, candidate_rank);",
        f"CREATE INDEX IF NOT EXISTS idx_ai_cand_ein ON {CAND_TABLE}(ein);",
    ]
    run_index_statements(conn, statements, "candidate")
    analyze_tables(conn, [CAND_TABLE])


def create_candidate_schema(conn: sqlite3.Connection, full_refresh: bool = False, create_indexes: bool = True) -> None:
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
    """)
    conn.commit()
    if create_indexes:
        create_candidate_indexes(conn)


def identity_rows_by_sql(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> List[sqlite3.Row]:
    return list(conn.execute(sql, params))


# Additional very-common nonprofit/name words that make token fallback expensive
# and usually add little identifying power unless paired with a distinctive token.
TOKEN_FALLBACK_STOPWORDS = NAME_STOPWORDS | {
    "SCHOOL", "SCHOOLS", "CENTER", "CENTRE", "UNIVERSITY", "COLLEGE", "ACADEMY",
    "ASSOCIATION", "SOCIETY", "CHURCH", "MINISTRY", "MINISTRIES", "HEALTH", "MEDICAL",
    "HOSPITAL", "CLINIC", "COMMUNITY", "SERVICE", "SERVICES", "PROGRAM", "PROGRAMS",
    "PUBLIC", "AMERICAN", "NATIONAL", "INTERNATIONAL", "LOCAL", "COUNTY", "CITY",
    "FRIENDS", "FAMILY", "FAMILIES", "YOUTH", "CHILD", "CHILDREN", "EDUCATION",
    "EDUCATIONAL", "ART", "ARTS", "MUSEUM", "CLUB", "TRUST", "CHARITABLE",
    "NONPROFIT", "NON", "PROFIT", "RELIEF", "SUPPORT", "DEVELOPMENT", "COUNCIL",
}


def distinctive_name_tokens(name_norm: str, max_tokens: int = 5) -> List[str]:
    """Return tokens useful for candidate fallback lookups.

    The old candidate generator queried org_identity_token for up to eight name
    tokens, including very common words. On a large EO BMF + returns identity
    table, tokens such as CENTER, SCHOOL, COMMUNITY, HEALTH, SERVICES, etc. can
    touch enormous row sets. This helper keeps only more distinctive tokens for
    the expensive fallback stage. If no distinctive token is available, we skip
    token fallback rather than scanning millions of generic token hits.
    """
    out: List[str] = []
    for t in name_tokens(name_norm):
        if t in TOKEN_FALLBACK_STOPWORDS:
            continue
        if len(t) < 4:
            continue
        out.append(t)
        if len(out) >= max_tokens:
            break
    return out


def _add_rows(rows: Dict[int, sqlite3.Row], found: Iterable[sqlite3.Row]) -> None:
    for r in found:
        rows[int(r["identity_id"])] = r


def _unique_ein_count(rows: Dict[int, sqlite3.Row]) -> int:
    return len({digits9(r["ein"]) for r in rows.values() if digits9(r["ein"])})


def get_candidate_identity_rows(
    conn: sqlite3.Connection,
    sig: sqlite3.Row,
    *,
    candidate_mode: str = "fast",
    use_fts: bool = False,
    token_limit: int = 50,
    enough_candidates: int = 8,
) -> List[sqlite3.Row]:
    """Return org_identity rows that might match one recipient signature.

    v1.2 behavior:
      - Always run cheap/high-signal lookups first: reported EIN, exact name +
        address/location, address/location, and exact normalized name.
      - In default `fast` mode, stop there. This is dramatically faster and is
        the right first candidate-generation pass for millions of signatures.
      - `balanced` and `broad` modes add token fallback only if the cheap stage
        did not already find enough distinct EINs. The token fallback is now
        constrained through org_identity_token.state/zip5, so SQLite can use the
        token+geo indexes instead of joining a huge token set to org_identity.
      - `broad` mode can also use FTS, but only as a later fallback.
    """
    rows: Dict[int, sqlite3.Row] = {}
    reported_ein = digits9(sig["reported_ein"])
    name_norm = clean_text(sig["recipient_name_norm"])
    street_norm = clean_text(sig["street_norm"])
    city = clean_text(sig["city"])
    state = clean_text(sig["state"])
    z5 = clean_text(sig["zip5"])
    mode = (candidate_mode or "fast").lower()
    if mode not in {"fast", "balanced", "broad"}:
        mode = "fast"

    queries: List[Tuple[str, Sequence[Any]]] = []
    base_cols = f"SELECT * FROM {ORG_IDENTITY_TABLE} WHERE "

    # Cheap, high-signal lookups. These are backed by org_identity indexes and
    # are normally safe to run for every signature.
    if reported_ein:
        queries.append((base_cols + "ein=? ORDER BY source_rank, tax_year DESC LIMIT 75", [reported_ein]))
    if name_norm and street_norm and z5:
        queries.append((base_cols + "name_norm=? AND street_norm=? AND zip5=? ORDER BY source_rank, tax_year DESC LIMIT 50", [name_norm, street_norm, z5]))
    if name_norm and street_norm and city and state:
        queries.append((base_cols + "name_norm=? AND street_norm=? AND city=? AND state=? ORDER BY source_rank, tax_year DESC LIMIT 50", [name_norm, street_norm, city, state]))
    if name_norm and z5:
        queries.append((base_cols + "name_norm=? AND zip5=? ORDER BY source_rank, tax_year DESC LIMIT 50", [name_norm, z5]))
    if name_norm and city and state:
        queries.append((base_cols + "name_norm=? AND city=? AND state=? ORDER BY source_rank, tax_year DESC LIMIT 50", [name_norm, city, state]))
    if street_norm and z5:
        queries.append((base_cols + "street_norm=? AND zip5=? ORDER BY source_rank, tax_year DESC LIMIT 100", [street_norm, z5]))
    if street_norm and city and state:
        queries.append((base_cols + "street_norm=? AND city=? AND state=? ORDER BY source_rank, tax_year DESC LIMIT 100", [street_norm, city, state]))
    if name_norm and state:
        queries.append((base_cols + "name_norm=? AND state=? ORDER BY source_rank, tax_year DESC LIMIT 75", [name_norm, state]))
    if name_norm:
        queries.append((base_cols + "name_norm=? ORDER BY source_rank, tax_year DESC LIMIT 75", [name_norm]))

    for sql, params in queries:
        _add_rows(rows, identity_rows_by_sql(conn, sql, params))

    if mode == "fast":
        return list(rows.values())

    # Skip expensive fallback if cheap lookups already found a useful candidate
    # set. This prevents common names/addresses from triggering unnecessary
    # token/FTS searches.
    if _unique_ein_count(rows) >= int(enough_candidates):
        return list(rows.values())

    # Token overlap fallback: useful for abbreviations/partial names, but only
    # with distinctive tokens and only with geography when possible.
    toks = distinctive_name_tokens(name_norm, max_tokens=4 if mode == "balanced" else 6)
    if toks and (z5 or state):
        ph = ",".join("?" for _ in toks)
        params: List[Any] = list(toks)
        if z5:
            geo_clause = " AND tok.zip5=?"
            params.append(z5)
        else:
            geo_clause = " AND tok.state=?"
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
            _add_rows(rows, conn.execute(sql, params))
        except sqlite3.Error:
            pass

    if mode != "broad" or not use_fts:
        return list(rows.values())

    if _unique_ein_count(rows) >= int(enough_candidates):
        return list(rows.values())

    # FTS fallback, intentionally last. It can be useful, but it is much more
    # expensive than exact/name/address lookup on very large identity tables.
    if name_norm and table_exists(conn, "org_identity_fts"):
        toks = distinctive_name_tokens(name_norm, max_tokens=5)
        if toks:
            match = " ".join('"' + t.replace('"', '') + '"' for t in toks)
            params: List[Any] = [match]
            geo_clause = ""
            if z5:
                geo_clause = "AND oi.zip5=?"
                params.append(z5)
            elif state:
                geo_clause = "AND oi.state=?"
                params.append(state)
            try:
                sql = f"""
                SELECT oi.*
                FROM org_identity_fts f
                JOIN {ORG_IDENTITY_TABLE} oi ON oi.identity_id = f.rowid
                WHERE org_identity_fts MATCH ? {geo_clause}
                ORDER BY rank
                LIMIT 75
                """
                _add_rows(rows, conn.execute(sql, params))
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


def insert_candidate_rows(
    conn: sqlite3.Connection,
    signature_hash: str,
    candidates: Sequence[CandidateChoice],
    delete_existing: bool = True,
    update_signature: bool = False,
) -> None:
    """Insert candidate rows for one signature.

    v1.4 change: by default this no longer updates grant_recipient_signature.
    Updating that table once per signature became a bottleneck at millions of
    signatures. cmd_generate_candidates stages counts and bulk-updates signature
    status periodically / at the end instead.
    """
    if delete_existing:
        conn.execute(f"DELETE FROM {CAND_TABLE} WHERE signature_hash=?", (signature_hash,))
    rows = []
    ts = now_stamp()
    for i, c in enumerate(candidates, 1):
        rows.append((
            signature_hash, f"C{i}", i, c.identity_id, c.ein, c.candidate_name, c.source, c.source_rank,
            c.street, c.city, c.state, c.zip5, c.name_score, c.address_score, c.zip_match,
            c.city_state_match, c.state_match, c.exact_name, c.exact_address, c.reported_ein_match,
            c.candidate_score, ";".join(c.reasons), ts,
        ))
    if rows:
        conn.executemany(f"""
            INSERT INTO {CAND_TABLE} (
              signature_hash, candidate_id, candidate_rank, identity_id, ein, candidate_name, source, source_rank,
              street, city, state, zip5, name_score, address_score, zip_match, city_state_match, state_match,
              exact_name, exact_address, reported_ein_match, candidate_score, candidate_reason, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
    if update_signature:
        conn.execute(f"UPDATE {SIG_TABLE} SET candidate_count=?, ai_queue_status=CASE WHEN ? > 0 THEN 'candidates_ready' ELSE 'no_candidates' END, updated_at=? WHERE signature_hash=?",
                     (len(candidates), len(candidates), ts, signature_hash))


def create_candidate_count_stage(conn: sqlite3.Connection) -> None:
    """Create temp staging table used to bulk-update signature candidate status.

    This table lives only for the current SQLite connection. It lets candidate
    generation process millions of signatures without issuing millions of
    UPDATE statements against grant_recipient_signature.
    """
    conn.executescript("""
    DROP TABLE IF EXISTS temp.tmp_ai_candidate_counts;
    CREATE TEMP TABLE tmp_ai_candidate_counts (
      signature_hash TEXT PRIMARY KEY,
      candidate_count INTEGER NOT NULL
    );
    """)


def stage_candidate_counts(conn: sqlite3.Connection, rows: Sequence[Tuple[str, int]]) -> None:
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO temp.tmp_ai_candidate_counts(signature_hash, candidate_count) VALUES (?,?)",
        rows,
    )


def bulk_update_signature_candidate_status(conn: sqlite3.Connection, label: str = "candidate status") -> int:
    """Bulk-update candidate_count / ai_queue_status for staged signatures.

    Returns the number of staged signatures updated. The temp rows are deleted
    after the update so this can be called periodically without letting the temp
    table grow without bound.
    """
    row = conn.execute("SELECT COUNT(*) AS n FROM temp.tmp_ai_candidate_counts").fetchone()
    n = int(row["n"] if row is not None else 0)
    if n <= 0:
        return 0
    ts = now_stamp()
    print(f"Bulk-updating {label} for {n:,} signatures...", flush=True)
    conn.execute(f"""
        UPDATE {SIG_TABLE}
        SET candidate_count = COALESCE((
              SELECT t.candidate_count
              FROM temp.tmp_ai_candidate_counts t
              WHERE t.signature_hash = {SIG_TABLE}.signature_hash
            ), 0),
            ai_queue_status = CASE
              WHEN COALESCE((
                SELECT t.candidate_count
                FROM temp.tmp_ai_candidate_counts t
                WHERE t.signature_hash = {SIG_TABLE}.signature_hash
              ), 0) > 0 THEN 'candidates_ready'
              ELSE 'no_candidates'
            END,
            updated_at = ?
        WHERE signature_hash IN (SELECT signature_hash FROM temp.tmp_ai_candidate_counts)
    """, (ts,))
    conn.execute("DELETE FROM temp.tmp_ai_candidate_counts")
    return n

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
    conn = connect(args.db, readonly=False, exclusive=True)
    if not table_exists(conn, ORG_IDENTITY_TABLE):
        raise RuntimeError(f"Missing {ORG_IDENTITY_TABLE}. Run build-identity first.")
    if not table_exists(conn, SIG_TABLE):
        raise RuntimeError(f"Missing {SIG_TABLE}. Run build-signatures first.")
    create_candidate_schema(conn, full_refresh=args.full_refresh, create_indexes=not args.full_refresh)
    create_candidate_count_stage(conn)
    delete_existing = not args.full_refresh
    processed = 0
    with_candidates = 0
    staged_count_rows: List[Tuple[str, int]] = []
    started = time.time()
    mode = getattr(args, "candidate_mode", "fast")
    use_fts = bool(mode == "broad" and not args.no_fts)
    print(f"Candidate generation mode: {mode} (token fallback {'on' if mode in ('balanced','broad') else 'off'}, FTS {'on' if use_fts else 'off'})", flush=True)
    print(
        "v1.4 optimization: candidate counts are staged and bulk-updated; "
        "signature status will update in batches instead of once per signature.",
        flush=True,
    )
    if args.full_refresh and (args.limit or args.state or args.min_total_amount is not None or args.queue_status):
        print(
            "Note: --full-refresh drops the entire candidate table, but your filters/limit process only a subset of signatures. "
            "Only processed signatures will have candidate_count/ai_queue_status refreshed.",
            flush=True,
        )

    status_update_every = max(int(getattr(args, "status_update_every", 0) or 0), 0)
    for sig in iter_signatures_for_candidates(conn, args):
        identity_rows = get_candidate_identity_rows(
            conn,
            sig,
            candidate_mode=mode,
            use_fts=use_fts,
            token_limit=args.token_limit,
            enough_candidates=args.enough_candidates,
        )
        candidates = best_candidates_by_ein(sig, identity_rows, args.max_candidates, args.min_candidate_score)
        insert_candidate_rows(conn, sig["signature_hash"], candidates, delete_existing=delete_existing, update_signature=False)
        processed += 1
        cand_count = len(candidates)
        staged_count_rows.append((sig["signature_hash"], cand_count))
        if candidates:
            with_candidates += 1
        if processed % args.commit_every == 0:
            stage_candidate_counts(conn, staged_count_rows)
            staged_count_rows.clear()
            # For long non-full-refresh targeted passes, periodic bulk status updates
            # make progress visible without returning to per-row UPDATE behavior.
            if status_update_every and processed % status_update_every == 0:
                bulk_update_signature_candidate_status(conn, "candidate status progress")
            conn.commit()
            elapsed = max(1.0, time.time() - started)
            print(f"Generated candidates for {processed:,} signatures; {with_candidates:,} have candidates; {processed/elapsed:,.0f}/sec", flush=True)
    if staged_count_rows:
        stage_candidate_counts(conn, staged_count_rows)
        staged_count_rows.clear()
    conn.commit()

    # This is the v1.4 speedup: one or a few set-based UPDATEs instead of one
    # UPDATE per processed signature.
    bulk_update_signature_candidate_status(conn, "final candidate status")
    conn.commit()

    if args.full_refresh:
        print("Candidate generation complete; creating candidate indexes after load...", flush=True)
        create_candidate_indexes(conn)
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
            "A blank or missing reported EIN is normal in grant schedules, especially 990-PF filings. Do not mark a case ambiguous merely because reported_ein is blank.",
            "A known reported EIN should be kept unless name/address evidence strongly contradicts it.",
            "Treat legal/entity words and punctuation as weak evidence: THE, INC, INCORPORATED, CORP, LLC, FOUNDATION, FUND, COMPANY, CO, LTD, ASSOCIATION. Their presence/absence should not block a match when the core name and location agree.",
            "If one candidate has exact_name=true, exact_address=true, zip_match=true, and candidate_score >= 95, choose SELECT_CANDIDATE with high confidence unless another candidate has similarly strong evidence or a reported EIN conflict exists.",
            "If one candidate has exact_name=true and either zip_match=true or city_state_match=true, it is usually enough to choose that candidate when alternatives are clearly weaker.",
            "Prefer exact address plus ZIP and strong name evidence over broad name-only similarity.",
            "Return AMBIGUOUS or HUMAN_REVIEW only when multiple candidates are genuinely plausible, evidence conflicts, or the best candidate is weak.",
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


def call_ollama(
    input_obj: Dict[str, Any],
    model: str,
    url: str,
    timeout: int,
    num_ctx: int,
    num_predict: int,
    format_mode: str = "schema",
    debug_raw_path: Optional[str] = None,
) -> Dict[str, Any]:
    system_msg = """
You are a careful nonprofit identity matching adjudicator.
You receive one grant-recipient record and a candidate list generated by a database.
Your job is to choose the correct candidate when the evidence is strong, not to require absolute certainty.
Return only JSON that follows the provided schema.
Never invent an EIN or candidate ID.
A blank reported EIN is common in grant schedules and is not by itself a reason for ambiguity.
Legal suffix/noise differences such as INC, FOUNDATION, FUND, THE, LLC, CORP, CO, LTD, ASSOCIATION, punctuation, and spacing are weak evidence and should not block a match when core name plus address/location agree.
If one candidate has exact name/address/ZIP evidence and no similarly strong alternative, return SELECT_CANDIDATE with high confidence and needs_human_review=false.
Return AMBIGUOUS or HUMAN_REVIEW only when evidence is genuinely conflicting, weak, or there are multiple similarly plausible candidates.
Precision is more important than recall: a wrong EIN is worse than no match, but do not overuse HUMAN_REVIEW for obvious exact matches.
""".strip()
    user_msg = json.dumps(input_obj, ensure_ascii=False, sort_keys=True)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0,
            "top_p": 0.9,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    fmt = (format_mode or "schema").lower().strip()
    if fmt == "schema":
        payload["format"] = AI_DECISION_SCHEMA
    elif fmt == "json":
        payload["format"] = "json"
    elif fmt == "none":
        # Keep the prompt instruction but do not use Ollama's format parameter.
        pass
    else:
        raise ValueError(f"Unknown Ollama format mode: {format_mode!r}")

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw_err = ""
        try:
            raw_err = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise OllamaCallError(f"HTTP {e.code} from Ollama endpoint; response={_snippet(raw_err)}") from e

    if debug_raw_path:
        try:
            with open(debug_raw_path, "a", encoding="utf-8") as fh:
                fh.write("\n\n--- OLLAMA RAW RESPONSE ---\n")
                fh.write(f"timestamp={now_stamp()} status={status} url={url} model={model} format_mode={fmt}\n")
                fh.write(raw)
                fh.write("\n--- END RAW RESPONSE ---\n")
        except Exception:
            pass

    if not raw or not raw.strip():
        raise OllamaCallError("Empty response body from Ollama endpoint")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise OllamaCallError(f"Non-JSON response from Ollama endpoint; first chars={_snippet(raw)}") from e

    if isinstance(data, dict) and data.get("error"):
        raise OllamaCallError(f"Ollama error: {data.get('error')}; response={_snippet(raw)}")

    content = ""
    if isinstance(data.get("message"), dict):
        content = data["message"].get("content") or ""
    if not content:
        content = data.get("response") or data.get("content") or ""
    content = clean_text(content)
    if not content:
        keys = sorted(data.keys()) if isinstance(data, dict) else []
        raise OllamaCallError(f"Ollama JSON response had no assistant content; keys={keys}; response={_snippet(raw)}")

    try:
        return json.loads(content)
    except json.JSONDecodeError as first_error:
        # tolerate accidental fenced JSON or extra text around JSON
        m = re.search(r"\{.*\}", content, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        raise OllamaCallError(f"Assistant content was not valid JSON; first chars={_snippet(content)}") from first_error


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
    call_failures = 0
    consecutive_call_failures = 0
    started = time.time()
    try:
        for sig in iter_signatures_for_adjudication(conn, args):
            cands = candidates_for_signature(conn, sig["signature_hash"], args.max_candidates)
            if not cands:
                continue
            input_obj = build_ai_input(sig, cands)
            last_error: Optional[BaseException] = None
            output: Dict[str, Any] = {}
            for attempt in range(max(0, args.ollama_retries) + 1):
                try:
                    output = call_ollama(
                        input_obj,
                        model=args.model,
                        url=args.ollama_url,
                        timeout=args.timeout,
                        num_ctx=args.num_ctx,
                        num_predict=args.num_predict,
                        format_mode=args.format_mode,
                        debug_raw_path=args.debug_raw_out,
                    )
                    last_error = None
                    break
                except (urllib.error.URLError, TimeoutError, OllamaCallError, Exception) as e:
                    last_error = e
                    if attempt < max(0, args.ollama_retries):
                        time.sleep(max(0.0, args.retry_sleep))
            had_call_error = last_error is not None
            if had_call_error:
                call_failures += 1
                consecutive_call_failures += 1
                output = {
                    "decision": "HUMAN_REVIEW",
                    "candidate_id": "",
                    "confidence": 0,
                    "confidence_label": "none",
                    "reason_codes": ["ollama_call_failed"],
                    "explanation": f"Ollama call failed: {type(last_error).__name__}: {last_error}",
                    "needs_human_review": True,
                }
            else:
                consecutive_call_failures = 0
            validation = validate_ai_output(output, cands, sig, args.auto_accept_threshold)
            if had_call_error:
                validation["validation_status"] = "ollama_error"
                validation["validation_error"] = clean_text(output.get("explanation"))[:1000]
                validation["auto_accept"] = 0
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
            if had_call_error:
                print(f"Ollama call failure {call_failures:,} total / {consecutive_call_failures:,} consecutive: {validation['validation_error']}", flush=True)
                if args.fail_fast or (args.max_call_failures and call_failures >= args.max_call_failures) or (args.max_consecutive_call_failures and consecutive_call_failures >= args.max_consecutive_call_failures):
                    if writer is None:
                        conn.commit()
                    raise RuntimeError(
                        f"Stopping adjudication after Ollama failures: total={call_failures}, consecutive={consecutive_call_failures}. "
                        "Fix --ollama-url/model/format-mode or use --max-call-failures 0 to disable this stop."
                    )
            if processed % args.progress_every == 0:
                elapsed = max(1.0, time.time() - started)
                print(f"Adjudicated {processed:,} signatures at {processed/elapsed:,.2f}/sec; call_failures={call_failures:,}", flush=True)
        if writer is None:
            conn.commit()
    finally:
        if out_fh is not None:
            out_fh.flush()
            out_fh.close()
    print(f"AI adjudication complete: {processed:,} signatures", flush=True)



# ---------------------------------------------------------------------------
# Ollama test / diagnostics
# ---------------------------------------------------------------------------


def cmd_test_ollama(args: argparse.Namespace) -> None:
    """Send one tiny structured adjudication request to Ollama and print diagnostics."""
    input_obj = {
        "task": "Choose the correct nonprofit EIN for this test grant recipient.",
        "rules": [
            "Choose only from provided candidates.",
            "Return JSON only.",
            "This is an exact core-name, exact address, exact ZIP test. Missing reported_ein is expected and must not make the answer ambiguous.",
            "The correct behavior is SELECT_CANDIDATE with candidate_id C1, high confidence, and needs_human_review=false.",
        ],
        "grant_recipient_signature": {
            "signature_hash": "SIG_TEST",
            "reported_ein": "",
            "name": "OREGON FOOD BANK",
            "name_norm": "OREGON FOOD BANK",
            "street": "7900 NE 33RD DR",
            "street_norm": "7900 NE 33RD DR",
            "city": "PORTLAND",
            "state": "OR",
            "zip5": "97211",
            "country": "US",
            "grant_count": 1,
            "total_amount": 1000,
            "sample_purpose": "GENERAL SUPPORT",
            "sample_grantor_ein": "000000000",
            "sample_grantor_name": "TEST GRANTOR",
        },
        "first_pass": {
            "statuses": {"unresolved": 1},
            "methods": {"none": 1},
            "warning_flags": "",
            "min_confidence": 0,
            "max_confidence": 0,
            "avg_confidence": 0,
        },
        "candidates": [
            {
                "candidate_id": "C1",
                "ein": "930782152",
                "name": "OREGON FOOD BANK INC",
                "source": "test",
                "street": "7900 NE 33RD DR",
                "city": "PORTLAND",
                "state": "OR",
                "zip5": "97211",
                "name_score": 1.0,
                "address_score": 1.0,
                "zip_match": True,
                "city_state_match": True,
                "state_match": True,
                "exact_name": True,
                "exact_address": True,
                "reported_ein_match": False,
                "candidate_score": 99.0,
                "candidate_reason": "test exact name/address/zip candidate",
            }
        ],
    }
    print(f"Testing Ollama endpoint: {args.ollama_url}", flush=True)
    print(f"Model: {args.model}; format mode: {args.format_mode}; timeout: {args.timeout}s", flush=True)
    started = time.time()
    output = call_ollama(
        input_obj,
        model=args.model,
        url=args.ollama_url,
        timeout=args.timeout,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
        format_mode=args.format_mode,
        debug_raw_path=args.debug_raw_out,
    )
    elapsed = time.time() - started
    print(f"Ollama call succeeded in {elapsed:,.2f} seconds.", flush=True)
    print(json.dumps(output, indent=2, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# Apply decisions / final view
# ---------------------------------------------------------------------------


def create_applied_indexes(conn: sqlite3.Connection) -> None:
    statements = [
        f"CREATE INDEX IF NOT EXISTS idx_ai_applied_selected_ein ON {APPLIED_TABLE}(selected_ein);",
        f"CREATE INDEX IF NOT EXISTS idx_ai_applied_sig ON {APPLIED_TABLE}(signature_hash);",
    ]
    run_index_statements(conn, statements, "applied")
    analyze_tables(conn, [APPLIED_TABLE])


def refresh_final_view(conn: sqlite3.Connection) -> None:
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


def create_applied_schema_and_view(conn: sqlite3.Connection, full_refresh: bool = False, create_indexes: bool = True, create_view: bool = True) -> None:
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
    """)
    conn.commit()
    if create_indexes:
        create_applied_indexes(conn)
    if create_view:
        refresh_final_view(conn)


def cmd_apply_decisions(args: argparse.Namespace) -> None:
    conn = connect(args.db, readonly=False, exclusive=True)
    if not table_exists(conn, DECISION_TABLE):
        raise RuntimeError(f"Missing {DECISION_TABLE}. Run adjudicate first.")
    if args.full_refresh:
        print("Full refresh: deferring applied-match indexes and final view until after bulk apply...", flush=True)
    create_applied_schema_and_view(conn, full_refresh=args.full_refresh, create_indexes=not args.full_refresh, create_view=not args.full_refresh)
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
    if args.full_refresh:
        print("Bulk apply complete; creating applied-match indexes and final view...", flush=True)
        create_applied_indexes(conn)
        refresh_final_view(conn)
    print(f"Applied {count:,} grant-level AI matches into {APPLIED_TABLE}; final view is {FINAL_VIEW}", flush=True)



# ---------------------------------------------------------------------------
# Stats / progress reporting
# ---------------------------------------------------------------------------


def _scalar(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return row[0]
    return row[0]


def _safe_count(conn: sqlite3.Connection, table: str) -> Optional[int]:
    if not table_exists(conn, table):
        return None
    return int(_scalar(conn, f"SELECT COUNT(*) FROM {table}") or 0)


def _fmt_num(x: Any) -> str:
    if x is None:
        return ""
    try:
        if isinstance(x, float):
            return f"{x:,.2f}"
        return f"{int(x):,}"
    except Exception:
        return str(x)


def _pct(part: Any, total: Any) -> Optional[float]:
    try:
        part_f = float(part or 0)
        total_f = float(total or 0)
        if total_f == 0:
            return None
        return round(100.0 * part_f / total_f, 2)
    except Exception:
        return None


def _add_stat(rows: List[Dict[str, Any]], section: str, metric: str, bucket: str = "",
              count: Any = None, total_amount: Any = None, pct_of_grants: Any = None,
              pct_of_section: Any = None, signatures: Any = None, grants_represented: Any = None,
              notes: str = "") -> None:
    rows.append({
        "section": section,
        "metric": metric,
        "bucket": bucket or "",
        "count": int(count) if isinstance(count, bool) is False and count is not None and str(count).replace('.', '', 1).isdigit() else count,
        "signatures": signatures,
        "grants_represented": grants_represented,
        "total_amount": round(float(total_amount), 2) if total_amount not in (None, "") else total_amount,
        "pct_of_grants": pct_of_grants,
        "pct_of_section": pct_of_section,
        "notes": notes,
    })


def _money_expr(prefix: str = "") -> str:
    pfx = prefix + "." if prefix else ""
    return f"COALESCE({pfx}cash_amount,0)+COALESCE({pfx}noncash_amount,0)"


def _raw_grants_money_expr() -> str:
    return "COALESCE(cash_grant_amt,0)+COALESCE(non_cash_assistance_amt,0)"


def _confidence_bucket_expr(col: str) -> str:
    return f"""
    CASE
      WHEN {col} IS NULL THEN 'missing'
      WHEN {col} = 0 THEN '0'
      WHEN {col} < 0.50 THEN '0.01-0.49'
      WHEN {col} < 0.70 THEN '0.50-0.69'
      WHEN {col} < 0.85 THEN '0.70-0.84'
      WHEN {col} < 0.90 THEN '0.85-0.89'
      WHEN {col} < 0.92 THEN '0.90-0.919'
      WHEN {col} < 0.95 THEN '0.92-0.949'
      ELSE '0.95-1.00'
    END
    """


def _candidate_count_bucket_expr(col: str = "candidate_count") -> str:
    return f"""
    CASE
      WHEN {col} IS NULL OR {col}=0 THEN '0'
      WHEN {col}=1 THEN '1'
      WHEN {col} BETWEEN 2 AND 5 THEN '2-5'
      WHEN {col} BETWEEN 6 AND 10 THEN '6-10'
      WHEN {col} BETWEEN 11 AND 20 THEN '11-20'
      ELSE '21+'
    END
    """


def _has_table_or_note(conn: sqlite3.Connection, rows: List[Dict[str, Any]], table: str, section: str) -> bool:
    if table_exists(conn, table):
        return True
    _add_stat(rows, section, "table_missing", table, notes=f"{table} has not been created yet")
    return False


def _top_group_rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
    return list(conn.execute(sql, params))


def collect_stats(conn: sqlite3.Connection, top_n: int = 50, include_final_view: bool = True) -> List[Dict[str, Any]]:
    """Collect grant matching pipeline statistics.

    The report is intentionally tolerant of partially completed pipelines: if a
    table has not been created yet, the relevant section reports a missing table
    instead of failing.
    """
    stats: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Raw grants table
    # ------------------------------------------------------------------
    if _has_table_or_note(conn, stats, "grants", "raw_grants"):
        total_grants = int(_scalar(conn, "SELECT COUNT(*) FROM grants") or 0)
        total_amount = _scalar(conn, f"SELECT SUM({_raw_grants_money_expr()}) FROM grants") or 0
        _add_stat(stats, "raw_grants", "total_grants", count=total_grants, total_amount=total_amount, pct_of_grants=100.0)

        blank_sql = "recipient_ein IS NULL OR TRIM(CAST(recipient_ein AS TEXT))=''"
        blank = int(_scalar(conn, f"SELECT COUNT(*) FROM grants WHERE {blank_sql}") or 0)
        nonblank = total_grants - blank
        _add_stat(stats, "raw_grants", "reported_recipient_ein", "blank", count=blank, pct_of_grants=_pct(blank, total_grants))
        _add_stat(stats, "raw_grants", "reported_recipient_ein", "nonblank", count=nonblank, pct_of_grants=_pct(nonblank, total_grants))

        for r in _top_group_rows(conn, f"""
            SELECT COALESCE(NULLIF(TRIM(CAST(g.recipient_ein AS TEXT)),''),'(blank)') AS bucket,
                   COUNT(*) AS n,
                   SUM({_raw_grants_money_expr()}) AS amt
            FROM grants g
            GROUP BY bucket
            ORDER BY n DESC
            LIMIT ?
        """, (min(top_n, 25),)):
            # This top-EIN list is mostly useful for spotting blanks/placeholders.
            _add_stat(stats, "raw_grants", "top_reported_recipient_ein_values", r["bucket"], count=r["n"], total_amount=r["amt"], pct_of_grants=_pct(r["n"], total_grants))

        if table_exists(conn, "returns"):
            for r in _top_group_rows(conn, f"""
                SELECT COALESCE(r.return_type,'(missing)') AS bucket,
                       COUNT(*) AS n,
                       SUM({_raw_grants_money_expr()}) AS amt
                FROM grants g
                LEFT JOIN returns r ON r.filing_id = g.filing_id
                GROUP BY bucket
                ORDER BY n DESC
            """):
                _add_stat(stats, "raw_grants", "grant_rows_by_filer_return_type", r["bucket"], count=r["n"], total_amount=r["amt"], pct_of_grants=_pct(r["n"], total_grants))
    else:
        total_grants = None

    # ------------------------------------------------------------------
    # Deterministic first-pass resolver
    # ------------------------------------------------------------------
    if _has_table_or_note(conn, stats, RESOLVED_TABLE, "deterministic_resolver"):
        det_total = int(_scalar(conn, f"SELECT COUNT(*) FROM {RESOLVED_TABLE}") or 0)
        det_amt = _scalar(conn, f"SELECT SUM(total_amount) FROM {RESOLVED_TABLE}") or 0
        _add_stat(stats, "deterministic_resolver", "total_rows", count=det_total, total_amount=det_amt, pct_of_grants=_pct(det_total, total_grants))

        resolved_cond = "resolved_ein IS NOT NULL AND TRIM(CAST(resolved_ein AS TEXT))<>''"
        reported_blank_cond = "recipient_reported_ein IS NULL OR TRIM(CAST(recipient_reported_ein AS TEXT))=''"
        det_resolved = int(_scalar(conn, f"SELECT COUNT(*) FROM {RESOLVED_TABLE} WHERE {resolved_cond}") or 0)
        det_unresolved = det_total - det_resolved
        _add_stat(stats, "deterministic_resolver", "resolved_ein", "nonblank", count=det_resolved, pct_of_grants=_pct(det_resolved, total_grants), pct_of_section=_pct(det_resolved, det_total))
        _add_stat(stats, "deterministic_resolver", "resolved_ein", "blank_unresolved", count=det_unresolved, pct_of_grants=_pct(det_unresolved, total_grants), pct_of_section=_pct(det_unresolved, det_total))

        for label, cond in [
            ("reported_blank_and_resolved", f"({reported_blank_cond}) AND ({resolved_cond})"),
            ("reported_blank_and_unresolved", f"({reported_blank_cond}) AND NOT ({resolved_cond})"),
            ("reported_nonblank_and_resolved", f"NOT ({reported_blank_cond}) AND ({resolved_cond})"),
            ("reported_nonblank_and_unresolved", f"NOT ({reported_blank_cond}) AND NOT ({resolved_cond})"),
        ]:
            n = int(_scalar(conn, f"SELECT COUNT(*) FROM {RESOLVED_TABLE} WHERE {cond}") or 0)
            amt = _scalar(conn, f"SELECT SUM(total_amount) FROM {RESOLVED_TABLE} WHERE {cond}") or 0
            _add_stat(stats, "deterministic_resolver", "reported_ein_vs_resolved", label, count=n, total_amount=amt, pct_of_grants=_pct(n, total_grants), pct_of_section=_pct(n, det_total))

        for r in _top_group_rows(conn, f"""
            SELECT COALESCE(match_status,'(missing)') AS bucket,
                   COUNT(*) AS n,
                   SUM(total_amount) AS amt,
                   SUM(CASE WHEN {resolved_cond} THEN 1 ELSE 0 END) AS resolved_n
            FROM {RESOLVED_TABLE}
            GROUP BY bucket
            ORDER BY n DESC
            LIMIT ?
        """, (top_n,)):
            _add_stat(stats, "deterministic_resolver", "match_status", r["bucket"], count=r["n"], total_amount=r["amt"], pct_of_grants=_pct(r["n"], total_grants), pct_of_section=_pct(r["n"], det_total), notes=f"resolved_rows={r['resolved_n']}")

        for r in _top_group_rows(conn, f"""
            SELECT COALESCE(match_method,'(missing)') AS bucket,
                   COUNT(*) AS n,
                   SUM(total_amount) AS amt
            FROM {RESOLVED_TABLE}
            GROUP BY bucket
            ORDER BY n DESC
            LIMIT ?
        """, (top_n,)):
            _add_stat(stats, "deterministic_resolver", "match_method", r["bucket"], count=r["n"], total_amount=r["amt"], pct_of_grants=_pct(r["n"], total_grants), pct_of_section=_pct(r["n"], det_total))

        bucket = _confidence_bucket_expr("confidence")
        for r in _top_group_rows(conn, f"""
            SELECT {bucket} AS bucket, COUNT(*) AS n, SUM(total_amount) AS amt
            FROM {RESOLVED_TABLE}
            GROUP BY bucket
            ORDER BY CASE bucket
              WHEN 'missing' THEN 0 WHEN '0' THEN 1 WHEN '0.01-0.49' THEN 2 WHEN '0.50-0.69' THEN 3
              WHEN '0.70-0.84' THEN 4 WHEN '0.85-0.89' THEN 5 WHEN '0.90-0.919' THEN 6
              WHEN '0.92-0.949' THEN 7 ELSE 8 END
        """):
            _add_stat(stats, "deterministic_resolver", "confidence_bucket", r["bucket"], count=r["n"], total_amount=r["amt"], pct_of_grants=_pct(r["n"], total_grants), pct_of_section=_pct(r["n"], det_total))

        for label, cond in [
            ("confidence_lt_0_70", "confidence < 0.70"),
            ("confidence_lt_0_85", "confidence < 0.85"),
            ("confidence_lt_0_90", "confidence < 0.90"),
            ("confidence_lt_0_92", "confidence < 0.92"),
            ("warnings_present", "warning_flags IS NOT NULL AND TRIM(warning_flags)<>''"),
            ("unresolved_or_low_confidence_or_warning", f"NOT ({resolved_cond}) OR confidence < 0.92 OR (warning_flags IS NOT NULL AND TRIM(warning_flags)<>'')"),
        ]:
            n = int(_scalar(conn, f"SELECT COUNT(*) FROM {RESOLVED_TABLE} WHERE {cond}") or 0)
            amt = _scalar(conn, f"SELECT SUM(total_amount) FROM {RESOLVED_TABLE} WHERE {cond}") or 0
            _add_stat(stats, "deterministic_resolver", "review_pool", label, count=n, total_amount=amt, pct_of_grants=_pct(n, total_grants), pct_of_section=_pct(n, det_total))

        for r in _top_group_rows(conn, f"""
            SELECT COALESCE(NULLIF(TRIM(warning_flags),''),'(none)') AS bucket,
                   COUNT(*) AS n,
                   SUM(total_amount) AS amt
            FROM {RESOLVED_TABLE}
            GROUP BY bucket
            ORDER BY n DESC
            LIMIT ?
        """, (top_n,)):
            _add_stat(stats, "deterministic_resolver", "warning_flags_string", r["bucket"], count=r["n"], total_amount=r["amt"], pct_of_grants=_pct(r["n"], total_grants), pct_of_section=_pct(r["n"], det_total))

    # ------------------------------------------------------------------
    # Organization identity layer
    # ------------------------------------------------------------------
    if table_exists(conn, ORG_IDENTITY_TABLE):
        n = int(_scalar(conn, f"SELECT COUNT(*) FROM {ORG_IDENTITY_TABLE}") or 0)
        distinct_eins = int(_scalar(conn, f"SELECT COUNT(DISTINCT ein) FROM {ORG_IDENTITY_TABLE}") or 0)
        _add_stat(stats, "org_identity", "identity_rows", count=n)
        _add_stat(stats, "org_identity", "distinct_eins", count=distinct_eins)
        for r in _top_group_rows(conn, f"""
            SELECT source AS bucket, COUNT(*) AS n, COUNT(DISTINCT ein) AS distinct_eins
            FROM {ORG_IDENTITY_TABLE}
            GROUP BY source
            ORDER BY n DESC
            LIMIT ?
        """, (top_n,)):
            _add_stat(stats, "org_identity", "source", r["bucket"], count=r["n"], notes=f"distinct_eins={r['distinct_eins']}")
    else:
        _add_stat(stats, "org_identity", "table_missing", ORG_IDENTITY_TABLE, notes="Run build-identity")

    if table_exists(conn, ORG_TOKEN_TABLE):
        _add_stat(stats, "org_identity", "token_rows", count=int(_scalar(conn, f"SELECT COUNT(*) FROM {ORG_TOKEN_TABLE}") or 0))
    if table_exists(conn, f"{ORG_IDENTITY_TABLE}_fts"):
        # FTS row count is often equal-ish to indexed docs; this is cheap enough.
        try:
            _add_stat(stats, "org_identity", "fts_rows", count=int(_scalar(conn, f"SELECT COUNT(*) FROM {ORG_IDENTITY_TABLE}_fts") or 0))
        except sqlite3.Error as e:
            _add_stat(stats, "org_identity", "fts_rows", notes=f"could not count FTS rows: {e}")

    # ------------------------------------------------------------------
    # Signatures / AI work queue
    # ------------------------------------------------------------------
    if _has_table_or_note(conn, stats, SIG_TABLE, "signatures"):
        sig_total = int(_scalar(conn, f"SELECT COUNT(*) FROM {SIG_TABLE}") or 0)
        sig_grants = int(_scalar(conn, f"SELECT COALESCE(SUM(grant_count),0) FROM {SIG_TABLE}") or 0)
        sig_amt = _scalar(conn, f"SELECT COALESCE(SUM(total_amount),0) FROM {SIG_TABLE}") or 0
        _add_stat(stats, "signatures", "total_signatures", count=sig_total, grants_represented=sig_grants, total_amount=sig_amt, pct_of_grants=_pct(sig_grants, total_grants))

        for r in _top_group_rows(conn, f"""
            SELECT COALESCE(ai_queue_status,'(missing)') AS bucket,
                   COUNT(*) AS sigs,
                   SUM(grant_count) AS grants,
                   SUM(total_amount) AS amt
            FROM {SIG_TABLE}
            GROUP BY bucket
            ORDER BY sigs DESC
            LIMIT ?
        """, (top_n,)):
            _add_stat(stats, "signatures", "ai_queue_status", r["bucket"], signatures=r["sigs"], grants_represented=r["grants"], total_amount=r["amt"], pct_of_section=_pct(r["sigs"], sig_total), pct_of_grants=_pct(r["grants"], total_grants))

        bucket = _candidate_count_bucket_expr("candidate_count")
        for r in _top_group_rows(conn, f"""
            SELECT {bucket} AS bucket,
                   COUNT(*) AS sigs,
                   SUM(grant_count) AS grants,
                   SUM(total_amount) AS amt
            FROM {SIG_TABLE}
            GROUP BY bucket
            ORDER BY CASE bucket WHEN '0' THEN 0 WHEN '1' THEN 1 WHEN '2-5' THEN 2 WHEN '6-10' THEN 3 WHEN '11-20' THEN 4 ELSE 5 END
        """):
            _add_stat(stats, "signatures", "candidate_count_bucket", r["bucket"], signatures=r["sigs"], grants_represented=r["grants"], total_amount=r["amt"], pct_of_section=_pct(r["sigs"], sig_total), pct_of_grants=_pct(r["grants"], total_grants))

        bucket = _confidence_bucket_expr("first_pass_avg_confidence")
        for r in _top_group_rows(conn, f"""
            SELECT {bucket} AS bucket,
                   COUNT(*) AS sigs,
                   SUM(grant_count) AS grants,
                   SUM(total_amount) AS amt
            FROM {SIG_TABLE}
            GROUP BY bucket
            ORDER BY CASE bucket
              WHEN 'missing' THEN 0 WHEN '0' THEN 1 WHEN '0.01-0.49' THEN 2 WHEN '0.50-0.69' THEN 3
              WHEN '0.70-0.84' THEN 4 WHEN '0.85-0.89' THEN 5 WHEN '0.90-0.919' THEN 6
              WHEN '0.92-0.949' THEN 7 ELSE 8 END
        """):
            _add_stat(stats, "signatures", "first_pass_avg_confidence_bucket", r["bucket"], signatures=r["sigs"], grants_represented=r["grants"], total_amount=r["amt"], pct_of_section=_pct(r["sigs"], sig_total), pct_of_grants=_pct(r["grants"], total_grants))

        for r in _top_group_rows(conn, f"""
            SELECT COALESCE(queued_reason,'(missing)') AS bucket,
                   COUNT(*) AS sigs,
                   SUM(grant_count) AS grants,
                   SUM(total_amount) AS amt
            FROM {SIG_TABLE}
            GROUP BY bucket
            ORDER BY sigs DESC
            LIMIT ?
        """, (top_n,)):
            _add_stat(stats, "signatures", "queued_reason", r["bucket"], signatures=r["sigs"], grants_represented=r["grants"], total_amount=r["amt"], pct_of_section=_pct(r["sigs"], sig_total), pct_of_grants=_pct(r["grants"], total_grants))

    # ------------------------------------------------------------------
    # Candidate generation
    # ------------------------------------------------------------------
    if table_exists(conn, CAND_TABLE):
        cand_rows = int(_scalar(conn, f"SELECT COUNT(*) FROM {CAND_TABLE}") or 0)
        cand_sigs = int(_scalar(conn, f"SELECT COUNT(DISTINCT signature_hash) FROM {CAND_TABLE}") or 0)
        cand_eins = int(_scalar(conn, f"SELECT COUNT(DISTINCT ein) FROM {CAND_TABLE}") or 0)
        _add_stat(stats, "candidates", "candidate_rows", count=cand_rows)
        _add_stat(stats, "candidates", "signatures_with_candidates", signatures=cand_sigs)
        _add_stat(stats, "candidates", "distinct_candidate_eins", count=cand_eins)

        if table_exists(conn, SIG_TABLE):
            sig_total = int(_scalar(conn, f"SELECT COUNT(*) FROM {SIG_TABLE}") or 0)
            no_cand_sigs = int(_scalar(conn, f"SELECT COUNT(*) FROM {SIG_TABLE} WHERE COALESCE(candidate_count,0)=0") or 0)
            with_cand_sigs = sig_total - no_cand_sigs
            _add_stat(stats, "candidates", "signature_candidate_coverage", "with_candidates", signatures=with_cand_sigs, pct_of_section=_pct(with_cand_sigs, sig_total))
            _add_stat(stats, "candidates", "signature_candidate_coverage", "no_candidates", signatures=no_cand_sigs, pct_of_section=_pct(no_cand_sigs, sig_total))

        for r in _top_group_rows(conn, f"""
            SELECT COALESCE(source,'(missing)') AS bucket,
                   COUNT(*) AS n,
                   COUNT(DISTINCT signature_hash) AS sigs,
                   COUNT(DISTINCT ein) AS eins
            FROM {CAND_TABLE}
            GROUP BY bucket
            ORDER BY n DESC
            LIMIT ?
        """, (top_n,)):
            _add_stat(stats, "candidates", "candidate_source", r["bucket"], count=r["n"], signatures=r["sigs"], notes=f"distinct_eins={r['eins']}")

        for r in _top_group_rows(conn, f"""
            SELECT COALESCE(candidate_reason,'(missing)') AS bucket,
                   COUNT(*) AS n,
                   COUNT(DISTINCT signature_hash) AS sigs
            FROM {CAND_TABLE}
            GROUP BY bucket
            ORDER BY n DESC
            LIMIT ?
        """, (top_n,)):
            _add_stat(stats, "candidates", "candidate_reason", r["bucket"], count=r["n"], signatures=r["sigs"])

        score_bucket = """
            CASE
              WHEN candidate_score IS NULL THEN 'missing'
              WHEN candidate_score < 50 THEN '<50'
              WHEN candidate_score < 65 THEN '50-64.99'
              WHEN candidate_score < 80 THEN '65-79.99'
              WHEN candidate_score < 90 THEN '80-89.99'
              ELSE '90+'
            END
        """
        for r in _top_group_rows(conn, f"""
            SELECT {score_bucket} AS bucket, COUNT(*) AS n, COUNT(DISTINCT signature_hash) AS sigs
            FROM {CAND_TABLE}
            GROUP BY bucket
            ORDER BY CASE bucket WHEN 'missing' THEN 0 WHEN '<50' THEN 1 WHEN '50-64.99' THEN 2 WHEN '65-79.99' THEN 3 WHEN '80-89.99' THEN 4 ELSE 5 END
        """):
            _add_stat(stats, "candidates", "candidate_score_bucket", r["bucket"], count=r["n"], signatures=r["sigs"])
    else:
        _add_stat(stats, "candidates", "table_missing", CAND_TABLE, notes="Run generate-candidates")

    # ------------------------------------------------------------------
    # Ollama decisions
    # ------------------------------------------------------------------
    if table_exists(conn, DECISION_TABLE):
        dec_total = int(_scalar(conn, f"SELECT COUNT(*) FROM {DECISION_TABLE}") or 0)
        _add_stat(stats, "ai_decisions", "total_decisions", signatures=dec_total)
        for metric, col in [("decision", "decision"), ("validation_status", "validation_status"), ("auto_accept", "auto_accept"), ("needs_human_review", "needs_human_review")]:
            for r in _top_group_rows(conn, f"""
                SELECT COALESCE(CAST({col} AS TEXT),'(missing)') AS bucket,
                       COUNT(*) AS sigs
                FROM {DECISION_TABLE}
                GROUP BY bucket
                ORDER BY sigs DESC
                LIMIT ?
            """, (top_n,)):
                _add_stat(stats, "ai_decisions", metric, r["bucket"], signatures=r["sigs"], pct_of_section=_pct(r["sigs"], dec_total))

        bucket = _confidence_bucket_expr("confidence")
        for r in _top_group_rows(conn, f"""
            SELECT {bucket} AS bucket, COUNT(*) AS sigs
            FROM {DECISION_TABLE}
            GROUP BY bucket
            ORDER BY CASE bucket
              WHEN 'missing' THEN 0 WHEN '0' THEN 1 WHEN '0.01-0.49' THEN 2 WHEN '0.50-0.69' THEN 3
              WHEN '0.70-0.84' THEN 4 WHEN '0.85-0.89' THEN 5 WHEN '0.90-0.919' THEN 6
              WHEN '0.92-0.949' THEN 7 ELSE 8 END
        """):
            _add_stat(stats, "ai_decisions", "confidence_bucket", r["bucket"], signatures=r["sigs"], pct_of_section=_pct(r["sigs"], dec_total))

        auto_sig = int(_scalar(conn, f"SELECT COUNT(*) FROM {DECISION_TABLE} WHERE auto_accept=1 AND validation_status='ok'") or 0)
        _add_stat(stats, "ai_decisions", "auto_accepted_valid_signatures", signatures=auto_sig, pct_of_section=_pct(auto_sig, dec_total))
    else:
        _add_stat(stats, "ai_decisions", "table_missing", DECISION_TABLE, notes="Run adjudicate")

    # ------------------------------------------------------------------
    # Applied AI decisions and final view
    # ------------------------------------------------------------------
    if table_exists(conn, APPLIED_TABLE):
        applied_rows = int(_scalar(conn, f"SELECT COUNT(*) FROM {APPLIED_TABLE}") or 0)
        applied_sigs = int(_scalar(conn, f"SELECT COUNT(DISTINCT signature_hash) FROM {APPLIED_TABLE}") or 0)
        _add_stat(stats, "applied_ai", "applied_grant_rows", count=applied_rows, signatures=applied_sigs, pct_of_grants=_pct(applied_rows, total_grants))
        for r in _top_group_rows(conn, f"""
            SELECT COALESCE(ai_decision,'(missing)') AS bucket, COUNT(*) AS n, COUNT(DISTINCT signature_hash) AS sigs
            FROM {APPLIED_TABLE}
            GROUP BY bucket
            ORDER BY n DESC
            LIMIT ?
        """, (top_n,)):
            _add_stat(stats, "applied_ai", "ai_decision", r["bucket"], count=r["n"], signatures=r["sigs"], pct_of_grants=_pct(r["n"], total_grants))
    else:
        _add_stat(stats, "applied_ai", "table_missing", APPLIED_TABLE, notes="Run apply-decisions")

    if include_final_view:
        if table_exists(conn, FINAL_VIEW):
            final_total = int(_scalar(conn, f"SELECT COUNT(*) FROM {FINAL_VIEW}") or 0)
            final_resolved_cond = "final_resolved_ein IS NOT NULL AND TRIM(CAST(final_resolved_ein AS TEXT))<>''"
            final_resolved = int(_scalar(conn, f"SELECT COUNT(*) FROM {FINAL_VIEW} WHERE {final_resolved_cond}") or 0)
            _add_stat(stats, "final_view", "total_rows", count=final_total, pct_of_grants=_pct(final_total, total_grants))
            _add_stat(stats, "final_view", "final_resolved_ein", "nonblank", count=final_resolved, pct_of_grants=_pct(final_resolved, total_grants), pct_of_section=_pct(final_resolved, final_total))
            _add_stat(stats, "final_view", "final_resolved_ein", "blank_unresolved", count=final_total - final_resolved, pct_of_grants=_pct(final_total - final_resolved, total_grants), pct_of_section=_pct(final_total - final_resolved, final_total))
            for r in _top_group_rows(conn, f"""
                SELECT COALESCE(final_match_source,'(missing)') AS bucket, COUNT(*) AS n
                FROM {FINAL_VIEW}
                GROUP BY bucket
                ORDER BY n DESC
            """):
                _add_stat(stats, "final_view", "final_match_source", r["bucket"], count=r["n"], pct_of_grants=_pct(r["n"], total_grants), pct_of_section=_pct(r["n"], final_total))
            bucket = _confidence_bucket_expr("final_confidence")
            for r in _top_group_rows(conn, f"""
                SELECT {bucket} AS bucket, COUNT(*) AS n
                FROM {FINAL_VIEW}
                GROUP BY bucket
                ORDER BY CASE bucket
                  WHEN 'missing' THEN 0 WHEN '0' THEN 1 WHEN '0.01-0.49' THEN 2 WHEN '0.50-0.69' THEN 3
                  WHEN '0.70-0.84' THEN 4 WHEN '0.85-0.89' THEN 5 WHEN '0.90-0.919' THEN 6
                  WHEN '0.92-0.949' THEN 7 ELSE 8 END
            """):
                _add_stat(stats, "final_view", "final_confidence_bucket", r["bucket"], count=r["n"], pct_of_grants=_pct(r["n"], total_grants), pct_of_section=_pct(r["n"], final_total))
        else:
            _add_stat(stats, "final_view", "view_missing", FINAL_VIEW, notes="Run apply-decisions")
    else:
        _add_stat(stats, "final_view", "skipped", notes="Use without --skip-final-view to include final view counts")

    return stats


def print_stats(rows: Sequence[Dict[str, Any]], section_filter: Optional[str] = None) -> None:
    sections: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if section_filter and row.get("section") != section_filter:
            continue
        sections[str(row.get("section", ""))].append(row)
    for section, items in sections.items():
        print(f"\n=== {section} ===")
        print(f"{'metric':32} {'bucket':42} {'count':>14} {'sigs':>10} {'grants':>14} {'pct_grants':>10} {'pct_section':>11} {'amount':>16} notes")
        print("-" * 170)
        for r in items:
            print(
                f"{str(r.get('metric',''))[:32]:32} "
                f"{str(r.get('bucket',''))[:42]:42} "
                f"{_fmt_num(r.get('count')):>14} "
                f"{_fmt_num(r.get('signatures')):>10} "
                f"{_fmt_num(r.get('grants_represented')):>14} "
                f"{'' if r.get('pct_of_grants') is None else f'{r.get('pct_of_grants'):.2f}%':>10} "
                f"{'' if r.get('pct_of_section') is None else f'{r.get('pct_of_section'):.2f}%':>11} "
                f"{_fmt_num(r.get('total_amount')):>16} "
                f"{r.get('notes','')}"
            )


def cmd_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db, readonly=True)
    rows = collect_stats(conn, top_n=args.top_n, include_final_view=not args.skip_final_view)
    if args.csv_out:
        fieldnames = ["section", "metric", "bucket", "count", "signatures", "grants_represented", "total_amount", "pct_of_grants", "pct_of_section", "notes"]
        with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in fieldnames})
        print(f"Wrote stats CSV: {args.csv_out}", flush=True)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        print(f"Wrote stats JSON: {args.json_out}", flush=True)
    if not args.no_print:
        print_stats(rows, section_filter=args.section)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common_db(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-assisted second-pass grant recipient matcher (v1.5 diagnostics + fast candidates)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("verify-bmf", help="Verify eo-bmf/eo1.csv ... eo4.csv exist")
    p.add_argument("--project-dir", default=DEFAULT_PROJECT_DIR, help=f"Main project folder containing eo-bmf/ (default: {DEFAULT_PROJECT_DIR})")
    p.add_argument("--bmf-dir", default=None, help="Explicit EO BMF directory")
    p.set_defaults(func=cmd_verify_bmf)

    p = sub.add_parser("build-identity", help="Build org_identity from returns and EO BMF CSVs")
    add_common_db(p)
    p.add_argument("--project-dir", default=DEFAULT_PROJECT_DIR, help=f"Main project folder containing eo-bmf/ (default: {DEFAULT_PROJECT_DIR})")
    p.add_argument("--bmf-dir", default=None, help="Explicit EO BMF directory")
    p.add_argument("--full-refresh", action="store_true", help="Drop and rebuild org_identity")
    p.add_argument("--skip-returns", action="store_true", help="Do not import identity rows from returns")
    p.add_argument("--skip-bmf", action="store_true", help="Do not import EO BMF files")
    p.add_argument("--include-bmf-ico", action="store_true", help="Also index BMF ICO as low-priority alias; off by default")
    p.add_argument("--no-tokens", action="store_true", help="Do not build org_identity_token")
    p.add_argument("--no-fts", action="store_true", help="Do not create/rebuild FTS5 table")
    p.add_argument("--batch-size", type=int, default=50000)
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
    p.add_argument("--candidate-mode", choices=["fast", "balanced", "broad"], default="fast",
                   help="fast=exact/EIN/address/name only; balanced=adds geo-constrained token fallback; broad=also allows FTS fallback")
    p.add_argument("--enough-candidates", type=int, default=8,
                   help="In balanced/broad mode, skip token/FTS fallback once this many distinct EINs are found")
    p.add_argument("--token-limit", type=int, default=50)
    p.add_argument("--no-fts", action="store_true", help="Disable FTS even in broad candidate mode")
    p.add_argument("--commit-every", type=int, default=5000)
    p.add_argument("--status-update-every", type=int, default=0,
                   help="Bulk-update signature candidate_count/queue status every N processed signatures; default 0 updates only at end for maximum speed")
    p.set_defaults(func=cmd_generate_candidates)

    p = sub.add_parser("test-ollama", help="Send one tiny structured test request to Ollama and print diagnostics")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--num-ctx", type=int, default=4096)
    p.add_argument("--num-predict", type=int, default=300)
    p.add_argument("--format-mode", choices=["schema", "json", "none"], default="schema")
    p.add_argument("--debug-raw-out", default=None, help="Optional text file to append raw Ollama responses for debugging")
    p.set_defaults(func=cmd_test_ollama)

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
    p.add_argument("--format-mode", choices=["schema", "json", "none"], default="schema",
                   help="Ollama format parameter mode. Try 'json' if schema mode causes endpoint/model trouble.")
    p.add_argument("--ollama-retries", type=int, default=1, help="Retries per signature after an Ollama call failure")
    p.add_argument("--retry-sleep", type=float, default=2.0, help="Seconds to sleep between Ollama retries")
    p.add_argument("--max-call-failures", type=int, default=3,
                   help="Stop after this many total Ollama failures; use 0 to disable")
    p.add_argument("--max-consecutive-call-failures", type=int, default=3,
                   help="Stop after this many consecutive Ollama failures; use 0 to disable")
    p.add_argument("--fail-fast", action="store_true", help="Stop after the first Ollama call failure")
    p.add_argument("--debug-raw-out", default=None, help="Optional text file to append raw Ollama responses for debugging")
    p.set_defaults(func=cmd_adjudicate)

    p = sub.add_parser("apply-decisions", help="Apply auto-accepted AI decisions to separate table and final view")
    add_common_db(p)
    p.add_argument("--full-refresh", action="store_true")
    p.add_argument("--min-confidence", type=float, default=0.92)
    p.add_argument("--batch-size", type=int, default=50000)
    p.set_defaults(func=cmd_apply_decisions)

    p = sub.add_parser("stats", help="Report raw grant, deterministic resolver, candidate, AI decision, and final-view statistics")
    add_common_db(p)
    p.add_argument("--top-n", type=int, default=50, help="Maximum rows for grouped breakdowns")
    p.add_argument("--section", default=None, choices=["raw_grants", "deterministic_resolver", "org_identity", "signatures", "candidates", "ai_decisions", "applied_ai", "final_view"], help="Print only one stats section")
    p.add_argument("--skip-final-view", action="store_true", help="Skip counting the final resolved view if it is expensive or not needed")
    p.add_argument("--csv-out", default=None, help="Optional CSV output path")
    p.add_argument("--json-out", default=None, help="Optional JSON output path")
    p.add_argument("--no-print", action="store_true", help="Do not print tables to console")
    p.set_defaults(func=cmd_stats)

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
