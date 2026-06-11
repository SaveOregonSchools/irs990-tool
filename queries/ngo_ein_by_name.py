# queries/ngo_ein_by_name.py
# Bulk EIN finder by organization names with a SIMPLE-first strategy.
# Default: punctuation/spacing-insensitive canonical equality (no fuzz).
# Optional: per-name fuzzy fallback when enabled via checkbox.
#
# Output columns (same order you wanted):
#   search_name, org_name, ein, state, tax_year, filing_id, match_score, source
#
from typing import List, Tuple, Iterable, Optional
from common import connect_ro
import re
from difflib import SequenceMatcher
from datetime import datetime  # <- add this import at the top with the others

META = {
    "key": "ngo_ein_by_name",
    "name": "Find EINs by Organization Name (simple first, optional fuzzy)",
    "description": (
        "Paste a list of organization names. By default uses punctuation/spacing-insensitive matching "
        "(&↔AND, Inc.↔Inc, Corp.↔Corp, Co/Company, Ltd/Limited, optional leading THE). "
        "Optionally enable fuzzy matching per name if no simple match is found."
    ),
}

HEADERS = ["search_name", "org_name", "ein", "state", "website", "tax_year", "filing_id", "match_score", "source"]
META["headers"] = HEADERS

US_STATES = [
    "", "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
    "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]

# ---------- normalization helpers (SIMPLE MODE) ----------
_WS = re.compile(r"\s+")
# characters to strip -> space
_PUNCT = re.compile(r"[.,'\"()\/\\\-]+")  # note: hyphen becomes space
# unify some synonyms BEFORE stripping
def _pre_unify(s: str) -> str:
    s = s.upper()
    # replace & with AND but keep spaces around to avoid "LAND" edge cases
    s = s.replace("&", " AND ")
    # normalize long forms to short canonical tokens
    repl = {
        " INCORPORATED ": " INC ",
        " CORPORATION ": " CORP ",
        " LIMITED ": " LTD ",
        " COMPANY ": " CO ",
    }
    # pad with spaces to ease whole word replacements
    s = f" {s} "
    for k, v in repl.items():
        s = s.replace(k, v)
    return s.strip()

_SUFFIXES = {"INC","CORP","CO","LTD","LLC"}  # only corporate suffixes; we do NOT remove FOUNDATION, ASSOC, etc.

def _canon_tokens(s: Optional[str]) -> List[str]:
    if not s: return []
    s = _pre_unify(s)
    s = _PUNCT.sub(" ", s)        # strip punctuation -> space
    s = _WS.sub(" ", s).strip()   # collapse whitespace
    toks = s.split(" ")
    # collapse empty and trim
    toks = [t for t in toks if t]
    return toks

def _strip_leading_the(toks: List[str]) -> List[str]:
    return toks[1:] if toks and toks[0] == "THE" else toks

def _strip_trailing_suffixes(toks: List[str]) -> List[str]:
    i = len(toks)
    # consume trailing suffix tokens
    while i > 0 and toks[i-1] in _SUFFIXES:
        i -= 1
    return toks[:i]

# --- simple 1-entry cache so export doesn't re-run the query ---
_LAST_KEY = None
_LAST_ROWS = None
_LAST_STATUS = "idle"   # <-- NEW: human-readable status string for the UI

def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _cache_key_from_parsed(parsed_tuple):
    """
    Build a stable key from the already-parsed inputs so we don't redo work.
    parsed_tuple is exactly what _parse(form) returns.
    """
    (names, state, min_year, max_year, include_alt, distinct_ein,
     max_hits_per_name, max_total_rows, _max_names, fuzzy_enabled, fuzzy_threshold) = parsed_tuple

    # We key on everything that affects results (not on order of names parsing internals).
    # Tuple elements must be hashable; convert names list to a tuple.
    return (
        "v5",
        tuple(names),
        state, min_year, max_year, bool(include_alt), bool(distinct_ein),
        int(max_hits_per_name), int(max_total_rows), bool(fuzzy_enabled), float(fuzzy_threshold)
    )

def _canon_forms(s: Optional[str]) -> List[str]:
    """Return canonical string variants we consider 'equal'."""
    t = _canon_tokens(s)
    if not t: return []
    forms = []
    # core
    core = " ".join(t); forms.append(core)
    # no leading THE
    nt = _strip_leading_the(t); forms.append(" ".join(nt) if nt else "")
    # no trailing suffixes
    ts = _strip_trailing_suffixes(t); forms.append(" ".join(ts) if ts else "")
    # both: no THE + no suffix
    nts = _strip_trailing_suffixes(_strip_leading_the(t))
    forms.append(" ".join(nts) if nts else "")
    # dedupe preserving order
    seen, out = set(), []
    for f in forms:
        if f and f not in seen:
            seen.add(f); out.append(f)
    return out

def _simple_equal(a: str, b: str) -> Tuple[bool, float]:
    """Deterministic equivalence. Returns (match?, score). Score encodes which variant matched."""
    af = _canon_forms(a); bf = _canon_forms(b)
    a0, a1, a2, a3 = (af + ["", "", "", ""])[:4]
    b0, b1, b2, b3 = (bf + ["", "", "", ""])[:4]
    # strongest equality: core==core
    if a0 and a0 == b0: return True, 1.00
    # leading THE removal
    if a1 and (a1 == b0 or a1 == b1): return True, 0.99
    if b1 and (a0 == b1): return True, 0.99
    # trailing suffix removal
    if a2 and (a2 == b0 or a2 == b2): return True, 0.98
    if b2 and (a0 == b2): return True, 0.98
    # both adjustments
    combos = {a3, b3} - {""}
    if combos and (a3 in bf or b3 in af): return True, 0.97
    return False, 0.0

# ---------- fuzzy helpers (optional fallback) ----------
def _fuzzy_score(a: str, b: str) -> float:
    # Reuse the canon 'core' (first form) to avoid punctuation noise in ratio
    af = _canon_forms(a)
    bf = _canon_forms(b)
    a_core = af[0] if af else ""
    b_core = bf[0] if bf else ""
    if not a_core or not b_core:
        return 0.0
    return round(SequenceMatcher(None, a_core, b_core).ratio(), 4)

# ---------- input parsing ----------
def _toi(x):
    try: return int(str(x).strip())
    except: return None

def _split_names(blob: str, max_names: int) -> List[str]:
    if not blob: return []
    cleaned = re.sub(r"[,\t;]+", "\n", blob)
    parts = [p.strip().strip('"').strip("'") for p in cleaned.splitlines()]
    seen = set(); out = []
    for p in parts:
        if not p: continue
        k = p.upper()
        if k in seen: continue
        seen.add(k)
        out.append(p)
        if len(out) >= max_names:
            break
    return out

def _parse(form):
    f = form or {}
    names_blob = (f.get("names") or "").strip()
    q_single   = (f.get("q") or "").strip()
    max_names  = int(f.get("max_names") or 200)
    names = _split_names(names_blob, max_names) if names_blob else ([q_single] if q_single else [])

    state    = (f.get("state") or "").strip().upper() or None
    min_year = _toi(f.get("min_year"))
    max_year = _toi(f.get("max_year"))
    if min_year and not max_year: max_year = min_year
    if max_year and not min_year: min_year = max_year
    if min_year and max_year and min_year > max_year: min_year, max_year = max_year, min_year

    include_alt      = f.get("include_alt") in (True, "true", "on", "1")  # test DBA/InCare in simple pass too
    distinct_ein     = f.get("distinct_ein") not in ("false", False, None, "", 0)  # default True
    try:
        max_hits_per_name = max(1, min(1000, int(f.get("max_hits_per_name") or 5)))
    except:
        max_hits_per_name = 5
    try:
        max_total_rows = max(1, min(1048000, int(f.get("max_total_rows") or 5000)))
    except:
        max_total_rows = 5000

    # fuzzy controls
    fuzzy_enabled  = f.get("fuzzy_enabled") in (True, "true", "on", "1")
    try:
        fuzzy_threshold = float(f.get("fuzzy_threshold") or 0.86)
    except:
        fuzzy_threshold = 0.86

    return (names, state, min_year, max_year, include_alt, distinct_ein,
            max_hits_per_name, max_total_rows, max_names, fuzzy_enabled, fuzzy_threshold)

# ---------- UI ----------
def _state_options(selected: str) -> str:
    opts = ['<option value="">(Any)</option>']
    for s in US_STATES:
        if not s: continue
        sel = " selected" if s == selected else ""
        opts.append(f'<option value="{s}"{sel}>{s}</option>')
    return "\n".join(opts)

def _html(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def render_fields(form) -> str:
    f = form or {}
    names_val = f.get("names", "") or (f.get("q", "") or "")
    state_val = (f.get("state") or "").upper()
    min_year = str(f.get("min_year") or "")
    max_year = str(f.get("max_year") or "")
    include_alt = f.get("include_alt") in (True, "true", "on", "1")
    distinct_ein = f.get("distinct_ein") not in ("false", False, None, "", 0)
    max_hits_per_name = str(f.get("max_hits_per_name") or "5")
    max_total_rows = str(f.get("max_total_rows") or "5000")
    max_names = str(f.get("max_names") or "200")
    fuzzy_enabled = f.get("fuzzy_enabled") in (True, "true", "on", "1")
    fuzzy_threshold = str(f.get("fuzzy_threshold") or "0.86")

    return f"""
    <div class="row" style="display:flex; gap:16px; flex-wrap:wrap;">
      <div style="flex:1 1 600px;">
        <label for="names"><b>Organization names (paste list)</b></label><br>
        <textarea id="names" name="names" rows="8" style="min-width:600px;">{_html(names_val)}</textarea>
        <div style="color:#666;font-size:90%;margin-top:4px;">
          Simple match ignores punctuation (.,'"/-), normalizes "&rarr;AND", and treats Inc./Inc, Corp./Corp, Co/Company, Ltd/Limited the same.
          Leading "THE" is optional. No fuzziness unless enabled below.
        </div>
      </div>
      <div style="flex:0 1 160px;">
        <label for="state"><b>Filer State</b></label><br>
        <select id="state" name="state" style="min-width:140px;">{_state_options(state_val)}</select>
      </div>
      <div style="flex:0 1 140px;">
        <label for="min_year"><b>Min tax year</b></label><br>
        <input id="min_year" name="min_year" type="number" inputmode="numeric" value="{min_year}" placeholder="e.g. 2019" style="width:120px;">
      </div>
      <div style="flex:0 1 140px;">
        <label for="max_year"><b>Max tax year</b></label><br>
        <input id="max_year" name="max_year" type="number" inputmode="numeric" value="{max_year}" placeholder="e.g. 2025" style="width:120px;">
      </div>
    </div>
    <div class="row" style="display:flex; gap:20px; align-items:center; margin-top:8px;">
      <label><input type="checkbox" name="include_alt" {"checked" if include_alt else ""}>
        Also test DBA and In-Care-Of in simple matching
      </label>
      <label><input type="checkbox" name="distinct_ein" {"checked" if distinct_ein else ""}>
        One row per EIN (latest filing)
      </label>
      <label>Hits per name:
        <input type="number" name="max_hits_per_name" value="{max_hits_per_name}" min="1" max="1000" style="width:90px;">
      </label>
      <label>Total row cap:
        <input type="number" name="max_total_rows" value="{max_total_rows}" min="1" max="1048000" style="width:110px;">
      </label>
      <label>Max names:
        <input type="number" name="max_names" value="{max_names}" min="1" max="5000" style="width:90px;">
      </label>
    </div>
    <div class="row" style="display:flex; gap:20px; align-items:center; margin-top:8px;">
      <label><input type="checkbox" name="fuzzy_enabled" {"checked" if fuzzy_enabled else ""}>
        If no simple match, try fuzzy (SequenceMatcher)
      </label>
      <label>Fuzzy threshold:
        <input type="number" step="0.01" name="fuzzy_threshold" value="{fuzzy_threshold}" min="0.50" max="0.99" style="width:90px;">
      </label>
    </div>
    <div class="row" style="margin-top:10px;">
      <div style="
        display:inline-block;
        font-size:12px;
        color:#444;
        background:#f3f4f6;
        border:1px solid #e5e7eb;
        border-radius:6px;
        padding:6px 10px;">
        <b>Status:</b> {_html(globals().get("_LAST_STATUS", "idle"))}
      </div>
    </div>
    """

# ---------- candidate retrieval (LIKE; robust and index-friendly) ----------
def _candidate_tokens(name: str) -> List[str]:
    toks = _canon_tokens(name)
    # Prefer 2 strongest tokens (length desc) that are not common suffixes
    toks = [t for t in toks if t not in _SUFFIXES and t != "THE"]
    toks.sort(key=len, reverse=True)
    return toks[:2] if toks else []

def _candidate_rows_like(conn, name: str, state, min_year, max_year, limit: int, include_alt: bool):
    toks = _candidate_tokens(name)
    if not toks:
        return []
    where = []
    params: List = []
    # token constraints (use AND if 2 tokens)
    cols = ["org_name"]
    if include_alt:
        cols += ["dba_name", "in_care_of_name"]
    def col_like_clause(token: str) -> str:
        sub = " OR ".join(f"{c} LIKE ?" for c in cols)
        params.extend([f"%{token}%"] * len(cols))
        return f"({sub})"
    where.append(col_like_clause(toks[0]))
    if len(toks) > 1:
        where.append(col_like_clause(toks[1]))
    if state:
        where.append("state = ?"); params.append(state)
    if (min_year is not None) and (max_year is not None):
        where.append("tax_year BETWEEN ? AND ?"); params.extend([min_year, max_year])
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    sql = f"""
      SELECT ein, org_name, state, website, tax_year, filing_id, dba_name, in_care_of_name
      FROM returns
      {where_sql}
      ORDER BY tax_year DESC, filing_id DESC
      LIMIT ?
    """
    rows = conn.execute(sql, (*params, limit)).fetchall()
    return rows

# ---------- matching passes ----------
def _simple_pass(name: str, rows, include_alt: bool, distinct_ein: bool, max_hits: int):
    found = []
    seen_ein = set()
    for (ein, org_name, st, web, yr, fid, dba, ico) in rows:
        ok, score = _simple_equal(name, org_name)
        if not ok and include_alt:
            # try against alt display names
            ok1, sc1 = _simple_equal(name, dba or "")
            ok2, sc2 = _simple_equal(name, ico or "")
            if ok1 or ok2:
                ok = True
                score = max(sc1, sc2)
        if not ok:
            continue
        if distinct_ein and ein in seen_ein:
            continue
        seen_ein.add(ein)
        found.append((name, org_name, ein, st, web, yr, fid, score, "SIMPLE"))
        if len(found) >= max_hits:
            break
    # prefer higher score, then newest year
    found.sort(key=lambda r: (r[7], r[5]), reverse=True)
    return found[:max_hits]

def _fuzzy_pass(name: str, rows, include_alt: bool, distinct_ein: bool, max_hits: int, threshold: float):
    hits = []
    seen_ein = set()
    for (ein, org_name, st, web, yr, fid, dba, ico) in rows:
        score = _fuzzy_score(name, org_name)
        if include_alt and dba:
            score = max(score, _fuzzy_score(name, dba))
        if include_alt and ico:
            score = max(score, _fuzzy_score(name, ico))
        if score < threshold:
            continue
        if distinct_ein and ein in seen_ein:
            continue
        seen_ein.add(ein)
        hits.append((name, org_name, ein, st, web, yr, fid, score, "FUZZY"))
        if len(hits) >= max_hits:
            break
    hits.sort(key=lambda r: (r[7], r[5]), reverse=True)
    return hits[:max_hits]

# ---------- public API ----------
def _compute_rows(parsed):
    (names, state, min_year, max_year, include_alt, distinct_ein,
     max_hits_per_name, max_total_rows, _max_names, fuzzy_enabled, fuzzy_threshold) = parsed

    conn = connect_ro()
    out = []

    for name in names:
        cand = _candidate_rows_like(
            conn, name, state, min_year, max_year,
            limit=max(max_hits_per_name * 50, 500),
            include_alt=include_alt
        )

        rows = _simple_pass(name, cand, include_alt, distinct_ein, max_hits_per_name)

        if not rows and fuzzy_enabled:
            rows = _fuzzy_pass(name, cand, include_alt, distinct_ein, max_hits_per_name, fuzzy_threshold)

        out.extend(rows)
        if len(out) >= max_total_rows:
            out = out[:max_total_rows]
            break

    # final sort: SIMPLE over FUZZY, then score, then year
    out.sort(key=lambda r: (0 if r[8] == "SIMPLE" else 1, r[7], r[5]), reverse=True)
    return out


def run(form):
    parsed = _parse(form)
    rows = _compute_rows(parsed)

    # update cache
    global _LAST_KEY, _LAST_ROWS, _LAST_STATUS
    _LAST_KEY = _cache_key_from_parsed(parsed)
    _LAST_ROWS = rows
    _LAST_STATUS = f"Ran at {_ts()} • {len(rows)} row(s) • cache primed"

    return HEADERS, rows



def export_rows(form):
    global _LAST_KEY, _LAST_ROWS, _LAST_STATUS
    parsed = _parse(form)
    key_now = _cache_key_from_parsed(parsed)

    # If the form hasn't changed since run(), reuse results instantly.
    if _LAST_KEY == key_now and _LAST_ROWS is not None:
        _LAST_STATUS = f"Exported from cache at {_ts()} • {len(_LAST_ROWS)} row(s)"
        for r in _LAST_ROWS:
            yield r
        return

    # Otherwise, compute (and refresh the cache) just once.
    rows = _compute_rows(parsed)
    _LAST_KEY = key_now
    _LAST_ROWS = rows
    _LAST_STATUS = f"Export recomputed at {_ts()} • {len(rows)} row(s)"
    for r in rows:
        yield r
