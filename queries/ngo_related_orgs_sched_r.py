# queries/ngo_related_orgs_sched_r.py
# Schedule R: list related orgs for one/more filers (EINs), or for all filers in a State,
# optionally within a tax-year range. Excludes "Unrelated Taxable Partnership" by default.

from typing import List, Tuple, Iterable, Optional
from common import connect_ro, normalize_eins

META = {
    "key": "ngo_related_orgs_sched_r",
    "name": "Schedule R: Related Orgs (by EINs or State + Year Range)",
    "description": (
        "Enter one or more EINs (comma/semicolon/space/newline separated), or choose a filer state "
        "and an optional tax-year range. Returns one row per related-organization entry from Schedule R "
        "(Disregarded Entities, Related Tax-Exempt, Related Taxable Corps/Trusts, Related Taxable Partnerships, "
        "and Transactions with Related Orgs). Unrelated taxable partnerships (Part IV) are excluded by default."
    ),
}

HEADERS = [
    "filer_ein", "filer_tax_year", "filing_id",
    "relationship_category",
    "related_ein", "related_name",
    "ownership_pct", "controlled_org_ind",
    "primary_activities",
    "transaction_type", "involved_amt",
    "exempt_code_section", "public_charity_status",
    "address_line1", "city", "state_abbrev", "domicile_state", "country",
    "table_source",
]
META["headers"] = HEADERS

US_STATES = [
    "", "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
    "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]

def _val_js(val: str) -> str:
    return repr(val)

def render_fields(form) -> str:
    f = form or {}
    val_eins = f.get("ein_list", "")
    state_val = (f.get("state") or "").upper()
    min_year = str(f.get("min_year") or "")
    max_year = str(f.get("max_year") or "")
    include_unrelated = f.get("include_unrelated") in (True, "true", "on", "1")

    state_options = ['<option value="">(All filers’ states)</option>']
    for s in US_STATES:
        if not s:
            continue
        sel = " selected" if s == state_val else ""
        state_options.append(f'<option value="{s}"{sel}>{s}</option>')
    state_html = "\n".join(state_options)

    checked = "checked" if include_unrelated else ""
    return f"""
    <div class="row" style="display:flex; gap:16px; flex-wrap:wrap;">
      <div style="flex:0 1 220px;">
        <label for="state"><b>Filer state</b> (2-letter):</label><br>
        <select id="state" name="state" style="min-width:200px;">{state_html}</select>
        <div style="color:#666; font-size: 90%; margin-top:4px;">
          Provide a state <i>or</i> EINs. If both are blank, no results will be returned.
        </div>
      </div>
      <div style="flex:0 1 160px;">
        <label for="min_year"><b>Min tax year</b>:</label><br>
        <input id="min_year" name="min_year" type="number" inputmode="numeric" value="{min_year}" placeholder="e.g. 2018" style="width:140px;">
      </div>
      <div style="flex:0 1 160px;">
        <label for="max_year"><b>Max tax year</b>:</label><br>
        <input id="max_year" name="max_year" type="number" inputmode="numeric" value="{max_year}" placeholder="e.g. 2023" style="width:140px;">
      </div>
      <div style="flex:1 1 260px; align-self:flex-end;">
        <label><input type="checkbox" id="include_unrelated" name="include_unrelated" {checked}>
          Include Schedule R <b>Unrelated Taxable Partnerships</b> (Part IV)
        </label>
      </div>
    </div>

    <div class="row" style="margin-top:12px;">
      <label for="ein_list"><b>EIN(s):</b></label><br>
      <textarea id="ein_list" name="ein_list" rows="6" placeholder="e.g. 131624102, 941156365; 52-6043385
123456789" style="min-width:520px;"></textarea>
      <script>
        (function() {{
          var el = document.getElementById('ein_list');
          el.value = {_val_js(val_eins)};
        }})();
      </script>
      <div style="color:#666; font-size: 90%; margin-top:4px;">
        Separate by commas, semicolons, spaces, or new lines. Non-digits ignored; valid 9-digit EINs are kept.
      </div>
    </div>
    """

_SQL_BASE = """
WITH candidates AS (
  SELECT c.ein, c.tax_year, c.filing_id
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  {where_clause}
),
sr AS (
  SELECT
    c.ein AS filer_ein,
    c.tax_year AS filer_tax_year,
    r.filing_id,
    'Related Tax-Exempt Org' AS relationship_category,
    r.ein AS related_ein,
    COALESCE(r.business_name_line1_txt, r.disregarded_entity_name_business_name_line1_txt) AS related_name_line1,
    COALESCE(r.business_name_line2_txt, r.disregarded_entity_name_business_name_line2_txt) AS related_name_line2,
    CAST(NULL AS NUMERIC) AS ownership_pct,
    r.controlled_organization_ind,
    r.primary_activities_txt,
    CAST(NULL AS TEXT) AS transaction_type_txt,
    CAST(NULL AS NUMERIC) AS involved_amt,
    r.exempt_code_section_txt,
    r.public_charity_status_txt,
    r.address_line1_txt,
    r.city_nm,
    r.state_abbreviation_cd,
    r.legal_domicile_state_cd,
    r.country_cd,
    'irs990_schedule_r_id_related_tax_exempt_org_grp' AS table_source
  FROM candidates c
  JOIN irs990_schedule_r_id_related_tax_exempt_org_grp r ON r.filing_id = c.filing_id

  UNION ALL

  SELECT
    c.ein,
    c.tax_year,
    r.filing_id,
    'Related Taxable Corporation/Trust',
    r.ein,
    COALESCE(r.related_organization_name_business_name_line1_txt, r.business_name_line1_txt),
    COALESCE(r.related_organization_name_business_name_line2_txt, r.business_name_line2_txt),
    r.ownership_pct,
    r.controlled_organization_ind,
    r.primary_activities_txt,
    CAST(NULL AS TEXT),
    CAST(NULL AS NUMERIC),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    r.address_line1_txt,
    r.city_nm,
    r.state_abbreviation_cd,
    r.legal_domicile_state_cd,
    CAST(NULL AS TEXT),
    'irs990_schedule_r_id_related_org_txbl_corp_tr_grp'
  FROM candidates c
  JOIN irs990_schedule_r_id_related_org_txbl_corp_tr_grp r ON r.filing_id = c.filing_id

  UNION ALL

  SELECT
    c.ein,
    c.tax_year,
    r.filing_id,
    'Related Taxable Partnership',
    r.ein,
    COALESCE(r.related_organization_name_business_name_line1_txt, r.business_name_line1_txt),
    COALESCE(r.related_organization_name_business_name_line2_txt, r.business_name_line2_txt),
    r.ownership_pct,
    r.controlled_organization_ind,
    r.primary_activities_txt,
    CAST(NULL AS TEXT),
    CAST(NULL AS NUMERIC),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    r.address_line1_txt,
    r.city_nm,
    r.state_abbreviation_cd,
    r.legal_domicile_state_cd,
    CAST(NULL AS TEXT),
    'irs990_schedule_r_id_related_org_txbl_partnership_grp'
  FROM candidates c
  JOIN irs990_schedule_r_id_related_org_txbl_partnership_grp r ON r.filing_id = c.filing_id

  UNION ALL

  SELECT
    c.ein,
    c.tax_year,
    r.filing_id,
    'Disregarded Entity',
    CAST(NULL AS TEXT),
    r.disregarded_entity_name_business_name_line1_txt,
    r.disregarded_entity_name_business_name_line2_txt,
    CAST(NULL AS NUMERIC),
    CAST(NULL AS TEXT),
    r.primary_activities_txt,
    CAST(NULL AS TEXT),
    CAST(NULL AS NUMERIC),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    'irs990_schedule_r_id_disregarded_entities_grp'
  FROM candidates c
  JOIN irs990_schedule_r_id_disregarded_entities_grp r ON r.filing_id = c.filing_id

  UNION ALL

  SELECT
    c.ein,
    c.tax_year,
    r.filing_id,
    'Transactions with Related Org',
    CAST(NULL AS TEXT),
    r.business_name_line1_txt,
    r.business_name_line2_txt,
    CAST(NULL AS NUMERIC),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    r.transaction_type_txt,
    r.involved_amt,
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    'irs990_schedule_r_transactions_related_org_grp'
  FROM candidates c
  JOIN irs990_schedule_r_transactions_related_org_grp r ON r.filing_id = c.filing_id

  UNION ALL

  SELECT
    c.ein,
    c.tax_year,
    r.filing_id,
    'Unrelated Taxable Partnership',
    r.ein,
    r.business_name_line1_txt,
    CAST(NULL AS TEXT),
    r.ownership_pct,
    r.general_or_managing_partner_ind,
    r.primary_activities_txt,
    CAST(NULL AS TEXT),
    CAST(NULL AS NUMERIC),
    CAST(NULL AS TEXT),
    CAST(NULL AS TEXT),
    r.address_line1_txt,
    r.city_nm,
    r.state_abbreviation_cd,
    r.legal_domicile_state_cd,
    CAST(NULL AS TEXT),
    'irs990_schedule_r_unrelated_org_txbl_partnership_grp'
  FROM candidates c
  JOIN irs990_schedule_r_unrelated_org_txbl_partnership_grp r ON r.filing_id = c.filing_id
)
SELECT
  sr.filer_ein                   AS filer_ein,
  sr.filer_tax_year              AS filer_tax_year,
  sr.filing_id                   AS filing_id,
  sr.relationship_category       AS relationship_category,
  sr.related_ein                 AS related_ein,
  TRIM(COALESCE(sr.related_name_line1,'')) ||
    CASE WHEN TRIM(COALESCE(sr.related_name_line2,''))<>'' THEN ' '||TRIM(sr.related_name_line2) ELSE '' END
    AS related_name,
  sr.ownership_pct               AS ownership_pct,
  sr.controlled_organization_ind AS controlled_org_ind,
  sr.primary_activities_txt      AS primary_activities,
  sr.transaction_type_txt        AS transaction_type,
  sr.involved_amt                AS involved_amt,
  sr.exempt_code_section_txt     AS exempt_code_section,
  sr.public_charity_status_txt   AS public_charity_status,
  sr.address_line1_txt           AS address_line1,
  sr.city_nm                     AS city,
  sr.state_abbreviation_cd       AS state_abbrev,
  sr.legal_domicile_state_cd     AS domicile_state,
  sr.country_cd                  AS country,
  sr.table_source                AS table_source
FROM sr
{sr_where_clause}
ORDER BY sr.filer_ein, sr.filer_tax_year DESC, sr.relationship_category, related_name
"""

def _parse_eins(form) -> List[str]:
    text = (form or {}).get("ein_list", "")
    return normalize_eins(text)

def _parse_filters(form) -> Tuple[Optional[str], Optional[int], Optional[int], bool]:
    f = form or {}
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
    if min_year and not max_year:
        max_year = min_year
    elif max_year and not min_year:
        min_year = max_year
    if min_year and max_year and min_year > max_year:
        min_year, max_year = max_year, min_year
    include_unrelated = f.get("include_unrelated") in (True, "true", "on", "1")
    return (state if state else None), min_year, max_year, include_unrelated

def _build_where_for_candidates(eins: List[str], state: Optional[str],
                                min_year: Optional[int], max_year: Optional[int]):
    # WHERE for canonical_by_ein_year + returns
    clauses = []
    params: List = []

    if eins:
        placeholders = ",".join("?" for _ in eins)
        clauses.append(f"c.ein IN ({placeholders})")
        params.extend(eins)

    if state:
        clauses.append("r.state = ?")
        params.append(state)

    if (min_year is not None) and (max_year is not None):
        clauses.append("c.tax_year BETWEEN ? AND ?")
        params.extend([min_year, max_year])

    # If neither EINs nor State were provided, return no results (avoid scanning everything)
    if not eins and not state:
        return "WHERE 1=0", []

    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params

def _query(eins: List[str], state: Optional[str],
           min_year: Optional[int], max_year: Optional[int],
           include_unrelated: bool) -> List[Tuple]:
    conn = connect_ro()
    rows: List[Tuple] = []

    sr_filter = "" if include_unrelated else "\nWHERE sr.relationship_category <> 'Unrelated Taxable Partnership'"

    def _run_one(ein_subset: Optional[List[str]]):
        where_clause, params = _build_where_for_candidates(
            [] if ein_subset is None else ein_subset,
            state, min_year, max_year
        )
        sql = _SQL_BASE.format(
            where_clause=("\n" + where_clause if where_clause else ""),
            sr_where_clause=sr_filter
        )
        cur = conn.execute(sql, params)
        return cur.fetchall()

    if eins:
        # Chunk EIN lists to keep parameter lists reasonable
        CHUNK = 300
        for i in range(0, len(eins), CHUNK):
            rows.extend(_run_one(eins[i:i+CHUNK]))
    else:
        # State-only (and/or year) filter
        rows = _run_one(None)

    return rows

def run(form) -> Tuple[List[str], List[Tuple]]:
    state, min_year, max_year, include_unrelated = _parse_filters(form)
    eins = _parse_eins(form)
    return HEADERS, _query(eins, state, min_year, max_year, include_unrelated)

def export_rows(form) -> Iterable[Tuple]:
    state, min_year, max_year, include_unrelated = _parse_filters(form)
    eins = _parse_eins(form)
    return _query(eins, state, min_year, max_year, include_unrelated)
