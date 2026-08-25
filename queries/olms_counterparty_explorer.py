from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from common import connect_olms_ro
from queries._links import query_url
from queries._olms_common import clean_ein, h, iter_query, preview_limit


META = {
    "key": "olms_counterparty_explorer",
    "name": "OLMS Grantee / Vendor Explorer",
    "description": "Find a reported counterparty and see every OLMS union that reported paying it.",
}
HEADERS = [
    "counterparty_id", "union_name", "f_num", "affiliation", "period_end",
    "category", "total_amount", "itemized_amount", "non_itemized_amount",
    "type_or_class", "rpt_id",
]
META["headers"] = HEADERS

_LAST_KEY = None
_LAST_REPORT = None


def render_fields(form) -> str:
    form = form or {}
    return f"""
    <div class="row" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <input type="hidden" name="counterparty_id" value="{h(form.get('counterparty_id',''))}">
      <div><label><b>Name or alias:</b></label><br><input name="name" value="{h(form.get('name',''))}" style="width:300px" placeholder="e.g. Chicago Urban League"></div>
      <div><label><b>City:</b></label><br><input name="city" value="{h(form.get('city',''))}"></div>
      <div><label><b>State:</b></label><br><input name="state" maxlength="2" value="{h(form.get('state',''))}" style="width:65px"></div>
      <div><label><b>Matched EIN:</b></label><br><input name="ein" value="{h(form.get('ein',''))}"></div>
      <div><button name="_action" value="search">Search counterparties</button></div>
    </div>
    """


def _key(form):
    form = form or {}
    return tuple(str(form.get(k, "")) for k in ("counterparty_id", "name", "city", "state", "ein", "_action"))


def _search(form) -> List[Tuple]:
    form = form or {}
    clauses = []
    params: List[object] = []
    name = (form.get("name") or "").strip().upper()
    if name:
        clauses.append(
            "(UPPER(cp.canonical_name) LIKE ? OR EXISTS (SELECT 1 FROM counterparty_aliases a "
            "WHERE a.counterparty_id=cp.counterparty_id AND UPPER(a.alias) LIKE ?))"
        )
        params.extend([f"%{name}%", f"%{name}%"])
    city = (form.get("city") or "").strip().upper()
    if city:
        clauses.append("UPPER(COALESCE(cp.city,'')) LIKE ?")
        params.append(f"%{city}%")
    state = (form.get("state") or "").strip().upper()
    if state:
        clauses.append("UPPER(COALESCE(cp.state,''))=?")
        params.append(state)
    ein = clean_ein(form.get("ein", ""))
    if ein:
        clauses.append("cp.matched_ein=?")
        params.append(ein)
    if not clauses:
        return []
    conn = connect_olms_ro()
    try:
        return conn.execute(
            f"""
            SELECT cp.counterparty_id,cp.canonical_name,cp.city,cp.state,cp.zip5,
                   cp.matched_ein,cp.identity_strength,cp.occurrence_count,
                   COALESCE(SUM(p.total_amount),0),COUNT(DISTINCT p.f_num),
                   MIN(p.period_end),MAX(p.period_end)
            FROM counterparties cp
            LEFT JOIN v_payment_payees p USING(counterparty_id)
            WHERE {' AND '.join(clauses)}
            GROUP BY cp.counterparty_id
            ORDER BY CASE WHEN UPPER(cp.canonical_name)=? THEN 0 ELSE 1 END,
                     SUM(p.total_amount) DESC,cp.canonical_name
            LIMIT 100
            """,
            params + [name],
        ).fetchall()
    finally:
        conn.close()


def _build_report(form, row_limit=None) -> Dict:
    form = form or {}
    cp_id = (form.get("counterparty_id") or "").strip()
    if form.get("_action") == "search" or not cp_id:
        return {"search_results": _search(form), "rows": []}
    conn = connect_olms_ro()
    try:
        cp = conn.execute("SELECT * FROM counterparties WHERE counterparty_id=?", (cp_id,)).fetchone()
        if not cp:
            return {"error": "Counterparty was not found.", "rows": []}
        columns = [item[0] for item in conn.execute("SELECT * FROM counterparties WHERE 1=0").description]
        party = dict(zip(columns, cp))
        aliases = conn.execute(
            "SELECT alias,city,state,zip5,occurrence_count FROM counterparty_aliases WHERE counterparty_id=? ORDER BY occurrence_count DESC,alias",
            (cp_id,),
        ).fetchall()
        totals = conn.execute(
            """
            SELECT COALESCE(SUM(total_amount),0),COUNT(DISTINCT f_num),MIN(period_end),MAX(period_end),
                   COALESCE(SUM(CASE WHEN disbursement_code=503 THEN total_amount ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN disbursement_code=502 THEN total_amount ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN disbursement_code=501 THEN total_amount ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN disbursement_code IN (504,505,506) THEN total_amount ELSE 0 END),0)
            FROM v_payment_payees WHERE counterparty_id=?
            """,
            (cp_id,),
        ).fetchone()
        payer_sql = """
            SELECT counterparty_id,union_name,f_num,affiliation,period_end,
                   disbursement_category,total_amount,itemized_amount,non_itemized_amount,
                   type_or_class,rpt_id
            FROM v_payment_payees WHERE counterparty_id=?
            ORDER BY period_end DESC,total_amount DESC,union_name
            """
        payer_params = [cp_id]
        if row_limit is not None:
            payer_sql += " LIMIT ?"
            payer_params.append(int(row_limit))
        payer_rows = conn.execute(payer_sql, payer_params).fetchall()
        transactions = conn.execute(
            """
            SELECT transaction_date,union_name,purpose,transaction_amount,disbursement_category
            FROM v_payment_transactions WHERE counterparty_id=?
            ORDER BY transaction_date DESC,transaction_amount DESC LIMIT 250
            """,
            (cp_id,),
        ).fetchall()
        return {"party": party, "aliases": aliases, "totals": totals, "rows": payer_rows, "transactions": transactions}
    finally:
        conn.close()


def run(form):
    global _LAST_KEY, _LAST_REPORT
    limit = preview_limit(form)
    report = _build_report(form, limit)
    _LAST_KEY, _LAST_REPORT = _key(form), report
    return HEADERS, report.get("rows", [])


def export_rows(form) -> Iterable[Tuple]:
    cp_id = ((form or {}).get("counterparty_id") or "").strip()
    if not cp_id:
        return []
    return iter_query(
        """
        SELECT counterparty_id,union_name,f_num,affiliation,period_end,
               disbursement_category,total_amount,itemized_amount,non_itemized_amount,
               type_or_class,rpt_id
        FROM v_payment_payees WHERE counterparty_id=?
        ORDER BY period_end DESC,total_amount DESC,union_name
        """,
        [cp_id],
    )


def _money(value) -> str:
    try:
        return f"${float(value or 0):,.0f}"
    except Exception:
        return h(value)


def render_results(form, headers, rows) -> str:
    report = _LAST_REPORT if _LAST_KEY == _key(form) and _LAST_REPORT is not None else _build_report(form, preview_limit(form))
    if report.get("error"):
        return f'<div class="err">{h(report["error"])}</div>'
    if "search_results" in report:
        search = report["search_results"]
        if not search:
            return '<div class="err">Enter search criteria; no counterparties matched the current search.</div>'
        body = "".join(
            f"<tr><td><a href='{h(query_url('olms_counterparty_explorer', counterparty_id=r[0]))}'>{h(r[1])}</a></td>"
            f"<td>{h(r[2])}, {h(r[3])} {h(r[4])}</td><td>{h(r[5])}</td><td>{h(r[6])}</td>"
            f"<td>{h(r[7])}</td><td>{_money(r[8])}</td><td>{h(r[9])}</td><td>{h(r[10])} - {h(r[11])}</td></tr>"
            for r in search
        )
        return f"<h3>Counterparty candidates</h3><table><thead><tr><th>Name</th><th>Location</th><th>Matched EIN</th><th>Identity strength</th><th>Occurrences</th><th>Reported total</th><th>Paying unions</th><th>Observed periods</th></tr></thead><tbody>{body}</tbody></table>"
    party, totals = report["party"], report["totals"]
    aliases = "; ".join(f"{a[0]} ({a[1]}, {a[2]} {a[3]})" for a in report["aliases"][:20])
    payer_body = "".join(
        f"<tr><td>{h(r[1])}</td><td>{h(r[2])}</td><td>{h(r[3])}</td><td>{h(r[4])}</td><td>{h(r[5])}</td>"
        f"<td>{_money(r[6])}</td><td>{_money(r[7])}</td><td>{_money(r[8])}</td><td>{h(r[9])}</td></tr>"
        for r in report["rows"]
    )
    tx_body = "".join(
        f"<tr><td>{h(r[0])}</td><td>{h(r[1])}</td><td style='white-space:normal'>{h(r[2])}</td><td>{_money(r[3])}</td><td>{h(r[4])}</td></tr>"
        for r in report["transactions"]
    ) or "<tr><td colspan='5'>No itemized transaction rows.</td></tr>"
    return f"""
      <h2>{h(party.get('canonical_name'))}</h2>
      <p><b>Counterparty ID:</b> {h(party.get('counterparty_id'))} &nbsp; <b>Location:</b> {h(party.get('city'))}, {h(party.get('state'))} {h(party.get('zip5'))}</p>
      <p><b>Identity strength:</b> {h(party.get('identity_strength'))} &nbsp; <b>Matched EIN:</b> {h(party.get('matched_ein')) or 'Unmatched'} {h(party.get('match_status'))}</p>
      <p><b>Aliases:</b> {h(aliases)}</p>
      <div class="stats-summary">
        <div class="stat-tile"><div class="stat-label">Total reported</div><div class="stat-value">{_money(totals[0])}</div></div>
        <div class="stat-tile"><div class="stat-label">Paying unions</div><div class="stat-value">{h(totals[1])}</div></div>
        <div class="stat-tile"><div class="stat-label">Grants / contributions</div><div class="stat-value">{_money(totals[4])}</div></div>
        <div class="stat-tile"><div class="stat-label">Political</div><div class="stat-value">{_money(totals[5])}</div></div>
        <div class="stat-tile"><div class="stat-label">Representational</div><div class="stat-value">{_money(totals[6])}</div></div>
        <div class="stat-tile"><div class="stat-label">Overhead / admin / other</div><div class="stat-value">{_money(totals[7])}</div></div>
      </div>
      <p class="note">Annual summary totals are not added to the itemized transactions below.</p>
      <h3>Paying unions - annual payee summary</h3>
      <table><thead><tr><th>Union</th><th>F_NUM</th><th>Affiliation</th><th>Period</th><th>Category</th><th>Total</th><th>Itemized</th><th>Non-itemized</th><th>Type/class</th></tr></thead><tbody>{payer_body}</tbody></table>
      <h3>Itemized transactions (up to 250)</h3>
      <table><thead><tr><th>Date</th><th>Union</th><th>Purpose</th><th>Amount</th><th>Category</th></tr></thead><tbody>{tx_body}</tbody></table>
    """
