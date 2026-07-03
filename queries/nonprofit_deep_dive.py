import html
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from common import connect_ro, normalize_eins
from queries import ngo_core_data, ngo_grants_in, ngo_grants_out


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
    "top_grantees",
]
META["headers"] = HEADERS

HIDE_PREVIEW_LIMIT = True
HIDE_CSV_EXPORT = True
DISABLE_ROW_LIMIT = True
PDF_EXPORT = True
RUN_BUTTON_LABEL = "Open EIN"


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
    f = form or {}
    val = f.get("ein", "") or f.get("ein_list", "")
    org_search = f.get("org_search", "")
    return f"""
    <div class="row deep-search-fields" style="display:flex; gap:16px; flex-wrap:wrap; align-items:flex-end;">
      <div>
        <label for="ein"><b>Known EIN:</b></label><br>
        <input id="ein" name="ein" value="{_h(val)}" placeholder="e.g. 12-3456789" style="width:220px;">
      </div>
      <div>
        <label for="org_search"><b>Find EIN by organization name:</b></label><br>
        <input id="org_search" name="org_search" value="{_h(org_search)}" placeholder="e.g. Oregon Community Foundation" style="width:min(520px, 100%);">
      </div>
      <div>
        <button type="submit" name="_action" value="search_org">Search Name</button>
      </div>
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


def _index_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type = 'index' LIMIT 1",
        [name],
    ).fetchone()
    return bool(row)


def _search_terms(value: str) -> List[str]:
    raw = "".join(ch.upper() if ch.isalnum() else " " for ch in (value or ""))
    terms = []
    for token in raw.split():
        if len(token) < 2:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:6]


def _org_name_search(form) -> str:
    return (form or {}).get("org_search", "").strip()


def _name_relevance(org_name: str, phrase: str) -> int:
    name = (org_name or "").strip().casefold()
    phrase = (phrase or "").strip().casefold()
    if not name or not phrase:
        return 0
    if name == phrase:
        return 100
    if name.startswith(phrase):
        return 90
    if name.startswith("the " + phrase):
        return 85
    if phrase in name:
        return 75
    return 50


def _latest_canonical_by_ein(conn, eins: List[str]) -> Dict[str, Tuple]:
    clean = [ein for ein in dict.fromkeys(eins) if ein]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    sql = f"""
    SELECT ein, tax_year, return_type
    FROM (
      SELECT
        ein,
        tax_year,
        return_type,
        ROW_NUMBER() OVER (
          PARTITION BY ein
          ORDER BY tax_year DESC, filing_id DESC
        ) AS rn
      FROM canonical_by_ein_year
      WHERE ein IN ({placeholders})
    )
    WHERE rn = 1
    """
    return {ein: (tax_year, return_type) for ein, tax_year, return_type in conn.execute(sql, clean)}


def _search_orgs_with_clause(conn, phrase: str, where_sql: str, where_params: List[str], limit: int, use_name_index: bool = False) -> List[Dict]:
    fetch_limit = max(limit * 20, 250)
    returns_source = "returns AS r"
    if use_name_index and _index_exists(conn, "idx_returns_org_name_nocase"):
        returns_source = "returns AS r INDEXED BY idx_returns_org_name_nocase"
    sql = f"""
    SELECT
      r.filing_id,
      r.ein,
      r.org_name,
      r.city,
      r.state
    FROM {returns_source}
    WHERE {where_sql}
    LIMIT ?
    """
    rows = conn.execute(sql, where_params + [fetch_limit]).fetchall()
    latest = _latest_canonical_by_ein(conn, [row[1] for row in rows])
    best: Dict[str, Dict] = {}
    for _filing_id, ein, org_name, city, state in rows:
        if ein not in latest:
            continue
        tax_year, return_type = latest[ein]
        item = {
            "ein": ein,
            "org_name": org_name,
            "city": city,
            "state": state,
            "tax_year": tax_year,
            "return_type": return_type,
            "relevance": _name_relevance(org_name, phrase),
        }
        existing = best.get(ein)
        if not existing:
            best[ein] = item
            continue
        if (
            item["relevance"] > existing["relevance"]
            or (
                item["relevance"] == existing["relevance"]
                and int(item.get("tax_year") or 0) > int(existing.get("tax_year") or 0)
            )
        ):
            best[ein] = item

    out = []
    for item in sorted(
        best.values(),
        key=lambda row: (
            -int(row.get("relevance") or 0),
            -int(row.get("tax_year") or 0),
            str(row.get("org_name") or ""),
            str(row.get("ein") or ""),
        ),
    ):
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _search_orgs_by_name(name: str, limit: int = 25) -> List[Dict]:
    phrase = " ".join(_search_terms(name))
    if not phrase:
        return []

    conn = connect_ro()
    if not (_object_exists(conn, "returns") and _object_exists(conn, "canonical_by_ein_year")):
        return []

    prefix_results = _search_orgs_with_clause(
        conn,
        phrase,
        "(r.org_name LIKE ? OR r.org_name LIKE ?)",
        [f"{phrase}%", f"The {phrase}%"],
        limit,
        use_name_index=True,
    )
    if prefix_results:
        return prefix_results

    return _search_orgs_with_clause(
        conn,
        phrase,
        "r.org_name LIKE ?",
        [f"%{phrase}%"],
        limit,
    )


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


def _top_grantees_by_year(conn, ein: str) -> Dict[int, List[Dict]]:
    try:
        return _top_grantees_from_enhanced_grants(ein)
    except Exception:
        return _top_grantees_from_legacy_grants(conn, ein)


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


def _top_grantees_from_enhanced_grants(ein: str) -> Dict[int, List[Dict]]:
    headers, rows = ngo_grants_out.run({"ein_list": ein, "use_resolved_grants": "on"})
    idx = {name: pos for pos, name in enumerate(headers)}
    required = {"tax_year", "recipient_ein", "recipient_name", "total_amount"}
    if not required.issubset(idx):
        raise RuntimeError("Enhanced grants-paid query did not return the expected columns.")

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
            row[idx["recipient_ein"]] or "",
            row[idx["recipient_name"]] or "Unknown grantee",
        )
        item = totals.setdefault(
            key,
            {
                "tax_year": int(tax_year),
                "grantee_ein": key[1],
                "grantee_name": key[2],
                "amount": 0.0,
                "grant_count": 0,
            },
        )
        item["amount"] += amount
        item["grant_count"] += 1

    return _top_five_by_year(totals.values())


def _top_grantees_from_legacy_grants(conn, ein: str) -> Dict[int, List[Dict]]:
    if not (
        _object_exists(conn, "grants_compat_v1")
        and _object_exists(conn, "canonical_by_ein_year")
    ):
        return defaultdict(list)

    sql = """
    SELECT
      c.tax_year,
      g.recipient_ein AS grantee_ein,
      g.recipient_name AS grantee_name,
      SUM(COALESCE(g.cash_amount, 0) + COALESCE(g.noncash_amount, 0)) AS amount,
      COUNT(*) AS grant_count
    FROM grants_compat_v1 g
    JOIN canonical_by_ein_year c ON c.filing_id = g.filing_id
    WHERE c.ein = ?
      AND (COALESCE(g.cash_amount, 0) + COALESCE(g.noncash_amount, 0)) <> 0
    GROUP BY c.tax_year, g.recipient_ein, g.recipient_name
    ORDER BY c.tax_year DESC, amount DESC, grantee_name
    """
    items = [
        {
            "tax_year": int(tax_year),
            "grantee_ein": grantee_ein,
            "grantee_name": grantee_name,
            "amount": amount,
            "grant_count": grant_count,
        }
        for tax_year, grantee_ein, grantee_name, amount, grant_count in conn.execute(sql, [ein])
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
            str(item.get("grantor_name") or item.get("grantee_name") or ""),
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
    SELECT DISTINCT
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


def _top_grantees_text(grantees: List[Dict]) -> str:
    bits = []
    for g in grantees:
        name = g.get("grantee_name") or g.get("grantee_ein") or "Unknown"
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
            _top_grantees_text(y.get("top_grantees") or []),
        ))
    return rows


def _build_report(form) -> Dict:
    name = _org_name_search(form)
    if (form or {}).get("_action") == "search_org":
        return {
            "search_query": name,
            "search_results": _search_orgs_by_name(name),
            "years": [],
        }

    ein = _parse_ein(form)
    if not ein:
        if name:
            return {
                "search_query": name,
                "search_results": _search_orgs_by_name(name),
                "years": [],
            }
        return {"error": "Enter exactly one valid 9-digit EIN.", "years": []}

    years = _core_years(ein)
    if not years:
        return {"error": f"No canonical filings found for EIN {ein}.", "years": []}

    conn = connect_ro()
    top_grantors = _top_grantors_by_year(conn, ein)
    top_grantees = _top_grantees_by_year(conn, ein)
    compensation = _compensation_by_year(conn, ein)

    for y in years:
        tax_year = int(y.get("tax_year") or 0)
        y["top_grantors"] = top_grantors.get(tax_year, [])
        y["top_grantees"] = top_grantees.get(tax_year, [])
        y["compensation"] = compensation.get(tax_year, [])

    return {
        "ein": ein,
        "org_name": years[0].get("org_name"),
        "years": years,
    }


def _cache_key(form) -> str:
    ein = _clean_ein_text((form or {}).get("ein", "") or (form or {}).get("ein_list", ""))
    if ein:
        return ein
    return "search:" + _org_name_search(form).casefold()


def run(form):
    global _LAST_KEY, _LAST_REPORT
    key = _cache_key(form)
    report = _build_report(form)
    _LAST_KEY = key
    _LAST_REPORT = report
    return HEADERS, _rows_from_report(report)


def export_rows(form) -> Iterable[Tuple]:
    return _rows_from_report(_build_report(form))


def _compact_money(value) -> str:
    n = _num(value)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        return f"{sign}${n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{sign}${n / 1_000:.0f}K"
    return f"{sign}${n:.0f}"


def _axis_ticks(max_value: float, count: int = 4) -> List[float]:
    max_value = max(1.0, float(max_value or 0))
    return [max_value * i / count for i in range(count + 1)]


def _series_svg(title: str, years: List[Dict], series: List[Tuple[str, str, str]]) -> str:
    data = list(reversed(years))
    if not data:
        return ""
    width, height = 920, 340
    left, right, top, bottom = 92, 28, 34, 54
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
        f'<text x="{left}" y="18" text-anchor="start" class="axis-title">Amount</text>',
    ]
    for tick in _axis_ticks(max_val):
        ty = y(tick)
        lines.append(f'<line x1="{left}" y1="{ty:.1f}" x2="{width - right}" y2="{ty:.1f}" class="gridline"/>')
        lines.append(f'<text x="{left - 10}" y="{ty + 5:.1f}" text-anchor="end" class="axis-label">{_compact_money(tick)}</text>')
    for idx, row in enumerate(data):
        tx = x(idx)
        lines.append(f'<text x="{tx}" y="{height - 14}" text-anchor="middle" class="tick">{_h(row.get("tax_year"))}</text>')
    for label, key, color in series:
        pts = " ".join(f'{x(i):.1f},{y(row.get(key)):.1f}' for i, row in enumerate(data))
        lines.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="3"/>')
        for i, row in enumerate(data):
            cx, cy = x(i), y(row.get(key))
            label_y = max(16, cy - 10)
            lines.append(
                f'<g class="point"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{color}">'
                f'<title>{_h(label)} {row.get("tax_year")}: {_money(row.get(key))}</title></circle>'
                f'<text x="{cx + 8:.1f}" y="{label_y:.1f}" class="point-label">{_money(row.get(key))}</text></g>'
            )
    lines.append("</svg><div class=\"legend\">")
    for label, _, color in series:
        lines.append(f'<span><i style="background:{color}"></i>{_h(label)}</span>')
    lines.append("</div></div>")
    return "".join(lines)


def _lobbying_svg(years: List[Dict]) -> str:
    data = list(reversed(years))
    if not data:
        return ""
    width, height = 920, 340
    left, right, top, bottom = 92, 68, 34, 54
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
        f'<line x1="{width - right}" y1="{top}" x2="{width - right}" y2="{top + plot_h}" class="axis"/>',
        f'<text x="{left}" y="18" text-anchor="start" class="axis-title">Amount</text>',
        f'<text x="{width - right}" y="18" text-anchor="end" class="axis-title">% of Expenses</text>',
    ]
    for tick in _axis_ticks(max_amt):
        ty = y_amt(tick)
        lines.append(f'<line x1="{left}" y1="{ty:.1f}" x2="{width - right}" y2="{ty:.1f}" class="gridline"/>')
        lines.append(f'<text x="{left - 10}" y="{ty + 5:.1f}" text-anchor="end" class="axis-label">{_compact_money(tick)}</text>')
    for tick in _axis_ticks(max_pct):
        ty = y_pct(tick)
        lines.append(f'<text x="{width - right + 10}" y="{ty + 5:.1f}" text-anchor="start" class="axis-label">{tick:.1f}%</text>')
    for idx, row in enumerate(data):
        bx = x(idx) - bar_w / 2
        by = y_amt(row.get("lobbying_expense"))
        bh = top + plot_h - by
        label_y = max(16, by - 8)
        lines.append(
            f'<g class="bar"><rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{max(0, bh):.1f}" fill="#8ea6b4">'
            f'<title>{row.get("tax_year")}: {_money(row.get("lobbying_expense"))}</title></rect>'
            f'<text x="{x(idx):.1f}" y="{label_y:.1f}" text-anchor="middle" class="bar-label">{_money(row.get("lobbying_expense"))}</text></g>'
        )
        lines.append(f'<text x="{x(idx):.1f}" y="{height - 14}" text-anchor="middle" class="tick">{_h(row.get("tax_year"))}</text>')
    pts = " ".join(f'{x(i):.1f},{y_pct(row.get("lobbying_pct_expenses")):.1f}' for i, row in enumerate(data))
    lines.append(f'<polyline points="{pts}" fill="none" stroke="#b45339" stroke-width="3"/>')
    for i, row in enumerate(data):
        pct = row.get("lobbying_pct_expenses")
        cx, cy = x(i), y_pct(pct)
        label_y = max(16, cy - 10)
        lines.append(
            f'<g class="point"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="#b45339">'
            f'<title>{row.get("tax_year")}: {pct or 0:.2f}% of expenses</title></circle>'
            f'<text x="{cx + 8:.1f}" y="{label_y:.1f}" class="point-label">{pct or 0:.2f}%</text></g>'
        )
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


def _text_row(label: str, value) -> str:
    return f'<tr><td>{_h(label)}</td><td>{_h(value)}</td></tr>'


def _count_row(label: str, value) -> str:
    if value in (None, ""):
        display = ""
    else:
        try:
            display = f"{int(float(value)):,}"
        except Exception:
            display = str(value)
    return f'<tr><td>{_h(label)}</td><td class="num">{_h(display)}</td></tr>'


def _city_state(row: Dict) -> str:
    city = (row.get("city") or "").strip()
    state = (row.get("state") or "").strip()
    if city and state:
        return f"{city}, {state}"
    return city or state


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


def _grantee_rows(items: List[Dict]) -> str:
    if not items:
        return '<tr><td colspan="4" class="muted">No grantee rows found for this year.</td></tr>'
    rows = []
    for g in items:
        rows.append(
            f'<tr><td>{_h(g.get("grantee_name") or "")}</td><td>{_h(g.get("grantee_ein") or "")}</td>'
            f'<td class="num">{_money(g.get("amount"))}</td><td class="num">{_h(g.get("grant_count"))}</td></tr>'
        )
    return "".join(rows)


def _comp_rows(items: List[Dict]) -> str:
    if not items:
        return '<tr><td colspan="4" class="muted">No officer compensation rows found for this year.</td></tr>'

    def person_row(p: Dict, extra: bool = False) -> str:
        name = p.get("person_name") or ""
        title = p.get("title") or ""
        return (
            f'<tr{" class=\"comp-extra\"" if extra else ""}><td>{_h(name)}{(" (" + _h(title) + ")") if title else ""}</td>'
            f'<td class="num">{_money(p.get("comp_from_org"))}</td>'
            f'<td class="num">{_money(p.get("comp_from_related"))}</td>'
            f'<td class="num">{_money(p.get("other_compensation"))}</td></tr>'
        )

    rows = []
    visible = items[:5]
    hidden = items[5:]
    rows.extend(person_row(p) for p in visible)
    rows.extend(person_row(p, extra=True) for p in hidden)
    return "".join(rows)


def _year_card(row: Dict) -> str:
    revenue = row.get("total_revenue")
    expenses = row.get("total_expenses")
    comp_id = "comp_" + _clean_ein_text(str(row.get("ein") or "")) + "_" + _clean_ein_text(str(row.get("tax_year") or ""))
    compensation = row.get("compensation") or []
    comp_more = (
        f'<label for="{_h(comp_id)}" class="comp-toggle-label comp-expand">See full list</label>'
        f'<label for="{_h(comp_id)}" class="comp-toggle-label comp-collapse">Show fewer</label>'
        if len(compensation) > 5 else ""
    )
    return f"""
    <section class="deep-year">
      <div class="year-label">Fiscal Year Ending<br><strong>{_h(row.get("period_end") or row.get("tax_year"))}</strong></div>
      <div class="year-card">
        <div class="metrics">
          {_metric("Revenue", revenue)}
          {_metric("Expenses", expenses)}
          {_metric("Net Income", row.get("revenue_less_expenses"))}
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
        <div class="summary-grid assets-info-grid">
          <div>
            <h4>Assets/Debt</h4>
            <table class="left-data-table">
              {_simple_row("Total Assets", row.get("total_assets_eoy"))}
              {_simple_row("Total Liabilities", row.get("total_liabilities_eoy"))}
              {_simple_row("Net Assets", row.get("net_assets_eoy"))}
            </table>
          </div>
          <div>
            <h4>General Info</h4>
            <table class="left-data-table">
              {_text_row("Org Name", row.get("org_name"))}
              {_text_row("EIN", row.get("ein"))}
              {_text_row("Tax Exempt Status", row.get("tax_exempt_status"))}
              {_text_row("City, State", _city_state(row))}
              {_count_row("Employees", row.get("employees_count"))}
              {_count_row("Volunteers", row.get("volunteers_count"))}
            </table>
          </div>
        </div>
        <h4>Top Grantors</h4>
        <table class="left-data-table">
          <thead><tr><th>Grantor</th><th>EIN</th><th>Amount</th><th># of Grants</th></tr></thead>
          <tbody>{_grantor_rows(row.get("top_grantors") or [])}</tbody>
        </table>
        <h4>Top Grantees</h4>
        <table class="left-data-table">
          <thead><tr><th>Grantee</th><th>EIN</th><th>Amount</th><th># of Grants</th></tr></thead>
          <tbody>{_grantee_rows(row.get("top_grantees") or [])}</tbody>
        </table>
        <h4>Compensation</h4>
        <div class="comp-block">
          <input id="{_h(comp_id)}" class="comp-toggle" type="checkbox">
          <table class="left-data-table">
            <thead><tr><th>Key Employees and Officers</th><th>Compensation</th><th>Related</th><th>Other</th></tr></thead>
            <tbody>{_comp_rows(compensation)}</tbody>
          </table>
          {comp_more}
        </div>
      </div>
    </section>
    """


def _render_search_results(report: Dict) -> str:
    query = report.get("search_query") or ""
    results = report.get("search_results") or []
    if not results:
        rows = f"""
        <tr>
          <td colspan="6" class="muted">No canonical filings matched "{_h(query)}".</td>
        </tr>
        """
    else:
        rendered = []
        for item in results:
            ein = _h(item.get("ein"))
            city_state = ", ".join(
                part for part in [item.get("city"), item.get("state")] if part
            )
            rendered.append(f"""
            <tr>
              <td>{_h(item.get("org_name"))}</td>
              <td>{ein}</td>
              <td>{_h(city_state)}</td>
              <td>{_h(item.get("tax_year"))}</td>
              <td>{_h(item.get("return_type"))}</td>
              <td>
                <form method="post" action="/run" style="margin:0;">
                  <input type="hidden" name="qkey" value="nonprofit_deep_dive">
                  <input type="hidden" name="org_search" value="{_h(query)}">
                  <input type="hidden" name="ein" value="{ein}">
                  <button type="submit">Open</button>
                </form>
              </td>
            </tr>
            """)
        rows = "".join(rendered)

    return f"""
    <style>
      .deep-search-results {{ border: 1px solid #ddd; background: #fff; padding: 14px; margin: 18px 0; }}
      .deep-search-results h3 {{ margin: 0 0 10px; }}
      .deep-search-results table {{ width: 100%; border-collapse: collapse; }}
      .deep-search-results th, .deep-search-results td {{ padding: 7px 6px; border-bottom: 1px solid #eee; text-align: left; }}
      .deep-search-results td {{ white-space: normal; overflow: visible; text-overflow: clip; max-width: none; }}
      .deep-search-results tbody tr:nth-child(odd) {{ background: #f7f7f7; }}
      .deep-search-results button {{ min-height: 30px; padding: 5px 10px; }}
      .muted {{ color: #777; }}
    </style>
    <div class="deep-search-results">
      <h3>Organization Matches</h3>
      <table>
        <thead>
          <tr>
            <th>Organization</th>
            <th>EIN</th>
            <th>City, State</th>
            <th>Latest Year</th>
            <th>Filing Type</th>
            <th></th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def _render_report(report: Dict, print_mode: bool = False) -> str:
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
    print_css = """
      @page { size: letter portrait; margin: 0.35in; }
      @media print {
        body { background: #fff; color: #111827; }
        .print-toolbar { display: none !important; }
        .deep-dive { font-size: 9.5px; }
        .report-cover { break-after: page; page-break-after: always; }
        .report-cover h2 { font-size: 22px; margin: 0 0 1px; }
        .deep-subtitle { margin: 0 0 5px; }
        .deep-charts { grid-template-columns: 1fr; gap: 5px; margin: 5px 0 0; }
        .deep-chart { padding: 4px 6px; break-inside: avoid; page-break-inside: avoid; }
        .deep-chart h3 { font-size: 11px; margin-bottom: 2px; }
        .deep-chart svg { max-height: 2.2in; }
        .axis-title { font-size: 16px; }
        .axis-label, .tick { font-size: 13px; }
        .legend { font-size: 8.5px; gap: 6px; }
        .legend i { width: 9px; height: 9px; }
        .deep-year { break-before: page; page-break-before: always; break-after: page; page-break-after: always; grid-template-columns: 62px minmax(0, 1fr); gap: 6px; margin: 0; }
        .report-cover + .deep-year { break-before: auto; page-break-before: auto; }
        .deep-year:last-child { break-after: auto; page-break-after: auto; }
        .year-label { font-size: 8px; line-height: 1.18; }
        .year-label strong { font-size: 14px; }
        .year-card { padding: 7px; }
        .year-card h4 { margin: 5px 0 2px; padding-top: 4px; font-size: 9.5px; }
        .metrics { gap: 5px; padding-bottom: 5px; }
        .metric span { font-size: 8px; }
        .metric strong { font-size: 11px; }
        .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px; }
        .year-card table { font-size: 8.2px; }
        .year-card th, .year-card td { padding: 1.5px 2px; }
        .mini-bar { width: 32px; height: 7px; }
        .mini-bar i { height: 7px; }
        .comp-toggle-label, .comp-toggle { display: none !important; }
      }
    """ if print_mode else ""
    return f"""
    <style>
      .deep-dive h2 {{ margin-bottom: 4px; }}
      .deep-subtitle {{ color: #666; margin-top: 0; }}
      .deep-charts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(520px, 1fr)); gap: 14px; margin: 18px 0; }}
      .deep-chart {{ border: 1px solid #ddd; background: #fff; padding: 12px; }}
      .deep-chart h3 {{ margin: 0 0 8px 0; }}
      .deep-chart svg {{ width: 100%; height: auto; }}
      .axis {{ stroke: #d0d0d0; }}
      .gridline {{ stroke: #eef1f3; }}
      .axis-title {{ font-size: 20px; font-weight: 700; fill: #374151; }}
      .axis-label {{ font-size: 16px; fill: #374151; }}
      .tick {{ font-size: 16px; fill: #374151; }}
      .point-label, .bar-label {{ display: none; font-size: 15px; font-weight: 700; fill: #111827; paint-order: stroke; stroke: #fff; stroke-width: 4px; stroke-linejoin: round; }}
      .point:hover .point-label, .bar:hover .bar-label {{ display: block; }}
      .legend {{ display: flex; flex-wrap: wrap; gap: 12px; font-size: 13px; color: #444; }}
      .legend i {{ display: inline-block; width: 12px; height: 12px; margin-right: 5px; vertical-align: -2px; }}
      .deep-year {{ display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 24px; margin: 18px 0; }}
      .year-label {{ color: #666; font-size: 13px; }}
      .year-label strong {{ display: block; color: #1f2933; font-size: 24px; margin-top: 4px; }}
      .year-card {{ border: 1px solid #ddd; background: #fff; padding: 18px; }}
      .year-card h4 {{ margin: 18px 0 6px; border-top: 1px solid #ddd; padding-top: 10px; }}
      .metrics {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; border-bottom: 1px solid #ddd; padding: 0 0 12px; }}
      .metric span {{ display: block; color: #555; font-size: 12px; }}
      .metric strong {{ font-size: 17px; }}
      .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 18px; }}
      .year-card table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
      .year-card th {{ text-align: left; border-bottom: 1px solid #ddd; padding: 6px 4px; }}
      .year-card td {{ padding: 6px 4px; border: 0; max-width: none; overflow: visible; text-overflow: clip; white-space: normal; }}
      .year-card tbody tr:nth-child(odd), .year-card table > tr:nth-child(odd) {{ background: #f4f4f4; }}
      .num {{ text-align: right; white-space: nowrap; }}
      .left-data-table td, .left-data-table .num {{ text-align: left; }}
      .comp-toggle {{ display: none; }}
      .comp-extra {{ display: none; }}
      .comp-toggle:checked ~ table .comp-extra {{ display: table-row; }}
      .comp-toggle-label {{ color: #2d6cdf; cursor: pointer; font-weight: 650; display: inline-block; margin-top: 8px; }}
      .comp-collapse {{ display: none; }}
      .comp-toggle:checked ~ .comp-expand {{ display: none; }}
      .comp-toggle:checked ~ .comp-collapse {{ display: inline-block; }}
      .mini-bar {{ display: inline-block; width: 90px; height: 12px; background: #edf1f3; vertical-align: -2px; margin-right: 4px; }}
      .mini-bar i {{ display: block; height: 12px; background: #9aabb5; }}
      .muted {{ color: #777; }}
      @media (max-width: 760px) {{
        .deep-charts {{ grid-template-columns: 1fr; }}
        .deep-year {{ grid-template-columns: 1fr; gap: 8px; }}
        .metrics {{ grid-template-columns: 1fr; }}
      }}
      {print_css}
    </style>
    <div class="deep-dive">
      <div class="report-cover">
        <h2>{_h(report.get("org_name"))}</h2>
        <p class="deep-subtitle">EIN {_h(report.get("ein"))}</p>
        <div class="deep-charts">{''.join(charts)}</div>
      </div>
      {cards}
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
    else:
        body = _render_report(report, print_mode=True)
    title = f'{report.get("org_name") or "Nonprofit Deep Dive"} - PDF Export'
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
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          margin: 0 0 14px;
          padding: 10px 12px;
          border: 1px solid #d8dde6;
          background: #fff;
        }}
        .print-toolbar p {{ margin: 0; color: #647084; }}
        .print-toolbar button {{
          border: 1px solid #125f85;
          background: #1c78a6;
          color: #fff;
          border-radius: 6px;
          padding: 8px 12px;
          font: inherit;
          font-weight: 650;
          cursor: pointer;
        }}
        .err {{ background:#ffecec; border:1px solid #f5b5b5; padding:8px; white-space:pre-wrap; }}
        @media print {{
          body {{ max-width: none; padding: 0; margin: 0; background: #fff; }}
        }}
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
