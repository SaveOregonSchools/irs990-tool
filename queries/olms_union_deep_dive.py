from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from common import connect_olms_ro
from queries._olms_common import clean_ein, fnums, h


META = {
    "key": "olms_union_deep_dive",
    "name": "OLMS Union Deep Dive",
    "description": "Single-union OLMS identity, financial trends, filing compliance, grants, payees, and compensation.",
}

HEADERS = [
    "f_num", "period_end", "form_type", "members", "total_receipts",
    "total_disbursements", "assets", "liabilities", "regular_dues",
]
META["headers"] = HEADERS

HIDE_PREVIEW_LIMIT = True
HIDE_CSV_EXPORT = True
DISABLE_ROW_LIMIT = True
RUN_BUTTON_LABEL = "Open Union"

_LAST_KEY = None
_LAST_REPORT = None


def render_fields(form) -> str:
    form = form or {}
    return f"""
    <div class="row" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <div><label><b>OLMS file number:</b></label><br>
        <input name="f_num" value="{h(form.get('f_num',''))}" placeholder="e.g. 123456"></div>
      <div><label><b>Matched IRS EIN:</b></label><br>
        <input name="ein" value="{h(form.get('ein',''))}" placeholder="e.g. 12-3456789"></div>
      <div style="min-width:320px"><label><b>Organization name:</b></label><br>
        <input name="org_search" value="{h(form.get('org_search',''))}" style="width:100%" placeholder="e.g. Oregon Education Association"></div>
      <div><button name="_action" value="search_org">Search Name</button></div>
    </div>
    """


def _cache_key(form) -> Tuple[str, str, str, str]:
    form = form or {}
    return tuple(str(form.get(key, "")) for key in ("f_num", "ein", "org_search", "_action"))


def _selected_fnum(form) -> Optional[int]:
    parsed = fnums((form or {}).get("f_num", ""))
    if len(parsed) == 1:
        return parsed[0]
    ein = clean_ein((form or {}).get("ein", ""))
    if ein:
        conn = connect_olms_ro()
        try:
            row = conn.execute(
                "SELECT f_num FROM v_accepted_irs_matches WHERE candidate_ein=? ORDER BY f_num LIMIT 2",
                (ein,),
            ).fetchall()
            if len(row) == 1:
                return int(row[0][0])
        finally:
            conn.close()
    return None


def _search(name: str) -> List[Tuple]:
    name = (name or "").strip()
    if not name:
        return []
    conn = connect_olms_ro()
    try:
        return conn.execute(
            """
            SELECT o.f_num,o.display_name,o.affiliation,o.city,o.state,o.latest_period_end,
                   m.candidate_ein
            FROM organizations o
            LEFT JOIN v_accepted_irs_matches m USING(f_num)
            WHERE UPPER(o.display_name) LIKE ? OR UPPER(COALESCE(o.union_name,'')) LIKE ?
               OR UPPER(COALESCE(o.unit_name,'')) LIKE ?
            ORDER BY CASE WHEN UPPER(o.display_name)=UPPER(?) THEN 0 ELSE 1 END,
                     o.latest_period_end DESC,o.display_name
            LIMIT 50
            """,
            tuple([f"%{name.upper()}%"] * 3 + [name]),
        ).fetchall()
    finally:
        conn.close()


def _top_by_period(conn, view: str, f_num: int, amount_column: str) -> Dict[str, List[Tuple]]:
    result: Dict[str, List[Tuple]] = defaultdict(list)
    rows = conn.execute(
        f"""
        SELECT period_end,payee_name,{amount_column},disbursement_category,type_or_class
        FROM {view}
        WHERE f_num=?
        ORDER BY period_end DESC,{amount_column} DESC,payee_name
        """,
        (f_num,),
    )
    for row in rows:
        if len(result[row[0]]) < 5:
            result[row[0]].append(row[1:])
    return result


def _build_report(form) -> Dict:
    form = form or {}
    name = (form.get("org_search") or "").strip()
    selected = _selected_fnum(form)
    if form.get("_action") == "search_org" or (name and selected is None):
        matches = _search(name)
        if len(matches) != 1 or form.get("_action") == "search_org":
            return {"search_query": name, "search_results": matches, "rows": []}
        selected = int(matches[0][0])
    if selected is None:
        return {"error": "Enter one OLMS file number, a uniquely matched EIN, or search by organization name.", "rows": []}

    conn = connect_olms_ro()
    try:
        header = conn.execute(
            """
            SELECT o.*,m.candidate_ein,m.match_status,m.match_method,m.confidence
            FROM organizations o LEFT JOIN v_accepted_irs_matches m USING(f_num)
            WHERE o.f_num=?
            """,
            (selected,),
        ).fetchone()
        if not header:
            return {"error": f"No OLMS organization found for file number {selected}.", "rows": []}
        columns = [item[0] for item in conn.execute(
            """
            SELECT o.*,m.candidate_ein,m.match_status,m.match_method,m.confidence
            FROM organizations o LEFT JOIN v_accepted_irs_matches m USING(f_num) WHERE 1=0
            """
        ).description]
        org = dict(zip(columns, header))
        trends = conn.execute(
            """
            SELECT p.f_num,p.period_end,p.latest_form_type,f.members,f.ttl_receipts,
                   f.ttl_disbursements,f.ttl_assets,f.ttl_liabilities,
                   MAX(CASE WHEN r.rate_type=1101 THEN r.amount END) AS regular_dues,
                   p.latest_rpt_id
            FROM filing_periods p
            JOIN filings f ON f.rpt_id=p.latest_rpt_id
            LEFT JOIN rates_dues_fees r ON r.rpt_id=p.latest_rpt_id
            WHERE p.f_num=?
            GROUP BY p.period_key
            ORDER BY p.period_end DESC
            """,
            (selected,),
        ).fetchall()
        history = conn.execute(
            """
            SELECT period_start,period_end,latest_form_type,initial_receive_date,due_date,
                   filing_status,days_late,amendment_count,latest_receive_date,hardship,
                   terminal,latest_rpt_id
            FROM filing_periods WHERE f_num=? ORDER BY period_end DESC
            """,
            (selected,),
        ).fetchall()
        missing = conn.execute(
            """
            SELECT period_end,due_date,status,days_late_or_overdue,reason,data_as_of
            FROM compliance_results
            WHERE f_num=? AND result_kind<>'OBSERVED'
            ORDER BY period_end DESC
            """,
            (selected,),
        ).fetchall()
        grants = _top_by_period(conn, "v_grants_paid_summary", selected, "total_amount")
        vendors = _top_by_period(conn, "v_vendor_payments_summary", selected, "total_amount")
        officers: Dict[str, List[Tuple]] = defaultdict(list)
        for row in conn.execute(
            """
            SELECT p.period_end,TRIM(COALESCE(d.first_name,'') || ' ' || COALESCE(d.last_name,'')),
                   d.title,d.total
            FROM filing_periods p JOIN disbursements_employee_officer d ON d.rpt_id=p.latest_rpt_id
            WHERE p.f_num=? ORDER BY p.period_end DESC,d.total DESC
            """,
            (selected,),
        ):
            if len(officers[row[0]]) < 5:
                officers[row[0]].append(row[1:])
        return {
            "org": org,
            "rows": [tuple(row[:9]) for row in trends],
            "trend_rows": trends,
            "history": history,
            "missing": missing,
            "grants": grants,
            "vendors": vendors,
            "officers": officers,
        }
    finally:
        conn.close()


def run(form):
    global _LAST_KEY, _LAST_REPORT
    report = _build_report(form)
    _LAST_KEY = _cache_key(form)
    _LAST_REPORT = report
    return HEADERS, report.get("rows", [])


def export_rows(form) -> Iterable[Tuple]:
    return _build_report(form).get("rows", [])


def _money(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"${float(value):,.0f}"
    except Exception:
        return h(value)


def _render_search(report: Dict) -> str:
    rows = report.get("search_results", [])
    if not rows:
        return f'<div class="err">No OLMS organizations matched <b>{h(report.get("search_query"))}</b>.</div>'
    body = "".join(
        f"<tr><td><a href='/query/olms_union_deep_dive?f_num={row[0]}'>{h(row[0])}</a></td>"
        f"<td>{h(row[1])}</td><td>{h(row[2])}</td><td>{h(row[3])}, {h(row[4])}</td>"
        f"<td>{h(row[5])}</td><td>{h(row[6])}</td></tr>"
        for row in rows
    )
    return f"<h3>Union candidates</h3><table><thead><tr><th>F_NUM</th><th>Name</th><th>Affiliation</th><th>Location</th><th>Latest period</th><th>Matched EIN</th></tr></thead><tbody>{body}</tbody></table>"


def render_results(form, headers, rows) -> str:
    report = _LAST_REPORT if _LAST_KEY == _cache_key(form) and _LAST_REPORT is not None else _build_report(form)
    if "search_results" in report:
        return _render_search(report)
    if report.get("error"):
        return f'<div class="err"><b>{h(report["error"])}</b></div>'
    org = report["org"]
    ein = org.get("candidate_ein")
    ein_link = f"<a href='/query/nonprofit_deep_dive?ein={h(ein)}'>{h(ein)}</a>" if ein else "Unmatched"
    summary = f"""
      <div style="border:1px solid #d8dde6;padding:14px;border-radius:7px;margin:12px 0">
        <h2 style="margin-top:0">{h(org.get('display_name'))}</h2>
        <p><b>OLMS F_NUM:</b> {h(org.get('f_num'))} &nbsp; <b>Affiliation:</b> {h(org.get('affiliation'))}
        &nbsp; <b>Location:</b> {h(org.get('city'))}, {h(org.get('state'))}</p>
        <p><b>Status:</b> {'Terminated' if org.get('terminated') else 'Currently observed as active'} &nbsp;
        <b>Education scope:</b> {h(org.get('education_scope'))} ({h(org.get('education_scope_reason'))})</p>
        <p><b>Observed periods:</b> {h(org.get('first_period_end'))} to {h(org.get('latest_period_end'))} &nbsp;
        <b>Latest report received:</b> {h(org.get('latest_report_received'))}</p>
        <p><b>IRS match:</b> {ein_link} &nbsp; {h(org.get('match_status'))} {h(org.get('match_method'))}</p>
      </div>
    """
    trend_rows = "".join(
        f"<tr><td>{h(r[1])}</td><td>{h(r[2])}</td><td>{h(r[3])}</td><td>{_money(r[4])}</td>"
        f"<td>{_money(r[5])}</td><td>{_money(r[6])}</td><td>{_money(r[7])}</td><td>{_money(r[8])}</td></tr>"
        for r in report["trend_rows"]
    )
    history_rows = "".join(
        f"<tr><td>{h(r[0])} - {h(r[1])}</td><td>{h(r[2])}</td><td>{h(r[3])}</td><td>{h(r[4])}</td>"
        f"<td>{h(r[5])}</td><td>{h(r[6])}</td><td>{h(r[7])}</td><td>{h(r[8])}</td>"
        f"<td>{'Yes' if r[9] else 'No'}</td><td>{h(r[11])}</td></tr>"
        for r in report["history"]
    )
    missing_rows = "".join(
        f"<tr><td>{h(r[0])}</td><td>{h(r[1])}</td><td>{h(r[2])}</td><td>{h(r[3])}</td><td style='white-space:normal'>{h(r[4])}</td></tr>"
        for r in report["missing"]
    ) or "<tr><td colspan='5'>No additional expectation/gap result.</td></tr>"
    research = []
    for trend in report["trend_rows"][:10]:
        period = trend[1]
        grants = "; ".join(f"{name}: {_money(amount)}" for name, amount, _, _ in report["grants"].get(period, [])) or "None reported"
        vendors = "; ".join(f"{name}: {_money(amount)}" for name, amount, _, _ in report["vendors"].get(period, [])) or "None reported"
        officers = "; ".join(f"{name} ({title}): {_money(amount)}" for name, title, amount in report["officers"].get(period, [])) or "None reported"
        research.append(f"<tr><td>{h(period)}</td><td style='white-space:normal'>{h(grants)}</td><td style='white-space:normal'>{h(vendors)}</td><td style='white-space:normal'>{h(officers)}</td></tr>")
    data_as_of = report["missing"][0][5] if report["missing"] else "the latest loaded receive date"
    return summary + f"""
      <p class="note"><b>Based on OLMS data loaded as of {h(data_as_of)}.</b> Potential missing filings are conservative research flags, not legal conclusions.</p>
      <h3>Financial and membership trends</h3>
      <table><thead><tr><th>Period end</th><th>Form</th><th>Members</th><th>Receipts</th><th>Disbursements</th><th>Assets</th><th>Liabilities</th><th>Regular dues</th></tr></thead><tbody>{trend_rows}</tbody></table>
      <h3>Filing history</h3>
      <table><thead><tr><th>Period</th><th>Form</th><th>Original received</th><th>Due</th><th>Status</th><th>Days late</th><th>Amendments</th><th>Latest received</th><th>Hardship</th><th>RPT_ID</th></tr></thead><tbody>{history_rows}</tbody></table>
      <h3>Potential gaps / current expectation</h3>
      <table><thead><tr><th>Expected period end</th><th>Due</th><th>Status</th><th>Days overdue</th><th>Reason</th></tr></thead><tbody>{missing_rows}</tbody></table>
      <h3>Per-year research highlights (latest amendments)</h3>
      <table><thead><tr><th>Period</th><th>Top grants/contributions</th><th>Top vendors/payees</th><th>Top officers/employees</th></tr></thead><tbody>{''.join(research)}</tbody></table>
    """
