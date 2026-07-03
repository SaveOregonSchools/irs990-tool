from typing import List, Tuple, Iterable, Optional
from common import connect_ro, normalize_eins

META = {
    "key": "ngo_core_data_lookup",
    "name": "Core Data Lookup",
    "description": (
        "Enter one or more EINs (comma/semicolon/space/newline separated), "
        "or check 'Return all Non-Profits' to ignore EINs. "
        "Optionally filter by state (2-letter code) and/or tax-year range. "
        "Extends v4 with filing-header flags, website, tax status, organization form, "
        "expanded financial summary fields, preparer firm fields, and political/lobbying indicators."
    ),
}

HEADERS = [
    "ein",
    "org_name",
    "dba_name",
    "tax_year",
    "return_type",
    "tax_exempt_status",
    "period_start",
    "period_end",
    "us_address_line1",
    "city",
    "state",
    "zip",
    "website_address_txt",
    "formation_year",
    "legal_domicile_state_cd",
    "organization_form",
    "mission_desc",
    "employees_count",
    "volunteers_count",
    "contributions_and_grants",
    "program_service_revenue",
    "investment_income",
    "other_revenue",
    "total_revenue",
    "grants_paid",
    "salaries_comp_emp_benefits",
    "professional_fundraising_fees",
    "total_fundraising_expenses",
    "other_expenses",
    "total_expenses",
    "revenue_less_expenses",
    "total_assets_boy",
    "total_assets_eoy",
    "total_liabilities_boy",
    "total_liabilities_eoy",
    "net_assets_boy",
    "net_assets_eoy",
    "political_campaign_activity_ind",
    "lobbying_activities_ind",
    "dues_assessments_ind",
    "membership_dues",
    "government_grants",
    "lobbying_expense",
    "address_change_ind",
    "name_change_ind",
    "initial_return_ind",
    "final_return_ind",
    "amended_return_ind",
    "application_pending_ind",
    "filing_id",
]

META["headers"] = HEADERS

US_STATES = [
    "", "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
    "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]

_LOBBYING_EXPENSE_CASE = """
        CASE
          WHEN c.return_type LIKE '990PF%' THEN NULL
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE COALESCE(
            sc.total_lobbying_expenditures_amt,
            sc.total_lobbying_expend_grp_amt,
            NULLIF(COALESCE(sc.total_direct_lobbying_amt, 0) + COALESCE(sc.total_grassroots_lobbying_amt, 0), 0),
            NULLIF(
              COALESCE(sc.media_advertisements_amt, 0)
              + COALESCE(sc.mailings_members_amt, 0)
              + COALESCE(sc.publications_or_broadcast_amt, 0)
              + COALESCE(sc.grants_other_organizations_amt, 0)
              + COALESCE(sc.direct_contact_legislators_amt, 0)
              + COALESCE(sc.rallies_demonstrations_amt, 0)
              + COALESCE(sc.other_activities_amt, 0),
              0
            )
          )
        END
"""

def _val_js(val: str) -> str:
    return repr(val)

def render_fields(form) -> str:
    f = form or {}
    val_eins = f.get("ein_list", "")
    return_all = "checked" if f.get("return_all") in (True, "true", "on", "1") else ""
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
          If checked, the EIN list is ignored and every available record is returned (optionally filtered by state and/or year).
        </div>
      </div>
      <div style="flex:0 1 200px;">
        <label for="state"><b>State filter</b> (2-letter):</label><br>
        <select id="state" name="state" style="min-width:180px;">{state_html}</select>
      </div>
      <div style="flex:0 1 160px;">
        <label for="min_year"><b>Min tax year</b>:</label><br>
        <input id="min_year" name="min_year" type="number" inputmode="numeric" value="{min_year}" placeholder="e.g. 2018" style="width:140px;">
      </div>
      <div style="flex:0 1 160px;">
        <label for="max_year"><b>Max tax year</b>:</label><br>
        <input id="max_year" name="max_year" type="number" inputmode="numeric" value="{max_year}" placeholder="e.g. 2021" style="width:140px;">
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

_SQL_SELECT = f"""
SELECT
    t.ein,
    t.org_name,
    t.dba_name,
    t.tax_year,
    t.return_type,
    t.tax_exempt_status,
    t.period_start,
    t.period_end,
    t.us_address_line1,
    t.city,
    t.state,
    t.zip,
    t.website_address_txt,
    t.formation_year,
    t.legal_domicile_state_cd,
    t.organization_form,
    t.mission_desc,
    t.employees_count,
    t.volunteers_count,
    t.contributions_and_grants,
    t.program_service_revenue,
    t.investment_income,
    t.other_revenue,
    t.total_revenue,
    t.grants_paid,
    t.salaries_comp_emp_benefits,
    t.professional_fundraising_fees,
    t.total_fundraising_expenses,
    t.other_expenses,
    t.total_expenses,
    t.revenue_less_expenses,
    t.total_assets_boy,
    t.total_assets_eoy,
    t.total_liabilities_boy,
    t.total_liabilities_eoy,
    t.net_assets_boy,
    t.net_assets_eoy,
    t.political_campaign_activity_ind,
    t.lobbying_activities_ind,
    t.dues_assessments_ind,
    t.membership_dues,
    t.government_grants,
    t.lobbying_expense,
    t.address_change_ind,
    t.name_change_ind,
    t.initial_return_ind,
    t.final_return_ind,
    t.amended_return_ind,
    t.application_pending_ind,
    t.filing_id
FROM (
    SELECT
        c.ein,
        r.org_name,
        r.dba_name,
        c.tax_year,
        c.return_type,
        rha.tax_period_begin_dt AS period_start,
        c.period_end,
        r.us_address_line1,
        r.city,
        r.state,
        r.zip,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.net_assets_or_fund_balances_boyamt
          WHEN c.return_type LIKE '990PF%' THEN COALESCE(pfbs.total_assets_boyamt,0) - COALESCE(pfbs.total_liabilities_boyamt,0)
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE f990.net_assets_or_fund_balances_boyamt
        END AS net_assets_boy,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.net_assets_or_fund_balances_eoyamt
          WHEN c.return_type LIKE '990PF%' THEN COALESCE(pfbs.total_assets_eoyamt,0) - COALESCE(pfbs.total_liabilities_eoyamt,0)
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE f990.net_assets_or_fund_balances_eoyamt
        END AS net_assets_eoy,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.program_service_revenue_amt
          WHEN c.return_type LIKE '990PF%' THEN NULL
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE COALESCE(f990.total_program_service_revenue_amt, f990.cyprogram_service_revenue_amt)
        END AS program_service_revenue,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.membership_dues_amt
          WHEN c.return_type LIKE '990PF%' THEN NULL
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE f990.membership_dues_amt
        END AS membership_dues,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.investment_income_amt
          WHEN c.return_type LIKE '990PF%' THEN pfa.net_investment_income_amt
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE f990.cyinvestment_income_amt
        END AS investment_income,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.grants_and_similar_amounts_paid_amt
          WHEN c.return_type LIKE '990PF%' THEN pf.total_grant_or_contri_pd_dur_yr_amt
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE f990.cygrants_and_similar_paid_amt
        END AS grants_paid,
        {_LOBBYING_EXPENSE_CASE} AS lobbying_expense,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN NULL
          WHEN c.return_type LIKE '990PF%' THEN NULL
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE COALESCE(f990.total_employee_cnt, f990.employee_cnt)
        END AS employees_count,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN NULL
          WHEN c.return_type LIKE '990PF%' THEN NULL
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE f990.total_volunteers_cnt
        END AS volunteers_count,
        CASE
          WHEN c.return_type LIKE '990PF%' THEN pfa.contri_paid_rev_and_expnss_amt
          ELSE h.contributions
        END AS contributions_and_grants,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.primary_exempt_purpose_txt
          WHEN c.return_type LIKE '990PF%' THEN pf.mission_desc_txt
          WHEN c.return_type LIKE '990T%'  THEN NULL
          ELSE COALESCE(f990.mission_desc, f990.activity_or_mission_desc)
        END AS mission_desc,
        h.total_revenue,
        h.total_expenses,
        h.government_grants,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.address_change_ind
          WHEN c.return_type LIKE '990PF%' THEN pf.address_change_ind
          ELSE f990.address_change_ind
        END AS address_change_ind,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.name_change_ind
          WHEN c.return_type LIKE '990PF%' THEN pf.name_change_ind
          ELSE f990.name_change_ind
        END AS name_change_ind,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.initial_return_ind
          WHEN c.return_type LIKE '990PF%' THEN pf.initial_return_ind
          ELSE f990.initial_return_ind
        END AS initial_return_ind,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.final_return_ind
          WHEN c.return_type LIKE '990PF%' THEN pf.final_return_ind
          ELSE f990.final_return_ind
        END AS final_return_ind,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.amended_return_ind
          WHEN c.return_type LIKE '990PF%' THEN pf.amended_return_ind
          ELSE f990.amended_return_ind
        END AS amended_return_ind,
        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.application_pending_ind
          WHEN c.return_type LIKE '990PF%' THEN pf.application_pending_ind
          ELSE f990.application_pending_ind
        END AS application_pending_ind,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.website_address_txt
          WHEN c.return_type LIKE '990PF%' THEN pf.website_address_txt
          ELSE f990.website_address_txt
        END AS website_address_txt,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN NULL
          WHEN c.return_type LIKE '990PF%' THEN NULL
          ELSE f990.formation_yr
        END AS formation_year,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN NULL
          WHEN c.return_type LIKE '990PF%' THEN NULL
          ELSE f990.legal_domicile_state_cd
        END AS legal_domicile_state_cd,

        CASE
          WHEN c.return_type LIKE '990PF%' THEN
            CASE
              WHEN UPPER(TRIM(CAST(pf.organization501c3_exempt_pfind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN '501(c)(3)'
              WHEN UPPER(TRIM(CAST(pf.organization4947a1_trtd_pfind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN '4947(a)(1)'
              WHEN UPPER(TRIM(CAST(pf.organization501c3_exempt_pfind AS TEXT))) IN ('0','FALSE','F','NO','N')
               AND UPPER(TRIM(CAST(pf.organization4947a1_trtd_pfind AS TEXT))) IN ('0','FALSE','F','NO','N')
                THEN 'other taxable private foundation'
              ELSE ''
            END

          WHEN c.return_type LIKE '990EZ%' THEN
            CASE
              WHEN UPPER(TRIM(CAST(ez.organization501c3_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN '501(c)(3)'
              WHEN UPPER(TRIM(CAST(ez.organization501c_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
               AND COALESCE(TRIM(CAST(ez.attr_organization501c_type_txt AS TEXT)), '') <> ''
                THEN '501(c)(' || TRIM(CAST(ez.attr_organization501c_type_txt AS TEXT)) || ')'
              WHEN UPPER(TRIM(CAST(ez.organization4947a1_not_pfind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN '4947(a)(1)'
              ELSE ''
            END

          ELSE
            CASE
              WHEN UPPER(TRIM(CAST(f990.organization501c3_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN '501(c)(3)'
              WHEN UPPER(TRIM(CAST(f990.organization501c_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
               AND COALESCE(TRIM(CAST(f990.attr_organization501c_type_txt AS TEXT)), '') <> ''
                THEN '501(c)(' || TRIM(CAST(f990.attr_organization501c_type_txt AS TEXT)) || ')'
              WHEN UPPER(TRIM(CAST(f990.organization4947a1_not_pfind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN '4947(a)(1)'
              ELSE ''
            END
        END AS tax_exempt_status,

        CASE
          WHEN c.return_type LIKE '990PF%' THEN ''

          WHEN c.return_type LIKE '990EZ%' THEN
            CASE
              WHEN UPPER(TRIM(CAST(ez.type_of_organization_corp_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN 'Corporation'
              WHEN UPPER(TRIM(CAST(ez.type_of_organization_trust_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN 'Trust'
              WHEN UPPER(TRIM(CAST(ez.type_of_organization_assoc_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN 'Association'
              WHEN UPPER(TRIM(CAST(ez.type_of_organization_other_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN COALESCE(TRIM(CAST(ez.type_of_organization_other_desc AS TEXT)), '')
              ELSE ''
            END

          ELSE
            CASE
              WHEN UPPER(TRIM(CAST(f990.type_of_organization_corp_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN 'Corporation'
              WHEN UPPER(TRIM(CAST(f990.type_of_organization_trust_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN 'Trust'
              WHEN UPPER(TRIM(CAST(f990.type_of_organization_assoc_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN 'Association'
              WHEN UPPER(TRIM(CAST(f990.type_of_organization_other_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X')
                THEN COALESCE(TRIM(CAST(f990.other_organization_dsc AS TEXT)), '')
              ELSE ''
            END
        END AS organization_form,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.other_revenue_total_amt
          WHEN c.return_type LIKE '990PF%' THEN pfa.other_income_rev_and_expnss_amt
          ELSE f990.cyother_revenue_amt
        END AS other_revenue,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.salaries_other_comp_empl_bnft_amt
          WHEN c.return_type LIKE '990PF%' THEN COALESCE(pfa.oth_empl_slrs_wgs_rev_and_expnss_amt,0) + COALESCE(pfa.pension_empl_bnft_rev_and_expnss_amt,0)
          ELSE f990.cysalaries_comp_emp_bnft_paid_amt
        END AS salaries_comp_emp_benefits,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN NULL
          WHEN c.return_type LIKE '990PF%' THEN NULL
          ELSE f990.cytotal_prof_fndrsng_expns_amt
        END AS professional_fundraising_fees,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN NULL
          WHEN c.return_type LIKE '990PF%' THEN NULL
          ELSE f990.cytotal_fundraising_expense_amt
        END AS total_fundraising_expenses,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.other_expenses_total_amt
          WHEN c.return_type LIKE '990PF%' THEN pfa.other_expenses_rev_and_expnss_amt
          ELSE f990.cyother_expenses_amt
        END AS other_expenses,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ez.excess_or_deficit_for_year_amt
          WHEN c.return_type LIKE '990PF%' THEN pfa.excess_revenue_over_expenses_amt
          ELSE f990.cyrevenues_less_expenses_amt
        END AS revenue_less_expenses,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ezta.boyamt
          WHEN c.return_type LIKE '990PF%' THEN pfbs.total_assets_boyamt
          ELSE f990.total_assets_boyamt
        END AS total_assets_boy,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN ezta.eoyamt
          WHEN c.return_type LIKE '990PF%' THEN pfbs.total_assets_eoyamt
          ELSE f990.total_assets_eoyamt
        END AS total_assets_eoy,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN eztl.boyamt
          WHEN c.return_type LIKE '990PF%' THEN pfbs.total_liabilities_boyamt
          ELSE f990.total_liabilities_boyamt
        END AS total_liabilities_boy,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN eztl.eoyamt
          WHEN c.return_type LIKE '990PF%' THEN pfbs.total_liabilities_eoyamt
          ELSE f990.total_liabilities_eoyamt
        END AS total_liabilities_eoy,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN
            CASE
              WHEN UPPER(TRIM(CAST(ez.political_campaign_acty_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X') THEN 'Yes'
              WHEN UPPER(TRIM(CAST(ez.political_campaign_acty_ind AS TEXT))) IN ('0','FALSE','F','NO','N') THEN 'No'
              ELSE ''
            END
          WHEN c.return_type LIKE '990PF%' THEN ''
          ELSE
            CASE
              WHEN UPPER(TRIM(CAST(f990.political_campaign_acty_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X') THEN 'Yes'
              WHEN UPPER(TRIM(CAST(f990.political_campaign_acty_ind AS TEXT))) IN ('0','FALSE','F','NO','N') THEN 'No'
              ELSE ''
            END
        END AS political_campaign_activity_ind,

        CASE
          WHEN c.return_type LIKE '990EZ%' THEN
            CASE
              WHEN UPPER(TRIM(CAST(ez.lobbying_activities_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X') THEN 'Yes'
              WHEN UPPER(TRIM(CAST(ez.lobbying_activities_ind AS TEXT))) IN ('0','FALSE','F','NO','N') THEN 'No'
              ELSE ''
            END
          WHEN c.return_type LIKE '990PF%' THEN
            CASE
              WHEN UPPER(TRIM(CAST(pf.legislative_political_acty_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X') THEN 'Yes'
              WHEN UPPER(TRIM(CAST(pf.legislative_political_acty_ind AS TEXT))) IN ('0','FALSE','F','NO','N') THEN 'No'
              ELSE ''
            END
          ELSE
            CASE
              WHEN UPPER(TRIM(CAST(f990.lobbying_activities_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X') THEN 'Yes'
              WHEN UPPER(TRIM(CAST(f990.lobbying_activities_ind AS TEXT))) IN ('0','FALSE','F','NO','N') THEN 'No'
              ELSE ''
            END
        END AS lobbying_activities_ind,

        CASE
          WHEN c.return_type LIKE '990PF%' THEN ''
          ELSE
            CASE
              WHEN UPPER(TRIM(CAST(sc.substantially_all_dues_nonded_ind AS TEXT))) IN ('1','TRUE','T','YES','Y','X') THEN 'Yes'
              WHEN UPPER(TRIM(CAST(sc.substantially_all_dues_nonded_ind AS TEXT))) IN ('0','FALSE','F','NO','N') THEN 'No'
              ELSE ''
            END
        END AS dues_assessments_ind,

        c.filing_id
    FROM canonical_by_ein_year c
    LEFT JOIN returns r ON r.filing_id = c.filing_id
    LEFT JOIN return_header_all rha ON rha.filing_id = c.filing_id
    LEFT JOIN core_hot h ON h.filing_id = c.filing_id
    LEFT JOIN irs990_root f990 ON f990.filing_id = c.filing_id
    LEFT JOIN irs990_ez_root ez ON ez.filing_id = c.filing_id
    LEFT JOIN irs990_pf_root pf ON pf.filing_id = c.filing_id
    LEFT JOIN irs990_schedule_c_root sc ON sc.filing_id = c.filing_id
    LEFT JOIN irs990_ez_form990_total_assets_grp ezta ON ezta.filing_id = c.filing_id
    LEFT JOIN irs990_ez_sum_of_total_liabilities_grp eztl ON eztl.filing_id = c.filing_id
    LEFT JOIN irs990_pf_analysis_of_revenue_and_expenses pfa ON pfa.filing_id = c.filing_id
    LEFT JOIN irs990_pf_form990_pfbalance_sheets_grp pfbs ON pfbs.filing_id = c.filing_id
) t
"""

def _parse_eins(form) -> List[str]:
    text = (form or {}).get("ein_list", "")
    return normalize_eins(text)

def _parse_filters(form) -> Tuple[bool, Optional[str], Optional[int], Optional[int]]:
    f = form or {}
    return_all = f.get("return_all") in (True, "true", "on", "1")
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

    return return_all, (state if state else None), min_year, max_year

def _build_where(return_all: bool, eins: List[str], state: Optional[str], min_year: Optional[int], max_year: Optional[int]):
    clauses = []
    params: List = []

    if not return_all:
        if not eins:
            return "WHERE 1=0", []
        placeholders = ",".join("?" for _ in eins)
        clauses.append(f"t.ein IN ({placeholders})")
        params.extend(eins)

    if state:
        clauses.append("t.state = ?")
        params.append(state)

    if min_year is not None and max_year is not None:
        clauses.append("t.tax_year BETWEEN ? AND ?")
        params.extend([min_year, max_year])

    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params

def _query(return_all: bool, eins: List[str], state: Optional[str], min_year: Optional[int], max_year: Optional[int]) -> List[Tuple]:
    rows: List[Tuple] = []
    conn = connect_ro()
    where_sql, base_params = _build_where(return_all, eins, state, min_year, max_year)
    sql = _SQL_SELECT + "\n" + where_sql + "\nORDER BY t.ein, t.tax_year DESC, t.filing_id"
    cur = conn.execute(sql, base_params)
    rows.extend(cur.fetchall())
    return rows

def run(form):
    return_all, state, min_year, max_year = _parse_filters(form)
    eins = _parse_eins(form)
    return HEADERS, _query(return_all, eins, state, min_year, max_year)

def export_rows(form) -> Iterable[Tuple]:
    return_all, state, min_year, max_year = _parse_filters(form)
    eins = _parse_eins(form)
    return _query(return_all, eins, state, min_year, max_year)
