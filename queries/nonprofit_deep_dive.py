import html
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from common import connect_ro, normalize_eins
from queries import ngo_core_data, ngo_grants_in


META = {
    "key": "nonprofit_deep_dive",
    "name": "Nonprofit Deep Dive",
    "description": (
        "Single-EIN profile with financial trend charts, year-by-year filing summaries, "
        "top grantors, and officer/key employee compensation."
    ),
}


HEADERS = [
    "ein",
    "org_name",
    "tax_year",
    "return_type",
    "period_end",
    "total_revenue",
    "total_expenses",
    "revenue_less_expenses",
    "net_assets_eoy",
    "grants_paid",
    "government_grants",
    "lobbying_expense",
    "lobbying_pct_expenses",
    "top_grantors",
]
META["headers"] = HEADERS


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


def _bar_pct(part, whole) -> float:
    whole_n = _num(whole)
    if not whole_n:
        return 0.0
    return max(0.0, min(100.0, 100.0 * _num(part) / whole_n))


def _clean_ein_text(ein: str) -> str:
    return "".join(ch for ch in (ein or "") if ch.isdigit())


def render_fields(form) -> str:
    val = (form or {}).get("ein", "") or (form or {}).get("ein_list", "")
    return f"""
    <div class="row">
      <label for="ein"><b>EIN:</b></label><br>
      <input id="ein" name="ein" value="{_h(val)}" placeholder="e.g. 12-3456789" style="width:220px;">
    </div>
    """


def _parse_ein(form) -> Optional[str]:
    f = form or {}
    values = normalize_eins(f.get("ein", "") or f.get("ein_list", ""))
    if len(values) != 1:
        return None
    return values[0]


def _object_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table','view') LIMIT 1",
        [name],
    ).fetchone()
    return bool(row)


def _core_years(ein: str) -> List[Dict]:
    headers, rows = ngo_core_data.run({"ein_list": ein})
    out = []
    for row in rows:
        item = dict(zip(headers, row))
        item["lobbying_pct_expenses"] = (
            100.0 * _num(item.get("lobbying_expense")) / _num(item.get("total_expenses"))
            if _num(item.get("total_expenses")) else None
        )
        out.append(item)
    out.sort(key=lambda r: int(r.get("tax_year") or 0), reverse=True)
    return out


def _top_grantors_by_year(conn, ein: str) -> Dict[int, List[Dict]]:
    try:
        return _top_grantors_from_enhanced_grants(ein)
    except Exception:
        return _top_grantors_from_legacy_grants(conn, ein)


def _top_grantors_from_enhanced_grants(ein: str) -> Dict[int, List[Dict]]:
    headers, rows = ngo_grants_in.run({"ein_list": ein, "use_resolved_grants": "on"})
    idx = {name: pos for pos, name in enumerate(headers)}
    required = {"tax_year", "grantor_ein", "grantor_org_name", "total_amount"}
    if not required.issubset(idx):
        raise RuntimeError("Enhanced grants query did not return the expected columns.")

    totals: Dict[Tuple[int, str, str], Dict] = {}
    for row in rows:
        tax_year = row[idx["tax_year"]]
        if tax_year is None:
            continue
        amount = _num(row[idx["total_amount"]])
        if amount == 0:
            continue
        key = (
            int(tax_year),
            row[idx["grantor_ein"]] or "",
            row[idx["grantor_org_name"]] or "Unknown grantor",
        )
        item = totals.setdefault(
            key,
            {
                "tax_year": int(tax_year),
                "grantor_ein": key[1],
                "grantor_name": key[2],
                "amount": 0.0,
                "grant_count": 0,
            },
        )
        item["amount"] += amount
        item["grant_count"] += 1

    return _top_five_by_year(totals.values())


def _top_grantors_from_legacy_grants(conn, ein: str) -> Dict[int, List[Dict]]:
    if not (
        _object_exists(conn, "grants_compat_v1")
        and _object_exists(conn, "canonical_by_ein_year")
        and _object_exists(conn, "returns")
    ):
        return defaultdict(list)

    sql = """
    SELECT
      c.tax_year,
      r.ein AS grantor_ein,
      r.org_name AS grantor_name,
      SUM(COALESCE(g.cash_amount, 0) + COALESCE(g.noncash_amount, 0)) AS amount,
      COUNT(*) AS grant_count
    FROM grants_compat_v1 g
    JOIN canonical_by_ein_year c ON c.filing_id = g.filing_id
    JOIN returns r ON r.filing_id = c.filing_id
    WHERE g.recipient_ein = ?
      AND (COALESCE(g.cash_amount, 0) + COALESCE(g.noncash_amount, 0)) <> 0
    GROUP BY c.tax_year, r.ein, r.org_name
    ORDER BY c.tax_year DESC, amount DESC, grantor_name
    """
    items = [
        {
            "tax_year": int(tax_year),
            "grantor_ein": grantor_ein,
            "grantor_name": grantor_name,
            "amount": amount,
            "grant_count": grant_count,
        }
        for tax_year, grantor_ein, grantor_name, amount, grant_count in conn.execute(sql, [ein])
        if tax_year is not None
    ]
    return _top_five_by_year(items)


def _top_five_by_year(items: Iterable[Dict]) -> Dict[int, List[Dict]]:
    grouped: Dict[int, List[Dict]] = defaultdict(list)
    sorted_items = sorted(
        items,
        key=lambda item: (
            -int(item.get("tax_year") or 0),
            -_num(item.get("amount")),
            str(item.get("grantor_name") or ""),
        ),
    )
    for item in sorted_items:
        tax_year = int(item.get("tax_year") or 0)
        if not tax_year:
            continue
        bucket = grouped[tax_year]
        if len(bucket) < 5:
            bucket.append(item)
    return grouped


def _compensation_by_year(conn, ein: str) -> Dict[int, List[Dict]]:
    sql = """
    SELECT
      c.tax_year,
      o.person_name,
      o.title_txt,
      o.comp_from_org,
      o.comp_from_related,
      o.other_compensation
    FROM officers o
    JOIN canonical_by_ein_year c ON c.filing_id = o.filing_id
    WHERE c.ein = ?
    ORDER BY c.tax_year DESC,
      (COALESCE(o.comp_from_org,0) + COALESCE(o.comp_from_related,0) + COALESCE(o.other_compensation,0)) DESC,
      o.person_name
    """
    grouped: Dict[int, List[Dict]] = defaultdict(list)
    for tax_year, name, title, org_comp, related_comp, other_comp in conn.execute(sql, [ein]):
        if tax_year is None or not name:
            continue
        bucket = grouped[int(tax_year)]
        if len(bucket) < 10:
            bucket.append({
                "person_name": name,
                "title": title,
                "comp_from_org": org_comp,
                "comp_from_related": related_comp,
                "other_compensation": other_comp,
            })
    return grouped


def _top_grantors_text(grantors: List[Dict]) -> str:
    bits = []
    for g in grantors:
        name = g.get("grantor_name") or g.get("grantor_ein") or "Unknown"
        bits.append(f"{name}: {_money(g.get('amount'))}")
    return "; ".join(bits)


def _rows_from_report(report: Dict) -> List[Tuple]:
    rows = []
    for y in report.get("years", []):
        rows.append((
            y.get("ein"),
            y.get("org_name"),
            y.get("tax_year"),
            y.get("return_type"),
            y.get("period_end"),
            y.get("total_revenue"),
            y.get("total_expenses"),
            y.get("revenue_less_expenses"),
            y.get("net_assets_eoy"),
            y.get("grants_paid"),
            y.get("government_grants"),
            y.get("lobbying_expense"),
            y.get("lobbying_pct_expenses"),
            _top_grantors_text(y.get("top_grantors") or []),
        ))
    return rows


def _build_report(form) -> Dict:
    ein = _parse_ein(form)
    if not ein:
        return {"error": "Enter exactly one valid 9-digit EIN.", "years": []}

    years = _core_years(ein)
    if not years:
        return {"error": f"No canonical filings found for EIN {ein}.", "years": []}

    conn = connect_ro()
    top_grantors = _top_grantors_by_year(conn, ein)
    compensation = _compensation_by_year(conn, ein)

    for y in years:
        tax_year = int(y.get("tax_year") or 0)
        y["top_grantors"] = top_grantors.get(tax_year, [])
        y["compensation"] = compensation.get(tax_year, [])

    return {
        "ein": ein,
        "org_name": years[0].get("org_name"),
        "years": years,
    }


def _cache_key(form) -> str:
    return _clean_ein_text((form or {}).get("ein", "") or (form or {}).get("ein_list", ""))


def run(form):
    global _LAST_KEY, _LAST_REPORT
    key = _cache_key(form)
    report = _build_report(form)
    _LAST_KEY = key
    _LAST_REPORT = report
    return HEADERS, _rows_from_report(report)


def export_rows(form) -> Iterable[Tuple]:
    return _rows_from_report(_build_report(form))


def _series_svg(title: str, years: List[Dict], series: List[Tuple[str, str, str]]) -> str:
    data = list(reversed(years))
    if not data:
        return ""
    width, height = 760, 260
    left, right, top, bottom = 64, 18, 34, 42
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_val = max([_num(row.get(key)) for _, key, _ in series for row in data] + [1.0])

    def x(idx):
        return left + (plot_w * idx / max(1, len(data) - 1))

    def y(value):
        return top + plot_h - (plot_h * _num(value) / max_val)

    lines = [
        f'<div class="deep-chart"><h3>{_h(title)}</h3>',
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_h(title)}">',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" y2="{top + plot_h}" class="axis"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" class="axis"/>',
    ]
    for idx, row in enumerate(data):
        tx = x(idx)
        lines.append(f'<text x="{tx}" y="{height - 14}" text-anchor="middle" class="tick">{_h(row.get("tax_year"))}</text>')
    for label, key, color in series:
        pts = " ".join(f'{x(i):.1f},{y(row.get(key)):.1f}' for i, row in enumerate(data))
        lines.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="3"/>')
        for i, row in enumerate(data):
            lines.append(f'<circle cx="{x(i):.1f}" cy="{y(row.get(key)):.1f}" r="3.5" fill="{color}"><title>{_h(label)} {row.get("tax_year")}: {_money(row.get(key))}</title></circle>')
    lines.append("</svg><div class=\"legend\">")
    for label, _, color in series:
        lines.append(f'<span><i style="background:{color}"></i>{_h(label)}</span>')
    lines.append("</div></div>")
    return "".join(lines)


def _lobbying_svg(years: List[Dict]) -> str:
    data = list(reversed(years))
    if not data:
        return ""
    width, height = 760, 260
    left, right, top, bottom = 64, 34, 34, 42
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_amt = max([_num(row.get("lobbying_expense")) for row in data] + [1.0])
    max_pct = max([_num(row.get("lobbying_pct_expenses")) for row in data] + [1.0])
    bar_w = max(12, plot_w / max(1, len(data)) * 0.42)

    def x(idx):
        return left + (plot_w * (idx + 0.5) / max(1, len(data)))

    def y_amt(value):
        return top + plot_h - (plot_h * _num(value) / max_amt)

    def y_pct(value):
        return top + plot_h - (plot_h * _num(value) / max_pct)

    lines = [
        '<div class="deep-chart"><h3>Lobbying Expenses</h3>',
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Lobbying expenses and percent of expenses">',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" y2="{top + plot_h}" class="axis"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" class="axis"/>',
    ]
    for idx, row in enumerate(data):
        bx = x(idx) - bar_w / 2
        by = y_amt(row.get("lobbying_expense"))
        bh = top + plot_h - by
        lines.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{max(0, bh):.1f}" fill="#8ea6b4"><title>{row.get("tax_year")}: {_money(row.get("lobbying_expense"))}</title></rect>')
        lines.append(f'<text x="{x(idx):.1f}" y="{height - 14}" text-anchor="middle" class="tick">{_h(row.get("tax_year"))}</text>')
    pts = " ".join(f'{x(i):.1f},{y_pct(row.get("lobbying_pct_expenses")):.1f}' for i, row in enumerate(data))
    lines.append(f'<polyline points="{pts}" fill="none" stroke="#b45339" stroke-width="3"/>')
    for i, row in enumerate(data):
        pct = row.get("lobbying_pct_expenses")
        lines.append(f'<circle cx="{x(i):.1f}" cy="{y_pct(pct):.1f}" r="3.5" fill="#b45339"><title>{row.get("tax_year")}: {pct or 0:.2f}% of expenses</title></circle>')
    lines.append('</svg><div class="legend"><span><i style="background:#8ea6b4"></i>Lobbying amount</span><span><i style="background:#b45339"></i>% of expenses</span></div></div>')
    return "".join(lines)


def _metric(label: str, value) -> str:
    return f'<div class="metric"><span>{_h(label)}</span><strong>{_money(value)}</strong></div>'


def _ratio_row(label: str, value, total) -> str:
    pct = _bar_pct(value, total)
    return f"""
    <tr>
      <td>{_h(label)}</td>
      <td class="num">{_money(value)}</td>
      <td><span class="mini-bar"><i style="width:{pct:.1f}%"></i></span> {_h(_pct(value, total))}</td>
    </tr>
    """


def _simple_row(label: str, value) -> str:
    return f'<tr><td>{_h(label)}</td><td class="num">{_money(value)}</td></tr>'


def _grantor_rows(items: List[Dict]) -> str:
    if not items:
        return '<tr><td colspan="4" class="muted">No enhanced grantor matches found for this year.</td></tr>'
    rows = []
    for g in items:
        rows.append(
            f'<tr><td>{_h(g.get("grantor_name") or "")}</td><td>{_h(g.get("grantor_ein") or "")}</td>'
            f'<td class="num">{_money(g.get("amount"))}</td><td class="num">{_h(g.get("grant_count"))}</td></tr>'
        )
    return "".join(rows)


def _comp_rows(items: List[Dict]) -> str:
    if not items:
        return '<tr><td colspan="4" class="muted">No officer compensation rows found for this year.</td></tr>'
    rows = []
    for p in items:
        name = p.get("person_name") or ""
        title = p.get("title") or ""
        rows.append(
            f'<tr><td>{_h(name)}{(" (" + _h(title) + ")") if title else ""}</td>'
            f'<td class="num">{_money(p.get("comp_from_org"))}</td>'
            f'<td class="num">{_money(p.get("comp_from_related"))}</td>'
            f'<td class="num">{_money(p.get("other_compensation"))}</td></tr>'
        )
    return "".join(rows)


def _year_card(row: Dict) -> str:
    revenue = row.get("total_revenue")
    expenses = row.get("total_expenses")
    return f"""
    <section class="deep-year">
      <div class="year-label">Fiscal Year Ending<br><strong>{_h(row.get("period_end") or row.get("tax_year"))}</strong></div>
      <div class="year-card">
        <h3>Revenue <span>{_money(revenue)}</span></h3>
        <div class="metrics">
          {_metric("Expenses", expenses)}
          {_metric("Net Income", row.get("revenue_less_expenses"))}
          {_metric("Net Assets", row.get("net_assets_eoy"))}
        </div>
        <div class="summary-grid">
          <div>
            <h4>Notable Sources of Revenue</h4>
            <table>
              {_ratio_row("Contributions and Grants", row.get("contributions_and_grants"), revenue)}
              {_ratio_row("Government Grants", row.get("government_grants"), revenue)}
              {_ratio_row("Program Services", row.get("program_service_revenue"), revenue)}
              {_ratio_row("Membership Dues", row.get("membership_dues"), revenue)}
              {_ratio_row("Investment Income", row.get("investment_income"), revenue)}
              {_ratio_row("Other Revenue", row.get("other_revenue"), revenue)}
            </table>
          </div>
          <div>
            <h4>Notable Expenses</h4>
            <table>
              {_ratio_row("Grants Paid", row.get("grants_paid"), expenses)}
              {_ratio_row("Salaries and Benefits", row.get("salaries_comp_emp_benefits"), expenses)}
              {_ratio_row("Fundraising Expenses", row.get("total_fundraising_expenses"), expenses)}
              {_ratio_row("Professional Fundraising Fees", row.get("professional_fundraising_fees"), expenses)}
              {_ratio_row("Lobbying Expense", row.get("lobbying_expense"), expenses)}
              {_ratio_row("Other Expenses", row.get("other_expenses"), expenses)}
            </table>
          </div>
        </div>
        <h4>Assets/Debt</h4>
        <table>
          {_simple_row("Total Assets", row.get("total_assets_eoy"))}
          {_simple_row("Total Liabilities", row.get("total_liabilities_eoy"))}
          {_simple_row("Net Assets", row.get("net_assets_eoy"))}
        </table>
        <h4>Top Grantors</h4>
        <table>
          <thead><tr><th>Grantor</th><th>EIN</th><th>Amount</th><th>Grant Rows</th></tr></thead>
          <tbody>{_grantor_rows(row.get("top_grantors") or [])}</tbody>
        </table>
        <h4>Compensation</h4>
        <table>
          <thead><tr><th>Key Employees and Officers</th><th>Compensation</th><th>Related</th><th>Other</th></tr></thead>
          <tbody>{_comp_rows(row.get("compensation") or [])}</tbody>
        </table>
      </div>
    </section>
    """


def render_results(form, headers, rows) -> str:
    key = _cache_key(form)
    report = _LAST_REPORT if key == _LAST_KEY and _LAST_REPORT is not None else _build_report(form)
    if report.get("error"):
        return f'<div class="err"><b>{_h(report["error"])}</b></div>'

    years = report.get("years") or []
    charts = [
        _series_svg("Revenue vs Expenses", years, [
            ("Revenue", "total_revenue", "#2d6cdf"),
            ("Expenses", "total_expenses", "#b45339"),
        ]),
        _series_svg("Grants Paid vs Government Grants", years, [
            ("Grants Paid", "grants_paid", "#2f855a"),
            ("Government Grants", "government_grants", "#805ad5"),
        ]),
        _lobbying_svg(years),
    ]
    cards = "".join(_year_card(row) for row in years)
    return f"""
    <style>
      .deep-dive h2 {{ margin-bottom: 4px; }}
      .deep-subtitle {{ color: #666; margin-top: 0; }}
      .deep-charts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 14px; margin: 18px 0; }}
      .deep-chart {{ border: 1px solid #ddd; background: #fff; padding: 12px; }}
      .deep-chart h3 {{ margin: 0 0 8px 0; }}
      .deep-chart svg {{ width: 100%; height: auto; }}
      .axis {{ stroke: #d0d0d0; }}
      .tick {{ font-size: 12px; fill: #555; }}
      .legend {{ display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: #444; }}
      .legend i {{ display: inline-block; width: 12px; height: 12px; margin-right: 5px; vertical-align: -2px; }}
      .deep-year {{ display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 24px; margin: 18px 0; }}
      .year-label {{ color: #666; font-size: 13px; }}
      .year-label strong {{ display: block; color: #1f2933; font-size: 24px; margin-top: 4px; }}
      .year-card {{ border: 1px solid #ddd; background: #fff; padding: 18px; }}
      .year-card h3 {{ display: flex; justify-content: space-between; gap: 12px; margin: 0 0 14px; font-size: 22px; }}
      .year-card h4 {{ margin: 18px 0 6px; border-top: 1px solid #ddd; padding-top: 10px; }}
      .metrics {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; border-top: 1px solid #ddd; border-bottom: 1px solid #ddd; padding: 12px 0; }}
      .metric span {{ display: block; color: #555; font-size: 12px; }}
      .metric strong {{ font-size: 17px; }}
      .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 18px; }}
      .year-card table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
      .year-card th {{ text-align: left; border-bottom: 1px solid #ddd; padding: 6px 4px; }}
      .year-card td {{ padding: 6px 4px; border: 0; max-width: none; overflow: visible; text-overflow: clip; white-space: normal; }}
      .year-card tbody tr:nth-child(odd), .year-card table > tr:nth-child(odd) {{ background: #f4f4f4; }}
      .num {{ text-align: right; white-space: nowrap; }}
      .mini-bar {{ display: inline-block; width: 90px; height: 12px; background: #edf1f3; vertical-align: -2px; margin-right: 4px; }}
      .mini-bar i {{ display: block; height: 12px; background: #9aabb5; }}
      .muted {{ color: #777; }}
      @media (max-width: 760px) {{
        .deep-year {{ grid-template-columns: 1fr; gap: 8px; }}
        .metrics {{ grid-template-columns: 1fr; }}
      }}
    </style>
    <div class="deep-dive">
      <h2>{_h(report.get("org_name"))}</h2>
      <p class="deep-subtitle">EIN {_h(report.get("ein"))}</p>
      <div class="deep-charts">{''.join(charts)}</div>
      {cards}
    </div>
    """
