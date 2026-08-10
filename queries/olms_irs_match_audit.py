from typing import Iterable, List, Tuple

from queries._olms_common import (
    h, iter_query, preview_limit, query_rows, scope_clause, scope_select, where_sql,
)


META = {
    "key": "olms_irs_match_audit",
    "name": "OLMS / IRS Match Audit",
    "description": "Review deterministic and manual OLMS F_NUM-to-IRS EIN matches and candidates.",
}
HEADERS = [
    "f_num", "olms_name", "olms_city", "olms_state", "olms_zip",
    "candidate_ein", "irs_name", "irs_city", "irs_state", "irs_zip",
    "method", "name_score", "address_score", "confidence", "status", "reason",
    "manual_override_status", "affiliation", "education_scope",
]
META["headers"] = HEADERS

STATUSES = ["", "MATCHED_HIGH_CONFIDENCE", "MATCHED_MANUAL", "CANDIDATE_REVIEW", "UNMATCHED", "REJECTED_MANUAL"]


def render_fields(form) -> str:
    form = form or {}
    selected = (form.get("status") or "").upper()
    options = "".join(
        f'<option value="{item}"{" selected" if item == selected else ""}>{h(item or "All statuses")}</option>'
        for item in STATUSES
    )
    return f"""
    <div class="row" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <div><label><b>Scope:</b></label><br>{scope_select(form)}</div>
      <div><label><b>Status:</b></label><br><select name="status">{options}</select></div>
      <div><label><b>Minimum confidence:</b></label><br><input type="number" min="0" max="1" step="0.01" name="min_confidence" value="{h(form.get('min_confidence',''))}"></div>
      <div><label><b>Affiliation:</b></label><br><input name="affiliation" value="{h(form.get('affiliation',''))}"></div>
      <div><label><b>State:</b></label><br><input maxlength="2" name="state" value="{h(form.get('state',''))}" style="width:65px"></div>
    </div>
    <p class="note">Durable decisions belong in <code>config/olms_irs_match_overrides.csv</code>; this page is read-only.</p>
    """


def _sql(form) -> Tuple[str, List[object]]:
    form = form or {}
    clauses: List[str] = []
    params: List[object] = []
    scope_sql, scope_params = scope_clause(form.get("scope", "education"), "o")
    if scope_sql:
        clauses.append(scope_sql)
        params.extend(scope_params)
    status = (form.get("status") or "").strip().upper()
    if status:
        clauses.append("m.match_status=?")
        params.append(status)
    try:
        confidence = float(form.get("min_confidence"))
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        clauses.append("COALESCE(m.confidence,0)>=?")
        params.append(confidence)
    affiliation = (form.get("affiliation") or "").strip().upper()
    if affiliation:
        clauses.append("UPPER(COALESCE(o.affiliation,''))=?")
        params.append(affiliation)
    state = (form.get("state") or "").strip().upper()
    if state:
        clauses.append("UPPER(COALESCE(o.state,''))=?")
        params.append(state)
    sql = f"""
      SELECT m.f_num,m.olms_name,m.olms_city,m.olms_state,m.olms_zip,
             m.candidate_ein,m.irs_name,m.irs_city,m.irs_state,m.irs_zip,
             m.match_method,m.name_score,m.address_score,m.confidence,m.match_status,
             m.reason,m.manual_override_status,o.affiliation,o.education_scope
      FROM irs_matches m JOIN organizations o USING(f_num)
      {where_sql(clauses)}
      ORDER BY CASE m.match_status WHEN 'CANDIDATE_REVIEW' THEN 0 WHEN 'UNMATCHED' THEN 1 ELSE 2 END,
               m.confidence DESC,o.display_name,m.candidate_ein
    """
    return sql, params


def run(form):
    sql, params = _sql(form)
    return HEADERS, query_rows(sql, params, preview_limit(form))


def export_rows(form) -> Iterable[Tuple]:
    sql, params = _sql(form)
    return iter_query(sql, params)
