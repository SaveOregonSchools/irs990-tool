from __future__ import annotations

from typing import Iterable, List, Tuple

from queries._olms_common import (
    add_like, fnums, h, iter_query, preview_limit, query_rows, scope_clause,
    scope_select, to_int, truthy, where_sql, year_filters,
)


META = {
    "key": "olms_filing_compliance",
    "name": "OLMS Filing Compliance / Timeliness",
    "description": "Observed late filings and conservative potential-missing annual LM filing flags, with explanations.",
}

HEADERS = [
    "f_num", "union_name", "affiliation", "state", "period_end", "form_type",
    "due_date", "initial_receive_date", "status", "days_late_or_overdue",
    "amendment_count", "hardship", "terminated", "latest_rpt_id", "result_kind",
    "reason", "data_as_of",
]
META["headers"] = HEADERS

STATUSES = [
    "", "FILED_ON_TIME", "FILED_LATE", "POTENTIAL_MISSING_FILING",
    "HARDSHIP_REVIEW", "ORIGINAL_NOT_OBSERVED", "FYE_CHANGED_REVIEW",
    "INSUFFICIENT_HISTORY", "TERMINATED",
]


def render_fields(form) -> str:
    form = form or {}
    status = (form.get("status") or "").upper()
    options = "".join(
        f'<option value="{h(item)}"{" selected" if item == status else ""}>{h(item or "All statuses")}</option>'
        for item in STATUSES
    )
    checked = "checked" if truthy(form, "include_terminated") else ""
    return f"""
    <div class="row" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <div><label><b>Scope:</b></label><br>{scope_select(form)}</div>
      <div><label><b>Status:</b></label><br><select name="status">{options}</select></div>
      <div><label><b>Affiliation:</b></label><br><input name="affiliation" value="{h(form.get('affiliation',''))}" placeholder="NEA"></div>
      <div><label><b>State:</b></label><br><input name="state" maxlength="2" value="{h(form.get('state',''))}" style="width:70px"></div>
      <div><label><b>Min days late/overdue:</b></label><br><input type="number" name="min_days" value="{h(form.get('min_days',''))}" style="width:120px"></div>
      <div><label><input type="checkbox" name="include_terminated" {checked}> Include terminated</label></div>
    </div>
    <div class="row" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <div><label><b>F_NUM(s):</b></label><br><input name="f_nums" value="{h(form.get('f_nums',''))}"></div>
      <div><label><b>Organization name:</b></label><br><input name="org_name" value="{h(form.get('org_name',''))}" style="width:300px"></div>
      <div><label><b>Period year from:</b></label><br><input type="number" name="min_year" value="{h(form.get('min_year',''))}" style="width:100px"></div>
      <div><label><b>to:</b></label><br><input type="number" name="max_year" value="{h(form.get('max_year',''))}" style="width:100px"></div>
    </div>
    <p class="note">Observed <b>FILED_LATE</b> results are distinct from <b>POTENTIAL_MISSING_FILING</b> research flags. Hardship rows received after the normal deadline require review.</p>
    """


def _sql(form) -> Tuple[str, List[object]]:
    form = form or {}
    clauses: List[str] = []
    params: List[object] = []
    scope_sql, scope_params = scope_clause(form.get("scope", "education"), "o")
    if scope_sql:
        clauses.append(scope_sql)
        params.extend(scope_params)
    affiliation = (form.get("affiliation") or "").strip()
    if affiliation:
        clauses.append("UPPER(COALESCE(o.affiliation,''))=?")
        params.append(affiliation.upper())
    state = (form.get("state") or "").strip().upper()
    if state:
        clauses.append("UPPER(COALESCE(o.state,''))=?")
        params.append(state)
    numbers = fnums(form.get("f_nums", ""))
    if numbers:
        clauses.append("c.f_num IN (" + ",".join("?" for _ in numbers) + ")")
        params.extend(numbers)
    add_like(clauses, params, "o.display_name", form.get("org_name", ""))
    year_filters(clauses, params, "c.period_end", form.get("min_year"), form.get("max_year"))
    status = (form.get("status") or "").strip().upper()
    if status in STATUSES and status:
        clauses.append("c.status=?")
        params.append(status)
    min_days = to_int(form.get("min_days"))
    if min_days is not None:
        clauses.append("COALESCE(c.days_late_or_overdue,0)>=?")
        params.append(min_days)
    if not truthy(form, "include_terminated"):
        clauses.append("o.terminated=0 AND c.status<>'TERMINATED'")
    sql = f"""
      SELECT c.f_num,o.display_name,o.affiliation,o.state,c.period_end,c.form_type,
             c.due_date,c.initial_receive_date,c.status,c.days_late_or_overdue,
             c.amendment_count,c.hardship,c.terminated,c.latest_rpt_id,c.result_kind,
             c.reason,c.data_as_of
      FROM compliance_results c JOIN organizations o USING(f_num)
      {where_sql(clauses)}
      ORDER BY CASE c.status WHEN 'POTENTIAL_MISSING_FILING' THEN 0 WHEN 'FILED_LATE' THEN 1 ELSE 2 END,
               c.days_late_or_overdue DESC,c.period_end DESC,o.display_name
    """
    return sql, params


def run(form):
    sql, params = _sql(form)
    return HEADERS, query_rows(sql, params, preview_limit(form))


def export_rows(form) -> Iterable[Tuple]:
    sql, params = _sql(form)
    return iter_query(sql, params)
