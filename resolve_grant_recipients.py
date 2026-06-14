#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
resolve_grant_recipients_v2_1_fast.py

Builds a precomputed grant-recipient identity resolution layer for the IRS 990
SQLite database.

Purpose
-------
The raw grants table is limited by whatever recipient EIN/name/address was
reported on the filer return. Some grant rows have no recipient EIN, and some
have incorrect recipient EINs. This script walks every grant row and attempts to
resolve the recipient to a known organization EIN using a conservative matching
process:

1) Reported recipient EIN, if valid and known.
2) Exact normalized recipient name + ZIP.
3) Exact normalized recipient name + state.
4) Exact normalized recipient name + city/state.
5) Address-only / address-narrowed matching when the address points to one EIN
   or narrows candidates enough for a clear name winner.
6) Unique exact normalized recipient name nationally.
7) Optional fuzzy fallback within address/ZIP/state/city-state candidate pools.

In dry-run mode, nothing is written to the database; results are written to CSV.
In normal mode, results are written to table grant_recipient_resolved.

Version 2.1 performance change
------------------------------
For full-refresh database loads, secondary indexes are deferred until after all
rows are inserted. This is much faster for multi-million-row builds because
SQLite does not have to maintain several large indexes during every insert.

Examples
--------
Dry run to CSV:
  python resolve_grant_recipients_v2_1_fast.py --db C:\IRSDB\db\irs990.db --dry-run --csv-out grant_matches.csv

Write/update database table, processing only rows not already resolved:
  python resolve_grant_recipients_v2_1_fast.py --db C:\IRSDB\db\irs990.db

Rebuild the resolved table from scratch:
  python resolve_grant_recipients_v2_1_fast.py --db C:\IRSDB\db\irs990.db --full-refresh

Enable fuzzy fallback conservatively:
  python resolve_grant_recipients_v2_1_fast.py --db C:\IRSDB\db\irs990.db --enable-fuzzy --fuzzy-threshold 0.92
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict, Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

DEFAULT_DB = r"C:\IRSDB\db\irs990.db"
RESOLVED_TABLE = "grant_recipient_resolved"

# Only remove true legal suffix / punctuation noise. Do NOT remove words like
# FOUNDATION, ASSOCIATION, CENTER, SCHOOL, etc.; those are identity-bearing.
LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
    "LTD", "LIMITED", "LLC", "L.L.C", "PLC", "PLLC", "PC", "P.C",
}
LEADING_NOISE = {"THE"}

USPS_STREET = {
    "STREET": "ST", "ST.": "ST", "AVENUE": "AVE", "AVE.": "AVE",
    "ROAD": "RD", "RD.": "RD", "BOULEVARD": "BLVD", "BLVD.": "BLVD",
    "DRIVE": "DR", "DR.": "DR", "LANE": "LN", "LN.": "LN",
    "COURT": "CT", "CT.": "CT", "PLACE": "PL", "PL.": "PL",
    "PARKWAY": "PKWY", "PKWY.": "PKWY", "HIGHWAY": "HWY", "HWY.": "HWY",
    "SUITE": "STE", "STE.": "STE", "FLOOR": "FL", "FL.": "FL",
}

@dataclass(frozen=True)
class OrgCandidate:
    ein: str
    org_name: str
    dba_name: str
    city: str
    state: str
    zip5: str
    address1: str
    tax_year: Optional[int]
    filing_id: str
    name_norm: str
    address_norm: str

@dataclass
class MatchResult:
    grant_id: int
    filing_id: str
    grantor_ein: str
    grantor_name: str
    tax_year: Optional[int]
    return_type: str
    recipient_reported_ein: str
    recipient_reported_name: str
    recipient_city: str
    recipient_state: str
    recipient_zip: str
    cash_amount: Optional[float]
    noncash_amount: Optional[float]
    total_amount: float
    purpose: str
    resolved_ein: str
    resolved_org_name: str
    resolved_city: str
    resolved_state: str
    resolved_zip: str
    resolved_filing_id: str
    match_status: str
    match_method: str
    confidence: float
    name_score: float
    address_score: float
    warning_flags: str
    candidate_count: int
    processed_at: str


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
            conn.execute("PRAGMA locking_mode=EXCLUSIVE;")
    except Exception:
        pass
    return conn


def digits9(value: Optional[str]) -> str:
    d = re.sub(r"\D", "", value or "")
    return d if len(d) == 9 else ""


def zip5(value: Optional[str]) -> str:
    d = re.sub(r"\D", "", value or "")
    return d[:5] if len(d) >= 5 else ""


def clean_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def join_name(line1: Optional[str], line2: Optional[str] = None) -> str:
    a = clean_text(line1)
    b = clean_text(line2)
    return clean_text(f"{a} {b}" if b else a)


def normalize_name(value: Optional[str]) -> str:
    s = (value or "").upper()
    s = s.replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    tokens = [t for t in s.split() if t]
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


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return round(SequenceMatcher(None, a, b).ratio(), 4)


def exact_unique(candidates: Sequence[OrgCandidate]) -> Optional[OrgCandidate]:
    if not candidates:
        return None
    eins = {c.ein for c in candidates}
    if len(eins) == 1:
        # choose latest row for that EIN
        return sorted(candidates, key=lambda c: (c.tax_year or 0, c.filing_id), reverse=True)[0]
    return None


def best_by_fuzzy(name_norm: str, candidates: Sequence[OrgCandidate], threshold: float) -> Tuple[Optional[OrgCandidate], float, int]:
    best: Optional[OrgCandidate] = None
    best_score = 0.0
    ties = 0
    for cand in candidates:
        sc = ratio(name_norm, cand.name_norm)
        if sc > best_score:
            best = cand
            best_score = sc
            ties = 1
        elif sc == best_score and sc >= threshold:
            ties += 1
    if best is not None and best_score >= threshold and ties == 1:
        return best, best_score, len(candidates)
    return None, best_score, len(candidates)


def best_name_in_pool(name_norm: str, candidates: Sequence[OrgCandidate]) -> Tuple[Optional[OrgCandidate], float, float, int]:
    """Return best candidate by name score, second-best score, and distinct EIN count.

    The candidates may include many filing-year rows for the same EIN. We keep the
    best/latest row per EIN, score each EIN once, and return the best.
    """
    if not name_norm or not candidates:
        return None, 0.0, 0.0, 0

    latest_by_ein: Dict[str, OrgCandidate] = {}
    for c in candidates:
        old = latest_by_ein.get(c.ein)
        if old is None or (c.tax_year or 0, c.filing_id) > (old.tax_year or 0, old.filing_id):
            latest_by_ein[c.ein] = c

    scored: List[Tuple[float, OrgCandidate]] = []
    for c in latest_by_ein.values():
        scored.append((ratio(name_norm, c.name_norm), c))
    scored.sort(key=lambda x: (x[0], x[1].tax_year or 0, x[1].filing_id), reverse=True)

    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    return best, best_score, second_score, len(latest_by_ein)


def accept_address_unique(name_norm: str, addr_norm: str, candidates: Sequence[OrgCandidate],
                          min_name_score: float) -> Tuple[Optional[OrgCandidate], float, int, str]:
    """Accept an address-only match when all records at the address resolve to one EIN.

    A very low name score is still held back to avoid obvious mismatches, but the
    threshold is intentionally low because the address itself is the main signal.
    """
    uniq = exact_unique(candidates)
    if uniq is None:
        return None, 0.0, len({c.ein for c in candidates}), ""
    nscore = ratio(name_norm, uniq.name_norm) if name_norm else 0.0
    if not name_norm or nscore >= min_name_score:
        warn = "" if (not name_norm or nscore >= 0.72) else "address_unique_low_name_similarity"
        return uniq, nscore, 1, warn
    return None, nscore, 1, "address_unique_rejected_name_too_different"


class OrgIndex:
    def __init__(self) -> None:
        self.by_ein: Dict[str, OrgCandidate] = {}
        self.by_name_zip: Dict[Tuple[str, str], List[OrgCandidate]] = defaultdict(list)
        self.by_name_state: Dict[Tuple[str, str], List[OrgCandidate]] = defaultdict(list)
        self.by_name_city_state: Dict[Tuple[str, str, str], List[OrgCandidate]] = defaultdict(list)
        self.by_name: Dict[str, List[OrgCandidate]] = defaultdict(list)
        self.by_name_addr_zip: Dict[Tuple[str, str, str], List[OrgCandidate]] = defaultdict(list)
        self.by_name_addr_city_state: Dict[Tuple[str, str, str, str], List[OrgCandidate]] = defaultdict(list)
        self.by_addr_zip: Dict[Tuple[str, str], List[OrgCandidate]] = defaultdict(list)
        self.by_addr_city_state: Dict[Tuple[str, str, str], List[OrgCandidate]] = defaultdict(list)
        self.fuzzy_pool_addr_zip: Dict[Tuple[str, str], List[OrgCandidate]] = defaultdict(list)
        self.fuzzy_pool_addr_city_state: Dict[Tuple[str, str, str], List[OrgCandidate]] = defaultdict(list)
        self.fuzzy_pool_zip: Dict[str, List[OrgCandidate]] = defaultdict(list)
        self.fuzzy_pool_state: Dict[str, List[OrgCandidate]] = defaultdict(list)
        self.fuzzy_pool_city_state: Dict[Tuple[str, str], List[OrgCandidate]] = defaultdict(list)

    def add(self, cand: OrgCandidate) -> None:
        if not cand.ein or not cand.name_norm:
            return

        # Keep latest/best candidate for direct EIN lookup.
        old = self.by_ein.get(cand.ein)
        if old is None or (cand.tax_year or 0, cand.filing_id) > (old.tax_year or 0, old.filing_id):
            self.by_ein[cand.ein] = cand

        self.by_name[cand.name_norm].append(cand)

        if cand.address_norm and cand.zip5:
            self.by_addr_zip[(cand.address_norm, cand.zip5)].append(cand)
            self.by_name_addr_zip[(cand.name_norm, cand.address_norm, cand.zip5)].append(cand)
            self.fuzzy_pool_addr_zip[(cand.address_norm, cand.zip5)].append(cand)
        if cand.address_norm and cand.city and cand.state:
            self.by_addr_city_state[(cand.address_norm, cand.city, cand.state)].append(cand)
            self.by_name_addr_city_state[(cand.name_norm, cand.address_norm, cand.city, cand.state)].append(cand)
            self.fuzzy_pool_addr_city_state[(cand.address_norm, cand.city, cand.state)].append(cand)

        if cand.zip5:
            self.by_name_zip[(cand.name_norm, cand.zip5)].append(cand)
            self.fuzzy_pool_zip[cand.zip5].append(cand)
        if cand.state:
            self.by_name_state[(cand.name_norm, cand.state)].append(cand)
            self.fuzzy_pool_state[cand.state].append(cand)
        if cand.city and cand.state:
            self.by_name_city_state[(cand.name_norm, cand.city, cand.state)].append(cand)
            self.fuzzy_pool_city_state[(cand.city, cand.state)].append(cand)


def iter_org_candidates(conn: sqlite3.Connection) -> Iterator[OrgCandidate]:
    """Load historical org names/addresses from returns.

    This intentionally uses returns, not only canonical_by_ein_year, so historical
    names/addresses remain searchable. If the same EIN appears many times, the
    index keeps the latest row for direct EIN matching but keeps aliases for
    name/address matching.
    """
    sql = """
    SELECT
      r.ein, r.org_name, r.dba_name,
      r.city, r.state, r.zip, r.us_address_line1, r.filing_id,
      COALESCE(c.tax_year, r.tax_year) AS tax_year
    FROM returns r
    LEFT JOIN canonical_by_ein_year c ON c.filing_id = r.filing_id
    WHERE r.ein IS NOT NULL AND TRIM(r.ein) <> ''
      AND r.org_name IS NOT NULL AND TRIM(r.org_name) <> ''
    """
    for r in conn.execute(sql):
        ein = digits9(r["ein"])
        name = clean_text(r["org_name"])
        if not ein or not name:
            continue
        yield OrgCandidate(
            ein=ein,
            org_name=name,
            dba_name=clean_text(r["dba_name"]),
            city=clean_text(r["city"]).upper(),
            state=clean_text(r["state"]).upper(),
            zip5=zip5(r["zip"]),
            address1=clean_text(r["us_address_line1"]),
            tax_year=r["tax_year"],
            filing_id=clean_text(r["filing_id"]),
            name_norm=normalize_name(name),
            address_norm=normalize_address(r["us_address_line1"]),
        )
        # Also index DBA as an alias if present.
        dba = clean_text(r["dba_name"])
        if dba:
            yield OrgCandidate(
                ein=ein,
                org_name=name,
                dba_name=dba,
                city=clean_text(r["city"]).upper(),
                state=clean_text(r["state"]).upper(),
                zip5=zip5(r["zip"]),
                address1=clean_text(r["us_address_line1"]),
                tax_year=r["tax_year"],
                filing_id=clean_text(r["filing_id"]),
                name_norm=normalize_name(dba),
                address_norm=normalize_address(r["us_address_line1"]),
            )


def build_org_index(conn: sqlite3.Connection, progress_every: int = 250_000) -> OrgIndex:
    idx = OrgIndex()
    count = 0
    for cand in iter_org_candidates(conn):
        idx.add(cand)
        count += 1
        if progress_every and count % progress_every == 0:
            print(f"Loaded {count:,} organization identity rows...", flush=True)
    print(f"Loaded {count:,} organization identity rows; {len(idx.by_ein):,} unique EINs.", flush=True)
    return idx


def grant_sql(only_unresolved: bool, min_grant_id: Optional[int], max_grant_id: Optional[int]) -> Tuple[str, List[object]]:
    clauses = []
    params: List[object] = []
    if only_unresolved:
        clauses.append(f"NOT EXISTS (SELECT 1 FROM {RESOLVED_TABLE} rr WHERE rr.grant_id = g.id)")
    if min_grant_id is not None:
        clauses.append("g.id >= ?")
        params.append(min_grant_id)
    if max_grant_id is not None:
        clauses.append("g.id <= ?")
        params.append(max_grant_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"""
    SELECT
      g.id AS grant_id,
      g.filing_id,
      COALESCE(g.filer_ein, rf.ein) AS grantor_ein,
      COALESCE(g.filer_name, rf.org_name) AS grantor_name,
      c.tax_year,
      c.return_type,
      g.recipient_ein,
      g.business_name_line1_txt,
      g.business_name_line2_txt,
      COALESCE(g.us_city_nm, g.foreign_city_nm) AS recipient_city,
      COALESCE(g.us_state_abbreviation_cd, g.foreign_province_or_state_nm) AS recipient_state,
      g.us_zip_cd AS recipient_zip,
      COALESCE(g.us_address_line1_txt, g.foreign_address_line1_txt) AS recipient_address1,
      g.cash_grant_amt,
      g.non_cash_assistance_amt,
      g.purpose_of_grant_txt
    FROM grants g
    LEFT JOIN returns rf ON rf.filing_id = g.filing_id
    LEFT JOIN canonical_by_ein_year c ON c.filing_id = g.filing_id
    {where}
    ORDER BY g.id
    """
    return sql, params


def candidate_to_fields(c: Optional[OrgCandidate]) -> Tuple[str, str, str, str, str, str]:
    if c is None:
        return "", "", "", "", "", ""
    return c.ein, c.org_name, c.city, c.state, c.zip5, c.filing_id


def resolve_one(row: sqlite3.Row, idx: OrgIndex, enable_fuzzy: bool, fuzzy_threshold: float,
                bad_ein_name_threshold: float, accepted_ein_name_threshold: float,
                address_unique_min_name_score: float, address_name_threshold: float,
                address_name_margin: float) -> MatchResult:
    grant_id = int(row["grant_id"])
    reported_ein = digits9(row["recipient_ein"])
    recipient_name = join_name(row["business_name_line1_txt"], row["business_name_line2_txt"])
    name_norm = normalize_name(recipient_name)
    city = clean_text(row["recipient_city"]).upper()
    state = clean_text(row["recipient_state"]).upper()
    z5 = zip5(row["recipient_zip"])
    addr_norm = normalize_address(row["recipient_address1"])

    warnings: List[str] = []
    cand: Optional[OrgCandidate] = None
    method = ""
    status = "no_match"
    confidence = 0.0
    nscore = 0.0
    ascore = 0.0
    candidate_count = 0

    reported_ein_cand = idx.by_ein.get(reported_ein) if reported_ein else None

    # Step 1: Reported EIN, if known. Validate with name when possible.
    if reported_ein and reported_ein_cand:
        nscore = ratio(name_norm, reported_ein_cand.name_norm) if name_norm else 0.0
        ascore = ratio(addr_norm, reported_ein_cand.address_norm) if addr_norm else 0.0
        if not name_norm or nscore >= accepted_ein_name_threshold:
            cand = reported_ein_cand
            method = "reported_ein_known_name_agrees" if name_norm else "reported_ein_known_no_name_to_validate"
            status = "ein_exact"
            confidence = 0.99 if name_norm else 0.92
        else:
            warnings.append("reported_ein_name_disagrees")

    elif reported_ein and not reported_ein_cand:
        warnings.append("reported_ein_not_found_in_returns")

    # Step 2: If no accepted EIN match, try strongest deterministic name+address matches.
    if cand is None and name_norm:
        strong_checks: List[Tuple[str, Sequence[OrgCandidate], float]] = []
        if addr_norm and z5:
            strong_checks.append(("name_address_zip_exact", idx.by_name_addr_zip.get((name_norm, addr_norm, z5), []), 0.985))
        if addr_norm and city and state:
            strong_checks.append(("name_address_city_state_exact", idx.by_name_addr_city_state.get((name_norm, addr_norm, city, state), []), 0.975))
        if z5:
            strong_checks.append(("name_zip_exact", idx.by_name_zip.get((name_norm, z5), []), 0.97))
        if city and state:
            strong_checks.append(("name_city_state_exact", idx.by_name_city_state.get((name_norm, city, state), []), 0.95))

        for label, pool, base_conf in strong_checks:
            candidate_count = max(candidate_count, len(pool))
            uniq = exact_unique(pool)
            if uniq is not None:
                cand = uniq
                method = label
                nscore = 1.0
                ascore = ratio(addr_norm, cand.address_norm) if addr_norm else 0.0
                confidence = base_conf
                status = "name_address_high_confidence"
                break

    # Step 3: Address-only/address-narrowed matching.
    # This handles grant rows with blank EINs and imperfect names. If only one EIN
    # is known at an exact street+ZIP or street+city/state, accept it unless the
    # name is wildly different. If multiple EINs share the address, use the
    # address to narrow candidates and require a clear best name winner.
    if cand is None and addr_norm:
        address_pools: List[Tuple[str, Sequence[OrgCandidate], float]] = []
        if z5:
            address_pools.append(("address_zip_unique", idx.by_addr_zip.get((addr_norm, z5), []), 0.90))
        if city and state:
            address_pools.append(("address_city_state_unique", idx.by_addr_city_state.get((addr_norm, city, state), []), 0.875))

        for label, pool, base_conf in address_pools:
            if not pool:
                continue
            candidate_count = max(candidate_count, len(pool))
            unique_cand, score, distinct_eins, warn = accept_address_unique(
                name_norm, addr_norm, pool, address_unique_min_name_score
            )
            if unique_cand is not None:
                cand = unique_cand
                method = label
                nscore = score
                ascore = 1.0
                confidence = base_conf if score < 0.72 else min(0.96, base_conf + 0.05)
                status = "address_unique"
                if warn:
                    warnings.append(warn)
                break

            # Multiple EINs at same address: pick only when name clearly selects one.
            if name_norm:
                best, best_score, second_score, distinct_count = best_name_in_pool(name_norm, pool)
                if distinct_count > 1:
                    warnings.append("multiple_eins_at_address")
                if (
                    best is not None
                    and best_score >= address_name_threshold
                    and (best_score - second_score) >= address_name_margin
                ):
                    cand = best
                    method = label.replace("unique", "name_within_address")
                    nscore = best_score
                    ascore = 1.0
                    confidence = min(0.95, base_conf + 0.03 + max(0.0, best_score - address_name_threshold) / 4)
                    status = "address_narrowed_name_match"
                    break

    # Step 4: Broader deterministic name/location matches.
    if cand is None and name_norm:
        checks: List[Tuple[str, Sequence[OrgCandidate], float]] = []
        if state:
            checks.append(("name_state_exact", idx.by_name_state.get((name_norm, state), []), 0.94))
        checks.append(("name_unique_national_exact", idx.by_name.get(name_norm, []), 0.88))

        for label, pool, base_conf in checks:
            candidate_count = max(candidate_count, len(pool))
            uniq = exact_unique(pool)
            if uniq is not None:
                cand = uniq
                method = label
                nscore = 1.0
                ascore = ratio(addr_norm, cand.address_norm) if addr_norm else 0.0
                confidence = base_conf
                status = "name_exact"
                break

    # Step 5: Optional fuzzy matching, constrained to address/geographic pools only.
    if cand is None and enable_fuzzy and name_norm:
        fuzzy_pools: List[Tuple[str, Sequence[OrgCandidate], float]] = []
        if addr_norm and z5:
            fuzzy_pools.append(("fuzzy_name_within_address_zip", idx.fuzzy_pool_addr_zip.get((addr_norm, z5), []), 0.92))
        if addr_norm and city and state:
            fuzzy_pools.append(("fuzzy_name_within_address_city_state", idx.fuzzy_pool_addr_city_state.get((addr_norm, city, state), []), 0.90))
        if z5:
            fuzzy_pools.append(("fuzzy_name_within_zip", idx.fuzzy_pool_zip.get(z5, []), 0.90))
        if city and state:
            fuzzy_pools.append(("fuzzy_name_within_city_state", idx.fuzzy_pool_city_state.get((city, state), []), 0.88))
        if state:
            fuzzy_pools.append(("fuzzy_name_within_state", idx.fuzzy_pool_state.get(state, []), 0.84))

        for label, pool, base_conf in fuzzy_pools:
            # Avoid very large state-wide fuzzy scans unless the pool is reasonable.
            if len(pool) > 25_000 and label == "fuzzy_name_within_state":
                warnings.append("state_fuzzy_pool_too_large_skipped")
                continue
            best, best_score, pool_size = best_by_fuzzy(name_norm, pool, fuzzy_threshold)
            candidate_count = max(candidate_count, pool_size)
            if best is not None:
                cand = best
                method = label
                nscore = best_score
                ascore = ratio(addr_norm, cand.address_norm) if addr_norm else 0.0
                confidence = min(0.93, base_conf + max(0.0, best_score - fuzzy_threshold) / 2)
                status = "fuzzy_probable"
                break

    # Step 4: If reported EIN looked wrong but name/address found another EIN, flag as corrected.
    if cand is not None and reported_ein and cand.ein != reported_ein:
        if reported_ein_cand is not None:
            reported_score = ratio(name_norm, reported_ein_cand.name_norm) if name_norm else 0.0
            if reported_score < bad_ein_name_threshold:
                status = "possible_bad_ein_corrected"
                warnings.append(f"reported_ein_points_to={reported_ein_cand.org_name}")
            else:
                status = "conflicting_ein_match"
                warnings.append("reported_ein_and_name_match_different_known_eins")
        else:
            status = "reported_ein_not_found_name_matched"

    if cand is None:
        method = method or "none"
        status = "unresolved"
        confidence = 0.0
        nscore = 0.0
        ascore = 0.0
        candidate_count = candidate_count or len(idx.by_name.get(name_norm, [])) if name_norm else 0

    resolved_ein, resolved_name, resolved_city, resolved_state, resolved_zip, resolved_filing_id = candidate_to_fields(cand)
    cash = row["cash_grant_amt"]
    noncash = row["non_cash_assistance_amt"]
    total = float(cash or 0) + float(noncash or 0)

    return MatchResult(
        grant_id=grant_id,
        filing_id=clean_text(row["filing_id"]),
        grantor_ein=digits9(row["grantor_ein"]),
        grantor_name=clean_text(row["grantor_name"]),
        tax_year=row["tax_year"],
        return_type=clean_text(row["return_type"]),
        recipient_reported_ein=reported_ein,
        recipient_reported_name=recipient_name,
        recipient_city=city,
        recipient_state=state,
        recipient_zip=z5,
        cash_amount=cash,
        noncash_amount=noncash,
        total_amount=total,
        purpose=clean_text(row["purpose_of_grant_txt"]),
        resolved_ein=resolved_ein,
        resolved_org_name=resolved_name,
        resolved_city=resolved_city,
        resolved_state=resolved_state,
        resolved_zip=resolved_zip,
        resolved_filing_id=resolved_filing_id,
        match_status=status,
        match_method=method,
        confidence=round(confidence, 4),
        name_score=round(nscore, 4),
        address_score=round(ascore, 4),
        warning_flags=";".join(warnings),
        candidate_count=int(candidate_count or 0),
        processed_at=now_stamp(),
    )


FIELDNAMES = [
    "grant_id", "filing_id", "grantor_ein", "grantor_name", "tax_year", "return_type",
    "recipient_reported_ein", "recipient_reported_name", "recipient_city", "recipient_state", "recipient_zip",
    "cash_amount", "noncash_amount", "total_amount", "purpose",
    "resolved_ein", "resolved_org_name", "resolved_city", "resolved_state", "resolved_zip", "resolved_filing_id",
    "match_status", "match_method", "confidence", "name_score", "address_score",
    "warning_flags", "candidate_count", "processed_at",
]


def result_to_tuple(r: MatchResult) -> Tuple[object, ...]:
    return tuple(getattr(r, f) for f in FIELDNAMES)


RESOLVED_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS idx_grr_resolved_ein ON {RESOLVED_TABLE}(resolved_ein)",
    f"CREATE INDEX IF NOT EXISTS idx_grr_reported_ein ON {RESOLVED_TABLE}(recipient_reported_ein)",
    f"CREATE INDEX IF NOT EXISTS idx_grr_status ON {RESOLVED_TABLE}(match_status)",
    f"CREATE INDEX IF NOT EXISTS idx_grr_grantor_ein_year ON {RESOLVED_TABLE}(grantor_ein, tax_year)",
    f"CREATE INDEX IF NOT EXISTS idx_grr_name_state ON {RESOLVED_TABLE}(recipient_reported_name, recipient_state)",
]


def drop_resolved_indexes(conn: sqlite3.Connection) -> None:
    for name in (
        "idx_grr_resolved_ein",
        "idx_grr_reported_ein",
        "idx_grr_status",
        "idx_grr_grantor_ein_year",
        "idx_grr_name_state",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.commit()


def create_resolved_indexes(conn: sqlite3.Connection) -> None:
    # Build indexes after bulk insert. This is much faster than maintaining
    # them while inserting millions of rows.
    for i, stmt in enumerate(RESOLVED_INDEXES, 1):
        print(f"Creating post-load index {i}/{len(RESOLVED_INDEXES)}...", flush=True)
        conn.execute(stmt)
        conn.commit()
    try:
        conn.execute(f"ANALYZE {RESOLVED_TABLE}")
        conn.commit()
    except Exception:
        pass


def create_resolved_table(conn: sqlite3.Connection, create_indexes: bool = True) -> None:
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS {RESOLVED_TABLE} (
      grant_id INTEGER PRIMARY KEY,
      filing_id TEXT NOT NULL,
      grantor_ein TEXT,
      grantor_name TEXT,
      tax_year INTEGER,
      return_type TEXT,
      recipient_reported_ein TEXT,
      recipient_reported_name TEXT,
      recipient_city TEXT,
      recipient_state TEXT,
      recipient_zip TEXT,
      cash_amount NUMERIC,
      noncash_amount NUMERIC,
      total_amount NUMERIC,
      purpose TEXT,
      resolved_ein TEXT,
      resolved_org_name TEXT,
      resolved_city TEXT,
      resolved_state TEXT,
      resolved_zip TEXT,
      resolved_filing_id TEXT,
      match_status TEXT,
      match_method TEXT,
      confidence NUMERIC,
      name_score NUMERIC,
      address_score NUMERIC,
      warning_flags TEXT,
      candidate_count INTEGER,
      processed_at TEXT
    );
    """)
    conn.commit()
    if create_indexes:
        create_resolved_indexes(conn)


def full_refresh_table(conn: sqlite3.Connection, defer_indexes: bool = True) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {RESOLVED_TABLE}")
    conn.commit()
    create_resolved_table(conn, create_indexes=not defer_indexes)


def insert_batch(conn: sqlite3.Connection, batch: Sequence[MatchResult], *, upsert: bool) -> None:
    placeholders = ",".join("?" for _ in FIELDNAMES)
    cols = ",".join(FIELDNAMES)
    if upsert:
        updates = ",".join(f"{c}=excluded.{c}" for c in FIELDNAMES if c != "grant_id")
        sql = f"""
        INSERT INTO {RESOLVED_TABLE} ({cols}) VALUES ({placeholders})
        ON CONFLICT(grant_id) DO UPDATE SET {updates}
        """
    else:
        # Full-refresh loads an empty table, so plain INSERT is faster than UPSERT.
        sql = f"INSERT INTO {RESOLVED_TABLE} ({cols}) VALUES ({placeholders})"
    conn.executemany(sql, [result_to_tuple(r) for r in batch])
    conn.commit()


def process(conn: sqlite3.Connection, idx: OrgIndex, args: argparse.Namespace) -> Counter:
    only_unresolved = (not args.full_refresh) and (not args.dry_run)
    bulk_insert_mode = (not args.dry_run) and args.full_refresh
    sql, params = grant_sql(only_unresolved, args.min_grant_id, args.max_grant_id)
    cur = conn.execute(sql, params)

    counts: Counter = Counter()
    batch: List[MatchResult] = []
    out_fh = None
    writer = None

    if args.dry_run:
        out_path = Path(args.csv_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_fh = out_path.open("w", newline="", encoding="utf-8-sig")
        writer = csv.writer(out_fh)
        writer.writerow(FIELDNAMES)
        print(f"Dry run enabled; writing CSV to {out_path}", flush=True)

    started = time.time()
    try:
        for row in cur:
            result = resolve_one(
                row, idx,
                enable_fuzzy=args.enable_fuzzy,
                fuzzy_threshold=args.fuzzy_threshold,
                bad_ein_name_threshold=args.bad_ein_name_threshold,
                accepted_ein_name_threshold=args.accepted_ein_name_threshold,
                address_unique_min_name_score=args.address_unique_min_name_score,
                address_name_threshold=args.address_name_threshold,
                address_name_margin=args.address_name_margin,
            )
            counts["processed"] += 1
            counts[result.match_status] += 1
            if result.warning_flags:
                counts["with_warnings"] += 1

            if writer is not None:
                writer.writerow(result_to_tuple(result))
                if args.flush_csv_every and counts["processed"] % args.flush_csv_every == 0:
                    out_fh.flush()
            else:
                batch.append(result)
                if len(batch) >= args.batch_size:
                    insert_batch(conn, batch, upsert=not bulk_insert_mode)
                    batch.clear()

            if args.limit and counts["processed"] >= args.limit:
                break

            if args.progress_every and counts["processed"] % args.progress_every == 0:
                elapsed = time.time() - started
                rate = counts["processed"] / elapsed if elapsed else 0
                print(
                    f"Processed {counts['processed']:,} grants "
                    f"({rate:,.0f}/sec); resolved={counts['ein_exact'] + counts['name_exact'] + counts['name_address_high_confidence'] + counts['address_unique'] + counts['address_narrowed_name_match'] + counts['fuzzy_probable'] + counts['possible_bad_ein_corrected']:,}; "
                    f"unresolved={counts['unresolved']:,}",
                    flush=True,
                )

        if batch:
            insert_batch(conn, batch, upsert=not bulk_insert_mode)
            batch.clear()
    finally:
        if out_fh is not None:
            out_fh.close()

    return counts


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Resolve raw IRS 990 grant recipients to likely organization EINs.")
    p.add_argument("--db", default=os.getenv("IRS_DB_PATH", DEFAULT_DB), help="Path to SQLite IRS 990 database.")
    p.add_argument("--dry-run", action="store_true", help="Do not write DB table; write results to CSV instead.")
    p.add_argument("--csv-out", default="grant_recipient_resolved_dry_run.csv", help="CSV output path for --dry-run.")
    p.add_argument("--full-refresh", action="store_true", help="Drop/recreate grant_recipient_resolved before processing all grants.")
    p.add_argument("--enable-fuzzy", action="store_true", help="Enable conservative fuzzy name fallback within geographic pools.")
    p.add_argument("--fuzzy-threshold", type=float, default=0.92, help="Minimum fuzzy name score when --enable-fuzzy is used.")
    p.add_argument("--accepted-ein-name-threshold", type=float, default=0.72, help="Minimum name similarity to accept a known reported EIN when recipient name exists.")
    p.add_argument("--bad-ein-name-threshold", type=float, default=0.55, help="Below this name score, a conflicting reported EIN is flagged as likely bad.")
    p.add_argument("--address-unique-min-name-score", type=float, default=0.35, help="Minimum name similarity to accept a unique exact address match when recipient name exists. Low by design because address is the primary signal.")
    p.add_argument("--address-name-threshold", type=float, default=0.72, help="Minimum name similarity to select one EIN when multiple EINs share the same exact address.")
    p.add_argument("--address-name-margin", type=float, default=0.08, help="Required gap between best and second-best name score when multiple EINs share an address.")
    p.add_argument("--batch-size", type=int, default=50_000, help="Rows per database commit in normal mode. Larger is faster but uses more memory.")
    p.add_argument("--progress-every", type=int, default=50_000, help="Progress interval; 0 disables progress messages.")
    p.add_argument("--flush-csv-every", type=int, default=100_000, help="Dry-run CSV flush interval; 0 disables periodic flush.")
    p.add_argument("--no-defer-indexes", action="store_true", help="In --full-refresh mode, create secondary indexes before loading rows. Slower; mainly for debugging.")
    p.add_argument("--limit", type=int, default=0, help="Process only N grant rows for testing; 0 means no limit.")
    p.add_argument("--min-grant-id", type=int, default=None, help="Optional lower grant.id bound for testing/incremental work.")
    p.add_argument("--max-grant-id", type=int, default=None, help="Optional upper grant.id bound for testing/incremental work.")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not Path(args.db).exists():
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2

    if args.dry_run and args.full_refresh:
        print("Note: --full-refresh has no effect in --dry-run mode.", flush=True)

    conn = connect(args.db, readonly=args.dry_run)
    try:
        defer_indexes = args.full_refresh and not args.no_defer_indexes
        if not args.dry_run:
            if args.full_refresh:
                if defer_indexes:
                    print(f"Full refresh requested; recreating {RESOLVED_TABLE} without secondary indexes for fast bulk load...", flush=True)
                else:
                    print(f"Full refresh requested; recreating {RESOLVED_TABLE} with indexes before load...", flush=True)
                full_refresh_table(conn, defer_indexes=defer_indexes)
            else:
                create_resolved_table(conn, create_indexes=True)

        print("Building organization identity index from returns...", flush=True)
        idx = build_org_index(conn)

        print("Resolving grant recipient rows...", flush=True)
        counts = process(conn, idx, args)

        if (not args.dry_run) and defer_indexes:
            print("Bulk load complete; creating indexes after load...", flush=True)
            create_resolved_indexes(conn)

        print("\nDone.")
        print(f"Processed: {counts['processed']:,}")
        for key, val in counts.most_common():
            if key != "processed":
                print(f"{key}: {val:,}")

        if args.dry_run:
            print(f"CSV written to: {args.csv_out}")
        else:
            print(f"Database table updated: {RESOLVED_TABLE}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
