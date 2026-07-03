from typing import Iterable, List, Optional, Tuple

from common import connect_ro, normalize_eins


META = {
    "key": "lobbying_political_activity",
    "name": "Lobbying & Political Activity Explorer",
    "description": (
        "Explore expanded Schedule C lobbying, political campaign, 527, dues/proxy-tax, "
        "and 990-PF political/legislative indicators by EIN, filer state, and tax year."
    ),
}


HEADERS = [
    "ein",
    "org_name",
    "dba_name",
    "tax_year",
    "return_type",
    "state",
    "tax_exempt_status",
    "filing_id",
    "activity_summary",
    "political_campaign_activity_ind",
    "lobbying_activities_ind",
    "pf_legislative_political_acty_ind",
    "pf_more_than100_spent_ind",
    "pf_form1120_pol_filed_ind",
    "pf_influence_legislation_ind",
    "pf_influence_election_ind",
    "political_expenditures_amt",
    "expended527_activities_amt",
    "total_exempt_function_expend_amt",
    "form1120_pol_filed_ind",
    "total_lobbying_expenditures_amt",
    "total_direct_lobbying_amt",
    "total_grassroots_lobbying_amt",
    "lobbying_nontaxable_amt",
    "grassroots_nontaxable_amt",
    "lobbying_ceiling_amt",
    "grassroots_ceiling_amt",
    "lobbying_excess_amt",
    "lobbying_grassroots_excess_amt",
    "volunteers_ind",
    "paid_staff_or_management_ind",
    "media_advertisements_ind",
    "media_advertisements_amt",
    "mailings_members_ind",
    "mailings_members_amt",
    "publications_or_broadcast_ind",
    "publications_or_broadcast_amt",
    "grants_other_organizations_ind",
    "grants_other_organizations_amt",
    "direct_contact_legislators_ind",
    "direct_contact_legislators_amt",
    "rallies_demonstrations_ind",
    "rallies_demonstrations_amt",
    "other_activities_ind",
    "other_activities_amt",
    "not_described_section501c3_ind",
    "substantially_all_dues_nonded_ind",
    "dues_assessments_amt",
    "non_deductible_lbbyng_pltcl_cy_amt",
    "non_deductible_lbbyng_pltcl_tot_amt",
    "aggregate_reported_dues_ntc_amt",
    "carried_over_amt",
    "schedule_c_supplemental_count",
    "schedule_c_explanations",
]
META["headers"] = HEADERS


US_STATES = [
    "", "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]


ACTIVITY_MODES = {
    "any": "Any lobbying/political activity",
    "lobbying": "Lobbying activity only",
    "political": "Political campaign / 527 activity only",
    "proxy_dues": "Dues / proxy-tax indicators only",
    "pf_flags": "990-PF political/legislative flags only",
    "all_rows": "All matching filings, even with no activity",
}


def _html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _val_js(val: str) -> str:
    return repr(val)


def _checked(form, key: str) -> str:
    return "checked" if (form or {}).get(key) in (True, "true", "on", "1") else ""


def render_fields(form) -> str:
    f = form or {}
    val_eins = f.get("ein_list", "")
    state_val = (f.get("state") or "").upper()
    min_year = str(f.get("min_year") or "")
    max_year = str(f.get("max_year") or "")
    min_amount = str(f.get("min_amount") or "")
    activity_mode = (f.get("activity_mode") or "any").strip().lower()
    if activity_mode not in ACTIVITY_MODES:
        activity_mode = "any"

    state_options = ['<option value="">(All filer states)</option>']
    for s in US_STATES:
        if not s:
            continue
        sel = " selected" if s == state_val else ""
        state_options.append(f'<option value="{s}"{sel}>{s}</option>')
    state_html = "\n".join(state_options)

    mode_html = "\n".join(
        f'<option value="{key}"{" selected" if key == activity_mode else ""}>{_html(label)}</option>'
        for key, label in ACTIVITY_MODES.items()
    )

    return f"""
    <div class="row" style="display:flex; gap:16px; flex-wrap:wrap;">
      <div style="flex:0 1 220px;">
        <label for="state"><b>Filer state</b>:</label><br>
        <select id="state" name="state" style="min-width:200px;">{state_html}</select>
      </div>
      <div style="flex:0 1 160px;">
        <label for="min_year"><b>Min tax year</b>:</label><br>
        <input id="min_year" name="min_year" type="number" inputmode="numeric" value="{_html(min_year)}" placeholder="e.g. 2018" style="width:140px;">
      </div>
      <div style="flex:0 1 160px;">
        <label for="max_year"><b>Max tax year</b>:</label><br>
        <input id="max_year" name="max_year" type="number" inputmode="numeric" value="{_html(max_year)}" placeholder="e.g. 2024" style="width:140px;">
      </div>
      <div style="flex:0 1 260px;">
        <label for="activity_mode"><b>Activity filter</b>:</label><br>
        <select id="activity_mode" name="activity_mode" style="min-width:250px;">{mode_html}</select>
      </div>
      <div style="flex:0 1 180px;">
        <label for="min_amount"><b>Min amount</b>:</label><br>
        <input id="min_amount" name="min_amount" type="number" inputmode="numeric" value="{_html(min_amount)}" placeholder="optional" style="width:160px;">
      </div>
    </div>

    <div class="row" style="margin-top:12px;">
      <label><input type="checkbox" name="return_all" {_checked(f, "return_all")}>
        <b>Return all matching activity</b>
      </label>
      <div style="color:#666; font-size:90%; margin-top:4px;">
        If unchecked, provide at least one EIN or a filer state. Leaving this unchecked prevents accidental full-database scans.
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
      <div style="color:#666; font-size:90%; margin-top:4px;">
        Separate by commas, semicolons, spaces, or new lines. Non-digits ignored; valid 9-digit EINs are kept.
      </div>
    </div>
    """


_TRUTHY = "('X','1','TRUE','T','YES','Y')"

_POLITICAL_EXPR = f"""
(
  UPPER(TRIM(COALESCE(f990.political_campaign_acty_ind, ez.political_campaign_acty_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(sc.form1120_pol_filed_ind, pf.form1120_pol_filed_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(pf.more_than100_spent_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(pf.influence_election_ind, ''))) IN {_TRUTHY}
  OR COALESCE(sc.political_expenditures_amt, 0) <> 0
  OR COALESCE(sc.expended527_activities_amt, 0) <> 0
  OR COALESCE(sc.total_exempt_function_expend_amt, 0) <> 0
)
"""

_LOBBYING_EXPR = f"""
(
  UPPER(TRIM(COALESCE(f990.lobbying_activities_ind, ez.lobbying_activities_ind, pf.legislative_political_acty_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(pf.influence_legislation_ind, ''))) IN {_TRUTHY}
  OR COALESCE(sc.total_lobbying_expenditures_amt, 0) <> 0
  OR COALESCE(sc.total_direct_lobbying_amt, 0) <> 0
  OR COALESCE(sc.total_grassroots_lobbying_amt, 0) <> 0
  OR COALESCE(sc.lobbying_nontaxable_amt, 0) <> 0
  OR COALESCE(sc.grassroots_nontaxable_amt, 0) <> 0
  OR COALESCE(sc.direct_contact_legislators_amt, 0) <> 0
  OR UPPER(TRIM(COALESCE(sc.volunteers_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(sc.paid_staff_or_management_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(sc.media_advertisements_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(sc.mailings_members_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(sc.publications_or_broadcast_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(sc.grants_other_organizations_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(sc.direct_contact_legislators_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(sc.rallies_demonstrations_ind, ''))) IN {_TRUTHY}
  OR UPPER(TRIM(COALESCE(sc.other_activities_ind, ''))) IN {_TRUTHY}
)
"""

_PROXY_DUES_EXPR = f"""
(
  UPPER(TRIM(COALESCE(sc.substantially_all_dues_nonded_ind, ''))) IN {_TRUTHY}
  OR COALESCE(sc.dues_assessments_amt, 0) <> 0
  OR COALESCE(sc.non_deductible_lbbyng_pltcl_cy_amt, 0) <> 0
  OR COALESCE(sc.non_deductible_lbbyng_pltcl_tot_amt, 0) <> 0
  OR COALESCE(sc.aggregate_reported_dues_ntc_amt, 0) <> 0
  OR COALESCE(sc.carried_over_amt, 0) <> 0
)
"""

_PF_FLAGS_EXPR = f"""
(
  c.return_type LIKE '990PF%'
  AND (
    UPPER(TRIM(COALESCE(pf.legislative_political_acty_ind, ''))) IN {_TRUTHY}
    OR UPPER(TRIM(COALESCE(pf.more_than100_spent_ind, ''))) IN {_TRUTHY}
    OR UPPER(TRIM(COALESCE(pf.form1120_pol_filed_ind, ''))) IN {_TRUTHY}
    OR UPPER(TRIM(COALESCE(pf.influence_legislation_ind, ''))) IN {_TRUTHY}
    OR UPPER(TRIM(COALESCE(pf.influence_election_ind, ''))) IN {_TRUTHY}
  )
)
"""

_AMOUNT_EXPR = """
(
  COALESCE(sc.political_expenditures_amt, 0) >= ?
  OR COALESCE(sc.expended527_activities_amt, 0) >= ?
  OR COALESCE(sc.total_exempt_function_expend_amt, 0) >= ?
  OR COALESCE(sc.total_lobbying_expenditures_amt, 0) >= ?
  OR COALESCE(sc.total_direct_lobbying_amt, 0) >= ?
  OR COALESCE(sc.total_grassroots_lobbying_amt, 0) >= ?
  OR COALESCE(sc.lobbying_nontaxable_amt, 0) >= ?
  OR COALESCE(sc.grassroots_nontaxable_amt, 0) >= ?
  OR COALESCE(sc.direct_contact_legislators_amt, 0) >= ?
  OR COALESCE(sc.dues_assessments_amt, 0) >= ?
  OR COALESCE(sc.non_deductible_lbbyng_pltcl_cy_amt, 0) >= ?
  OR COALESCE(sc.non_deductible_lbbyng_pltcl_tot_amt, 0) >= ?
  OR COALESCE(sc.aggregate_reported_dues_ntc_amt, 0) >= ?
)
"""


_SQL = """
WITH candidates AS (
SELECT
  c.ein,
  r.org_name,
  r.dba_name,
  c.tax_year,
  c.return_type,
  r.state,
  CASE
    WHEN c.return_type LIKE '990PF%' THEN
      CASE
        WHEN UPPER(TRIM(COALESCE(pf.organization501c3_exempt_pfind, ''))) IN ('X','1','TRUE','T','YES','Y') THEN '501(c)(3)'
        WHEN UPPER(TRIM(COALESCE(pf.organization4947a1_trtd_pfind, ''))) IN ('X','1','TRUE','T','YES','Y') THEN '4947(a)(1)'
        ELSE ''
      END
    WHEN c.return_type LIKE '990EZ%' THEN
      CASE
        WHEN UPPER(TRIM(COALESCE(ez.organization501c3_ind, ''))) IN ('X','1','TRUE','T','YES','Y') THEN '501(c)(3)'
        WHEN UPPER(TRIM(COALESCE(ez.organization501c_ind, ''))) IN ('X','1','TRUE','T','YES','Y')
             AND COALESCE(TRIM(ez.attr_organization501c_type_txt), '') <> ''
          THEN '501(c)(' || TRIM(ez.attr_organization501c_type_txt) || ')'
        ELSE ''
      END
    ELSE
      CASE
        WHEN UPPER(TRIM(COALESCE(f990.organization501c3_ind, ''))) IN ('X','1','TRUE','T','YES','Y') THEN '501(c)(3)'
        WHEN UPPER(TRIM(COALESCE(f990.organization501c_ind, ''))) IN ('X','1','TRUE','T','YES','Y')
             AND COALESCE(TRIM(f990.attr_organization501c_type_txt), '') <> ''
          THEN '501(c)(' || TRIM(f990.attr_organization501c_type_txt) || ')'
        ELSE ''
      END
  END AS tax_exempt_status,
  c.filing_id,
  TRIM(
    CASE WHEN {political_expr} THEN 'political; ' ELSE '' END ||
    CASE WHEN {lobbying_expr} THEN 'lobbying; ' ELSE '' END ||
    CASE WHEN {proxy_dues_expr} THEN 'dues/proxy-tax; ' ELSE '' END ||
    CASE WHEN {pf_flags_expr} THEN '990-PF flags; ' ELSE '' END
  ) AS activity_summary,
  CASE WHEN UPPER(TRIM(COALESCE(f990.political_campaign_acty_ind, ez.political_campaign_acty_ind, ''))) IN ('X','1','TRUE','T','YES','Y') THEN 'Yes'
       WHEN COALESCE(f990.political_campaign_acty_ind, ez.political_campaign_acty_ind) IS NOT NULL THEN 'No'
       ELSE '' END AS political_campaign_activity_ind,
  CASE WHEN UPPER(TRIM(COALESCE(f990.lobbying_activities_ind, ez.lobbying_activities_ind, ''))) IN ('X','1','TRUE','T','YES','Y') THEN 'Yes'
       WHEN COALESCE(f990.lobbying_activities_ind, ez.lobbying_activities_ind) IS NOT NULL THEN 'No'
       ELSE '' END AS lobbying_activities_ind,
  pf.legislative_political_acty_ind,
  pf.more_than100_spent_ind,
  pf.form1120_pol_filed_ind AS pf_form1120_pol_filed_ind,
  pf.influence_legislation_ind,
  pf.influence_election_ind,
  sc.political_expenditures_amt,
  sc.expended527_activities_amt,
  sc.total_exempt_function_expend_amt,
  sc.form1120_pol_filed_ind,
  sc.total_lobbying_expenditures_amt,
  sc.total_direct_lobbying_amt,
  sc.total_grassroots_lobbying_amt,
  sc.lobbying_nontaxable_amt,
  sc.grassroots_nontaxable_amt,
  sc.lobbying_ceiling_amt,
  sc.grassroots_ceiling_amt,
  sc.lobbying_excess_amt,
  sc.lobbying_grassroots_excess_amt,
  sc.volunteers_ind,
  sc.paid_staff_or_management_ind,
  sc.media_advertisements_ind,
  sc.media_advertisements_amt,
  sc.mailings_members_ind,
  sc.mailings_members_amt,
  sc.publications_or_broadcast_ind,
  sc.publications_or_broadcast_amt,
  sc.grants_other_organizations_ind,
  sc.grants_other_organizations_amt,
  sc.direct_contact_legislators_ind,
  sc.direct_contact_legislators_amt,
  sc.rallies_demonstrations_ind,
  sc.rallies_demonstrations_amt,
  sc.other_activities_ind,
  sc.other_activities_amt,
  sc.not_described_section501c3_ind,
  sc.substantially_all_dues_nonded_ind,
  sc.dues_assessments_amt,
  sc.non_deductible_lbbyng_pltcl_cy_amt,
  sc.non_deductible_lbbyng_pltcl_tot_amt,
  sc.aggregate_reported_dues_ntc_amt,
  sc.carried_over_amt
FROM canonical_by_ein_year c
JOIN returns r ON r.filing_id = c.filing_id
LEFT JOIN irs990_root f990 ON f990.filing_id = c.filing_id
LEFT JOIN irs990_ez_root ez ON ez.filing_id = c.filing_id
LEFT JOIN irs990_pf_root pf ON pf.filing_id = c.filing_id
LEFT JOIN irs990_schedule_c_root sc ON sc.filing_id = c.filing_id
{where_clause}
),
supp AS (
  SELECT
    s.filing_id,
    COUNT(*) AS schedule_c_supplemental_count,
    SUBSTR(
      GROUP_CONCAT(
        TRIM(COALESCE(s.form_and_line_reference_desc, '') ||
             CASE WHEN COALESCE(s.form_and_line_reference_desc, '') <> ''
                    AND COALESCE(s.explanation_txt, '') <> ''
                  THEN ': ' ELSE '' END ||
             COALESCE(s.explanation_txt, '')),
        ' || '
      ),
      1,
      2000
    ) AS schedule_c_explanations
  FROM irs990_schedule_c_supplemental_info s
  JOIN candidates c ON c.filing_id = s.filing_id
  GROUP BY s.filing_id
)
SELECT
  c.ein,
  c.org_name,
  c.dba_name,
  c.tax_year,
  c.return_type,
  c.state,
  c.tax_exempt_status,
  c.filing_id,
  c.activity_summary,
  c.political_campaign_activity_ind,
  c.lobbying_activities_ind,
  c.legislative_political_acty_ind,
  c.more_than100_spent_ind,
  c.pf_form1120_pol_filed_ind,
  c.influence_legislation_ind,
  c.influence_election_ind,
  c.political_expenditures_amt,
  c.expended527_activities_amt,
  c.total_exempt_function_expend_amt,
  c.form1120_pol_filed_ind,
  c.total_lobbying_expenditures_amt,
  c.total_direct_lobbying_amt,
  c.total_grassroots_lobbying_amt,
  c.lobbying_nontaxable_amt,
  c.grassroots_nontaxable_amt,
  c.lobbying_ceiling_amt,
  c.grassroots_ceiling_amt,
  c.lobbying_excess_amt,
  c.lobbying_grassroots_excess_amt,
  c.volunteers_ind,
  c.paid_staff_or_management_ind,
  c.media_advertisements_ind,
  c.media_advertisements_amt,
  c.mailings_members_ind,
  c.mailings_members_amt,
  c.publications_or_broadcast_ind,
  c.publications_or_broadcast_amt,
  c.grants_other_organizations_ind,
  c.grants_other_organizations_amt,
  c.direct_contact_legislators_ind,
  c.direct_contact_legislators_amt,
  c.rallies_demonstrations_ind,
  c.rallies_demonstrations_amt,
  c.other_activities_ind,
  c.other_activities_amt,
  c.not_described_section501c3_ind,
  c.substantially_all_dues_nonded_ind,
  c.dues_assessments_amt,
  c.non_deductible_lbbyng_pltcl_cy_amt,
  c.non_deductible_lbbyng_pltcl_tot_amt,
  c.aggregate_reported_dues_ntc_amt,
  c.carried_over_amt,
  COALESCE(supp.schedule_c_supplemental_count, 0),
  COALESCE(supp.schedule_c_explanations, '')
FROM candidates c
LEFT JOIN supp ON supp.filing_id = c.filing_id
ORDER BY c.tax_year DESC, c.ein, c.org_name
"""


_SQL = _SQL.format(
    political_expr=_POLITICAL_EXPR,
    lobbying_expr=_LOBBYING_EXPR,
    proxy_dues_expr=_PROXY_DUES_EXPR,
    pf_flags_expr=_PF_FLAGS_EXPR,
    where_clause="{where_clause}",
)


def _parse_int(value) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _parse_float(value) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _parse_filters(form) -> Tuple[List[str], bool, Optional[str], Optional[int], Optional[int], str, Optional[float]]:
    f = form or {}
    eins = normalize_eins(f.get("ein_list", ""))
    return_all = f.get("return_all") in (True, "true", "on", "1")
    state = (f.get("state") or "").strip().upper()
    if state and state not in US_STATES:
        state = None
    min_year = _parse_int(f.get("min_year"))
    max_year = _parse_int(f.get("max_year"))
    if min_year and not max_year:
        max_year = min_year
    elif max_year and not min_year:
        min_year = max_year
    if min_year and max_year and min_year > max_year:
        min_year, max_year = max_year, min_year
    activity_mode = (f.get("activity_mode") or "any").strip().lower()
    if activity_mode not in ACTIVITY_MODES:
        activity_mode = "any"
    min_amount = _parse_float(f.get("min_amount"))
    return eins, return_all, (state if state else None), min_year, max_year, activity_mode, min_amount


def _activity_clause(mode: str) -> str:
    if mode == "all_rows":
        return ""
    if mode == "lobbying":
        return _LOBBYING_EXPR
    if mode == "political":
        return _POLITICAL_EXPR
    if mode == "proxy_dues":
        return _PROXY_DUES_EXPR
    if mode == "pf_flags":
        return _PF_FLAGS_EXPR
    return f"(({_POLITICAL_EXPR}) OR ({_LOBBYING_EXPR}) OR ({_PROXY_DUES_EXPR}) OR ({_PF_FLAGS_EXPR}))"


def _build_where(
    eins: List[str],
    return_all: bool,
    state: Optional[str],
    min_year: Optional[int],
    max_year: Optional[int],
    activity_mode: str,
    min_amount: Optional[float],
) -> Tuple[str, List]:
    clauses = []
    params: List = []

    if not return_all:
        if eins:
            placeholders = ",".join("?" for _ in eins)
            clauses.append(f"c.ein IN ({placeholders})")
            params.extend(eins)
        elif state:
            clauses.append("r.state = ?")
            params.append(state)
        else:
            clauses.append("1=0")
    else:
        if eins:
            placeholders = ",".join("?" for _ in eins)
            clauses.append(f"c.ein IN ({placeholders})")
            params.extend(eins)
        if state:
            clauses.append("r.state = ?")
            params.append(state)

    if min_year is not None and max_year is not None:
        clauses.append("c.tax_year BETWEEN ? AND ?")
        params.extend([min_year, max_year])

    activity = _activity_clause(activity_mode)
    if activity:
        clauses.append(activity)

    if min_amount is not None:
        clauses.append(_AMOUNT_EXPR)
        params.extend([min_amount] * 13)

    if not clauses:
        return "", params

    return "WHERE " + " AND ".join(f"({c})" for c in clauses), params


def _query(
    eins: List[str],
    return_all: bool,
    state: Optional[str],
    min_year: Optional[int],
    max_year: Optional[int],
    activity_mode: str,
    min_amount: Optional[float],
) -> List[Tuple]:
    conn = connect_ro()
    rows: List[Tuple] = []

    def _run_one(ein_subset: List[str]) -> List[Tuple]:
        where_clause, params = _build_where(
            ein_subset,
            return_all,
            state,
            min_year,
            max_year,
            activity_mode,
            min_amount,
        )
        return conn.execute(_SQL.format(where_clause=where_clause), params).fetchall()

    if eins:
        chunk = 300
        for i in range(0, len(eins), chunk):
            rows.extend(_run_one(eins[i:i + chunk]))
    else:
        rows = _run_one([])

    return rows


def run(form) -> Tuple[List[str], List[Tuple]]:
    return HEADERS, _query(*_parse_filters(form))


def export_rows(form) -> Iterable[Tuple]:
    return _query(*_parse_filters(form))
