# queries/ngo_grants_out.py
# Modeled after ngo_core_data_v4, but focused on "grants paid" from the most recent filing per EIN.
# Uses: canonical_by_ein_year, returns, grants
#
# Behavior:
# - If "Return all Non-Profits" is checked, EIN list is ignored and we consider all filers,
#   optionally filtered by filer state and/or tax year range.
# - Otherwise, we consider only the provided EINs.
# - From the resulting candidate (EIN, tax_year) rows, we pick the most recent filing per EIN
#   (max tax_year; tiebreak on return_ts), then list every grant row tied to that filing_id.
#
# Fields referenced (per schema):
# canonical_by_ein_year: ein, tax_year, filing_id, return_type, return_ts, period_end
# returns: filing_id, ein, org_name, dba_name, city, state, zip
# grants_compat_v1: filing_id, recipient_ein, recipient_name, city, state, country, cash_amount, noncash_amount, purpose
#
# Optional enhanced mode keeps filer-EIN search semantics, but displays final
# resolved grant-recipient identity and match audit fields.

from typing import List, Tuple, Iterable, Optional
from common import connect_ro, normalize_eins

META = {
    "key": "ngo_grants_out",
    "name": "Grants Paid (all filings with grants)",
    "description": (
        "Enter one or more EINs (comma/semicolon/space/newline separated), "
        "or check 'Return all Non-Profits' to ignore EINs. "
        "Optionally filter by filer state (2-letter code) and/or a tax-year range. "
        "For each relevant EIN and tax year, lists every grant paid (from the grants_compat_v1 view)."
    ),
}

BASE_HEADERS = [
    # Filer (grant maker)
    "filer_ein", "filer_org_name", "filer_dba_name",
    "filer_city", "filer_state", "filer_zip",
    "tax_year", "return_type", "period_end", "filing_id",
    # Recipient (grantee)
    "recipient_ein", "recipient_name",
    "recipient_city", "recipient_state", "recipient_country",
    "cash_amount", "noncash_amount", "total_amount",
    "purpose",
]

ENHANCED_AUDIT_HEADERS = [
    "match_reliability_bucket",
    "match_needs_spot_check",
    "grant_id",
    "reported_recipient_ein",
    "reported_recipient_name",
    "reported_recipient_address1",
    "reported_recipient_address2",
    "reported_recipient_city",
    "reported_recipient_state",
    "reported_recipient_zip",
    "reported_recipient_country",
    "final_match_source",
    "final_confidence",
    "deterministic_match_status",
    "deterministic_match_method",
    "deterministic_confidence",
    "deterministic_name_score",
    "deterministic_address_score",
    "deterministic_warning_flags",
    "ai_model",
    "ai_decision",
    "ai_selected_candidate_id",
    "ai_confidence",
    "ai_reason_codes",
    "ai_explanation",
    "ai_needs_human_review",
    "ai_auto_accept",
    "ai_validation_status",
    "ai_validation_error",
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
    "grant_recipient_ai_decision",
    "grant_recipient_resolved_plus_ai_v1",
]

def _val_js(val: str) -> str:
    # Safe JS string (basic)
    return repr(val)

def _truthy(form, key: str, default: bool = False) -> bool:
    f = form or {}
    val = f.get(key)
    return default if val is None else val in (True, "true", "on", "1", "yes", "y")

def _checked(form, key: str, default: bool = False) -> str:
    return "checked" if _truthy(form, key, default) else ""

def render_fields(form) -> str:
    f = form or {}
    val_eins = f.get("ein_list", "")
    return_all = "checked" if f.get("return_all") in (True, "true", "on", "1") else ""
    use_resolved = _checked(f, "use_resolved_grants")
    state_val = (f.get("state") or "").upper()
    min_year = str(f.get("min_year") or "")
    max_year = str(f.get("max_year") or "")

    state_options = ["<option value=\"\">(All states)</option>"]
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
          If checked, the EIN list is ignored; results are optionally filtered by filer state and/or tax year. For each EIN, the most recent filing is used.
        </div>
      </div>
      <div style="flex:0 1 260px;">
        <label><input type="checkbox" id="use_resolved_grants" name="use_resolved_grants" {use_resolved}>
          <b>Use enhanced grant-recipient matching</b>
        </label>
        <div style="color:#666; font-size: 90%; margin-top:4px;">
          When checked, recipient columns use resolved grant-recipient matches and exports include match audit fields.
        </div>
      </div>
      <div style="flex:0 1 200px;">
        <label for="state"><b>Filer state filter</b> (2-letter):</label><br>
        <select id="state" name="state" style="min-width:180px;">{state_html}</select>
        <div style="color:#666; font-size: 90%; margin-top:4px;">
          Leave blank for all states.
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
      <label for="ein_list"><b>EIN(s):</b></label><br>
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

# Build the core SQL using CTEs so we can pick the most recent filing per EIN (within filtered candidates).
_SQL_BASE = """
WITH candidates AS (
  SELECT
    c.ein,
    c.tax_year,
    c.filing_id,
    c.return_type,
    c.return_ts,
    c.period_end
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  {where_clause}
),
candidates_with_grants AS (
  SELECT DISTINCT c.*
  FROM candidates c
  JOIN grants_compat_v1 g ON g.filing_id = c.filing_id
)
SELECT
  rf.ein              AS filer_ein,
  rf.org_name         AS filer_org_name,
  rf.dba_name         AS filer_dba_name,
  rf.city             AS filer_city,
  rf.state            AS filer_state,
  rf.zip              AS filer_zip,
  c.tax_year          AS tax_year,
  c.return_type       AS return_type,
  c.period_end        AS period_end,
  c.filing_id         AS filing_id,
  g.recipient_ein     AS recipient_ein,
  g.recipient_name    AS recipient_name,
  g.city              AS recipient_city,
  g.state             AS recipient_state,
  g.country           AS recipient_country,
  g.cash_amount       AS cash_amount,
  g.noncash_amount    AS noncash_amount,
  (COALESCE(g.cash_amount,0) + COALESCE(g.noncash_amount,0)) AS total_amount,
  g.purpose           AS purpose
FROM candidates_with_grants c
JOIN returns rf
  ON rf.filing_id = c.filing_id
JOIN grants_compat_v1 g
  ON g.filing_id = c.filing_id
ORDER BY rf.ein, c.tax_year DESC, total_amount DESC, g.recipient_name, g.recipient_ein
"""

_SQL_ENHANCED = """
WITH candidates AS (
  SELECT
    c.ein,
    c.tax_year,
    c.filing_id,
    c.return_type,
    c.return_ts,
    c.period_end
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  {where_clause}
),
candidate_grants AS (
  SELECT DISTINCT
    c.ein,
    c.tax_year,
    c.filing_id,
    c.return_type,
    c.return_ts,
    c.period_end,
    rr.grant_id
  FROM candidates c
  JOIN grant_recipient_resolved rr
    ON rr.grantor_ein = c.ein
   AND rr.filing_id = c.filing_id
),
gsrc AS (
  SELECT
    rr.grant_id,
    rr.filing_id,
    rr.recipient_reported_ein,
    rr.recipient_reported_name,
    rr.recipient_city,
    rr.recipient_state,
    rr.recipient_zip,
    rr.cash_amount,
    rr.noncash_amount,
    rr.purpose,
    CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN aa.selected_ein ELSE rr.resolved_ein END AS final_resolved_ein,
    CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN aa.selected_name ELSE rr.resolved_org_name END AS final_resolved_org_name,
    CASE
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' AND aa.model='rule:reported_ein_identity_lookup' THEN 'reported_ein_identity_lookup'
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' AND aa.model='rule:reported_ein_address_location' THEN 'reported_ein_address_location'
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' AND aa.model='rule:reported_ein_from_filing_unverified' THEN 'reported_ein_from_filing_unverified'
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' AND aa.model LIKE 'rule:reported_ein%' THEN 'reported_ein_rule'
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' AND aa.model LIKE 'rule:%' THEN 'deterministic_rule'
      WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN 'ai_adjudicated'
      ELSE 'deterministic'
    END AS final_match_source,
    CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein <> '' THEN aa.ai_confidence ELSE rr.confidence END AS final_confidence,
    rr.match_status,
    rr.match_method,
    rr.confidence AS deterministic_confidence,
    rr.name_score,
    rr.address_score,
    rr.warning_flags,
    aa.model AS ai_model,
    d.decision AS ai_decision,
    d.selected_candidate_id AS ai_selected_candidate_id,
    d.confidence AS ai_confidence,
    d.reason_codes_json AS ai_reason_codes,
    d.explanation AS ai_explanation,
    d.needs_human_review AS ai_needs_human_review,
    d.auto_accept AS ai_auto_accept,
    d.validation_status AS ai_validation_status,
    d.validation_error AS ai_validation_error
  FROM grant_recipient_resolved rr
  JOIN candidate_grants cg ON cg.grant_id = rr.grant_id
  LEFT JOIN grant_recipient_ai_applied aa ON aa.grant_id = rr.grant_id
  LEFT JOIN grant_recipient_ai_decision d ON d.signature_hash = aa.signature_hash
)
SELECT
  rf.ein              AS filer_ein,
  rf.org_name         AS filer_org_name,
  rf.dba_name         AS filer_dba_name,
  rf.city             AS filer_city,
  rf.state            AS filer_state,
  rf.zip              AS filer_zip,
  c.tax_year          AS tax_year,
  c.return_type       AS return_type,
  c.period_end        AS period_end,
  c.filing_id         AS filing_id,
  gsrc.final_resolved_ein AS recipient_ein,
  COALESCE(NULLIF(gsrc.final_resolved_org_name,''), NULLIF(gsrc.recipient_reported_name,'')) AS recipient_name,
  gsrc.recipient_city AS recipient_city,
  gsrc.recipient_state AS recipient_state,
  CASE
    WHEN COALESCE(NULLIF(TRIM(g.us_state_abbreviation_cd), ''), NULLIF(TRIM(gsrc.recipient_state), '')) IS NOT NULL THEN 'US'
    ELSE g.foreign_country_cd
  END AS recipient_country,
  gsrc.cash_amount    AS cash_amount,
  gsrc.noncash_amount AS noncash_amount,
  (COALESCE(gsrc.cash_amount,0) + COALESCE(gsrc.noncash_amount,0)) AS total_amount,
  gsrc.purpose        AS purpose,
  CASE
    WHEN gsrc.final_match_source = 'ai_adjudicated' THEN 'ai_adjudicated_accepted_spot_check'
    WHEN gsrc.final_match_source = 'deterministic_rule' THEN 'deterministic_rule_high_confidence'
    WHEN gsrc.final_match_source LIKE 'reported_ein%' THEN 'reported_ein_based'
    WHEN gsrc.final_match_source = 'deterministic' AND COALESCE(gsrc.final_confidence,0) >= 0.95 THEN 'deterministic_high_confidence'
    WHEN gsrc.final_match_source = 'deterministic' THEN 'deterministic_lower_confidence'
    ELSE COALESCE(gsrc.final_match_source, 'unknown')
  END AS match_reliability_bucket,
  CASE
    WHEN gsrc.final_match_source = 'ai_adjudicated' THEN 'YES'
    WHEN COALESCE(gsrc.final_confidence,0) < 0.95 THEN 'YES'
    WHEN COALESCE(gsrc.ai_needs_human_review,0) = 1 THEN 'YES'
    ELSE 'NO'
  END AS match_needs_spot_check,
  gsrc.grant_id                  AS grant_id,
  gsrc.recipient_reported_ein    AS reported_recipient_ein,
  gsrc.recipient_reported_name   AS reported_recipient_name,
  COALESCE(g.us_address_line1_txt, g.foreign_address_line1_txt) AS reported_recipient_address1,
  g.us_address_line2_txt         AS reported_recipient_address2,
  gsrc.recipient_city            AS reported_recipient_city,
  gsrc.recipient_state           AS reported_recipient_state,
  gsrc.recipient_zip             AS reported_recipient_zip,
  CASE
    WHEN COALESCE(NULLIF(TRIM(g.us_state_abbreviation_cd), ''), NULLIF(TRIM(gsrc.recipient_state), '')) IS NOT NULL THEN 'US'
    ELSE g.foreign_country_cd
  END AS reported_recipient_country,
  gsrc.final_match_source        AS final_match_source,
  gsrc.final_confidence          AS final_confidence,
  gsrc.match_status              AS deterministic_match_status,
  gsrc.match_method              AS deterministic_match_method,
  gsrc.deterministic_confidence  AS deterministic_confidence,
  gsrc.name_score                AS deterministic_name_score,
  gsrc.address_score             AS deterministic_address_score,
  gsrc.warning_flags             AS deterministic_warning_flags,
  gsrc.ai_model                  AS ai_model,
  gsrc.ai_decision               AS ai_decision,
  gsrc.ai_selected_candidate_id  AS ai_selected_candidate_id,
  gsrc.ai_confidence             AS ai_confidence,
  gsrc.ai_reason_codes           AS ai_reason_codes,
  gsrc.ai_explanation            AS ai_explanation,
  gsrc.ai_needs_human_review     AS ai_needs_human_review,
  gsrc.ai_auto_accept            AS ai_auto_accept,
  gsrc.ai_validation_status      AS ai_validation_status,
  gsrc.ai_validation_error       AS ai_validation_error
FROM candidate_grants c
JOIN returns rf ON rf.filing_id = c.filing_id
JOIN gsrc       ON gsrc.grant_id = c.grant_id
LEFT JOIN grants g ON g.id = gsrc.grant_id
ORDER BY rf.ein, c.tax_year DESC, total_amount DESC, recipient_name, recipient_ein
"""


def _parse_eins(form) -> List[str]:
    text = (form or {}).get("ein_list", "")
    return normalize_eins(text)

def _parse_filters(form) -> Tuple[bool, bool, Optional[str], Optional[int], Optional[int]]:
    f = form or {}
    return_all = f.get("return_all") in (True, "true", "on", "1")
    use_resolved = _truthy(f, "use_resolved_grants")
    state = (f.get("state") or "").strip().upper()
    if state and state not in US_STATES:
        state = None
    def _to_int(x):
        try:
            return int(x)
        except Exception:
            return None
    min_year = _to_int(f.get("min_year"))
    max_year = _to_int(f.get("max_year"))
    # If only one provided, use that single year
    if min_year and not max_year:
        max_year = min_year
    elif max_year and not min_year:
        min_year = max_year
    # Swap if reversed
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

def _build_where_for_candidates(return_all: bool, eins: List[str], state: Optional[str], min_year: Optional[int], max_year: Optional[int]):
    clauses = []
    params: List = []

    if not return_all:
        if not eins:
            # No EINs -> no results
            return "WHERE 1=0", []
        placeholders = ",".join("?" for _ in eins)
        clauses.append(f"c.ein IN ({placeholders})")
        params.extend(eins)

    if state:
        clauses.append("r.state = ?")
        params.append(state)

    if (min_year is not None) and (max_year is not None):
        clauses.append("c.tax_year BETWEEN ? AND ?")
        params.extend([min_year, max_year])

    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params

def _query(return_all: bool, use_resolved: bool, eins: List[str], state: Optional[str], min_year: Optional[int], max_year: Optional[int]) -> List[Tuple]:
    conn = connect_ro()
    if use_resolved:
        _ensure_enhanced_objects(conn)
    rows: List[Tuple] = []

    def _run_one(ein_subset: Optional[List[str]]):
        where_clause, params = _build_where_for_candidates(
            return_all if ein_subset is None else False,
            [] if ein_subset is None else ein_subset,
            state,
            min_year,
            max_year,
        )
        template = _SQL_ENHANCED if use_resolved else _SQL_BASE
        sql = template.format(where_clause=("\n" + where_clause if where_clause else ""))
        cur = conn.execute(sql, params)
        return cur.fetchall()

    if not return_all:
        # chunk EIN IN() lists to avoid very large parameter lists
        CHUNK = 300
        for i in range(0, len(eins), CHUNK):
            rows.extend(_run_one(eins[i:i+CHUNK]))
    else:
        rows = _run_one(None)

    return rows

def run(form) -> Tuple[List[str], List[Tuple]]:
    return_all, use_resolved, state, min_year, max_year = _parse_filters(form)
    eins = [] if return_all else _parse_eins(form)
    return headers_for_form(form), _query(return_all, use_resolved, eins, state, min_year, max_year)

def export_rows(form) -> Iterable[Tuple]:
    # For CSV export streams
    return_all, use_resolved, state, min_year, max_year = _parse_filters(form)
    eins = [] if return_all else _parse_eins(form)
    return _query(return_all, use_resolved, eins, state, min_year, max_year)
