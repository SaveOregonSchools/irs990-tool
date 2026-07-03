# queries/people_lookup.py
# Find filings where a PERSON appears (officers/board, highest-comp employees,
# former/key employees, contractors, grant recipients, return header signers/preparers,
# books-in-care-of, and several Schedule J/L/EZ variants).
#
# v2 changes:
# - Uses vw_contractors instead of nonexistent raw "contractors" object
# - Uses grants_compat_v1 instead of raw grants for recipient-name searching/detail
# - Keeps dynamic column checks so the module stays resilient to schema drift

from typing import List, Tuple, Iterable, Optional
from common import connect_ro

META = {
    "key": "people_lookup_v2",
    "name": "Find Filings by Person Name",
    "description": (
        "Enter a full name (e.g., 'Jane A. Doe') to find any filings where that person appears: "
        "officers/board/highest-comp employees, contractors, grant recipients, return header "
        "signers/preparers/books-in-care-of, and select Schedule J/L groups. "
        "Use the 'contains' option for fuzzy matching (default). Optional state and tax-year filters."
    ),
}

HEADERS = ["filing_id", "ein", "org_name", "tax_year", "found_in", "detail"]
META["headers"] = HEADERS

US_STATES = [
    "", "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI",
    "ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO",
    "MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA",
    "RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]

def _html(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def render_fields(form) -> str:
    f = form or {}
    person = f.get("person_name", "") or ""
    fuzzy = f.get("fuzzy_match") not in ("false", False, None, "", 0)  # default True
    state_val = (f.get("state") or "").upper()
    min_year = str(f.get("min_year") or "")
    max_year = str(f.get("max_year") or "")
    max_rows = str(f.get("max_rows") or "5000")

    opts = ['<option value="">(Any)</option>']
    for s in US_STATES:
        if not s:
            continue
        sel = " selected" if s == state_val else ""
        opts.append(f'<option value="{s}"{sel}>{s}</option>')

    return f"""
    <div class="row" style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
      <label style="min-width:340px;">
        Person full name:&nbsp;
        <input type="text" name="person_name" value="{_html(person)}" style="width:320px">
      </label>
      <label><input type="checkbox" name="fuzzy_match" {"checked" if fuzzy else ""}>
        Use <b>contains</b> match
      </label>
      <label>State:
        <select name="state">{''.join(opts)}</select>
      </label>
      <label>Min year:
        <input type="number" name="min_year" value="{min_year}" style="width:100px">
      </label>
      <label>Max year:
        <input type="number" name="max_year" value="{max_year}" style="width:100px">
      </label>
      <label>Max rows:
        <input type="number" name="max_rows" value="{max_rows}" min="1" max="1048000" style="width:110px">
      </label>
      <span style="color:#666; font-size:90%;">
        Broad contains searches are fastest with a state or tax-year filter. Use exact match for indexed full-name lookups.
      </span>
    </div>
    """

def _toi(x) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None

def _parse(form):
    f = form or {}
    person = (f.get("person_name") or "").strip()
    fuzzy = f.get("fuzzy_match") not in ("false", False, None, "", 0)  # default True
    state = (f.get("state") or "").strip().upper() or None
    min_year = _toi(f.get("min_year"))
    max_year = _toi(f.get("max_year"))
    if min_year and not max_year:
        max_year = min_year
    if max_year and not min_year:
        min_year = max_year
    if min_year and max_year and min_year > max_year:
        min_year, max_year = max_year, min_year
    max_rows = _toi(f.get("max_rows")) or 5000
    return person, fuzzy, state, min_year, max_year, max_rows

def _exists(conn, table: str) -> bool:
    sql = "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=? LIMIT 1"
    return bool(conn.execute(sql, (table,)).fetchone())

def _has_col(conn, table: str, col: str) -> bool:
    try:
        cols = {r[1].lower() for r in conn.execute(f"PRAGMA table_info('{table}')")}
    except Exception:
        return False
    return col.lower() in cols

def _name_pred(col: str, fuzzy: bool) -> str:
    return (f"UPPER({col}) LIKE UPPER(?)" if fuzzy else f"UPPER({col}) = UPPER(?)")

# curated: (table, column, label, detail_key)
CURATED = [
    ("officers", "person_name", "Officer/Director/Trustee", "officers"),
    ("highest_comp_employees", "person_name", "Highly Compensated Employee", "officers"),
    ("former_key_people", "person_name", "Former/Key Employee", "former_key_people"),

    # v2: normalized contractor/grant layers
    ("vw_contractors", "contractor_name", "Contractor", "vw_contractors"),
    ("grants_compat_v1", "recipient_name", "Grant Recipient", "grants_compat"),

    ("return_header_all", "person_nm", "Return Header (Signer/Officer)", "rha_person"),
    ("return_header_all", "preparer_person_nm", "Return Header (Preparer)", "rha_preparer"),

    ("irs990_books_in_care_of_detail", "person_nm", "Books In Care Of", "books_in_care_of"),
    ("irs990_ez_books_in_care_of_detail", "person_nm", "Books In Care Of (EZ)", "books_in_care_of_ez"),

    ("irs990_ez_officer_director_trustee_empl_grp", "person_nm", "EZ Officer/Director/Trustee/Employee", "ez_officer"),
    ("irs990_schedule_j_rltd_org_officer_trst_key_empl_grp", "person_nm", "Schedule J (Officer/Trustee/Key)", "sched_j"),
    ("irs990_pf_officer_dir_trst_key_empl_info_grp", "person_nm", "990-PF Officer/Director/Trustee/Key", "pf_officer"),

    ("irs990_schedule_l_bus_tr_involve_interested_prsn_grp", "person_nm", "Schedule L (Business Transactions)", "sched_l_bus"),
    ("irs990_schedule_l_disqualified_person_ex_bnft_tr_grp", "person_nm", "Schedule L (Excess Benefit Transaction)", "sched_l_ebt"),
    ("irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp", "person_nm", "Schedule L (Grant/Assist to Interested Person)", "sched_l_grant"),
    ("irs990_schedule_l_loans_btwn_org_interested_prsn_grp", "person_nm", "Schedule L (Loans Between Org & Interested Person)", "sched_l_loan"),
]

def _detail_expr(conn, table: str, alias: str, detail_key: str, hit_col: str) -> str:
    def col_ok(c: str) -> bool:
        return _has_col(conn, table, c)

    if detail_key == "officers":
        parts = []
        if col_ok("title_txt"):
            parts.append(f"'title='||COALESCE({alias}.title_txt,'')")
        if col_ok("avg_hours_week"):
            parts.append(f"'hrs/wk='||COALESCE(CAST({alias}.avg_hours_week AS TEXT),'')")
        if col_ok("comp_from_org"):
            parts.append(f"'comp_org='||COALESCE(CAST({alias}.comp_from_org AS TEXT),'')")
        if col_ok("comp_from_related"):
            parts.append(f"'comp_rel='||COALESCE(CAST({alias}.comp_from_related AS TEXT),'')")
        if col_ok("other_compensation"):
            parts.append(f"'other='||COALESCE(CAST({alias}.other_compensation AS TEXT),'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "former_key_people":
        parts = []
        if col_ok("title_txt"):
            parts.append(f"'title='||COALESCE({alias}.title_txt,'')")
        if col_ok("comp_from_org"):
            parts.append(f"'comp_org='||COALESCE(CAST({alias}.comp_from_org AS TEXT),'')")
        if col_ok("comp_from_related"):
            parts.append(f"'comp_rel='||COALESCE(CAST({alias}.comp_from_related AS TEXT),'')")
        if col_ok("other_compensation"):
            parts.append(f"'other='||COALESCE(CAST({alias}.other_compensation AS TEXT),'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "vw_contractors":
        parts = []
        if col_ok("services_desc"):
            parts.append(f"'services='||COALESCE({alias}.services_desc,'')")
        if col_ok("compensation_amt"):
            parts.append(f"'comp='||COALESCE(CAST({alias}.compensation_amt AS TEXT),'')")
        if col_ok("city"):
            parts.append(f"'city='||COALESCE({alias}.city,'')")
        if col_ok("region"):
            parts.append(f"'region='||COALESCE({alias}.region,'')")
        if col_ok("country"):
            parts.append(f"'country='||COALESCE({alias}.country,'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "grants_compat":
        parts = []
        if col_ok("recipient_ein"):
            parts.append(f"'recp_ein='||COALESCE({alias}.recipient_ein,'')")
        if col_ok("cash_amount"):
            parts.append(f"'cash='||COALESCE(CAST({alias}.cash_amount AS TEXT),'')")
        if col_ok("noncash_amount"):
            parts.append(f"'noncash='||COALESCE(CAST({alias}.noncash_amount AS TEXT),'')")
        if col_ok("purpose"):
            parts.append(f"'purpose='||COALESCE({alias}.purpose,'')")
        if col_ok("city"):
            parts.append(f"'city='||COALESCE({alias}.city,'')")
        if col_ok("state"):
            parts.append(f"'state='||COALESCE({alias}.state,'')")
        if col_ok("country"):
            parts.append(f"'country='||COALESCE({alias}.country,'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "rha_person":
        parts = []
        if col_ok("person_title_txt"):
            parts.append(f"'title='||COALESCE({alias}.person_title_txt,'')")
        if col_ok("signature_dt"):
            parts.append(f"'signed='||COALESCE({alias}.signature_dt,'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "rha_preparer":
        parts = []
        if col_ok("preparer_firm_name_business_name_line1_txt"):
            parts.append(f"'firm='||COALESCE({alias}.preparer_firm_name_business_name_line1_txt,'')")
        if col_ok("ptin"):
            parts.append(f"'ptin='||COALESCE({alias}.ptin,'')")
        if col_ok("preparation_dt"):
            parts.append(f"'prep_dt='||COALESCE({alias}.preparation_dt,'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "books_in_care_of":
        parts = []
        if col_ok("address_line1_txt"):
            parts.append(f"'addr1='||COALESCE({alias}.address_line1_txt,'')")
        if col_ok("city_nm"):
            parts.append(f"'city='||COALESCE({alias}.city_nm,'')")
        if col_ok("state_abbreviation_cd"):
            parts.append(f"'state='||COALESCE({alias}.state_abbreviation_cd,'')")
        if col_ok("zipcd"):
            parts.append(f"'zip='||COALESCE({alias}.zipcd,'')")
        if col_ok("phone_num"):
            parts.append(f"'phone='||COALESCE(CAST({alias}.phone_num AS TEXT),'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "books_in_care_of_ez":
        parts = []
        if col_ok("address_line1_txt"):
            parts.append(f"'addr1='||COALESCE({alias}.address_line1_txt,'')")
        if col_ok("city_nm"):
            parts.append(f"'city='||COALESCE({alias}.city_nm,'')")
        if col_ok("state_abbreviation_cd"):
            parts.append(f"'state='||COALESCE({alias}.state_abbreviation_cd,'')")
        if col_ok("zipcd"):
            parts.append(f"'zip='||COALESCE({alias}.zipcd,'')")
        if col_ok("phone_num"):
            parts.append(f"'phone='||COALESCE(CAST({alias}.phone_num AS TEXT),'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "ez_officer":
        parts = []
        if col_ok("title_txt"):
            parts.append(f"'title='||COALESCE({alias}.title_txt,'')")
        if col_ok("average_hrs_per_wk_devoted_to_pos_rt"):
            parts.append(f"'hrs/wk='||COALESCE({alias}.average_hrs_per_wk_devoted_to_pos_rt,'')")
        if col_ok("compensation_amt"):
            parts.append(f"'comp='||COALESCE(CAST({alias}.compensation_amt AS TEXT),'')")
        if col_ok("employee_benefit_program_amt"):
            parts.append(f"'benefits='||COALESCE(CAST({alias}.employee_benefit_program_amt AS TEXT),'')")
        if col_ok("expense_account_other_allwnc_amt"):
            parts.append(f"'other='||COALESCE(CAST({alias}.expense_account_other_allwnc_amt AS TEXT),'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "sched_j":
        parts = []
        if col_ok("title_txt"):
            parts.append(f"'title='||COALESCE({alias}.title_txt,'')")
        if col_ok("base_compensation_filing_org_amt"):
            parts.append(f"'base='||COALESCE(CAST({alias}.base_compensation_filing_org_amt AS TEXT),'')")
        if col_ok("bonus_filing_organization_amount"):
            parts.append(f"'bonus='||COALESCE(CAST({alias}.bonus_filing_organization_amount AS TEXT),'')")
        if col_ok("total_compensation_filing_org_amt"):
            parts.append(f"'total_org='||COALESCE(CAST({alias}.total_compensation_filing_org_amt AS TEXT),'')")
        if col_ok("total_compensation_rltd_orgs_amt"):
            parts.append(f"'total_rel='||COALESCE(CAST({alias}.total_compensation_rltd_orgs_amt AS TEXT),'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "pf_officer":
        parts = []
        if col_ok("title_txt"):
            parts.append(f"'title='||COALESCE({alias}.title_txt,'')")
        if col_ok("average_hrs_per_wk_devoted_to_pos_rt"):
            parts.append(f"'hrs/wk='||COALESCE({alias}.average_hrs_per_wk_devoted_to_pos_rt,'')")
        if col_ok("compensation_amt"):
            parts.append(f"'comp='||COALESCE(CAST({alias}.compensation_amt AS TEXT),'')")
        if col_ok("employee_benefits_amt"):
            parts.append(f"'benefits='||COALESCE(CAST({alias}.employee_benefits_amt AS TEXT),'')")
        if col_ok("expense_account_amt"):
            parts.append(f"'expense='||COALESCE(CAST({alias}.expense_account_amt AS TEXT),'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "sched_l_bus":
        parts = []
        if col_ok("relationship_description_txt"):
            parts.append(f"'relationship='||COALESCE({alias}.relationship_description_txt,'')")
        if col_ok("transaction_amt"):
            parts.append(f"'amount='||COALESCE(CAST({alias}.transaction_amt AS TEXT),'')")
        if col_ok("transaction_desc"):
            parts.append(f"'desc='||COALESCE({alias}.transaction_desc,'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "sched_l_ebt":
        parts = []
        if col_ok("rln_disqualified_person_org_txt"):
            parts.append(f"'relationship='||COALESCE({alias}.rln_disqualified_person_org_txt,'')")
        if col_ok("transaction_corrected_ind"):
            parts.append(f"'corrected='||COALESCE(CAST({alias}.transaction_corrected_ind AS TEXT),'')")
        if col_ok("transaction_desc"):
            parts.append(f"'desc='||COALESCE({alias}.transaction_desc,'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "sched_l_grant":
        parts = []
        if col_ok("relationship_with_org_txt"):
            parts.append(f"'relationship='||COALESCE({alias}.relationship_with_org_txt,'')")
        if col_ok("cash_grant_amt"):
            parts.append(f"'cash='||COALESCE(CAST({alias}.cash_grant_amt AS TEXT),'')")
        if col_ok("type_of_assistance_txt"):
            parts.append(f"'assist_type='||COALESCE({alias}.type_of_assistance_txt,'')")
        if col_ok("assistance_purpose_txt"):
            parts.append(f"'purpose='||COALESCE({alias}.assistance_purpose_txt,'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    if detail_key == "sched_l_loan":
        parts = []
        if col_ok("relationship_with_org_txt"):
            parts.append(f"'relationship='||COALESCE({alias}.relationship_with_org_txt,'')")
        if col_ok("original_principal_amt"):
            parts.append(f"'orig_principal='||COALESCE(CAST({alias}.original_principal_amt AS TEXT),'')")
        if col_ok("balance_due_amt"):
            parts.append(f"'balance_due='||COALESCE(CAST({alias}.balance_due_amt AS TEXT),'')")
        if col_ok("loan_purpose_txt"):
            parts.append(f"'purpose='||COALESCE({alias}.loan_purpose_txt,'')")
        base = " || ' | ' || ".join(parts) if parts else f"COALESCE({alias}.{hit_col},'')"
        return f"'name='||COALESCE({alias}.{hit_col},'')||CASE WHEN {alias}.{hit_col} IS NOT NULL THEN ' | ' ELSE '' END||({base})"

    return f"COALESCE({alias}.{hit_col}, '')"

def _base_where(person: str, fuzzy: bool, state: Optional[str], min_year: Optional[int], max_year: Optional[int]) -> Tuple[str, List]:
    where = []
    params: List = []

    if state:
        where.append("r.state = ?")
        params.append(state)

    if min_year is not None and max_year is not None:
        where.append("r.tax_year BETWEEN ? AND ?")
        params.extend([min_year, max_year])

    return (" AND ".join(where), params)

def _returns_source(state: Optional[str], min_year: Optional[int], max_year: Optional[int], cols: str = "*") -> Tuple[str, List, bool]:
    where = []
    params: List = []

    if state:
        where.append("state = ?")
        params.append(state)

    if min_year is not None and max_year is not None:
        where.append("tax_year BETWEEN ? AND ?")
        params.extend([min_year, max_year])

    if not where:
        return "returns", [], False

    return f"(SELECT {cols} FROM returns WHERE {' AND '.join(where)})", params, True

def _query_table(conn, table: str, hit_col: str, label: str, detail_key: str,
                 person: str, fuzzy: bool, state: Optional[str],
                 min_year: Optional[int], max_year: Optional[int], max_rows: int) -> List[Tuple]:
    if not _exists(conn, table):
        return []
    if not _has_col(conn, table, "filing_id"):
        return []
    if not _has_col(conn, table, hit_col):
        return []

    pred = _name_pred(f"t.{hit_col}", fuzzy)
    person_param = f"%{person}%" if fuzzy else person

    returns_sql, returns_params, returns_filtered = _returns_source(
        state,
        min_year,
        max_year,
        "filing_id, ein, org_name, tax_year, state",
    )
    detail_expr = _detail_expr(conn, table, "t", detail_key, hit_col)

    if returns_filtered:
        from_sql = f"""
    FROM {returns_sql} r
    JOIN {table} t
      ON t.filing_id = r.filing_id
"""
    else:
        from_sql = f"""
    FROM {table} t
    JOIN returns r
      ON r.filing_id = t.filing_id
"""

    sql = f"""
    SELECT
      r.filing_id,
      r.ein,
      r.org_name,
      r.tax_year,
      ? AS found_in,
      {detail_expr} AS detail
    {from_sql}
    WHERE {pred}
    LIMIT ?
    """
    params = [label] + returns_params + [person_param, max_rows]
    return conn.execute(sql, params).fetchall()

def _query_in_care_of(conn, person: str, fuzzy: bool, state: Optional[str],
                      min_year: Optional[int], max_year: Optional[int], max_rows: int) -> List[Tuple]:
    if not _exists(conn, "returns"):
        return []
    if not _has_col(conn, "returns", "in_care_of_name"):
        return []

    pred = _name_pred("r.in_care_of_name", fuzzy)
    person_param = f"%{person}%" if fuzzy else person

    returns_sql, returns_params, _ = _returns_source(
        state,
        min_year,
        max_year,
        "filing_id, ein, org_name, tax_year, state, in_care_of_name, us_address_line1, city, zip",
    )

    sql = f"""
    SELECT
      r.filing_id,
      r.ein,
      r.org_name,
      r.tax_year,
      'Returns (In Care Of Name)' AS found_in,
      'name='||COALESCE(r.in_care_of_name,'')
        ||CASE WHEN COALESCE(r.us_address_line1,'')<>'' THEN ' | addr1='||r.us_address_line1 ELSE '' END
        ||CASE WHEN COALESCE(r.city,'')<>'' THEN ' | city='||r.city ELSE '' END
        ||CASE WHEN COALESCE(r.state,'')<>'' THEN ' | state='||r.state ELSE '' END
        ||CASE WHEN COALESCE(r.zip,'')<>'' THEN ' | zip='||r.zip ELSE '' END
        AS detail
    FROM {returns_sql} r
    WHERE {pred}
    LIMIT ?
    """
    params = returns_params + [person_param, max_rows]
    return conn.execute(sql, params).fetchall()

def _dedupe_keep_first(rows: List[Tuple]) -> List[Tuple]:
    seen = set()
    out = []
    for row in rows:
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out

def _run_query(form) -> List[Tuple]:
    person, fuzzy, state, min_year, max_year, max_rows = _parse(form)
    if not person:
        return []

    conn = connect_ro()
    all_rows: List[Tuple] = []

    # direct returns "in care of" path
    all_rows.extend(_query_in_care_of(conn, person, fuzzy, state, min_year, max_year, max_rows))

    # curated sources
    remaining = max_rows
    for table, hit_col, label, detail_key in CURATED:
        if remaining <= 0:
            break
        rows = _query_table(conn, table, hit_col, label, detail_key, person, fuzzy, state, min_year, max_year, remaining)
        all_rows.extend(rows)
        remaining = max_rows - len(all_rows)

    all_rows = _dedupe_keep_first(all_rows)
    all_rows.sort(key=lambda r: (str(r[1] or ""), -(int(r[3]) if str(r[3]).isdigit() else 0), str(r[4] or ""), str(r[5] or "")))
    return all_rows[:max_rows]

def run(form):
    rows = _run_query(form)
    return HEADERS, rows

def export_rows(form) -> Iterable[Tuple]:
    return _run_query(form)
