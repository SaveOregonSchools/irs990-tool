from typing import Iterable, List, Tuple

from common import connect_olms_ro, current_olms_db_path
from queries._olms_common import h, iter_query, preview_limit, query_rows


META = {
    "key": "olms_import_audit",
    "name": "OLMS Import / Data Quality Audit",
    "description": "Cached sidecar coverage, source hashes, repairs, quarantines, duplicates, and orphan detail counts.",
}
HEADERS = [
    "import_run_id", "build_status", "started_at", "completed_at", "source_year",
    "logical_table", "data_filename", "data_sha256", "schema_hash",
    "rows_attempted", "rows_loaded", "rows_repaired", "rows_quarantined",
    "source_status",
]
META["headers"] = HEADERS


def render_fields(form) -> str:
    form = form or {}
    return f"""
    <div class="row" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <div><label><b>Source year:</b></label><br><input type="number" name="year" value="{h(form.get('year',''))}" style="width:100px"></div>
      <div><label><b>Logical table contains:</b></label><br><input name="table" value="{h(form.get('table',''))}"></div>
    </div>
    <p class="note">Full raw repaired and quarantined records are exported by the build command under <code>exports/</code>.</p>
    """


def _sql(form) -> Tuple[str, List[object]]:
    form = form or {}
    clauses = ["r.import_run_id=(SELECT MAX(import_run_id) FROM import_runs)"]
    params: List[object] = []
    try:
        year = int(form.get("year"))
    except (TypeError, ValueError):
        year = None
    if year:
        clauses.append("s.source_year=?")
        params.append(year)
    table = (form.get("table") or "").strip().upper()
    if table:
        clauses.append("UPPER(s.logical_table) LIKE ?")
        params.append(f"%{table}%")
    sql = f"""
      SELECT r.import_run_id,r.status,r.started_at,r.completed_at,s.source_year,
             s.logical_table,s.data_filename,s.data_sha256,s.schema_hash,
             s.rows_attempted,s.rows_loaded,s.rows_repaired,s.rows_quarantined,s.status
      FROM import_runs r JOIN import_sources s USING(import_run_id)
      WHERE {' AND '.join(clauses)}
      ORDER BY s.source_year DESC,s.logical_table
    """
    return sql, params


def run(form):
    sql, params = _sql(form)
    return HEADERS, query_rows(sql, params, preview_limit(form))


def export_rows(form) -> Iterable[Tuple]:
    sql, params = _sql(form)
    return iter_query(sql, params)


def render_results(form, headers, rows) -> str:
    conn = connect_olms_ro()
    try:
        stats = conn.execute(
            "SELECT metric,bucket,value,notes FROM olms_stats_cache ORDER BY metric,bucket"
        ).fetchall()
        latest = conn.execute(
            """
            SELECT status,completed_at,rows_attempted,rows_loaded,rows_repaired,
                   rows_quarantined,duplicate_rows,conflicting_duplicates,orphan_rows
            FROM import_runs ORDER BY import_run_id DESC LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    tiles = [
        (metric + (f" - {bucket}" if bucket else ""), value if value is not None else bucket)
        for metric, bucket, value, _ in stats
        if metric in {
            "years_loaded", "unique_labor_organizations", "total_reports", "likely_education",
            "irs_high_confidence_matches", "irs_unmatched", "counterparty_identities",
            "potential_missing_filings", "historically_late_filings", "repaired_records",
            "quarantined_records", "orphan_detail_rows", "duplicate_conflicts", "data_as_of",
        }
    ]
    tile_html = "".join(
        f'<div class="stat-tile"><div class="stat-label">{h(label.replace("_", " ").title())}</div><div class="stat-value">{h(value)}</div></div>'
        for label, value in tiles
    )
    run_html = ""
    if latest:
        run_html = (
            f"<p><b>Latest build:</b> {h(latest[0])} at {h(latest[1])}. "
            f"Attempted {h(latest[2])}; loaded {h(latest[3])}; repaired {h(latest[4])}; "
            f"quarantined {h(latest[5])}; identical duplicates {h(latest[6])}; "
            f"conflicting duplicate rows {h(latest[7])}; orphan rows {h(latest[8])}.</p>"
        )
    body = "".join(
        "<tr>" + "".join(f"<td title='{h(value)}'>{h(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    headings = "".join(f"<th>{h(value)}</th>" for value in headers)
    return f"""
      <p><b>OLMS database:</b> <code>{h(current_olms_db_path())}</code></p>
      {run_html}
      <div class="stats-summary">{tile_html}</div>
      <h3>Latest import sources</h3>
      <div style="overflow:auto;max-height:60vh"><table><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></div>
    """
