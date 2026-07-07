import html
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from common import connect_ro, normalize_eins
from queries import ngo_core_data
from queries import nonprofit_deep_dive as deep


META = {
    "key": "fraud_risk_dashboard",
    "name": "Fraud & Risk Indicator Dashboard",
    "description": (
        "Single-EIN dashboard of explainable financial, governance, lobbying, grant, contractor, "
        "and related-organization risk indicators."
    ),
}

HEADERS = [
    "ein",
    "org_name",
    "latest_tax_year",
    "risk_score",
    "high_indicators",
    "medium_indicators",
    "low_indicators",
    "top_indicators",
]
META["headers"] = HEADERS

HIDE_PREVIEW_LIMIT = True
HIDE_CSV_EXPORT = True
DISABLE_ROW_LIMIT = True
PDF_EXPORT = True
RUN_BUTTON_LABEL = "Analyze EIN"

_LAST_KEY = None
_LAST_REPORT = None


def _h(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _num(value) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _money(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"${float(value):,.0f}"
    except Exception:
        return _h(value)


def _pct(part, whole) -> str:
    whole_n = _num(whole)
    if not whole_n:
        return ""
    return f"{100.0 * _num(part) / whole_n:.1f}%"


def _parse_ein(form) -> Optional[str]:
    values = normalize_eins((form or {}).get("ein", "") or (form or {}).get("ein_list", ""))
    return values[0] if len(values) == 1 else None


def _org_name_search(form) -> str:
    return (form or {}).get("org_search", "").strip()


def _object_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table','view') LIMIT 1",
            [name],
        ).fetchone()
    )


def render_fields(form) -> str:
    f = form or {}
    val = f.get("ein", "") or f.get("ein_list", "")
    org_search = f.get("org_search", "")
    return f"""
    <div class="row risk-search-fields" style="display:flex; gap:16px; flex-wrap:wrap; align-items:flex-end;">
      <div>
        <label for="ein"><b>Known EIN:</b></label><br>
        <input id="ein" name="ein" value="{_h(val)}" placeholder="e.g. 12-3456789" style="width:220px;">
      </div>
      <div>
        <label for="org_search"><b>Find EIN by organization name:</b></label><br>
        <input id="org_search" name="org_search" value="{_h(org_search)}" placeholder="e.g. Learning Policy" style="width:min(520px, 100%);">
      </div>
      <div>
        <button type="submit" name="_action" value="search_org">Search Name</button>
      </div>
    </div>
    """


def _core_years(ein: str) -> List[Dict]:
    headers, rows = ngo_core_data.run({"ein_list": ein})
    out = []
    for row in rows:
        item = dict(zip(headers, row))
        out.append(item)
    out.sort(key=lambda r: int(r.get("tax_year") or 0), reverse=True)
    return out


def _indicator(severity: str, category: str, title: str, tax_year, evidence: str, why: str, next_step: str) -> Dict:
    return {
        "severity": severity,
        "category": category,
        "title": title,
        "tax_year": tax_year,
        "evidence": evidence,
        "why": why,
        "next_step": next_step,
    }


def _severity_counts(indicators: List[Dict]) -> Dict[str, int]:
    counts = {"High": 0, "Medium": 0, "Low": 0}
    for item in indicators:
        sev = item.get("severity")
        if sev in counts:
            counts[sev] += 1
    return counts


def _risk_score(indicators: List[Dict]) -> int:
    counts = _severity_counts(indicators)
    return min(100, counts["High"] * 24 + counts["Medium"] * 11 + counts["Low"] * 4)


def _ratio(part, whole) -> float:
    whole_n = _num(whole)
    if not whole_n:
        return 0.0
    return _num(part) / whole_n


def _yes(value) -> bool:
    return str(value or "").strip().casefold() in {"yes", "x", "1", "true", "t", "y"}


def _financial_indicators(years: List[Dict]) -> List[Dict]:
    indicators = []
    for row in years:
        year = row.get("tax_year")
        revenue = _num(row.get("total_revenue"))
        expenses = _num(row.get("total_expenses"))
        net_income = _num(row.get("revenue_less_expenses"))
        assets = _num(row.get("total_assets_eoy"))
        liabilities = _num(row.get("total_liabilities_eoy"))
        net_assets = _num(row.get("net_assets_eoy"))
        grants_paid = _num(row.get("grants_paid"))
        lobbying = _num(row.get("lobbying_expense"))
        employees = _num(row.get("employees_count"))

        if net_income < 0:
            deficit_ratio = abs(net_income) / max(revenue, expenses, 1)
            severity = "High" if deficit_ratio >= 0.25 else "Medium"
            indicators.append(_indicator(
                severity,
                "Financial",
                "Operating deficit",
                year,
                f"Revenue {_money(revenue)}; expenses {_money(expenses)}; net income {_money(net_income)}.",
                "Large or repeated deficits can indicate financial stress, timing issues, or sustainability concerns.",
                "Review the return narrative, balance sheet, major revenue sources, and whether the deficit repeats.",
            ))

        if net_assets < 0:
            indicators.append(_indicator(
                "High",
                "Financial",
                "Negative net assets",
                year,
                f"Net assets were {_money(net_assets)}.",
                "Negative net assets can indicate insolvency risk or accumulated losses.",
                "Review liabilities, notes, debt schedules, and subsequent-year recovery.",
            ))
        elif assets and liabilities / assets >= 0.8:
            indicators.append(_indicator(
                "Medium",
                "Financial",
                "High liability-to-asset ratio",
                year,
                f"Liabilities {_money(liabilities)} were {_pct(liabilities, assets)} of assets.",
                "High leverage may increase financial risk or indicate restricted liquidity.",
                "Review debt composition, payables, and whether liabilities are program-related or unusual.",
            ))

        if expenses >= 500_000 and employees == 0:
            indicators.append(_indicator(
                "Medium",
                "Operations",
                "High expenses with zero employees",
                year,
                f"Reported employees: 0; total expenses: {_money(expenses)}.",
                "Organizations with large operations but no employees may rely heavily on contractors or affiliates.",
                "Review contractor payments, related-party transactions, and management service agreements.",
            ))

        grant_ratio = _ratio(grants_paid, expenses)
        if expenses and grant_ratio >= 0.75:
            indicators.append(_indicator(
                "Medium",
                "Grants",
                "Grants dominate expenses",
                year,
                f"Grants paid {_money(grants_paid)} were {_pct(grants_paid, expenses)} of expenses.",
                "Grant-heavy entities warrant review of recipients, concentration, and whether funds pass through intermediaries.",
                "Review top grantees, recipient EINs, related organizations, and grant purpose descriptions.",
            ))

        lobby_ratio = _ratio(lobbying, expenses)
        if lobbying and lobby_ratio >= 0.05:
            indicators.append(_indicator(
                "Medium" if lobby_ratio < 0.20 else "High",
                "Lobbying / Political",
                "Material lobbying expense",
                year,
                f"Lobbying expense {_money(lobbying)} was {_pct(lobbying, expenses)} of expenses.",
                "Material lobbying can be legitimate, but it is a key review area for exempt organizations.",
                "Open the Lobbying & Political Activity module and inspect Schedule C details.",
            ))
        elif _yes(row.get("lobbying_activities_ind")):
            indicators.append(_indicator(
                "Low",
                "Lobbying / Political",
                "Lobbying activity flag",
                year,
                "The core filing indicates lobbying activity.",
                "A lobbying flag without a large amount may still merit Schedule C review.",
                "Inspect Schedule C activity descriptions and expenditure fields.",
            ))

        if _yes(row.get("political_campaign_activity_ind")):
            indicators.append(_indicator(
                "High",
                "Lobbying / Political",
                "Political campaign activity flag",
                year,
                "The filing indicates political campaign activity.",
                "Political campaign intervention is a high-risk area, especially for 501(c)(3) organizations.",
                "Review Schedule C political activity and exempt-status context.",
            ))

        if _yes(row.get("dues_assessments_ind")):
            indicators.append(_indicator(
                "Low",
                "Lobbying / Political",
                "Membership dues or proxy-tax flag",
                year,
                "The filing reports dues, assessments, or similar political/lobbying-related activity.",
                "Dues and proxy-tax activity can reveal indirect political or lobbying funding flows.",
                "Review Schedule C dues and proxy-tax details.",
            ))

    asc = list(reversed(years))
    for prev, curr in zip(asc, asc[1:]):
        for label, key in [("revenue", "total_revenue"), ("expenses", "total_expenses"), ("grants paid", "grants_paid")]:
            old = _num(prev.get(key))
            new = _num(curr.get(key))
            if old < 1 or new < 1:
                continue
            change = (new - old) / old
            if abs(change) >= 1.0:
                indicators.append(_indicator(
                    "Medium",
                    "Trend",
                    f"Large year-over-year {label} change",
                    curr.get("tax_year"),
                    f"{label.title()} changed from {_money(old)} to {_money(new)} ({change * 100:.1f}%).",
                    "Sharp changes can be normal but may signal unusual events, restatements, or changed reporting.",
                    "Compare return narratives, major donors/grantees, and whether the change reverses in later years.",
                ))
    return indicators


def _compensation_indicators(conn, years: List[Dict]) -> List[Dict]:
    if not _object_exists(conn, "officers"):
        return []
    by_year = {int(row.get("tax_year") or 0): row for row in years}
    sql = """
    SELECT
      tax_year,
      SUM(person_total) AS total_comp,
      MAX(person_total) AS max_comp,
      SUM(CASE WHEN comp_from_related > 0 THEN comp_from_related ELSE 0 END) AS related_comp,
      COUNT(*) AS rows_count
    FROM (
      SELECT DISTINCT
        c.tax_year,
        o.person_name,
        o.title_txt,
        COALESCE(o.comp_from_org,0) AS comp_from_org,
        COALESCE(o.comp_from_related,0) AS comp_from_related,
        COALESCE(o.other_compensation,0) AS other_compensation,
        COALESCE(o.comp_from_org,0) + COALESCE(o.comp_from_related,0) + COALESCE(o.other_compensation,0) AS person_total
      FROM officers o
      JOIN canonical_by_ein_year c ON c.filing_id = o.filing_id
      WHERE c.ein = ?
    ) x
    GROUP BY tax_year
    """
    ein = years[0].get("ein") if years else ""
    indicators = []
    for tax_year, total_comp, max_comp, related_comp, rows_count in conn.execute(sql, [ein]):
        row = by_year.get(int(tax_year or 0), {})
        expenses = _num(row.get("total_expenses"))
        if expenses and _ratio(total_comp, expenses) >= 0.25:
            indicators.append(_indicator(
                "Medium",
                "Compensation",
                "Compensation concentration",
                tax_year,
                f"Officer/key employee compensation {_money(total_comp)} was {_pct(total_comp, expenses)} of expenses.",
                "High compensation concentration may be appropriate for small organizations but is a governance review point.",
                "Review officer roles, hours, related compensation, and comparability documentation.",
            ))
        if expenses and _ratio(max_comp, expenses) >= 0.20:
            indicators.append(_indicator(
                "Medium",
                "Compensation",
                "Single person compensation concentration",
                tax_year,
                f"Largest reported person compensation was {_money(max_comp)}, {_pct(max_comp, expenses)} of expenses.",
                "A single highly compensated person can be a risk marker when expenses are otherwise modest.",
                "Review title, hours, compensation basis, and related-party context.",
            ))
        if _num(related_comp) > 0:
            indicators.append(_indicator(
                "Low",
                "Compensation",
                "Compensation from related organizations",
                tax_year,
                f"Related-organization compensation totaled {_money(related_comp)}.",
                "Related compensation can indicate shared control, affiliated activity, or complex compensation arrangements.",
                "Compare Schedule R, Schedule J, and officer disclosures across related entities.",
            ))
    return indicators


def _grant_indicators(conn, years: List[Dict]) -> List[Dict]:
    if not _object_exists(conn, "grants_compat_v1"):
        return []
    by_year = {int(row.get("tax_year") or 0): row for row in years}
    ein = years[0].get("ein") if years else ""
    sql = """
    WITH candidate_filings AS (
      SELECT filing_id, tax_year
      FROM canonical_by_ein_year
      WHERE ein = ?
    ),
    grant_rows AS (
      SELECT
        c.tax_year,
        g.filing_id,
        g.recipient_ein,
        g.recipient_name,
        COALESCE(g.cash_amount,0) + COALESCE(g.noncash_amount,0) AS amount
      FROM candidate_filings c
      JOIN grants_compat_v1 g ON g.filing_id = c.filing_id
    ),
    recipient_totals AS (
      SELECT tax_year, recipient_ein, recipient_name, SUM(amount) AS recipient_total
      FROM grant_rows
      GROUP BY tax_year, recipient_ein, recipient_name
    ),
    year_totals AS (
      SELECT
        tax_year,
        COUNT(*) AS grant_rows,
        SUM(amount) AS total_amount,
        SUM(CASE WHEN COALESCE(TRIM(recipient_ein),'') = '' THEN amount ELSE 0 END) AS missing_ein_amount
      FROM grant_rows
      GROUP BY tax_year
    ),
    largest AS (
      SELECT tax_year, MAX(recipient_total) AS largest_recipient_total
      FROM recipient_totals
      GROUP BY tax_year
    )
    SELECT
      y.tax_year,
      y.grant_rows,
      y.total_amount,
      y.missing_ein_amount,
      l.largest_recipient_total
    FROM year_totals y
    LEFT JOIN largest l ON l.tax_year = y.tax_year
    ORDER BY y.tax_year DESC
    """
    indicators = []
    years_with_rows = set()
    for tax_year, grant_rows, total_amount, missing_ein_amount, largest_recipient_total in conn.execute(sql, [ein]):
        years_with_rows.add(int(tax_year))
        total = _num(total_amount)
        if total and _ratio(missing_ein_amount, total) >= 0.50:
            indicators.append(_indicator(
                "Medium",
                "Grants",
                "Most grant dollars lack recipient EINs",
                tax_year,
                f"Grant rows totaled {_money(total)}; missing-recipient-EIN amount was {_money(missing_ein_amount)}.",
                "Missing recipient EINs make it harder to trace funding flows and identify related parties.",
                "Review grant recipient names, addresses, and enhanced grant matching results.",
            ))
        if total and _ratio(largest_recipient_total, total) >= 0.60 and grant_rows > 1:
            indicators.append(_indicator(
                "Low",
                "Grants",
                "Grant concentration in one recipient",
                tax_year,
                f"Largest recipient received {_money(largest_recipient_total)} of {_money(total)}.",
                "Recipient concentration can be normal but is useful for pass-through and control review.",
                "Inspect top grantees and compare recipient governance or related-organization status.",
            ))

    for row in years:
        tax_year = int(row.get("tax_year") or 0)
        grants_paid = _num(row.get("grants_paid"))
        if grants_paid > 0 and tax_year not in years_with_rows:
            indicators.append(_indicator(
                "Low",
                "Data Quality",
                "Grants paid total has no grant-detail rows",
                tax_year,
                f"Core grants paid reports {_money(grants_paid)}, but no grant rows were found in grants_compat_v1.",
                "This can occur because of return type, schema variation, missing schedules, or extraction gaps.",
                "Compare the source XML and grant extraction coverage for the filing.",
            ))
    return indicators


def _contractor_indicators(conn, years: List[Dict]) -> List[Dict]:
    if not _object_exists(conn, "vw_contractors"):
        return []
    by_year = {int(row.get("tax_year") or 0): row for row in years}
    ein = years[0].get("ein") if years else ""
    sql = """
    SELECT
      c.tax_year,
      SUM(COALESCE(vc.compensation_amt,0)) AS total_contractors,
      MAX(COALESCE(vc.compensation_amt,0)) AS largest_contractor,
      COUNT(*) AS contractor_rows
    FROM canonical_by_ein_year c
    JOIN vw_contractors vc ON vc.filing_id = c.filing_id
    WHERE c.ein = ?
    GROUP BY c.tax_year
    """
    indicators = []
    for tax_year, total_contractors, largest_contractor, contractor_rows in conn.execute(sql, [ein]):
        expenses = _num(by_year.get(int(tax_year or 0), {}).get("total_expenses"))
        if expenses and _ratio(total_contractors, expenses) >= 0.30:
            indicators.append(_indicator(
                "Medium",
                "Contractors",
                "Contractor payments are a large share of expenses",
                tax_year,
                f"Contractors totaled {_money(total_contractors)}, {_pct(total_contractors, expenses)} of expenses.",
                "Heavy contractor reliance can obscure who performs work or whether services are related-party controlled.",
                "Review vendor names, services, addresses, and overlap with officers or related organizations.",
            ))
        if expenses and _ratio(largest_contractor, expenses) >= 0.20 and contractor_rows > 1:
            indicators.append(_indicator(
                "Low",
                "Contractors",
                "Single contractor concentration",
                tax_year,
                f"Largest contractor payment was {_money(largest_contractor)}, {_pct(largest_contractor, expenses)} of expenses.",
                "Vendor concentration can be legitimate but useful for procurement and related-party review.",
                "Review the contractor identity, services, and whether payments repeat across years.",
            ))
    return indicators


def _related_org_indicators(conn, years: List[Dict]) -> List[Dict]:
    filing_years = {
        row.get("filing_id"): int(row.get("tax_year") or 0)
        for row in years
        if row.get("filing_id")
    }
    if not filing_years:
        return []

    placeholders = ",".join("?" for _ in filing_years)
    params = list(filing_years)
    related_by_year: Dict[int, Dict[str, float]] = defaultdict(lambda: {"rows": 0, "controlled": 0, "involved": 0.0})

    controlled_tables = [
        "irs990_schedule_r_id_related_tax_exempt_org_grp",
        "irs990_schedule_r_id_related_org_txbl_corp_tr_grp",
        "irs990_schedule_r_id_related_org_txbl_partnership_grp",
    ]
    for table in controlled_tables:
        if not _object_exists(conn, table):
            continue
        sql = f"""
        SELECT
          filing_id,
          COUNT(*) AS row_count,
          SUM(CASE WHEN UPPER(TRIM(COALESCE(controlled_organization_ind,''))) IN ('X','1','TRUE','T','YES','Y') THEN 1 ELSE 0 END) AS controlled_count
        FROM {table}
        WHERE filing_id IN ({placeholders})
        GROUP BY filing_id
        """
        for filing_id, row_count, controlled_count in conn.execute(sql, params):
            year = filing_years.get(filing_id)
            if not year:
                continue
            related_by_year[year]["rows"] += row_count or 0
            related_by_year[year]["controlled"] += controlled_count or 0

    for table in [
        "irs990_schedule_r_id_disregarded_entities_grp",
        "irs990_schedule_r_unrelated_org_txbl_partnership_grp",
    ]:
        if not _object_exists(conn, table):
            continue
        sql = f"""
        SELECT filing_id, COUNT(*) AS row_count
        FROM {table}
        WHERE filing_id IN ({placeholders})
        GROUP BY filing_id
        """
        for filing_id, row_count in conn.execute(sql, params):
            year = filing_years.get(filing_id)
            if year:
                related_by_year[year]["rows"] += row_count or 0

    if _object_exists(conn, "irs990_schedule_r_transactions_related_org_grp"):
        sql = f"""
        SELECT filing_id, COUNT(*) AS row_count, SUM(COALESCE(involved_amt,0)) AS involved_amount
        FROM irs990_schedule_r_transactions_related_org_grp
        WHERE filing_id IN ({placeholders})
        GROUP BY filing_id
        """
        for filing_id, row_count, involved_amount in conn.execute(sql, params):
            year = filing_years.get(filing_id)
            if year:
                related_by_year[year]["rows"] += row_count or 0
                related_by_year[year]["involved"] += _num(involved_amount)

    by_year = {int(row.get("tax_year") or 0): row for row in years}
    indicators = []
    for tax_year, summary in related_by_year.items():
        related_rows = int(summary.get("rows") or 0)
        controlled_count = int(summary.get("controlled") or 0)
        involved_amount = _num(summary.get("involved"))
        expenses = _num(by_year.get(int(tax_year or 0), {}).get("total_expenses"))
        if related_rows:
            indicators.append(_indicator(
                "Low",
                "Related Organizations",
                "Schedule R related organizations reported",
                tax_year,
                f"{related_rows} related-organization rows; {controlled_count or 0} controlled-organization rows.",
                "Related organizations are not inherently problematic, but they matter for tracing control and funding flows.",
                "Review Schedule R relationship categories, transactions, and exempt-code sections.",
            ))
        if expenses and _ratio(involved_amount, expenses) >= 0.20:
            indicators.append(_indicator(
                "Medium",
                "Related Organizations",
                "Material related-organization transaction amount",
                tax_year,
                f"Schedule R involved amount {_money(involved_amount)} was {_pct(involved_amount, expenses)} of expenses.",
                "Large related-party transactions can indicate money movement through affiliated entities.",
                "Compare transaction types to grants, contractor payments, and officer overlap.",
            ))
    return indicators


def _build_indicators(years: List[Dict]) -> List[Dict]:
    conn = connect_ro()
    indicators = []
    indicators.extend(_financial_indicators(years))
    indicators.extend(_compensation_indicators(conn, years))
    indicators.extend(_grant_indicators(conn, years))
    indicators.extend(_contractor_indicators(conn, years))
    indicators.extend(_related_org_indicators(conn, years))
    order = {"High": 0, "Medium": 1, "Low": 2}
    indicators.sort(key=lambda i: (order.get(i.get("severity"), 9), -(int(i.get("tax_year") or 0)), i.get("category"), i.get("title")))
    return indicators


def _improvement_notes() -> List[Dict]:
    return [
        {
            "title": "IRS TEOS / Pub 78 exempt-status checks",
            "body": "Use IRS Exempt Organizations Business Master File, Pub 78, and revocation data to verify current exemption, deductibility, subsection, and revocation risk.",
        },
        {
            "title": "FEC and state campaign-finance APIs",
            "body": "Match EINs, names, officers, and addresses against FEC committees, independent expenditures, and state campaign-finance records to detect political-adjacent activity not fully visible on Form 990.",
        },
        {
            "title": "Lobbying registries",
            "body": "Integrate Senate LDA, state lobbying registries, and local lobbying disclosures to compare self-reported 990 lobbying with registered lobbying clients, issues, and payments.",
        },
        {
            "title": "Sanctions, charity regulator, and corporate registry data",
            "body": "Cross-check directors, vendors, and related organizations against state charity registrations, secretary-of-state records, OFAC sanctions, and address/agent networks.",
        },
        {
            "title": "Network analysis",
            "body": "Build graph edges for grants, contractors, shared officers, shared addresses, and Schedule R relationships to flag pass-through chains and recurring circular flows.",
        },
    ]


def _rows_from_report(report: Dict) -> List[Tuple]:
    if report.get("error") or report.get("search_results") is not None:
        return []
    counts = report.get("counts") or {}
    indicators = report.get("indicators") or []
    years = report.get("years") or []
    latest = years[0] if years else {}
    top = "; ".join(f"{i['severity']}: {i['title']} ({i.get('tax_year') or ''})" for i in indicators[:5])
    return [(
        report.get("ein"),
        report.get("org_name"),
        latest.get("tax_year"),
        report.get("risk_score"),
        counts.get("High", 0),
        counts.get("Medium", 0),
        counts.get("Low", 0),
        top,
    )]


def _build_report(form) -> Dict:
    name = _org_name_search(form)
    if (form or {}).get("_action") == "search_org":
        return {"search_query": name, "search_results": deep._search_orgs_by_name(name), "years": []}

    ein = _parse_ein(form)
    if not ein:
        if name:
            return {"search_query": name, "search_results": deep._search_orgs_by_name(name), "years": []}
        return {"error": "Enter exactly one valid 9-digit EIN, or search by organization name.", "years": []}

    years = _core_years(ein)
    if not years:
        return {"error": f"No canonical filings found for EIN {ein}.", "years": []}

    indicators = _build_indicators(years)
    counts = _severity_counts(indicators)
    return {
        "ein": ein,
        "org_name": years[0].get("org_name"),
        "years": years,
        "indicators": indicators,
        "counts": counts,
        "risk_score": _risk_score(indicators),
        "improvements": _improvement_notes(),
    }


def _cache_key(form) -> str:
    ein = "".join(ch for ch in ((form or {}).get("ein", "") or (form or {}).get("ein_list", "")) if ch.isdigit())
    if ein:
        return ein
    return "search:" + _org_name_search(form).casefold()


def run(form):
    global _LAST_KEY, _LAST_REPORT
    report = _build_report(form)
    _LAST_KEY = _cache_key(form)
    _LAST_REPORT = report
    return HEADERS, _rows_from_report(report)


def export_rows(form) -> Iterable[Tuple]:
    return _rows_from_report(_build_report(form))


def _render_search_results(report: Dict) -> str:
    query = report.get("search_query") or ""
    results = report.get("search_results") or []
    if not results:
        rows = f'<tr><td colspan="6" class="muted">No canonical filings matched "{_h(query)}".</td></tr>'
    else:
        rendered = []
        for item in results:
            city_state = ", ".join(part for part in [item.get("city"), item.get("state")] if part)
            rendered.append(f"""
            <tr>
              <td>{_h(item.get("org_name"))}</td>
              <td>{_h(item.get("ein"))}</td>
              <td>{_h(city_state)}</td>
              <td>{_h(item.get("tax_year"))}</td>
              <td>{_h(item.get("return_type"))}</td>
              <td>
                <form method="post" action="/run" style="margin:0;">
                  <input type="hidden" name="qkey" value="fraud_risk_dashboard">
                  <input type="hidden" name="org_search" value="{_h(query)}">
                  <input type="hidden" name="ein" value="{_h(item.get("ein"))}">
                  <button type="submit">Analyze</button>
                </form>
              </td>
            </tr>
            """)
        rows = "".join(rendered)
    return f"""
    <style>
      .risk-search-results {{ border:1px solid #d8dde6; background:#fff; padding:14px; margin:18px 0; }}
      .risk-search-results h3 {{ margin:0 0 10px; }}
      .risk-search-results table {{ width:100%; border-collapse:collapse; }}
      .risk-search-results th, .risk-search-results td {{ padding:7px 6px; border-bottom:1px solid #eee; text-align:left; white-space:normal; }}
      .risk-search-results tbody tr:nth-child(odd) {{ background:#f7f7f7; }}
      .risk-search-results button {{ min-height:30px; padding:5px 10px; }}
      .muted {{ color:#777; }}
    </style>
    <div class="risk-search-results">
      <h3>Organization Matches</h3>
      <table>
        <thead><tr><th>Organization</th><th>EIN</th><th>City, State</th><th>Latest Year</th><th>Filing Type</th><th></th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def _score_band(score: int) -> str:
    if score >= 70:
        return "High Review Priority"
    if score >= 35:
        return "Moderate Review Priority"
    return "Baseline Review Priority"


def _indicator_cards(indicators: List[Dict]) -> str:
    if not indicators:
        return '<div class="empty-note">No configured indicators were triggered from the currently available data.</div>'
    cards = []
    for item in indicators:
        sev = item.get("severity")
        cards.append(f"""
        <article class="risk-card risk-{_h(sev).lower()}">
          <div class="risk-card-head">
            <span class="badge">{_h(sev)}</span>
            <span class="category">{_h(item.get("category"))}</span>
            <span class="year">{_h(item.get("tax_year"))}</span>
          </div>
          <h3>{_h(item.get("title"))}</h3>
          <p><b>Evidence:</b> {_h(item.get("evidence"))}</p>
          <p><b>Why it matters:</b> {_h(item.get("why"))}</p>
          <p><b>Next step:</b> {_h(item.get("next_step"))}</p>
        </article>
        """)
    return "".join(cards)


def _year_summary_rows(years: List[Dict], indicators: List[Dict]) -> str:
    counts_by_year: Dict[int, Dict[str, int]] = defaultdict(lambda: {"High": 0, "Medium": 0, "Low": 0})
    for item in indicators:
        year = int(item.get("tax_year") or 0)
        if year:
            counts_by_year[year][item.get("severity")] += 1
    rows = []
    for row in years:
        year = int(row.get("tax_year") or 0)
        counts = counts_by_year[year]
        rows.append(f"""
        <tr>
          <td>{_h(year)}</td>
          <td>{_h(row.get("return_type"))}</td>
          <td>{_money(row.get("total_revenue"))}</td>
          <td>{_money(row.get("total_expenses"))}</td>
          <td>{_money(row.get("grants_paid"))}</td>
          <td>{_money(row.get("lobbying_expense"))}</td>
          <td>{counts["High"]}</td>
          <td>{counts["Medium"]}</td>
          <td>{counts["Low"]}</td>
        </tr>
        """)
    return "".join(rows)


def _improvement_cards(items: List[Dict]) -> str:
    return "".join(f'<li><b>{_h(i["title"])}:</b> {_h(i["body"])}</li>' for i in items)


def _render_report(report: Dict, print_mode: bool = False) -> str:
    indicators = report.get("indicators") or []
    counts = report.get("counts") or {}
    score = int(report.get("risk_score") or 0)
    years = report.get("years") or []
    latest = years[0] if years else {}
    print_css = """
      @page { size: letter portrait; margin: 0.4in; }
      @media print {
        body { background:#fff; }
        .print-toolbar { display:none !important; }
        .risk-dashboard { font-size: 10px; }
        .risk-grid { grid-template-columns: repeat(4, 1fr); }
        .risk-card, .risk-panel { break-inside: avoid; page-break-inside: avoid; }
        .risk-card { padding: 8px; margin-bottom: 7px; }
        .risk-card h3 { font-size: 12px; }
      }
    """ if print_mode else ""
    return f"""
    <style>
      .risk-dashboard {{ --border:#d8dde6; --muted:#647084; --ink:#202733; }}
      .risk-hero {{ border:1px solid var(--border); background:#fff; padding:16px; margin:18px 0; }}
      .risk-hero h2 {{ margin:0 0 4px; }}
      .risk-subtitle {{ color:var(--muted); margin:0; }}
      .risk-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap:10px; margin-top:14px; }}
      .risk-metric {{ border:1px solid var(--border); background:#f7f9fc; padding:10px; }}
      .risk-metric span {{ display:block; color:var(--muted); font-size:12px; }}
      .risk-metric strong {{ display:block; font-size:22px; margin-top:3px; }}
      .risk-panel {{ border:1px solid var(--border); background:#fff; padding:14px; margin:16px 0; }}
      .risk-panel h3 {{ margin:0 0 10px; }}
      .risk-table {{ width:100%; border-collapse:collapse; font-size:13px; }}
      .risk-table th, .risk-table td {{ padding:6px 5px; border-bottom:1px solid #eee; text-align:left; white-space:normal; }}
      .risk-table tbody tr:nth-child(odd) {{ background:#f7f7f7; }}
      .risk-cards {{ display:grid; grid-template-columns:1fr; gap:10px; }}
      .risk-card {{ border:1px solid var(--border); border-left-width:5px; background:#fff; padding:12px; }}
      .risk-card h3 {{ margin:8px 0; }}
      .risk-card p {{ margin:5px 0; line-height:1.35; }}
      .risk-card-head {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; color:var(--muted); font-size:12px; }}
      .badge {{ border-radius:999px; padding:2px 8px; font-weight:750; color:#fff; background:#647084; }}
      .risk-high {{ border-left-color:#b42318; }}
      .risk-high .badge {{ background:#b42318; }}
      .risk-medium {{ border-left-color:#b35c00; }}
      .risk-medium .badge {{ background:#b35c00; }}
      .risk-low {{ border-left-color:#28698f; }}
      .risk-low .badge {{ background:#28698f; }}
      .empty-note {{ color:var(--muted); padding:12px; background:#f7f9fc; border:1px solid var(--border); }}
      .improvement-list {{ margin:0; padding-left:18px; }}
      .improvement-list li {{ margin:7px 0; line-height:1.35; }}
      {print_css}
    </style>
    <div class="risk-dashboard">
      <section class="risk-hero">
        <h2>{_h(report.get("org_name"))}</h2>
        <p class="risk-subtitle">EIN {_h(report.get("ein"))} &middot; Latest filing {_h(latest.get("tax_year"))} {_h(latest.get("return_type"))}</p>
        <div class="risk-grid">
          <div class="risk-metric"><span>Risk Score</span><strong>{score}</strong></div>
          <div class="risk-metric"><span>Review Band</span><strong>{_h(_score_band(score))}</strong></div>
          <div class="risk-metric"><span>High</span><strong>{counts.get("High", 0)}</strong></div>
          <div class="risk-metric"><span>Medium / Low</span><strong>{counts.get("Medium", 0)} / {counts.get("Low", 0)}</strong></div>
        </div>
      </section>

      <section class="risk-panel">
        <h3>Year Summary</h3>
        <table class="risk-table">
          <thead><tr><th>Tax Year</th><th>Type</th><th>Revenue</th><th>Expenses</th><th>Grants Paid</th><th>Lobbying</th><th>High</th><th>Medium</th><th>Low</th></tr></thead>
          <tbody>{_year_summary_rows(years, indicators)}</tbody>
        </table>
      </section>

      <section class="risk-panel">
        <h3>Triggered Indicators</h3>
        <div class="risk-cards">{_indicator_cards(indicators)}</div>
      </section>

      <section class="risk-panel">
        <h3>Ways To Improve This Dashboard</h3>
        <ul class="improvement-list">{_improvement_cards(report.get("improvements") or [])}</ul>
      </section>
    </div>
    """


def render_results(form, headers, rows) -> str:
    key = _cache_key(form)
    report = _LAST_REPORT if key == _LAST_KEY and _LAST_REPORT is not None else _build_report(form)
    if "search_results" in report:
        return _render_search_results(report)
    if report.get("error"):
        return f'<div class="err"><b>{_h(report["error"])}</b></div>'
    return _render_report(report)


def render_pdf_export(form) -> str:
    report = _build_report(form)
    if report.get("error"):
        body = f'<div class="err"><b>{_h(report["error"])}</b></div>'
    elif "search_results" in report:
        body = _render_search_results(report)
    else:
        body = _render_report(report, print_mode=True)
    title = f'{report.get("org_name") or "Fraud Risk Dashboard"} - PDF Export'
    return f"""<!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>{_h(title)}</title>
      <style>
        body {{
          font-family: system-ui, Segoe UI, Arial, sans-serif;
          color: #202733;
          background: #f7f9fc;
          margin: 0 auto;
          max-width: 1280px;
          padding: 18px;
        }}
        .print-toolbar {{
          display:flex;
          align-items:center;
          justify-content:space-between;
          gap:12px;
          margin:0 0 14px;
          padding:10px 12px;
          border:1px solid #d8dde6;
          background:#fff;
        }}
        .print-toolbar p {{ margin:0; color:#647084; }}
        .print-toolbar button {{
          border:1px solid #125f85;
          background:#1c78a6;
          color:#fff;
          border-radius:6px;
          padding:8px 12px;
          font:inherit;
          font-weight:650;
          cursor:pointer;
        }}
        .err {{ background:#ffecec; border:1px solid #f5b5b5; padding:8px; white-space:pre-wrap; }}
        @media print {{ body {{ max-width:none; padding:0; margin:0; background:#fff; }} }}
      </style>
    </head>
    <body>
      <div class="print-toolbar">
        <p>Use your browser print dialog to save this report as a PDF.</p>
        <button type="button" onclick="window.print()">Print / Save PDF</button>
      </div>
      {body}
      <script>
        window.addEventListener("load", function() {{
          setTimeout(function() {{ window.print(); }}, 250);
        }});
      </script>
    </body>
    </html>"""
