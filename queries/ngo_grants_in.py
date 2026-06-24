# queries/ngo_grants_in.py
# Lists grants RECEIVED by orgs.
#
# Drop-in replacement with an optional enhanced matching mode.
#
# Legacy mode (default):
#   Uses grants_compat_v1.recipient_ein exactly as before.
#
# Enhanced mode:
#   Uses the grant-recipient resolution layer built by resolve_grant_recipients.py
#   + grant_ai_assist.py apply-decisions. Searches by final resolved recipient EIN
#   rather than only the EIN reported in the source filing.
#
# Required enhanced objects:
#   grant_recipient_resolved
#   grant_recipient_ai_applied
#   grant_recipient_resolved_plus_ai_v1
#
# Behavior:
# - If "Return all Non-Profits" is checked, EIN list is ignored.
#   Optionally filter by RECIPIENT state and/or tax-year range.
# - Otherwise, legacy mode matches provided EINs to g.recipient_ein.
# - Otherwise, enhanced mode matches provided EINs to final_resolved_ein.
# - Shows ALL filings/years with grants (no latest-only compression).

from typing import Iterable, List, Optional, Sequence, Tuple
from common import connect_ro, normalize_eins

META = {
    "key": "ngo_grants_in",
    "name": "NGO Grants Received (all filings with grants)",
    "description": (
        "Enter one or more recipient EINs (comma/semicolon/space/newline separated), "
        "or check 'Return all Non-Profits' to ignore EINs. "
        "Optionally filter by RECIPIENT state and/or a tax-year range. "
        "Default mode uses the original grant recipient EIN reported in the filing. "
        "Enhanced mode uses the resolved grant-recipient matching layer, including "
        "deterministic, reported-EIN, and accepted AI-assisted matches."
    ),
}

BASE_HEADERS = [
    # Recipient (grantee)
    "recipient_ein", "recipient_name",
    "recipient_city", "recipient_state", "recipient_country",
    # Grantmaker (filer)
    "grantor_ein", "grantor_org_name", "grantor_dba_name",
    "grantor_city", "grantor_state", "grantor_zip",
    # Filing context
    "tax_year", "return_type", "period_end", "filing_id",
    # Amounts and purpose
    "cash_amount", "noncash_amount", "total_amount",
    "purpose",
]

ENHANCED_AUDIT_HEADERS = [
    "grant_id",
    "reported_recipient_ein",
    "reported_recipient_name",
    "final_match_source",
    "final_confidence",
    "deterministic_match_status",
    "deterministic_match_method",
    "deterministic_confidence",
    "ai_decision",
    "ai_confidence",
]

HEADERS = BASE_HEADERS
META["headers"] = HEADERS

US_STATES = [
    "", "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
    "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]

ENHANCED_REQUIRED_OBJECTS = [
    "grant_recipient_resolved",
    "grant_recipient_ai_applied",
    "grant_recipient_resolved_plus_ai_v1",
]


def _val_js(val: str) -> str:
    return repr(val)


def _checked(form, key: str, default: bool = False) -> str:
    f = form or {}
    val = f.get(key)
    active = default if val is None else val in (True, "true", "on", "1", "yes", "y")
    return "checked" if active else ""


def _truthy(form, key: str, default: bool = False) -> bool:
    f = form or {}
    val = f.get(key)
    return default if val is None else val in (True, "true", "on", "1", "yes", "y")


def render_fields(form) -> str:
    f = form or {}
    val_eins = f.get("ein_list", "")
    return_all = _checked(f, "return_all")
    use_resolved = _checked(f, "use_resolved_grants")
    state_val = (f.get("state") or "").upper()
    min_year = str(f.get("min_year") or "")
    max_year = str(f.get("max_year") or "")

    state_options = ["<option value=\"\">(All recipient states)</option>"]
    for s in US_STATES:
        if not s:
            continue
        sel = " selected" if s == state_val else ""
        state_options.append(f"<option value=\"{s}\"{sel}>{s}</option>")
    state_html = "\n".join(state_options)

    return f"""
    <div class="row" style="display:flex; gap:16px; flex-wrap:wrap;">
      <div style="flex:1 1 320px;">
        <label><input type="checkbox" id="return_all" name="return_all" {return_all}>
          <b>Return all Non-Profits</b>
        </label>
        <div style="color:#666; font-size: 90%; margin-top:4px;">
          If checked, the EIN list is ignored; results are optionally filtered by <b>recipient</b> state and/or tax year.
        </div>
      </div>
      <div style="flex:0 1 260px;">
        <label><input type="checkbox" id="use_resolved_grants" name="use_resolved_grants" {use_resolved}>
          <b>Use enhanced grant-recipient matching</b>
        </label>
        <div style="color:#666; font-size: 90%; margin-top:4px;">
          When checked, searches by <code>final_resolved_ein</code> from <code>grant_recipient_resolved_plus_ai_v1</code>.
          When unchecked, uses the original reported <code>recipient_ein</code> from <code>grants_compat_v1</code>.
        </div>
      </div>
      <div style="flex:0 1 220px;">
        <label for="state"><b>Recipient state filter</b> (2-letter):</label><br>
        <select id="state" name="state" style="min-width:200px;">{state_html}</select>
        <div style="color:#666; font-size: 90%; margin-top:4px;">
          Leave blank for all states. In enhanced mode this filters on the recipient location reported in the grant row.
        </div>
      </div>
      <div style="flex:0 1 160px;">
        <label for="min_year"><b>Min tax year</b>:</label><br>
        <input id="min_year" name="min_year" type="number" inputmode="numeric" value="{min_year}" placeholder="e.g. 2019" style="width:140px;">
      </div>
      <div style="flex:0 1 160px;">
        <label for="max_year"><b>Max tax year</b>:</label><br>
        <input id="max_year" name="max_year" type="number" inputmode="numeric" value="{max_year}" placeholder="e.g. 2023" style="width:140px;">
      </div>
    </div>

    <div class="row" style="margin-top:12px;">
      <label for="ein_list"><b>Recipient EIN(s):</b></label><br>
      <textarea id="ein_list" name="ein_list" rows="6" placeholder="e.g. 131624102, 941156365; 52-6043385
123456789"></textarea>
      <script>
        (function() {{
          var el = document.getElementById('ein_list');
          el.value = {_val_js(val_eins)};
          function toggleEins() {{
            var checked = document.getElementById('return_all').checked;
            el.disabled = checked;
            el.style.backgroundColor = checked ? '#f3f3f3' : 'white';
          }}
          document.getElementById('return_all').addEventListener('change', toggleEins);
          toggleEins();
        }})();
      </script>
      <div style="color:#666; font-size: 90%; margin-top:4px;">
        Separate by commas, semicolons, spaces, or new lines. Non-digits ignored; valid 9-digit EINs are kept.
      </div>
    </div>
    """


# ---------------------------------------------------------------------------
# Legacy SQL: original grants_compat_v1 behavior.
# ---------------------------------------------------------------------------

_SQL_LEGACY = """
WITH gsrc AS (
  SELECT
    g.filing_id,
    g.recipient_ein,
    g.recipient_name,
    g.city    AS recipient_city,
    g.state   AS recipient_state,
    g.country AS recipient_country,
    g.cash_amount,
    g.noncash_amount,
    g.purpose
  FROM grants_compat_v1 g
  {g_where_clause}
)
SELECT
  -- Recipient
  gsrc.recipient_ein         AS recipient_ein,
  gsrc.recipient_name        AS recipient_name,
  gsrc.recipient_city        AS recipient_city,
  gsrc.recipient_state       AS recipient_state,
  gsrc.recipient_country     AS recipient_country,

  -- Grantmaker (filer)
  rf.ein                     AS grantor_ein,
  rf.org_name                AS grantor_org_name,
  rf.dba_name                AS grantor_dba_name,
  rf.city                    AS grantor_city,
  rf.state                   AS grantor_state,
  rf.zip                     AS grantor_zip,

  -- Filing context
  c.tax_year                 AS tax_year,
  c.return_type              AS return_type,
  c.period_end               AS period_end,
  c.filing_id                AS filing_id,

  -- Amounts & purpose
  gsrc.cash_amount           AS cash_amount,
  gsrc.noncash_amount        AS noncash_amount,
  (COALESCE(gsrc.cash_amount,0) + COALESCE(gsrc.noncash_amount,0)) AS total_amount,
  gsrc.purpose               AS purpose

FROM gsrc
JOIN canonical_by_ein_year c ON c.filing_id = gsrc.filing_id
JOIN returns rf              ON rf.filing_id = c.filing_id
{c_where_clause}
ORDER BY recipient_ein, c.tax_year DESC, total_amount DESC, grantor_org_name, grantor_ein
"""


# ---------------------------------------------------------------------------
# Enhanced SQL: final resolved recipient EIN/name.
#
# For specific EIN searches, use a UNION that can take advantage of indexes on:
#   grant_recipient_ai_applied(selected_ein)
#   grant_recipient_resolved(resolved_ein)
#
# For Return All mode, use one scan over the resolved/apply layer.
# ---------------------------------------------------------------------------

_ENHANCED_SELECT = """
SELECT
  -- Final recipient identity used for searching/reporting
  gsrc.final_resolved_ein AS recipient_ein,
  COALESCE(NULLIF(gsrc.final_resolved_org_name,''), NULLIF(gsrc.recipient_reported_name,'')) AS recipient_name,
  gsrc.recipient_city AS recipient_city,
  gsrc.recipient_state AS recipient_state,
  CASE
    WHEN COALESCE(NULLIF(TRIM(g.us_state_abbreviation_cd), ''), NULLIF(TRIM(gsrc.recipient_state), '')) IS NOT NULL THEN 'US'
    ELSE g.foreign_country_cd
  END AS recipient_country,

  -- Grantmaker (filer)
  rf.ein                     AS grantor_ein,
  rf.org_name                AS grantor_org_name,
  rf.dba_name                AS grantor_dba_name,
  rf.city                    AS grantor_city,
  rf.state                   AS grantor_state,
  rf.zip                     AS grantor_zip,

  -- Filing context
  c.tax_year                 AS tax_year,
  c.return_type              AS return_type,
  c.period_end               AS period_end,
  c.filing_id                AS filing_id,

  -- Amounts & purpose
  gsrc.cash_amount           AS cash_amount,
  gsrc.noncash_amount        AS noncash_amount,
  (COALESCE(gsrc.cash_amount,0) + COALESCE(gsrc.noncash_amount,0)) AS total_amount,
  gsrc.purpose               AS purpose,

  -- Enhanced match audit columns
  gsrc.grant_id                  AS grant_id,
  gsrc.recipient_reported_ein    AS reported_recipient_ein,
  gsrc.recipient_reported_name   AS reported_recipient_name,
  gsrc.final_match_source        AS final_match_source,
  gsrc.final_confidence          AS final_confidence,
  gsrc.match_status              AS deterministic_match_status,
  gsrc.match_method              AS deterministic_match_method,
  gsrc.deterministic_confidence  AS deterministic_confidence,
  gsrc.ai_decision               AS ai_decision,
  gsrc.ai_confidence             AS ai_confidence
FROM gsrc
JOIN canonical_by_ein_year c ON c.filing_id = gsrc.filing_id
JOIN returns rf              ON rf.filing_id = c.filing_id
LEFT JOIN grants g           ON g.id = gsrc.grant_id
{c_where_clause}
ORDER BY recipient_ein, c.tax_year DESC, total_amount DESC, grantor_org_name, grantor_ein
"""

_ENHANCED_ALL_CTE = """
WITH gsrc AS (
  SELECT
    rr.grant_id,
    rr.filing_id,
    rr.recipient_reported_ein,
    rr.recipient_reported_name,
    rr.recipient_city,
    rr.recipient_state,
    rr.cash_amount,
    rr.noncash_amount,
    rr.purpose,
    CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN aa.selected_ein ELSE rr.resolved_ein END AS final_resolved_ein,
    CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN aa.selected_name ELSE rr.resolved_org_name END AS final_resolved_org_name,
    CASE
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' AND aa.model='rule:reported_ein_identity_lookup' THEN 'reported_ein_identity_lookup'
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' AND aa.model='rule:reported_ein_from_filing_unverified' THEN 'reported_ein_from_filing_unverified'
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' AND aa.model LIKE 'rule:%' THEN 'reported_ein_rule'
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN 'ai_assisted'
      ELSE 'deterministic'
    END AS final_match_source,
    CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN aa.ai_confidence ELSE rr.confidence END AS final_confidence,
    rr.match_status,
    rr.match_method,
    rr.confidence AS deterministic_confidence,
    aa.ai_decision,
    aa.ai_confidence
  FROM grant_recipient_resolved rr
  LEFT JOIN grant_recipient_ai_applied aa ON aa.grant_id = rr.grant_id
  {rr_where_clause}
)
"""

_ENHANCED_TARGETED_CTE = """
WITH gsrc AS (
  -- Applied AI/rule matches, searched by selected_ein.
  SELECT
    rr.grant_id,
    rr.filing_id,
    rr.recipient_reported_ein,
    rr.recipient_reported_name,
    rr.recipient_city,
    rr.recipient_state,
    rr.cash_amount,
    rr.noncash_amount,
    rr.purpose,
    aa.selected_ein AS final_resolved_ein,
    aa.selected_name AS final_resolved_org_name,
    CASE
      WHEN aa.model='rule:reported_ein_identity_lookup' THEN 'reported_ein_identity_lookup'
      WHEN aa.model='rule:reported_ein_from_filing_unverified' THEN 'reported_ein_from_filing_unverified'
      WHEN aa.model LIKE 'rule:%' THEN 'reported_ein_rule'
      ELSE 'ai_assisted'
    END AS final_match_source,
    aa.ai_confidence AS final_confidence,
    rr.match_status,
    rr.match_method,
    rr.confidence AS deterministic_confidence,
    aa.ai_decision,
    aa.ai_confidence
  FROM grant_recipient_ai_applied aa
  JOIN grant_recipient_resolved rr ON rr.grant_id = aa.grant_id
  WHERE aa.selected_ein IN ({placeholders})
  {applied_rr_extra_where}

  UNION ALL

  -- Deterministic matches that were not replaced by an applied AI/rule match.
  SELECT
    rr.grant_id,
    rr.filing_id,
    rr.recipient_reported_ein,
    rr.recipient_reported_name,
    rr.recipient_city,
    rr.recipient_state,
    rr.cash_amount,
    rr.noncash_amount,
    rr.purpose,
    rr.resolved_ein AS final_resolved_ein,
    rr.resolved_org_name AS final_resolved_org_name,
    'deterministic' AS final_match_source,
    rr.confidence AS final_confidence,
    rr.match_status,
    rr.match_method,
    rr.confidence AS deterministic_confidence,
    CAST(NULL AS TEXT) AS ai_decision,
    CAST(NULL AS NUMERIC) AS ai_confidence
  FROM grant_recipient_resolved rr
  LEFT JOIN grant_recipient_ai_applied aa ON aa.grant_id = rr.grant_id
  WHERE aa.grant_id IS NULL
    AND rr.resolved_ein IN ({placeholders})
  {det_rr_extra_where}
)
"""


def _parse_eins(form) -> List[str]:
    text = (form or {}).get("ein_list", "")
    return normalize_eins(text)


def _to_int(x) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


def _parse_filters(form) -> Tuple[bool, bool, Optional[str], Optional[int], Optional[int]]:
    f = form or {}
    return_all = f.get("return_all") in (True, "true", "on", "1")
    use_resolved = _truthy(f, "use_resolved_grants")
    state = (f.get("state") or "").strip().upper()
    if state and state not in US_STATES:
        state = None

    min_year = _to_int(f.get("min_year"))
    max_year = _to_int(f.get("max_year"))
    if min_year and not max_year:
        max_year = min_year
    elif max_year and not min_year:
        min_year = max_year
    if min_year and max_year and min_year > max_year:
        min_year, max_year = max_year, min_year

    return return_all, use_resolved, (state if state else None), min_year, max_year


def headers_for_form(form) -> List[str]:
    _, use_resolved, _, _, _ = _parse_filters(form)
    return BASE_HEADERS + ENHANCED_AUDIT_HEADERS if use_resolved else BASE_HEADERS


def export_headers(form) -> List[str]:
    return headers_for_form(form)


def _object_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def _ensure_enhanced_objects(conn) -> None:
    missing = [name for name in ENHANCED_REQUIRED_OBJECTS if not _object_exists(conn, name)]
    if missing:
        raise RuntimeError(
            "Enhanced grant matching was selected, but required object(s) are missing: "
            + ", ".join(missing)
            + ". Run the grant resolver and then `python grant_ai_assist_v1.py apply-decisions --full-refresh`."
        )


def _build_legacy_where(return_all: bool, eins: List[str], state: Optional[str],
                        min_year: Optional[int], max_year: Optional[int]):
    g_clauses = []
    g_params: List = []

    c_clauses = []
    c_params: List = []

    if not return_all:
        if not eins:
            return "WHERE 1=0", [], "", []
        placeholders = ",".join("?" for _ in eins)
        g_clauses.append(f"g.recipient_ein IN ({placeholders})")
        g_params.extend(eins)

    if state:
        g_clauses.append("g.state = ?")
        g_params.append(state)

    if (min_year is not None) and (max_year is not None):
        c_clauses.append("c.tax_year BETWEEN ? AND ?")
        c_params.extend([min_year, max_year])

    g_where = ("WHERE " + " AND ".join(g_clauses)) if g_clauses else ""
    c_where = ("WHERE " + " AND ".join(c_clauses)) if c_clauses else ""
    return g_where, g_params, c_where, c_params


def _build_enhanced_sql(return_all: bool, eins: List[str], state: Optional[str],
                        min_year: Optional[int], max_year: Optional[int]) -> Tuple[str, List]:
    c_clauses = []
    c_params: List = []
    if (min_year is not None) and (max_year is not None):
        c_clauses.append("c.tax_year BETWEEN ? AND ?")
        c_params.extend([min_year, max_year])
    c_where = ("WHERE " + " AND ".join(c_clauses)) if c_clauses else ""

    if return_all:
        rr_clauses = []
        rr_params: List = []
        if state:
            rr_clauses.append("rr.recipient_state = ?")
            rr_params.append(state)
        rr_where = ("WHERE " + " AND ".join(rr_clauses)) if rr_clauses else ""
        sql = _ENHANCED_ALL_CTE.format(rr_where_clause=rr_where) + _ENHANCED_SELECT.format(c_where_clause=c_where)
        return sql, rr_params + c_params

    if not eins:
        rr_where = "WHERE 1=0"
        sql = _ENHANCED_ALL_CTE.format(rr_where_clause=rr_where) + _ENHANCED_SELECT.format(c_where_clause=c_where)
        return sql, c_params

    placeholders = ",".join("?" for _ in eins)
    applied_extra = ""
    det_extra = ""
    params: List = []
    params.extend(eins)
    if state:
        applied_extra = "AND rr.recipient_state = ?"
        params.append(state)
    params.extend(eins)
    if state:
        det_extra = "AND rr.recipient_state = ?"
        params.append(state)

    cte = _ENHANCED_TARGETED_CTE.format(
        placeholders=placeholders,
        applied_rr_extra_where=applied_extra,
        det_rr_extra_where=det_extra,
    )
    sql = cte + _ENHANCED_SELECT.format(c_where_clause=c_where)
    return sql, params + c_params


def _query_legacy(eins: List[str], return_all: bool, state: Optional[str],
                  min_year: Optional[int], max_year: Optional[int]) -> List[Tuple]:
    g_where, g_params, c_where, c_params = _build_legacy_where(return_all, eins, state, min_year, max_year)
    sql = _SQL_LEGACY.format(g_where_clause=g_where, c_where_clause=c_where)
    conn = connect_ro()
    cur = conn.execute(sql, g_params + c_params)
    return cur.fetchall()


def _query_enhanced(eins: List[str], return_all: bool, state: Optional[str],
                    min_year: Optional[int], max_year: Optional[int]) -> List[Tuple]:
    conn = connect_ro()
    _ensure_enhanced_objects(conn)
    sql, params = _build_enhanced_sql(return_all, eins, state, min_year, max_year)
    cur = conn.execute(sql, params)
    return cur.fetchall()


def run(form):
    eins = _parse_eins(form)
    return_all, use_resolved, state, min_year, max_year = _parse_filters(form)
    if use_resolved:
        rows = _query_enhanced(eins, return_all, state, min_year, max_year)
    else:
        rows = _query_legacy(eins, return_all, state, min_year, max_year)
    return headers_for_form(form), rows


def export_rows(form) -> Iterable[Tuple]:
    eins = _parse_eins(form)
    return_all, use_resolved, state, min_year, max_year = _parse_filters(form)
    if use_resolved:
        return _query_enhanced(eins, return_all, state, min_year, max_year)
    return _query_legacy(eins, return_all, state, min_year, max_year)
