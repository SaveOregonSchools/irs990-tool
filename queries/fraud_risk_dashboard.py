import html
import json
import math
import re
import sqlite3
import textwrap
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from common import attach_grant_work_ro, connect_ro, normalize_eins
from queries._risk_external import fetch_external_checks
from queries._risk_network import (
    available as risk_network_available,
    network_for_ein,
    risk_network_path,
)
from queries._risk_screening import lookup_irs_status, lookup_name_candidates
from queries import ngo_core_data
from queries import nonprofit_deep_dive as deep


META = {
    "key": "fraud_risk_dashboard",
    "name": "Fraud & Risk Indicator Dashboard",
    "description": (
        "Single-EIN dashboard of explainable financial, governance, IRS-status, federal-audit, "
        "public-record, and relationship-network review indicators."
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


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


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


def _object_ref(conn, name: str) -> Optional[str]:
    """Return a safely qualified known object in main or grant_work."""
    for schema in ("main", "grant_work"):
        try:
            row = conn.execute(
                f"SELECT 1 FROM {schema}.sqlite_master "
                "WHERE name = ? AND type IN ('table','view') LIMIT 1",
                [name],
            ).fetchone()
        except Exception:
            continue
        if row:
            return f"{schema}.{name}"
    return None


def _object_columns(conn, object_ref: Optional[str]) -> Set[str]:
    if not object_ref or "." not in object_ref:
        return set()
    schema, name = object_ref.split(".", 1)
    if schema not in {"main", "grant_work"} or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        return set()
    try:
        return {row[1] for row in conn.execute(f"PRAGMA {schema}.table_info({name})")}
    except Exception:
        return set()


def _attach_optional_sidecar(conn) -> bool:
    try:
        return attach_grant_work_ro(conn)
    except Exception:
        return False


def render_fields(form) -> str:
    f = form or {}
    val = f.get("ein", "") or f.get("ein_list", "")
    org_search = f.get("org_search", "")
    external_mode = f.get("external_mode", "live")
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
      <div>
        <label for="external_mode"><b>External checks:</b></label><br>
        <select id="external_mode" name="external_mode" style="min-width:250px;">
          <option value="live" {'selected' if external_mode == 'live' else ''}>Run configured live APIs</option>
          <option value="local" {'selected' if external_mode == 'local' else ''}>Local data only</option>
        </select>
      </div>
    </div>
    <p class="note" style="margin:8px 0 0;">
      Live mode checks public federal sources. FAC, SAM, and FEC require API keys; LDA permits anonymous access. Unavailable sources are reported explicitly.
    </p>
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
    """Score distinct screening signals without rewarding longer filing history."""
    severity_rank = {"High": 3, "Medium": 2, "Low": 1}
    base_weight = {"High": 20, "Medium": 9, "Low": 3}
    grouped: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for item in indicators:
        if item.get("category") in {
            "Data Quality", "Disclosure Context", "External Coverage", "External Lead"
        }:
            continue
        grouped[(item.get("category") or "Other", item.get("title") or "")].append(item)

    category_points: Dict[str, int] = defaultdict(int)
    for (category, _title), items in grouped.items():
        strongest = max(items, key=lambda item: severity_rank.get(item.get("severity"), 0))
        severity = strongest.get("severity")
        recurrence_bonus = min(8, max(0, len(items) - 1) * (2 if severity == "High" else 1))
        category_points[category] += base_weight.get(severity, 0) + recurrence_bonus
    return min(100, sum(min(points, 30) for points in category_points.values()))


def _ratio(part, whole) -> float:
    whole_n = _num(whole)
    if not whole_n:
        return 0.0
    return _num(part) / whole_n


def _yes(value) -> bool:
    return str(value or "").strip().casefold() in {"yes", "x", "1", "true", "t", "y"}


_PRIVATE_FOUNDATION_CODES = {"02", "03", "04"}
_SCHEDULE_C_ABSOLUTE_MATERIALITY = 100_000
_SCHEDULE_C_RATIO_MATERIALITY = 0.05


def _optional_num(value) -> Optional[float]:
    """Return a reported finite number, preserving missing/invalid as None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bmf_subsection(bmf: Optional[Dict]) -> str:
    return _text((bmf or {}).get("subsection")).strip().lstrip("0")


def _filing_subsection(row: Dict, bmf: Optional[Dict]) -> Tuple[str, str]:
    """Prefer the filing-year exemption disclosure over today's BMF snapshot."""
    status = _text(row.get("tax_exempt_status")).strip()
    match = re.search(r"501\s*\(\s*c\s*\)\s*\(\s*0*(\d+)\s*\)", status, re.I)
    if match:
        return match.group(1), "filing-year Form 990"
    subsection = _bmf_subsection(bmf)
    return (subsection, "current EO BMF fallback") if subsection else ("", "unavailable")


def _is_private_foundation(row: Dict, bmf: Optional[Dict]) -> bool:
    return_type = re.sub(r"[^A-Z0-9]", "", _text(row.get("return_type")).upper())
    if "990PF" in return_type:
        return True
    if return_type in {"990", "990EZ"}:
        return False
    foundation = _text((bmf or {}).get("foundation")).strip().zfill(2)
    return foundation in _PRIVATE_FOUNDATION_CODES


def _filing_period_days(row: Dict) -> Optional[int]:
    try:
        start = date.fromisoformat(_text(row.get("period_start"))[:10])
        end = date.fromisoformat(_text(row.get("period_end"))[:10])
    except (TypeError, ValueError):
        return None
    days = (end - start).days + 1
    return days if days > 0 else None


def _is_material_schedule_c_amount(amount: float, expenses: float) -> bool:
    return amount >= _SCHEDULE_C_ABSOLUTE_MATERIALITY or (
        expenses > 0
        and _ratio(amount, expenses) >= _SCHEDULE_C_RATIO_MATERIALITY
    )


def _financial_indicators(years: List[Dict], bmf: Optional[Dict] = None) -> List[Dict]:
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
        employees = _optional_num(row.get("employees_count"))
        is_private_foundation = _is_private_foundation(row, bmf)
        subsection, subsection_source = _filing_subsection(row, bmf)
        is_501c3 = subsection == "3"

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

        if (
            expenses >= 500_000
            and employees == 0
            and not is_private_foundation
        ):
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
        if expenses and grant_ratio >= 0.75 and not is_private_foundation:
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
                "Disclosure Context",
                "Lobbying activity flag",
                year,
                "The core filing indicates lobbying activity.",
                "A lobbying flag without a large amount may still merit Schedule C review.",
                "Inspect Schedule C activity descriptions and expenditure fields.",
            ))

        if _yes(row.get("political_campaign_activity_ind")):
            if is_501c3:
                political_severity = "High"
                political_context = f"The {subsection_source} identifies the filer as a 501(c)(3), for which campaign intervention is prohibited."
            elif subsection:
                political_severity = "Low"
                political_context = f"The {subsection_source} identifies subsection 501(c)({subsection}); disclosed activity can be lawful subject to that subsection's tax and reporting rules."
            else:
                political_severity = "Medium"
                political_context = "The filing-year and current exempt subsections are unavailable, so the applicable campaign-activity restriction is unresolved."
            indicators.append(_indicator(
                political_severity,
                "Lobbying / Political",
                "Political campaign activity flag",
                year,
                "The filing indicates political campaign activity.",
                political_context,
                "Review Schedule C political activity and exempt-status context.",
            ))

        if _yes(row.get("dues_assessments_ind")):
            indicators.append(_indicator(
                "Low",
                "Disclosure Context",
                "Membership dues or proxy-tax flag",
                year,
                "The filing reports dues, assessments, or similar political/lobbying-related activity.",
                "Dues and proxy-tax activity can reveal indirect political or lobbying funding flows.",
                "Review Schedule C dues and proxy-tax details.",
            ))

    asc = list(reversed(years))
    short_periods_reported: Set[int] = set()
    for prev, curr in zip(asc, asc[1:]):
        prev_days = _filing_period_days(prev)
        curr_days = _filing_period_days(curr)
        if (prev_days is not None and prev_days < 300) or (curr_days is not None and curr_days < 300):
            current_year = int(curr.get("tax_year") or 0)
            if current_year not in short_periods_reported:
                short_periods_reported.add(current_year)
                indicators.append(_indicator(
                    "Low",
                    "Data Quality",
                    "Short tax period excluded from annual trend scoring",
                    curr.get("tax_year"),
                    f"Adjacent filing periods span {prev_days or 'unknown'} and {curr_days or 'unknown'} day(s).",
                    "Comparing a short-period return directly with a full-year return can create a false trend anomaly.",
                    "Annualize comparable flow measures or review the period-change explanation before drawing a trend conclusion.",
                ))
            continue
        for label, key in [("revenue", "total_revenue"), ("expenses", "total_expenses"), ("grants paid", "grants_paid")]:
            old = _num(prev.get(key))
            new = _num(curr.get(key))
            if old < 1 or new < 1:
                continue
            change = (new - old) / old
            factor = new / old
            if factor >= 2.0 or factor <= 0.5:
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


def _grant_detail_consistency(conn, years: List[Dict]) -> Dict[int, Dict]:
    """Compare extracted grant detail with the canonical return total.

    A materially inflated detail total is treated as possible duplicate ingestion.
    The comparison is deliberately a data-quality control, not a fraud finding.
    """
    grants = _object_ref(conn, "grants_compat_v1")
    filing_rows = {
        _text(row.get("filing_id")): row
        for row in years
        if _text(row.get("filing_id"))
    }
    if not grants or not filing_rows:
        return {}
    placeholders = ",".join("?" for _ in filing_rows)
    sql = f"""
    SELECT filing_id, COUNT(*),
           SUM(COALESCE(cash_amount,0) + COALESCE(noncash_amount,0))
    FROM {grants}
    WHERE filing_id IN ({placeholders})
    GROUP BY filing_id
    """
    results: Dict[int, Dict] = {}
    for filing_id, row_count, detail_total in conn.execute(sql, list(filing_rows)):
        core = filing_rows.get(_text(filing_id)) or {}
        tax_year = int(core.get("tax_year") or 0)
        core_total = _num(core.get("grants_paid"))
        detail = _num(detail_total)
        difference = detail - core_total
        material = core_total > 0 and abs(difference) >= max(10_000, core_total * 0.20)
        inflated = material and detail > core_total * 1.25
        results[tax_year] = {
            "filing_id": filing_id,
            "row_count": int(row_count or 0),
            "core_total": core_total,
            "detail_total": detail,
            "difference": difference,
            "material_mismatch": material,
            "inflated": inflated,
        }
    return results


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
    detail_consistency = _grant_detail_consistency(conn, years)
    for tax_year, summary in detail_consistency.items():
        if not summary.get("material_mismatch"):
            continue
        detail_total = summary.get("detail_total")
        core_total = summary.get("core_total")
        direction = "exceeds" if _num(detail_total) > _num(core_total) else "is below"
        duplicate_note = (
            " The inflated detail is excluded from scored outgoing network conclusions for this filing year."
            if summary.get("inflated") else ""
        )
        indicators.append(_indicator(
            "Medium",
            "Data Quality",
            "Grant detail does not reconcile to return total",
            tax_year,
            f"{summary.get('row_count') or 0} extracted grant row(s) total {_money(detail_total)}, which {direction} the core grants-paid total of {_money(core_total)}.{duplicate_note}",
            "A material mismatch can reflect duplicate child-row ingestion, incomplete schedule extraction, or a difference in source-field scope; it is not itself evidence of misconduct.",
            "Compare the source XML, remove duplicate child rows by filing, rebuild recipient resolution, and then rebuild network evidence before scoring the affected year.",
        ))
    years_with_rows = set()
    for tax_year, grant_rows, total_amount, missing_ein_amount, largest_recipient_total in conn.execute(sql, [ein]):
        years_with_rows.add(int(tax_year))
        total = _num(total_amount)
        if total and _ratio(missing_ein_amount, total) >= 0.50:
            indicators.append(_indicator(
                "Medium",
                "Data Quality",
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
    expanded = _object_ref(conn, "sched_r_related_orgs_expanded")
    required = {"filing_id", "relationship_category", "controlled_organization_ind", "involved_amt"}
    if not expanded or not required.issubset(_object_columns(conn, expanded)):
        return []

    filing_years = {
        row.get("filing_id"): int(row.get("tax_year") or 0)
        for row in years
        if row.get("filing_id")
    }
    if not filing_years:
        return []
    placeholders = ",".join("?" for _ in filing_years)
    related_by_year: Dict[int, Dict[str, float]] = defaultdict(
        lambda: {"entities": 0, "transactions": 0, "unrelated": 0, "controlled": 0, "involved": 0.0}
    )
    sql = f"""
    SELECT
      filing_id,
      SUM(CASE WHEN relationship_category NOT IN ('Transactions with Related Org', 'Unrelated Taxable Partnership') THEN 1 ELSE 0 END) AS entity_rows,
      SUM(CASE WHEN relationship_category = 'Transactions with Related Org' THEN 1 ELSE 0 END) AS transaction_rows,
      SUM(CASE WHEN relationship_category = 'Unrelated Taxable Partnership' THEN 1 ELSE 0 END) AS unrelated_rows,
      SUM(CASE WHEN relationship_category <> 'Unrelated Taxable Partnership'
                AND UPPER(TRIM(COALESCE(controlled_organization_ind,''))) IN ('X','1','TRUE','T','YES','Y') THEN 1 ELSE 0 END) AS controlled_count,
      SUM(CASE WHEN relationship_category = 'Transactions with Related Org' THEN COALESCE(involved_amt,0) ELSE 0 END) AS involved_amount
    FROM {expanded}
    WHERE filing_id IN ({placeholders})
    GROUP BY filing_id
    """
    for filing_id, entity_rows, transaction_rows, unrelated_rows, controlled_count, involved_amount in conn.execute(
        sql, list(filing_years)
    ):
        year = filing_years.get(filing_id)
        if not year:
            continue
        related_by_year[year]["entities"] += entity_rows or 0
        related_by_year[year]["transactions"] += transaction_rows or 0
        related_by_year[year]["unrelated"] += unrelated_rows or 0
        related_by_year[year]["controlled"] += controlled_count or 0
        related_by_year[year]["involved"] += _num(involved_amount)

    by_year = {int(row.get("tax_year") or 0): row for row in years}
    indicators = []
    for tax_year, summary in related_by_year.items():
        related_rows = int(summary.get("entities") or 0)
        transaction_rows = int(summary.get("transactions") or 0)
        unrelated_rows = int(summary.get("unrelated") or 0)
        controlled_count = int(summary.get("controlled") or 0)
        involved_amount = _num(summary.get("involved"))
        expenses = _num(by_year.get(int(tax_year or 0), {}).get("total_expenses"))
        if related_rows:
            indicators.append(_indicator(
                "Low",
                "Disclosure Context",
                "Schedule R related organizations reported",
                tax_year,
                f"{related_rows} related-entity rows; {controlled_count or 0} controlled entities; {transaction_rows} related-transaction rows. "
                f"{unrelated_rows} explicitly unrelated partnership row(s) were excluded.",
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


def _grant_resolution_spec(conn) -> Optional[Dict[str, str]]:
    """Prefer the reviewed/applied recipient layer while retaining a deterministic fallback."""
    enhanced = _object_ref(conn, "grant_recipient_resolved_plus_ai_v1")
    enhanced_columns = _object_columns(conn, enhanced)
    required = {"final_resolved_ein", "final_resolved_org_name", "final_confidence", "final_match_source"}
    if enhanced and required.issubset(enhanced_columns):
        return {
            "table": enhanced,
            "ein": "final_resolved_ein",
            "name": "final_resolved_org_name",
            "confidence": "final_confidence",
            "match_source": "final_match_source",
            "label": "reviewed enhanced",
        }
    deterministic = _object_ref(conn, "grant_recipient_resolved")
    if not deterministic:
        return None
    return {
        "table": deterministic,
        "ein": "resolved_ein",
        "name": "resolved_org_name",
        "confidence": "confidence",
        "match_source": "",
        "label": "deterministic",
    }


def _grant_identity_indicators(conn, years: List[Dict]) -> List[Dict]:
    spec = _grant_resolution_spec(conn)
    if not spec:
        return []
    resolved = spec["table"]
    ein_col = spec["ein"]
    confidence_col = spec["confidence"]
    ein = _text(years[0].get("ein")) if years else ""
    sql = f"""
    SELECT tax_year,
      COUNT(*) AS row_count,
      SUM(COALESCE(total_amount,0)) AS total_amount,
      SUM(CASE WHEN COALESCE(TRIM({ein_col}),'') = '' THEN COALESCE(total_amount,0) ELSE 0 END) AS unresolved_amount,
      SUM(CASE WHEN COALESCE(match_status,'') = 'conflicting_ein_match' OR COALESCE(warning_flags,'') LIKE '%conflict%' THEN 1 ELSE 0 END) AS conflict_rows,
      SUM(CASE WHEN COALESCE(recipient_reported_ein,'') <> '' AND COALESCE({ein_col},'') <> ''
                AND recipient_reported_ein <> {ein_col} THEN 1 ELSE 0 END) AS corrected_ein_rows,
      SUM(CASE WHEN {confidence_col} IS NOT NULL AND {confidence_col} < 0.70 THEN 1 ELSE 0 END) AS low_confidence_rows
    FROM {resolved}
    WHERE grantor_ein = ? AND COALESCE(total_amount,0) > 0
    GROUP BY tax_year
    ORDER BY tax_year DESC
    """
    indicators = []
    for tax_year, rows_count, total_amount, unresolved_amount, conflicts, corrected, low_confidence in conn.execute(sql, [ein]):
        total = _num(total_amount)
        unresolved = _num(unresolved_amount)
        if total and unresolved / total >= 0.50:
            indicators.append(_indicator(
                "Medium",
                "Data Quality",
                "Most grant dollars remain unresolved to a recipient EIN",
                tax_year,
                f"The {spec['label']} matching layer left {_money(unresolved)} of {_money(total)} ({_pct(unresolved, total)}) without a resolved EIN.",
                "Unresolved recipient identity limits network completeness and related-party screening.",
                "Review recipient signatures, candidate matches, warning flags, and source XML before relying on network totals.",
            ))
        if conflicts:
            indicators.append(_indicator(
                "Medium",
                "Data Quality",
                "Grant recipient EIN conflicts",
                tax_year,
                f"{conflicts} of {rows_count} resolved-grant row(s) contain an EIN conflict warning.",
                "A reported EIN that conflicts with name or address evidence can attach funds to the wrong organization.",
                "Review the reported and resolved identities, match method, confidence, and warning flags.",
            ))
        if corrected or low_confidence:
            indicators.append(_indicator(
                "Low",
                "Data Quality",
                "Grant identity matching requires review",
                tax_year,
                f"{corrected or 0} row(s) changed a reported EIN; {low_confidence or 0} row(s) have confidence below 0.70.",
                "These rows may be useful network leads but should not be treated as verified identities without review.",
                "Inspect enhanced-match evidence and prefer exact, high-confidence matches for downstream conclusions.",
            ))
    return indicators


def _schedule_c_indicators(
    conn, years: List[Dict], bmf: Optional[Dict] = None
) -> List[Dict]:
    table = _object_ref(conn, "irs990_schedule_c_root")
    if not table:
        return []
    filing_years = {
        row.get("filing_id"): int(row.get("tax_year") or 0)
        for row in years
        if row.get("filing_id")
    }
    if not filing_years:
        return []
    by_year = {int(row.get("tax_year") or 0): row for row in years}
    placeholders = ",".join("?" for _ in filing_years)
    sql = f"""
    SELECT
      filing_id,
      COALESCE(political_expenditures_amt,0),
      COALESCE(expended527_activities_amt,0),
      COALESCE(lobbying_excess_amt,0),
      COALESCE(lobbying_grassroots_excess_amt,0),
      COALESCE(grants_other_organizations_amt,0),
      COALESCE(non_deductible_lbbyng_pltcl_cy_amt,0),
      form1120_pol_filed_ind
    FROM {table}
    WHERE filing_id IN ({placeholders})
    """
    indicators = []
    for row in conn.execute(sql, list(filing_years)):
        filing_id, political, section527, excess, grass_excess, lobbying_grants, nondeductible, form1120 = row
        year = filing_years.get(filing_id)
        filing_row = by_year.get(int(year or 0), {})
        subsection, subsection_source = _filing_subsection(filing_row, bmf)
        is_501c3 = subsection == "3"
        if _num(political) or _num(section527):
            total = _num(political) + _num(section527)
            expenses = _num(filing_row.get("total_expenses"))
            material = _is_material_schedule_c_amount(total, expenses)
            materiality = (
                f"The combined amount was {_pct(total, expenses)} of total expenses"
                if expenses > 0
                else "Total expenses were unavailable for a ratio comparison"
            )
            materiality += (
                " and met the dashboard materiality screen ($100,000 or 5% of expenses)."
                if material
                else " and did not meet the dashboard materiality screen ($100,000 or 5% of expenses)."
            )
            if is_501c3:
                political_severity = "High"
                political_context = (
                    f"The {subsection_source} identifies the filer as a 501(c)(3), "
                    "for which campaign intervention is prohibited."
                )
            elif subsection:
                political_severity = "Medium" if material else "Low"
                political_context = (
                    f"The {subsection_source} identifies the filer as a 501(c)({subsection}); "
                    "political-activity rules differ from the 501(c)(3) prohibition, "
                    "so this disclosure is a review signal rather than evidence of a violation."
                )
            else:
                political_severity = "Medium" if material else "Low"
                political_context = (
                    "The current exempt subsection is unavailable, so the applicable "
                    "political-activity rules require confirmation."
                )
            indicators.append(_indicator(
                political_severity,
                "Lobbying / Political" if (is_501c3 or material) else "Disclosure Context",
                "Schedule C political or section 527 expenditures",
                year,
                f"Political expenditures {_money(political)}; section 527 activity {_money(section527)}; combined {_money(total)}. {materiality}",
                political_context,
                "Review Schedule C Part I, any Form 1120-POL filing, the activity narrative, and recipient details.",
            ))
        if _num(excess) or _num(grass_excess):
            indicators.append(_indicator(
                "High",
                "Lobbying / Political",
                "Schedule C lobbying-limit excess",
                year,
                f"Lobbying excess {_money(excess)}; grassroots lobbying excess {_money(grass_excess)}.",
                "A reported excess is more specific than a general lobbying flag and may carry tax or exemption consequences.",
                "Review the section 501(h) calculation, carryovers, excise tax reporting, and Schedule C explanation.",
            ))
        if _num(lobbying_grants) > 0:
            lobbying_grants_amount = _num(lobbying_grants)
            expenses = _num(filing_row.get("total_expenses"))
            material = _is_material_schedule_c_amount(lobbying_grants_amount, expenses)
            subsection_context = (
                f" for subsection 501(c)({subsection}) from the {subsection_source}"
                if subsection
                else " while the filing-year and current exempt subsections are unavailable"
            )
            if expenses > 0:
                lobbying_grant_evidence = (
                    f"Schedule C reports {_money(lobbying_grants_amount)} in grants "
                    f"to other organizations for lobbying "
                    f"({_pct(lobbying_grants_amount, expenses)} of expenses)."
                )
            else:
                lobbying_grant_evidence = (
                    f"Schedule C reports {_money(lobbying_grants_amount)} in grants "
                    "to other organizations for lobbying; total expenses were unavailable."
                )
            indicators.append(_indicator(
                "Medium" if material else "Low",
                "Lobbying / Political" if material else "Disclosure Context",
                "Grants used for lobbying activities",
                year,
                lobbying_grant_evidence,
                f"Lobbying conducted through grants can be harder to connect to the filer using only its direct-expense total{subsection_context}; the amount {'met' if material else 'did not meet'} the dashboard materiality screen ($100,000 or 5% of expenses).",
                "Compare Schedule C grant detail with the grant-recipient network and recipient lobbying disclosures.",
            ))
        if _num(nondeductible) > 0:
            indicators.append(_indicator(
                "Low",
                "Disclosure Context",
                "Nondeductible lobbying or political amount",
                year,
                f"The current-year nondeductible lobbying/political amount was {_money(nondeductible)}.",
                "This is a useful cross-check against dues notices and proxy-tax reporting.",
                "Review Schedule C Part III-A and member notices.",
            ))
        if _yes(form1120):
            indicators.append(_indicator(
                "Low",
                "Disclosure Context",
                "Form 1120-POL filing reported",
                year,
                "Schedule C indicates that Form 1120-POL was filed.",
                "The filing may reflect taxable political organization income or expenditures that merit reconciliation.",
                "Compare the Form 1120-POL period and amounts with Schedule C and the Form 990 return.",
            ))
    return indicators


def _schedule_l_indicators(conn, years: List[Dict]) -> List[Dict]:
    filing_years = {
        row.get("filing_id"): int(row.get("tax_year") or 0)
        for row in years
        if row.get("filing_id")
    }
    if not filing_years:
        return []
    by_year = {int(row.get("tax_year") or 0): row for row in years}
    placeholders = ",".join("?" for _ in filing_years)
    params = list(filing_years)
    indicators = []

    excess_table = _object_ref(conn, "irs990_schedule_l_disqualified_person_ex_bnft_tr_grp")
    if excess_table:
        sql = f"""
        SELECT filing_id, COUNT(*),
          SUM(CASE WHEN UPPER(TRIM(COALESCE(transaction_corrected_ind,''))) IN ('X','1','TRUE','T','YES','Y') THEN 1 ELSE 0 END)
        FROM {excess_table}
        WHERE filing_id IN ({placeholders})
        GROUP BY filing_id
        """
        for filing_id, row_count, corrected_count in conn.execute(sql, params):
            indicators.append(_indicator(
                "Medium" if corrected_count == row_count else "High",
                "Interested Persons",
                "Schedule L excess-benefit transaction",
                filing_years.get(filing_id),
                f"{row_count} excess-benefit transaction row(s); {corrected_count or 0} marked corrected.",
                "Excess-benefit transactions with disqualified persons are a direct related-party governance concern.",
                "Review the persons, transaction descriptions, correction dates, and any section 4958 excise taxes.",
            ))

    checks = [
        (
            "irs990_schedule_l_bus_tr_involve_interested_prsn_grp",
            "transaction_amt",
            "Business transactions with interested persons",
            "Business transactions involving insiders can indicate conflicts of interest or non-arm's-length terms.",
        ),
        (
            "irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp",
            "cash_grant_amt",
            "Grants or assistance to interested persons",
            "Financial assistance to insiders or related persons merits a purpose and approval review.",
        ),
        (
            "irs990_schedule_l_loans_btwn_org_interested_prsn_grp",
            "balance_due_amt",
            "Loans involving interested persons",
            "Outstanding insider loans can present collection, private-benefit, and governance risks.",
        ),
    ]
    for table_name, amount_col, title, why in checks:
        table = _object_ref(conn, table_name)
        if not table:
            continue
        sql = f"""
        SELECT filing_id, COUNT(*), SUM(COALESCE({amount_col},0)), MAX(COALESCE({amount_col},0))
        FROM {table}
        WHERE filing_id IN ({placeholders})
        GROUP BY filing_id
        """
        for filing_id, row_count, total_amount, max_amount in conn.execute(sql, params):
            year = filing_years.get(filing_id)
            expenses = _num(by_year.get(int(year or 0), {}).get("total_expenses"))
            material = _num(total_amount) >= 100_000 or (expenses and _ratio(total_amount, expenses) >= 0.10)
            indicators.append(_indicator(
                "Medium" if material else "Low",
                "Interested Persons",
                title,
                year,
                f"{row_count} row(s), totaling {_money(total_amount)}; largest amount {_money(max_amount)}.",
                why,
                "Review identities, relationships, approvals, transaction terms, repayment/correction status, and Schedule L explanations.",
            ))
    return indicators


_BMF_STATUS = {
    "01": "Unconditional exemption",
    "25": "Terminating private-foundation status",
}

_BMF_DEDUCTIBILITY = {
    "1": "Contributions are deductible",
    "2": "Contributions are not deductible",
    "4": "Contributions are deductible by treaty",
}

_BMF_FOUNDATION = {
    "00": "Not a 501(c)(3) organization",
    "02": "Private operating foundation (investment-income excise-tax exempt)",
    "03": "Private operating foundation",
    "04": "Private non-operating foundation",
    "09": "Public-charity status pending/suspense",
    "10": "Church",
    "11": "School",
    "12": "Hospital or medical research organization",
    "13": "College/university support organization",
    "14": "Governmental unit",
    "15": "Publicly supported organization",
    "16": "Publicly supported organization under 509(a)(2)",
    "17": "Supporting organization under 509(a)(3)",
    "18": "Public-safety testing organization",
    "21": "509(a)(3) supporting organization, Type I",
    "22": "509(a)(3) supporting organization, Type II",
    "23": "509(a)(3) supporting organization, Type III functionally integrated",
    "24": "509(a)(3) supporting organization, Type III not functionally integrated",
}


def _format_bmf_tax_period(value) -> str:
    raw = re.sub(r"\D", "", _text(value))
    if len(raw) != 6:
        return _text(value)
    return f"{raw[:4]}-{raw[4:]}"


def _load_bmf_profile(conn, ein: str, years: List[Dict]) -> Dict:
    table = _object_ref(conn, "org_identity")
    if not table:
        return {"available": False, "matched": False}
    sql = f"""
    SELECT source_detail, display_name, street, city, state, zip5,
           subsection, foundation, deductibility, ntee_cd, status, tax_period,
           asset_amt, income_amt, revenue_amt, extra_json
    FROM {table}
    WHERE ein = ? AND source = 'bmf_name'
    ORDER BY identity_id
    LIMIT 1
    """
    row = conn.execute(sql, [ein]).fetchone()
    if not row:
        return {"available": True, "matched": False}
    keys = [
        "source_detail", "display_name", "street", "city", "state", "zip5",
        "subsection", "foundation", "deductibility", "ntee_cd", "status", "tax_period",
        "asset_amt", "income_amt", "revenue_amt", "extra_json",
    ]
    profile = dict(zip(keys, row))
    try:
        profile["extra"] = json.loads(profile.get("extra_json") or "{}")
    except (TypeError, ValueError):
        profile["extra"] = {}
    profile.update({
        "available": True,
        "matched": True,
        "status_label": _BMF_STATUS.get(_text(profile.get("status")), f"IRS status code {_text(profile.get('status')) or '(blank)'}"),
        "deductibility_label": _BMF_DEDUCTIBILITY.get(_text(profile.get("deductibility")), f"Deductibility code {_text(profile.get('deductibility')) or '(blank)'}"),
        "foundation_label": _BMF_FOUNDATION.get(_text(profile.get("foundation")), f"Foundation code {_text(profile.get('foundation')) or '(blank)'}"),
        "subsection_label": f"501(c)({_text(profile.get('subsection')).lstrip('0')})" if _text(profile.get("subsection")).strip("0") else "",
        "tax_period_label": _format_bmf_tax_period(profile.get("tax_period")),
    })
    latest = years[0] if years else {}
    filing_address = " ".join(
        _text(latest.get(key)).strip()
        for key in ("us_address_line1", "city", "state", "zip")
        if _text(latest.get(key)).strip()
    )
    bmf_address = " ".join(
        _text(profile.get(key)).strip()
        for key in ("street", "city", "state", "zip5")
        if _text(profile.get(key)).strip()
    )
    profile["filing_address"] = filing_address
    profile["bmf_address"] = bmf_address
    return profile


def _bmf_indicators(bmf: Dict, years: List[Dict]) -> List[Dict]:
    if not bmf.get("available") or not bmf.get("matched"):
        return []
    latest_year = (years[0] if years else {}).get("tax_year")
    indicators = []
    status = _text(bmf.get("status"))
    if status and status != "01":
        indicators.append(_indicator(
            "Medium",
            "IRS Status",
            "EO BMF status requires review",
            latest_year,
            f"The loaded EO BMF record reports {bmf.get('status_label')} (code {status}).",
            "A nonstandard BMF status is not proof of revocation, but it changes how the organization's exemption should be interpreted.",
            "Confirm current status in IRS TEOS, Pub. 78, automatic-revocation history, and determination letters.",
        ))
    return indicators


def _filing_status_indicators(years: List[Dict]) -> List[Dict]:
    indicators = []
    for row in years:
        year = row.get("tax_year")
        if _yes(row.get("final_return_ind")):
            indicators.append(_indicator(
                "Medium",
                "Filing Status",
                "Final return indicated",
                year,
                "The canonical filing is marked as a final return or termination.",
                "A final return changes the expected filing history and may reflect dissolution, merger, or cessation.",
                "Confirm later filings, state registration, asset disposition, related organizations, and current IRS status.",
            ))
        if _yes(row.get("application_pending_ind")):
            indicators.append(_indicator(
                "Medium",
                "IRS Status",
                "Exemption application pending",
                year,
                "The filing indicates that an exemption application was pending.",
                "Pending status affects how the filing and public fundraising claims should be interpreted.",
                "Compare the filing date with the BMF snapshot and any IRS determination letter.",
            ))
        if _yes(row.get("amended_return_ind")):
            indicators.append(_indicator(
                "Low",
                "Filing Status",
                "Amended return selected as canonical",
                year,
                "The latest canonical filing for this year is marked amended.",
                "Amendments are common, but material changes can explain otherwise unusual trends.",
                "Compare the amended return with earlier submissions for the same tax year.",
            ))
    return indicators


_GOVERNANCE_XML_FIELDS = {
    "voting_members": ("VotingMembersGoverningBodyCnt",),
    "independent_members": ("VotingMembersIndependentCnt",),
    "family_business_relationships": ("FamilyOrBusinessRlnInd",),
    "material_diversion": ("MaterialDiversionOrMisuseInd",),
    "minutes_governing_body": ("MinutesOfGoverningBodyInd",),
    "minutes_committees": ("MinutesOfCommitteesInd",),
    "form990_provided_to_board": ("Form990ProvidedToGvrnBodyInd",),
    "conflict_policy": ("ConflictOfInterestPolicyInd",),
    "whistleblower_policy": ("WhistleblowerPolicyInd",),
    "document_retention_policy": ("DocumentRetentionPolicyInd",),
    "financial_statements_audited": ("FSAuditedInd",),
    "audit_committee": ("AuditCommitteeInd",),
    "federal_grant_audit_required": ("FederalGrantAuditRequiredInd",),
    "federal_grant_audit_performed": ("FederalGrantAuditPerformedInd",),
}
_GOVERNANCE_COUNT_FIELDS = {"voting_members", "independent_members"}
_GOVERNANCE_TAG_TO_FIELD = {
    tag: field
    for field, tags in _GOVERNANCE_XML_FIELDS.items()
    for tag in tags
}


def _optional_bool(value) -> Optional[bool]:
    normalized = _text(value).strip().casefold()
    if normalized in {"1", "true", "t", "yes", "y", "x"}:
        return True
    if normalized in {"0", "false", "f", "no", "n"}:
        return False
    return None


def _extract_governance_xml(path) -> Dict:
    """Extract a small, schema-stable governance subset from one Form 990 XML."""
    root = ET.parse(path).getroot()
    raw: Dict[str, str] = {}
    for element in root.iter():
        local_tag = _text(element.tag).rsplit("}", 1)[-1]
        field = _GOVERNANCE_TAG_TO_FIELD.get(local_tag)
        if field and field not in raw and element.text not in (None, ""):
            raw[field] = _text(element.text).strip()

    result: Dict[str, Any] = {}
    for field in _GOVERNANCE_XML_FIELDS:
        value = raw.get(field)
        if field in _GOVERNANCE_COUNT_FIELDS:
            try:
                result[field] = int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                result[field] = None
        else:
            result[field] = _optional_bool(value)
    return result


def _load_governance_xml(ein: str, years: List[Dict], limit: int = 8) -> Dict:
    """Read governance controls from the local XML inventory without DB writes."""
    requested_years = list(years)[: max(1, int(limit or 1))]
    coverage = {
        "requested": len(requested_years),
        "missing": 0,
        "quarantined": 0,
        "parse_errors": 0,
        "empty": 0,
    }
    records = []

    def result(reason: Optional[str] = None) -> Dict:
        summary = {**coverage, "loaded": len(records)}
        response = {
            "available": bool(records),
            "records": records,
            **coverage,
            "coverage": summary,
            "source": "IRS Form 990 XML",
        }
        if reason:
            response["reason"] = reason
        return response

    inventory_path = deep.SOURCE_INVENTORY_DB_PATH
    if not inventory_path.is_file():
        coverage["missing"] = coverage["requested"]
        return result("xml_inventory_unavailable")

    inventory_uri = f"file:{inventory_path.as_posix()}?mode=ro&immutable=1"
    try:
        inventory = sqlite3.connect(inventory_uri, uri=True)
        inventory.execute("PRAGMA query_only = ON")
    except (OSError, sqlite3.Error):
        coverage["missing"] = coverage["requested"]
        return result("xml_inventory_unavailable")

    inventory_read_error = False
    try:
        for year in requested_years:
            filing_id = _text(year.get("filing_id")).strip()
            if not filing_id:
                coverage["missing"] += 1
                continue
            try:
                rows = inventory.execute(
                    """
                    SELECT filing_id, object_id, relative_path, source_file,
                           duplicate_status, keep_source_file, quarantine_status
                    FROM source_files
                    WHERE object_id = ?
                    ORDER BY source_file COLLATE NOCASE
                    """,
                    [deep._object_id_from_filing_id(filing_id)],
                ).fetchall()
            except sqlite3.Error:
                coverage["missing"] += 1
                inventory_read_error = True
                continue

            path = deep._preferred_inventory_source(rows, filing_id)
            if path is None:
                usable_rows = [row for row in rows if not _text(row[6]).strip()]
                if rows and not usable_rows:
                    coverage["quarantined"] += 1
                else:
                    coverage["missing"] += 1
                continue

            try:
                values = _extract_governance_xml(path)
            except (ET.ParseError, OSError, ValueError):
                coverage["parse_errors"] += 1
                continue
            if not any(value is not None for value in values.values()):
                coverage["empty"] += 1
                continue
            records.append({
                "tax_year": year.get("tax_year"),
                "filing_id": filing_id,
                **values,
            })
    finally:
        inventory.close()
    return result("xml_inventory_read_error" if inventory_read_error else None)


def _governance_indicators(governance: Dict, years: List[Dict]) -> List[Dict]:
    indicators = []
    records = governance.get("records") or []
    requested = int(_num(governance.get("requested") or len(records)))
    missing = int(_num(governance.get("missing")))
    quarantined = int(_num(governance.get("quarantined")))
    parse_errors = int(_num(governance.get("parse_errors")))
    empty = int(_num(governance.get("empty")))
    if requested and (len(records) < requested or missing or quarantined or parse_errors or empty):
        indicators.append(_indicator(
            "Medium" if not records else "Low",
            "Data Quality",
            "Governance XML coverage is incomplete",
            None,
            (
                f"Supported governance fields were extracted from {len(records)} of {requested} requested filing(s); "
                f"{missing} missing, {quarantined} quarantined, {parse_errors} parse error(s), and {empty} with no supported fields."
            ),
            "Incomplete source XML coverage can hide filer-reported governance and audit-control disclosures.",
            "Restore or rescan missing XML, review quarantined conflicts, and repair parse failures before treating absent indicators as negative findings.",
        ))
    financial_by_year = {row.get("tax_year"): row for row in years}
    for record in records:
        year = record.get("tax_year")
        if record.get("material_diversion") is True:
            indicators.append(_indicator(
                "High", "Governance", "Material diversion or misuse reported", year,
                "The Form 990 governance section answers yes to material diversion or misuse of assets.",
                "This is a direct filer disclosure of a potentially material control failure, not merely a statistical anomaly.",
                "Read the return explanation, quantify the loss and recovery, identify responsible parties, and compare corrective actions and law-enforcement disclosures.",
            ))

        if (
            record.get("federal_grant_audit_required") is True
            and record.get("federal_grant_audit_performed") is False
        ):
            indicators.append(_indicator(
                "High", "External Compliance", "Required federal grant audit reported as not performed", year,
                "The Form 990 says a federal grant audit was required but was not performed.",
                "This is a filer-reported compliance exception. FAC evidence should be reviewed to confirm scope, timing, and any later submission.",
                "Compare the fiscal period with FAC reports, federal awards expended, auditor communications, and corrective action.",
            ))

        members = record.get("voting_members")
        independent = record.get("independent_members")
        if isinstance(members, int) and members > 0 and isinstance(independent, int):
            if independent == 0:
                indicators.append(_indicator(
                    "Medium", "Governance", "No independent voting members reported", year,
                    f"The organization reported {members} voting governing-body member(s) and none independent.",
                    "A governing body without independent members has less structural protection against conflicts and private benefit.",
                    "Review board composition, related-party transactions, compensation approvals, and state-law governance requirements.",
                ))
            elif independent * 2 < members:
                indicators.append(_indicator(
                    "Medium", "Governance", "Independent members are a minority", year,
                    f"The organization reported {independent} independent member(s) among {members} voting governing-body members.",
                    "Minority independence can weaken oversight where financial or family relationships exist.",
                    "Review board rosters, independence criteria, recusals, and approvals of insider transactions.",
                ))

        if record.get("family_business_relationships") is True:
            indicators.append(_indicator(
                "Medium", "Governance", "Family or business relationships among leaders", year,
                "The Form 990 reports a family or business relationship among officers, directors, trustees, or key employees.",
                "Such relationships can be legitimate but increase conflict-of-interest and related-party oversight risk.",
                "Identify the people and relationship, then compare Schedule L, compensation, grants, contractors, and recusal records.",
            ))

        missing_minutes = [
            label for field, label in (
                ("minutes_governing_body", "governing body"),
                ("minutes_committees", "committees"),
            )
            if record.get(field) is False
        ]
        if missing_minutes:
            indicators.append(_indicator(
                "Medium", "Governance", "Contemporaneous minutes not maintained", year,
                "The organization reported that contemporaneous documentation was not maintained for " + " and ".join(missing_minutes) + ".",
                "Missing minutes reduce evidence that major decisions and conflicts received appropriate review.",
                "Request minutes, written consents, committee records, and documentation for compensation and related-party approvals.",
            ))

        absent_policies = [
            label for field, label in (
                ("conflict_policy", "conflict-of-interest"),
                ("whistleblower_policy", "whistleblower"),
                ("document_retention_policy", "document-retention"),
            )
            if record.get(field) is False
        ]
        if absent_policies:
            indicators.append(_indicator(
                "Low", "Governance", "Governance policies reported absent", year,
                "The organization reported no " + ", ".join(absent_policies) + " policy.",
                "These policies are not universally required, but their absence can weaken prevention, reporting, and evidence preservation controls.",
                "Confirm which policies are legally or contractually required and review any equivalent procedures in practice.",
            ))

        if record.get("form990_provided_to_board") is False:
            indicators.append(_indicator(
                "Low", "Governance", "Form 990 not provided to governing body", year,
                "The filing reports that a complete copy of Form 990 was not provided to all governing-body members before filing.",
                "Board review is an important accuracy and oversight control even though the response alone does not establish noncompliance.",
                "Confirm the return-review process, who reviewed the filing, and whether material schedules were discussed.",
            ))

        financial = financial_by_year.get(year) or {}
        if (
            record.get("financial_statements_audited") is False
            and max(_num(financial.get("total_assets_eoy")), _num(financial.get("total_revenue"))) >= 5_000_000
        ):
            indicators.append(_indicator(
                "Low", "Governance", "Large organization reports no audited financial statements", year,
                "The Form 990 reports no audited financial statements while revenue or ending assets were at least $5 million.",
                "An audit may not be legally required, but the combination warrants understanding the organization's assurance and oversight practices.",
                "Check state audit thresholds, grant agreements, FAC applicability, reviewed/compiled statements, and audit-committee oversight.",
            ))
    return indicators


_GENERIC_PERSON_NAMES = {
    "BOARD MEMBER", "DIRECTOR", "OFFICER", "TRUSTEE", "PRESIDENT",
    "SECRETARY", "TREASURER", "VARIOUS", "NONE", "N A", "NA",
}


def _name_key(value) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", _text(value).upper()).strip()


def _person_key(value) -> str:
    key = _name_key(value)
    if key in _GENERIC_PERSON_NAMES or len(key) < 5 or len(key.split()) < 2:
        return ""
    return key


def _connection_key(ein, name) -> str:
    digits = re.sub(r"\D", "", _text(ein))
    if len(digits) == 9:
        return digits
    normalized = _name_key(name)
    return f"name:{normalized}" if normalized else ""


def _trusted_grant_identity_sql(
    alias: str = "",
    *,
    ein_col: str = "resolved_ein",
    confidence_col: str = "confidence",
    match_source_col: str = "",
) -> str:
    """SQL predicate for identities safe enough to drive scored network signals."""
    prefix = f"{alias}." if alias else ""
    status_clause = f"COALESCE({prefix}match_status,'') NOT IN ('unresolved','conflicting_ein_match')"
    if match_source_col:
        status_clause = (
            f"COALESCE({prefix}{match_source_col},'') <> 'reported_ein_from_filing_unverified' "
            f"AND (COALESCE({prefix}{match_source_col},'') <> 'deterministic' OR {status_clause})"
        )
    return (
        f"COALESCE(TRIM({prefix}{ein_col}),'') <> '' "
        f"AND COALESCE({prefix}{confidence_col},0) >= 0.85 "
        f"AND {status_clause}"
    )


def _indexed_incoming_grant_rows_sql(conn, recipient_ein: str) -> Optional[Tuple[str, List]]:
    """Return an index-friendly incoming-grant row query and its parameters.

    The enhanced view computes its final EIN with CASE over a LEFT JOIN, so an
    outer `final_resolved_ein = ?` can scan the entire resolution corpus. Split
    applied and deterministic identities into their indexed branches instead.
    """
    resolved = _object_ref(conn, "grant_recipient_resolved")
    if not resolved:
        return None
    resolved_columns = _object_columns(conn, resolved)
    required = {
        "grant_id", "grantor_ein", "grantor_name", "tax_year", "total_amount",
        "resolved_ein", "confidence", "match_status",
    }
    if not required.issubset(resolved_columns):
        return None
    deterministic_trusted = _trusted_grant_identity_sql(alias="rr")
    applied = _object_ref(conn, "grant_recipient_ai_applied")
    applied_columns = _object_columns(conn, applied)
    applied_required = {"grant_id", "selected_ein", "ai_confidence", "model"}
    if applied and applied_required.issubset(applied_columns):
        sql = f"""
        SELECT rr.grantor_ein, rr.grantor_name, rr.tax_year, rr.total_amount
        FROM {applied} aa
        JOIN {resolved} rr ON rr.grant_id = aa.grant_id
        WHERE aa.selected_ein = ?
          AND COALESCE(aa.ai_confidence,0) >= 0.85
          AND COALESCE(aa.model,'') <> 'rule:reported_ein_from_filing_unverified'
          AND COALESCE(rr.total_amount,0) > 0
        UNION ALL
        SELECT rr.grantor_ein, rr.grantor_name, rr.tax_year, rr.total_amount
        FROM {resolved} rr
        WHERE rr.resolved_ein = ?
          AND COALESCE(rr.total_amount,0) > 0
          AND {deterministic_trusted}
          AND NOT EXISTS (
            SELECT 1 FROM {applied} aa WHERE aa.grant_id = rr.grant_id
          )
        """
        return sql, [recipient_ein, recipient_ein]
    sql = f"""
    SELECT rr.grantor_ein, rr.grantor_name, rr.tax_year, rr.total_amount
    FROM {resolved} rr
    WHERE rr.resolved_ein = ?
      AND COALESCE(rr.total_amount,0) > 0
      AND {deterministic_trusted}
    """
    return sql, [recipient_ein]


_GRANT_FLOW_WINDOW_YEARS = 2


def _add_connection(
    connections: Dict[str, Dict],
    *,
    subject_ein: str,
    ein=None,
    name=None,
    relationship: str,
    evidence: str,
    amount=0,
    year=None,
    min_year=None,
    max_year=None,
    year_amounts: Optional[Dict[int, float]] = None,
    occurrences=1,
    scored_relationship: bool = True,
) -> None:
    key = _connection_key(ein, name)
    if not key or key == subject_ein:
        return
    item = connections.setdefault(key, {
        "key": key,
        "ein": key if key.isdigit() else "",
        "name": _text(name).strip() or (key if not key.isdigit() else f"EIN {key}"),
        "relationships": set(),
        "scored_relationships": set(),
        "evidence": [],
        "years": set(),
        "year_ranges_by_type": {},
        "year_amounts_by_type": {},
        "amount_by_type": defaultdict(float),
        "occurrences": 0,
    })
    if name and (not item.get("name") or item.get("name", "").startswith("EIN ")):
        item["name"] = _text(name).strip()
    item["relationships"].add(relationship)
    if scored_relationship:
        item["scored_relationships"].add(relationship)
    if evidence and evidence not in item["evidence"] and len(item["evidence"]) < 8:
        item["evidence"].append(evidence)
    actual_year_amounts: Dict[int, float] = {}
    for raw_year, raw_amount in (year_amounts or {}).items():
        try:
            year_n = int(raw_year)
        except (TypeError, ValueError):
            continue
        actual_year_amounts[year_n] = actual_year_amounts.get(year_n, 0.0) + _num(raw_amount)
    if actual_year_amounts:
        yearly = item["year_amounts_by_type"].setdefault(relationship, {})
        for year_n, year_amount in actual_year_amounts.items():
            item["years"].add(year_n)
            yearly[year_n] = yearly.get(year_n, 0.0) + year_amount
        start_n, end_n = min(actual_year_amounts), max(actual_year_amounts)
        previous = item["year_ranges_by_type"].get(relationship)
        item["year_ranges_by_type"][relationship] = (
            min(start_n, previous[0]) if previous else start_n,
            max(end_n, previous[1]) if previous else end_n,
        )
    else:
        if year not in (None, "", 0, "0"):
            item["years"].add(int(year))
        range_start = min_year if min_year not in (None, "", 0, "0") else year
        range_end = max_year if max_year not in (None, "", 0, "0") else year
        if range_start not in (None, "", 0, "0") and range_end not in (None, "", 0, "0"):
            start_n, end_n = int(range_start), int(range_end)
            item["years"].update((start_n, end_n))
            previous = item["year_ranges_by_type"].get(relationship)
            item["year_ranges_by_type"][relationship] = (
                min(start_n, previous[0]) if previous else start_n,
                max(end_n, previous[1]) if previous else end_n,
            )
    item["amount_by_type"][relationship] += _num(amount)
    item["occurrences"] += int(occurrences or 0)


def _grant_network(
    conn,
    ein: str,
    years: List[Dict],
    connections: Dict[str, Dict],
    contaminated_years: Optional[Set[int]] = None,
) -> Dict:
    excluded_years = sorted({int(year) for year in (contaminated_years or set()) if year})
    outgoing_year_clause = ""
    outgoing_year_params: List[int] = []
    if excluded_years:
        outgoing_year_clause = " AND tax_year NOT IN (" + ",".join("?" for _ in excluded_years) + ")"
        outgoing_year_params = excluded_years
    metrics = {
        "grants_paid": 0.0,
        "grants_received": 0.0,
        "grant_counterparties": 0,
        "grant_match_warnings": 0,
        "grant_years_excluded_data_quality": len(excluded_years),
    }
    spec = _grant_resolution_spec(conn)
    if spec:
        resolved = spec["table"]
        ein_col = spec["ein"]
        name_col = spec["name"]
        trusted = _trusted_grant_identity_sql(
            ein_col=ein_col,
            confidence_col=spec["confidence"],
            match_source_col=spec["match_source"],
        )
        incoming_source = _indexed_incoming_grant_rows_sql(conn, ein)
        metrics["grants_paid"] = _num(conn.execute(
            f"""SELECT SUM(COALESCE(total_amount,0)) FROM {resolved}
                WHERE grantor_ein = ? AND COALESCE(total_amount,0) > 0
                  {outgoing_year_clause}""",
            [ein, *outgoing_year_params],
        ).fetchone()[0])
        if incoming_source:
            incoming_rows_sql, incoming_params = incoming_source
            metrics["grants_received"] = _num(conn.execute(
                f"SELECT SUM(COALESCE(total_amount,0)) FROM ({incoming_rows_sql}) incoming_rows",
                incoming_params,
            ).fetchone()[0])
        metrics["grant_match_warnings"] = int(conn.execute(
            f"""SELECT COUNT(*) FROM {resolved}
                WHERE grantor_ein = ? AND COALESCE(total_amount,0) > 0
                  {outgoing_year_clause}
                  AND (COALESCE(warning_flags,'') <> '' OR COALESCE(match_status,'') IN
                    ('unresolved','conflicting_ein_match','reported_ein_not_found_name_matched'))""",
            [ein, *outgoing_year_params],
        ).fetchone()[0] or 0)
        outgoing_sql = f"""
        SELECT
          TRIM({ein_col}) AS target_ein,
          MAX(COALESCE(NULLIF(TRIM({name_col}),''), NULLIF(TRIM(recipient_reported_name),''))) AS target_name,
          SUM(COALESCE(total_amount,0)) AS amount,
          COUNT(*) AS row_count,
          MIN(tax_year), MAX(tax_year)
        FROM {resolved}
        WHERE grantor_ein = ? AND COALESCE(total_amount,0) > 0 AND {trusted}
          {outgoing_year_clause}
        GROUP BY TRIM({ein_col})
        ORDER BY amount DESC
        LIMIT 60
        """
        outgoing = list(conn.execute(outgoing_sql, [ein, *outgoing_year_params]))
        outgoing_year_amounts: Dict[str, Dict[int, float]] = defaultdict(dict)
        outgoing_eins = [_text(row[0]).strip() for row in outgoing if _text(row[0]).strip()]
        if outgoing_eins:
            placeholders = ",".join("?" for _ in outgoing_eins)
            yearly_sql = f"""
            SELECT TRIM({ein_col}), tax_year, SUM(COALESCE(total_amount,0))
            FROM {resolved}
            WHERE grantor_ein = ? AND COALESCE(total_amount,0) > 0 AND {trusted}
              {outgoing_year_clause}
              AND tax_year IS NOT NULL AND TRIM({ein_col}) IN ({placeholders})
            GROUP BY TRIM({ein_col}), tax_year
            """
            for target_ein, tax_year, year_amount in conn.execute(
                yearly_sql, [ein, *outgoing_year_params, *outgoing_eins]
            ):
                try:
                    outgoing_year_amounts[_text(target_ein).strip()][int(tax_year)] = _num(year_amount)
                except (TypeError, ValueError):
                    continue
        for target_ein, target_name, amount, count, min_year, max_year in outgoing:
            year_text = str(max_year) if min_year == max_year else f"{min_year}-{max_year}"
            _add_connection(
                connections,
                subject_ein=ein,
                ein=target_ein,
                name=target_name,
                relationship="Grant paid",
                evidence=f"{count} grant row(s), {_money(amount)}, {year_text}",
                amount=amount,
                year=max_year,
                min_year=min_year,
                max_year=max_year,
                year_amounts=outgoing_year_amounts.get(_text(target_ein).strip()),
                occurrences=count,
            )

        incoming = []
        incoming_rows_sql = ""
        incoming_params: List = []
        if incoming_source:
            incoming_rows_sql, incoming_params = incoming_source
            incoming_sql = f"""
            SELECT grantor_ein, MAX(grantor_name), SUM(COALESCE(total_amount,0)), COUNT(*),
                   MIN(tax_year), MAX(tax_year)
            FROM ({incoming_rows_sql}) incoming_rows
            GROUP BY grantor_ein
            ORDER BY SUM(COALESCE(total_amount,0)) DESC
            LIMIT 60
            """
            incoming = list(conn.execute(incoming_sql, incoming_params))
        incoming_year_amounts: Dict[str, Dict[int, float]] = defaultdict(dict)
        incoming_eins = [_text(row[0]).strip() for row in incoming if _text(row[0]).strip()]
        if incoming_eins:
            placeholders = ",".join("?" for _ in incoming_eins)
            yearly_sql = f"""
            SELECT grantor_ein, tax_year, SUM(COALESCE(total_amount,0))
            FROM ({incoming_rows_sql}) incoming_rows
            WHERE tax_year IS NOT NULL AND grantor_ein IN ({placeholders})
            GROUP BY grantor_ein, tax_year
            """
            for grantor_ein, tax_year, year_amount in conn.execute(
                yearly_sql, [*incoming_params, *incoming_eins]
            ):
                try:
                    incoming_year_amounts[_text(grantor_ein).strip()][int(tax_year)] = _num(year_amount)
                except (TypeError, ValueError):
                    continue
        for grantor_ein, grantor_name, amount, count, min_year, max_year in incoming:
            year_text = str(max_year) if min_year == max_year else f"{min_year}-{max_year}"
            _add_connection(
                connections,
                subject_ein=ein,
                ein=grantor_ein,
                name=grantor_name,
                relationship="Grant received",
                evidence=f"{count} grant row(s), {_money(amount)}, {year_text}",
                amount=amount,
                year=max_year,
                min_year=min_year,
                max_year=max_year,
                year_amounts=incoming_year_amounts.get(_text(grantor_ein).strip()),
                occurrences=count,
            )
    else:
        grants = _object_ref(conn, "grants_compat_v1")
        if grants:
            filing_ids = [
                row.get("filing_id")
                for row in years
                if row.get("filing_id") and int(row.get("tax_year") or 0) not in excluded_years
            ]
            if filing_ids:
                placeholders = ",".join("?" for _ in filing_ids)
                metrics["grants_paid"] = _num(conn.execute(
                    f"SELECT SUM(COALESCE(cash_amount,0) + COALESCE(noncash_amount,0)) FROM {grants} WHERE filing_id IN ({placeholders})",
                    filing_ids,
                ).fetchone()[0])
                sql = f"""
                SELECT recipient_ein, recipient_name,
                       SUM(COALESCE(cash_amount,0) + COALESCE(noncash_amount,0)), COUNT(*)
                FROM {grants}
                WHERE filing_id IN ({placeholders})
                GROUP BY recipient_ein, recipient_name
                ORDER BY 3 DESC
                LIMIT 60
                """
                for target_ein, target_name, amount, count in conn.execute(sql, filing_ids):
                    _add_connection(
                        connections,
                        subject_ein=ein,
                        ein=target_ein,
                        name=target_name,
                        relationship="Grant paid (reported identity)",
                        evidence=f"{count} grant row(s), {_money(amount)}; recipient identity is not enhanced",
                        amount=amount,
                        occurrences=count,
                    )
    metrics["grant_counterparties"] = sum(
        1 for item in connections.values()
        if any(rel.startswith("Grant ") for rel in item["relationships"])
    )
    return metrics


def _schedule_r_network(conn, ein: str, years: List[Dict], connections: Dict[str, Dict]) -> Dict:
    table = _object_ref(conn, "sched_r_related_orgs_expanded")
    filing_ids = [row.get("filing_id") for row in years if row.get("filing_id")]
    required = {
        "filing_id", "related_ein", "related_name_line1", "related_name_line2",
        "relationship_category", "controlled_organization_ind", "transaction_type_txt", "involved_amt",
    }
    if not table or not filing_ids or not required.issubset(_object_columns(conn, table)):
        return {"schedule_r_edges": 0, "schedule_r_unrelated_partnerships": 0, "schedule_r_amount": 0.0}
    year_for_filing = {row.get("filing_id"): row.get("tax_year") for row in years}
    placeholders = ",".join("?" for _ in filing_ids)
    sql = f"""
    SELECT filing_id, related_ein, related_name_line1, related_name_line2,
           relationship_category, controlled_organization_ind,
           transaction_type_txt, COALESCE(involved_amt,0)
    FROM {table}
    WHERE filing_id IN ({placeholders})
    LIMIT 500
    """
    row_count = 0
    unrelated_count = 0
    amount_total = 0.0
    for filing_id, related_ein, name1, name2, category, controlled, transaction_type, amount in conn.execute(sql, filing_ids):
        name = " ".join(part for part in [_text(name1).strip(), _text(name2).strip()] if part)
        is_unrelated_partnership = _text(category).strip().casefold() == "unrelated taxable partnership"
        evidence_bits = [_text(category)]
        if _yes(controlled):
            evidence_bits.append("controlled organization")
        if transaction_type:
            evidence_bits.append(_text(transaction_type))
        if _num(amount):
            evidence_bits.append(_money(amount))
        _add_connection(
            connections,
            subject_ein=ein,
            ein=related_ein,
            name=name,
            relationship="Schedule R (unrelated partnership)" if is_unrelated_partnership else "Schedule R",
            evidence="; ".join(evidence_bits),
            amount=amount,
            year=year_for_filing.get(filing_id),
            scored_relationship=(
                not is_unrelated_partnership
                and len(re.sub(r"\D", "", _text(related_ein))) == 9
            ),
        )
        if is_unrelated_partnership:
            unrelated_count += 1
        else:
            row_count += 1
            amount_total += _num(amount)
    return {
        "schedule_r_edges": row_count,
        "schedule_r_unrelated_partnerships": unrelated_count,
        "schedule_r_amount": amount_total,
    }


def _shared_people_network(conn, ein: str, connections: Dict[str, Dict]) -> Dict:
    canonical = _object_ref(conn, "canonical_by_ein_year")
    returns = _object_ref(conn, "returns")
    if not canonical or not returns:
        return {"shared_people": 0}
    source_specs = [
        ("officers", "person_name", "officer/director/trustee"),
        ("highest_comp_employees", "person_name", "highly compensated employee"),
        ("former_key_people", "person_name", "former/key employee"),
        ("irs990_ez_officer_director_trustee_empl_grp", "person_nm", "EZ officer/director/trustee/employee"),
        ("irs990_schedule_j_rltd_org_officer_trst_key_empl_grp", "person_nm", "Schedule J officer/trustee/key employee"),
        ("irs990_pf_officer_dir_trst_key_empl_info_grp", "person_nm", "990-PF officer/director/trustee/key employee"),
    ]
    sources = []
    candidates: Dict[str, Dict] = {}
    for table_name, name_col, role_label in source_specs:
        people = _object_ref(conn, table_name)
        if not people or name_col not in _object_columns(conn, people):
            continue
        sources.append((people, name_col, role_label))
        subject_sql = f"""
        SELECT UPPER(p.{name_col}) AS person_key, MAX(p.{name_col}), COUNT(*)
        FROM {people} p
        JOIN {canonical} c ON c.filing_id = p.filing_id
        WHERE c.ein = ? AND COALESCE(TRIM(p.{name_col}),'') <> ''
        GROUP BY UPPER(p.{name_col})
        ORDER BY COUNT(*) DESC
        LIMIT 18
        """
        for raw_key, display_name, _subject_rows in conn.execute(subject_sql, [ein]):
            person_key = _person_key(raw_key)
            if not person_key:
                continue
            candidate = candidates.setdefault(person_key, {
                "display_name": _text(display_name).strip(),
                "query_keys": set(),
                "subject_roles": set(),
            })
            candidate["query_keys"].add(_text(raw_key))
            candidate["subject_roles"].add(role_label)

    shared_keys: Set[str] = set()
    suppressed_hubs = 0
    for person_key, candidate in candidates.items():
        query_keys = sorted(candidate["query_keys"])
        placeholders = ",".join("?" for _ in query_keys)
        other_eins: Set[str] = set()
        for people, name_col, _role_label in sources:
            degree_sql = f"""
            SELECT c.ein
            FROM {people} p
            JOIN {canonical} c ON c.filing_id = p.filing_id
            WHERE UPPER(p.{name_col}) IN ({placeholders}) AND c.ein <> ?
            GROUP BY c.ein
            LIMIT 11
            """
            other_eins.update(
                _text(row[0]) for row in conn.execute(degree_sql, [*query_keys, ein])
            )
            if len(other_eins) > 10:
                break
        if len(other_eins) > 10:
            suppressed_hubs += 1
            continue

        subject_roles = ", ".join(sorted(candidate["subject_roles"]))
        for people, name_col, role_label in sources:
            other_sql = f"""
            SELECT c.ein, MAX(r.org_name), COUNT(DISTINCT c.tax_year), MAX(c.tax_year), MAX(p.{name_col})
            FROM {people} p
            JOIN {canonical} c ON c.filing_id = p.filing_id
            JOIN {returns} r ON r.filing_id = c.filing_id
            WHERE UPPER(p.{name_col}) IN ({placeholders}) AND c.ein <> ?
            GROUP BY c.ein
            ORDER BY MAX(c.tax_year) DESC
            LIMIT 12
            """
            for other_ein, other_name, year_count, max_year, matched_name in conn.execute(
                other_sql, [*query_keys, ein]
            ):
                _add_connection(
                    connections,
                    subject_ein=ein,
                    ein=other_ein,
                    name=other_name,
                    relationship="Shared person name",
                    evidence=(
                        "Name-only candidate; Form 990 does not provide a unique person identifier. "
                        f"{_text(matched_name) or candidate['display_name']} appears for this filer as "
                        f"{subject_roles} and for the other organization as {role_label} "
                        f"({year_count} other filing year(s))."
                    ),
                    year=max_year,
                    occurrences=year_count,
                    scored_relationship=False,
                )
                shared_keys.add(person_key)
    return {
        "shared_people": len(shared_keys),
        "shared_people_sources": len(sources),
        "shared_people_hubs_suppressed": suppressed_hubs,
    }


def _shared_address_network(conn, ein: str, connections: Dict[str, Dict]) -> Dict:
    identities = _object_ref(conn, "org_identity")
    if not identities:
        return {"shared_addresses": 0}
    subject_sql = f"""
    SELECT DISTINCT street_norm, city, state, zip5, street
    FROM {identities}
    WHERE ein = ? AND COALESCE(street_norm,'') <> ''
      AND UPPER(COALESCE(street_norm,'')) NOT LIKE 'PO BOX%'
    ORDER BY source_rank
    LIMIT 8
    """
    shared_keys: Set[str] = set()
    suppressed_hubs = 0
    for street_norm, city, state, zip5, street in conn.execute(subject_sql, [ein]):
        if len(_text(street_norm)) < 6:
            continue
        if zip5:
            where = "street_norm = ? AND zip5 = ?"
            params = [street_norm, zip5, ein]
        else:
            where = "street_norm = ? AND city = ? AND state = ?"
            params = [street_norm, city, state, ein]
        degree_sql = f"""
        SELECT ein
        FROM {identities}
        WHERE {where} AND ein <> ?
        GROUP BY ein
        LIMIT 13
        """
        if len(conn.execute(degree_sql, params).fetchall()) > 12:
            suppressed_hubs += 1
            continue
        match_sql = f"""
        SELECT ein, MAX(display_name), COUNT(DISTINCT identity_key)
        FROM {identities}
        WHERE {where} AND ein <> ?
        GROUP BY ein
        ORDER BY COUNT(DISTINCT identity_key) DESC
        LIMIT 16
        """
        matches = conn.execute(match_sql, params).fetchall()
        address_label = ", ".join(part for part in [_text(street), _text(city), _text(state), _text(zip5)] if part)
        for other_ein, other_name, identity_count in matches:
            _add_connection(
                connections,
                subject_ein=ein,
                ein=other_ein,
                name=other_name,
                relationship="Shared address",
                evidence=f"Exact normalized address: {address_label}",
                occurrences=identity_count,
            )
            shared_keys.add(f"{street_norm}|{zip5 or city}|{state}")
    return {"shared_addresses": len(shared_keys), "shared_address_hubs_suppressed": suppressed_hubs}


def _contractor_network(conn, ein: str, connections: Dict[str, Dict]) -> Dict:
    contractors = _object_ref(conn, "vw_contractors")
    canonical = _object_ref(conn, "canonical_by_ein_year")
    if not contractors or not canonical:
        return {"contractors": 0, "shared_contractors": 0}
    sql = f"""
    SELECT vc.contractor_name, SUM(COALESCE(vc.compensation_amt,0)), COUNT(*),
           MIN(c.tax_year), MAX(c.tax_year)
    FROM {contractors} vc
    JOIN {canonical} c ON c.filing_id = vc.filing_id
    WHERE c.ein = ? AND COALESCE(TRIM(vc.contractor_name),'') <> ''
    GROUP BY vc.contractor_name
    ORDER BY 2 DESC
    LIMIT 20
    """
    vendor_names = []
    for vendor_name, amount, count, min_year, max_year in conn.execute(sql, [ein]):
        vendor_names.append(_text(vendor_name))
        year_text = str(max_year) if min_year == max_year else f"{min_year}-{max_year}"
        _add_connection(
            connections,
            subject_ein=ein,
            name=vendor_name,
            relationship="Contractor",
            evidence=f"{count} payment row(s), {_money(amount)}, {year_text}",
            amount=amount,
            year=max_year,
            occurrences=count,
        )

    raw = _object_ref(conn, "irs990_contractor_compensation_grp")
    returns = _object_ref(conn, "returns")
    shared = 0
    suppressed_hubs = 0
    if raw and returns:
        for vendor_name in vendor_names[:12]:
            degree_sql = f"""
            SELECT c.ein
            FROM {raw} vc
            JOIN {canonical} c ON c.filing_id = vc.filing_id
            WHERE (vc.business_name_line1_txt = ? OR vc.person_nm = ?) AND c.ein <> ?
            GROUP BY c.ein
            LIMIT 11
            """
            if len(conn.execute(degree_sql, [vendor_name, vendor_name, ein]).fetchall()) > 10:
                suppressed_hubs += 1
                continue
            match_sql = f"""
            SELECT c.ein, MAX(r.org_name), COUNT(*), MAX(c.tax_year)
            FROM {raw} vc
            JOIN {canonical} c ON c.filing_id = vc.filing_id
            JOIN {returns} r ON r.filing_id = c.filing_id
            WHERE (vc.business_name_line1_txt = ? OR vc.person_nm = ?) AND c.ein <> ?
            GROUP BY c.ein
            ORDER BY MAX(c.tax_year) DESC
            LIMIT 10
            """
            for other_ein, other_name, rows_count, max_year in conn.execute(match_sql, [vendor_name, vendor_name, ein]):
                _add_connection(
                    connections,
                    subject_ein=ein,
                    ein=other_ein,
                    name=other_name,
                    relationship="Shared contractor",
                    evidence=f"Both organizations reported contractor {_text(vendor_name)}",
                    year=max_year,
                    occurrences=rows_count,
                )
                shared += 1
    return {
        "contractors": len(vendor_names),
        "shared_contractors": shared,
        "shared_contractor_hubs_suppressed": suppressed_hubs,
    }


def _grant_paths(conn, ein: str, connections: Dict[str, Dict]) -> List[Dict]:
    spec = _grant_resolution_spec(conn)
    if not spec:
        return []
    resolved = spec["table"]
    ein_col = spec["ein"]
    name_col = spec["name"]
    first_hops = sorted(
        (
            item for item in connections.values()
            if item.get("ein") and "Grant paid" in item.get("relationships", set())
        ),
        key=lambda item: item["amount_by_type"].get("Grant paid", 0),
        reverse=True,
    )[:8]
    paths = []
    trusted = _trusted_grant_identity_sql(
        ein_col=ein_col,
        confidence_col=spec["confidence"],
        match_source_col=spec["match_source"],
    )
    for first in first_hops:
        first_year_amounts = {
            int(year): _num(amount)
            for year, amount in ((first.get("year_amounts_by_type") or {}).get("Grant paid") or {}).items()
        }
        first_years = sorted(first_year_amounts)
        if not first_years:
            continue
        sql = f"""
        SELECT {ein_col}, MAX({name_col}), SUM(COALESCE(total_amount,0)),
               COUNT(*), MIN(tax_year), MAX(tax_year)
        FROM {resolved}
        WHERE grantor_ein = ? AND COALESCE(total_amount,0) > 0 AND {trusted}
        GROUP BY {ein_col}
        ORDER BY 3 DESC
        LIMIT 4
        """
        second_hops = list(conn.execute(sql, [first["ein"]]))
        second_year_amounts: Dict[str, Dict[int, float]] = defaultdict(dict)
        second_year_rows: Dict[str, Dict[int, int]] = defaultdict(dict)
        second_eins = [_text(row[0]).strip() for row in second_hops if _text(row[0]).strip()]
        if second_eins:
            placeholders = ",".join("?" for _ in second_eins)
            yearly_sql = f"""
            SELECT {ein_col}, tax_year, SUM(COALESCE(total_amount,0)), COUNT(*)
            FROM {resolved}
            WHERE grantor_ein = ? AND COALESCE(total_amount,0) > 0 AND {trusted}
              AND tax_year IS NOT NULL AND {ein_col} IN ({placeholders})
            GROUP BY {ein_col}, tax_year
            """
            for second_ein, tax_year, year_amount, year_rows in conn.execute(
                yearly_sql, [first["ein"], *second_eins]
            ):
                try:
                    year_n = int(tax_year)
                except (TypeError, ValueError):
                    continue
                second_key = _text(second_ein).strip()
                second_year_amounts[second_key][year_n] = _num(year_amount)
                second_year_rows[second_key][year_n] = int(year_rows or 0)

        for second_ein, second_name, amount, count, min_year, max_year in second_hops:
            if not second_ein:
                continue
            second_key = _text(second_ein).strip()
            second_amounts = second_year_amounts.get(second_key) or {}
            second_rows = second_year_rows.get(second_key) or {}
            actual_second_years = sorted(second_amounts)
            qualifying_pairs = [
                (first_year, second_year)
                for first_year in first_years
                for second_year in actual_second_years
                if 0 <= second_year - first_year <= _GRANT_FLOW_WINDOW_YEARS
            ]
            qualifying_first_years = sorted({pair[0] for pair in qualifying_pairs})
            qualifying_second_years = sorted({pair[1] for pair in qualifying_pairs})
            qualifying_amount = sum(second_amounts[year] for year in qualifying_second_years)
            qualifying_rows = sum(second_rows.get(year, 0) for year in qualifying_second_years)
            returns_to_subject = second_key == ein
            chronology_supported = bool(qualifying_pairs)
            paths.append({
                "via_ein": first["ein"],
                "via_name": first.get("name"),
                "target_ein": second_key,
                "target_name": _text(second_name) or f"EIN {second_ein}",
                "amount": qualifying_amount if returns_to_subject and chronology_supported else _num(amount),
                "rows": qualifying_rows if returns_to_subject and chronology_supported else int(count or 0),
                "total_amount": _num(amount),
                "total_rows": int(count or 0),
                "first_years": first_years,
                "second_years": actual_second_years,
                "qualifying_first_years": qualifying_first_years,
                "qualifying_second_years": qualifying_second_years,
                "first_min_year": min(first_years),
                "first_max_year": max(first_years),
                "second_min_year": min(actual_second_years) if actual_second_years else min_year,
                "second_max_year": max(actual_second_years) if actual_second_years else max_year,
                "returns_to_subject": returns_to_subject,
                "chronology_supported": chronology_supported,
            })
    paths.sort(key=lambda p: (
        not (p["returns_to_subject"] and p["chronology_supported"]),
        not p["returns_to_subject"],
        -p["amount"],
    ))
    return paths[:20]


def _connection_rank(item: Dict) -> Tuple:
    weights = {
        "Schedule R": 7,
        "Shared officer": 6,
        "Shared employee/key person": 5,
        "Shared person name": 1,
        "Shared address": 5,
        "Grant paid": 4,
        "Grant received": 4,
        "Shared contractor": 3,
        "Contractor": 1,
        "Grant paid (reported identity)": 2,
    }
    relationship_score = sum(weights.get(rel, 1) for rel in item.get("relationships", set()))
    total_amount = sum(item.get("amount_by_type", {}).values())
    return relationship_score, len(item.get("relationships", set())), math.log10(max(total_amount, 1)), item.get("occurrences", 0)


def _connection_scored_relationships(item: Dict) -> Set[str]:
    if "scored_relationships" in item:
        return item.get("scored_relationships") or set()
    return item.get("relationships") or set()


def _build_network(conn, ein: str, years: List[Dict]) -> Dict:
    connections: Dict[str, Dict] = {}
    metrics = {}
    grant_consistency = _grant_detail_consistency(conn, years)
    contaminated_years = {
        int(year) for year, summary in grant_consistency.items() if summary.get("inflated")
    }
    metrics.update(_grant_network(conn, ein, years, connections, contaminated_years))
    metrics.update(_schedule_r_network(conn, ein, years, connections))
    metrics.update(_shared_people_network(conn, ein, connections))
    metrics.update(_shared_address_network(conn, ein, connections))
    metrics.update(_contractor_network(conn, ein, connections))
    paths = _grant_paths(conn, ein, connections)
    ranked = sorted(connections.values(), key=_connection_rank, reverse=True)
    metrics["connected_entities"] = len(ranked)
    metrics["multi_signal_entities"] = sum(
        1 for item in ranked if len(_connection_scored_relationships(item)) >= 2
    )
    return {
        "connections": ranked[:50],
        "paths": paths,
        "metrics": metrics,
        "truncated": len(ranked) > 50,
    }


def _year_values_text(years) -> str:
    values = sorted({int(year) for year in (years or [])})
    if not values:
        return "unknown years"
    if len(values) <= 6:
        return ", ".join(str(year) for year in values)
    return ", ".join(str(year) for year in values[:3]) + ", …, " + ", ".join(
        str(year) for year in values[-2:]
    )


def _network_indicators(network: Dict, latest_year) -> List[Dict]:
    indicators = []
    connections = network.get("connections") or []
    reciprocal = []
    for item in connections:
        if not {"Grant paid", "Grant received"}.issubset(item.get("relationships", set())):
            continue
        by_type = item.get("year_amounts_by_type") or {}
        paid_by_year = by_type.get("Grant paid") or {}
        received_by_year = by_type.get("Grant received") or {}
        qualifying_pairs = [
            (int(paid_year), int(received_year))
            for paid_year in paid_by_year
            for received_year in received_by_year
            if abs(int(paid_year) - int(received_year)) <= _GRANT_FLOW_WINDOW_YEARS
        ]
        if not qualifying_pairs:
            continue
        paid_years = sorted({pair[0] for pair in qualifying_pairs})
        received_years = sorted({pair[1] for pair in qualifying_pairs})
        reciprocal.append({
            "item": item,
            "paid_years": paid_years,
            "received_years": received_years,
            "paid": sum(_num(paid_by_year[year]) for year in paid_years),
            "received": sum(_num(received_by_year[year]) for year in received_years),
        })
    for match in reciprocal[:3]:
        item = match["item"]
        paid = match["paid"]
        received = match["received"]
        severity = "High" if min(paid, received) >= 100_000 else "Medium"
        indicators.append(_indicator(
            severity,
            "Network",
            "Reciprocal grant relationship",
            latest_year,
            f"{item.get('name')} ({item.get('ein') or 'no EIN'}) appears in both directions within two years: paid {_money(paid)} in {_year_values_text(match['paid_years'])} and received {_money(received)} in {_year_values_text(match['received_years'])}.",
            "Reciprocal funding can be legitimate, but it is a concrete circular-flow lead that warrants purpose and timing review.",
            "Compare grant purposes, dates, decision-makers, related-party status, and whether funds returned directly or through intermediaries.",
        ))

    overlaps = []
    financial = {"Grant paid", "Grant received", "Contractor"}
    identity = {
        "Schedule R", "Shared officer", "Shared employee/key person",
        "Shared person name", "Shared address",
    }
    for item in connections:
        strong_relationships = item.get("scored_relationships", item.get("relationships", set()))
        if item["relationships"] & financial and strong_relationships & identity:
            overlaps.append(item)
    for item in overlaps[:4]:
        indicators.append(_indicator(
            "Medium",
            "Network",
            "Financial flow overlaps an identity or control link",
            latest_year,
            f"{item.get('name')} is linked by {', '.join(sorted(item['relationships']))}.",
            "Multiple independent relationship types provide stronger related-party evidence than a name-only match.",
            "Review the underlying filings, amounts, roles, addresses, approvals, and conflict-of-interest disclosures.",
        ))

    circular_paths = [
        path for path in (network.get("paths") or [])
        if path.get("returns_to_subject") and path.get("chronology_supported")
    ]
    if circular_paths:
        top = circular_paths[0]
        indicators.append(_indicator(
            "High",
            "Network",
            "Chronologically plausible two-step grant return",
            latest_year,
            f"The filer paid {top.get('via_name')} in {_year_values_text(top.get('qualifying_first_years'))}; that intermediary reported {_money(top.get('amount'))} back to the filer in {_year_values_text(top.get('qualifying_second_years'))} across {top.get('rows')} qualifying row(s).",
            "The ordering makes this an actionable circular-funding lead, although it does not establish that the same dollars returned.",
            "Inspect every grant row in the path, purposes, dates, restrictions, and governing-person overlap before drawing a conclusion.",
        ))
    return indicators


def _sort_indicators(indicators: List[Dict]) -> List[Dict]:
    order = {"High": 0, "Medium": 1, "Low": 2}
    indicators.sort(key=lambda item: (
        order.get(item.get("severity"), 9),
        -(int(item.get("tax_year") or 0)),
        item.get("category") or "",
        item.get("title") or "",
    ))
    return indicators


def _single_audit_threshold(fiscal_year_start) -> Optional[int]:
    """Return the Uniform Guidance threshold when a fiscal start date is known."""
    value = _text(fiscal_year_start).strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    return 1_000_000 if value >= "2024-10-01" else 750_000


def _fac_report_issue_labels(report: Dict) -> Tuple[List[str], List[str]]:
    general = report.get("general") or {}
    high = []
    medium = []
    general_flags = [
        ("is_going_concern_included", "going-concern disclosure", "high"),
        ("is_internal_control_material_weakness_disclosed", "internal-control material weakness", "high"),
        ("is_material_noncompliance_disclosed", "material noncompliance", "high"),
        ("is_internal_control_deficiency_disclosed", "significant internal-control deficiency", "medium"),
    ]
    for field, label, level in general_flags:
        if general.get(field) is True:
            (high if level == "high" else medium).append(label)
    for finding in report.get("findings") or []:
        for field, label, level in [
            ("is_modified_opinion", "modified opinion", "high"),
            ("is_material_weakness", "finding with material weakness", "high"),
            ("is_questioned_costs", "questioned costs", "medium"),
            ("is_significant_deficiency", "finding with significant deficiency", "medium"),
            ("is_repeat_finding", "repeat finding", "medium"),
        ]:
            if finding.get(field) is True:
                target = high if level == "high" else medium
                if label not in target:
                    target.append(label)
    return high, medium


def _external_indicators(external: Dict, years: List[Dict]) -> List[Dict]:
    indicators: List[Dict] = []
    fac = external.get("fac") or {}
    if fac.get("status") == "ok":
        for report in fac.get("reports") or []:
            general = report.get("general") or {}
            high, medium = _fac_report_issue_labels(report)
            issues = high + medium
            if not issues:
                continue
            severity = "High" if high else "Medium"
            audit_year = general.get("audit_year") or _text(general.get("fy_end_date"))[:4]
            match_label = "primary EIN" if report.get("ein_match") == "primary_ein" else "additional EIN"
            indicators.append(_indicator(
                severity,
                "Federal Audit",
                "Federal Single Audit issues reported",
                audit_year,
                f"FAC report {general.get('report_id') or report.get('report_id')} matched the {match_label}; "
                f"federal awards expended were {_money(general.get('total_amount_expended')) or 'not stated'}; "
                f"reported issues: {', '.join(issues)}.",
                "FAC findings are direct audit evidence, but their scope, remediation status, and entity coverage require reading the report and corrective-action plan.",
                "Review finding text, questioned-cost support, repeat-finding references, corrective actions, and the linked federal programs.",
            ))
    elif fac.get("status") == "no_match":
        for row in years:
            threshold = _single_audit_threshold(row.get("period_start"))
            government_grants = _num(row.get("government_grants"))
            if threshold and government_grants >= threshold:
                indicators.append(_indicator(
                    "Low",
                    "External Coverage",
                    "No FAC match despite large Form 990 government-grant revenue",
                    row.get("tax_year"),
                    f"Form 990 government-grant revenue was {_money(government_grants)}; the audit threshold for a fiscal year beginning {row.get('period_start')} was {_money(threshold)}. FAC returned no EIN match.",
                    "This is a coverage lead only: Form 990 government-grant revenue is not the same measure as federal awards expended, and the organization may not have crossed the audit threshold.",
                    "Confirm federal award expenditures and alternate/additional EINs before treating the missing FAC match as a filing issue.",
                ))
                break

    sam = external.get("sam") or {}
    exclusions = sam.get("exclusions") or []
    if sam.get("status") == "ok" and exclusions:
        ueis = ", ".join(sam.get("queried_ueis") or [])
        indicators.append(_indicator(
            "High",
            "Federal Exclusions",
            "Active SAM exclusion record returned for a FAC-linked UEI",
            (years[0] if years else {}).get("tax_year"),
            f"SAM returned {len(exclusions)} exclusion record(s) for exact FAC-linked UEI(s) {ueis}.",
            "The SAM exclusions API returns active records, and an exact UEI bridge is strong identity evidence; applicability can still be limited to particular programs, agencies, or dates.",
            "Verify the exclusion record's active dates, classification, excluding agency, and applicability in SAM.gov.",
        ))

    fec = external.get("fec") or {}
    if fec.get("status") == "ok" and fec.get("candidates"):
        indicators.append(_indicator(
            "Low",
            "External Lead",
            "FEC committee name candidates require verification",
            (years[0] if years else {}).get("tax_year"),
            f"FEC committee search returned {len(fec.get('candidates') or [])} name/state candidate(s); FEC records do not provide a dependable EIN join.",
            "A name candidate can be a namesake, affiliated committee, or unrelated entity and is not a reporting inconsistency by itself.",
            "Verify committee IDs, addresses, treasurer, affiliation, dates, and source filings before linking activity to the nonprofit.",
        ))

    lda = external.get("lda") or {}
    if lda.get("status") == "ok" and lda.get("clients"):
        filing_count = sum(len(client.get("filings") or []) for client in lda.get("clients") or [])
        indicators.append(_indicator(
            "Low",
            "External Lead",
            "Federal lobbying client match",
            (years[0] if years else {}).get("tax_year"),
            f"The LDA search returned {len(lda.get('clients') or [])} exact/strong client match(es) with {filing_count} recent filing(s).",
            "A nonprofit may lawfully be a lobbying client even when a lobbying firm is the registrant; name matching and reporting periods still need verification.",
            "Compare client, registrant, issue areas, filing periods, and amounts with Schedule C lobbying disclosures.",
        ))
    return indicators


def _recent_people_for_screening(conn, ein: str, limit: int = 8) -> List[Dict]:
    """Return a small exact-name set of recent filer-reported principals."""
    canonical = _object_ref(conn, "canonical_by_ein_year")
    if not canonical:
        return []
    source_specs = [
        ("officers", "person_name", "Form 990 officer/director/trustee"),
        ("highest_comp_employees", "person_name", "highly compensated employee"),
        ("former_key_people", "person_name", "former/key employee"),
        ("irs990_ez_officer_director_trustee_empl_grp", "person_nm", "Form 990-EZ officer/director/trustee"),
        ("irs990_pf_officer_dir_trst_key_empl_info_grp", "person_nm", "Form 990-PF officer/director/trustee"),
    ]
    by_name: Dict[str, Dict] = {}
    for table_name, name_col, role in source_specs:
        table = _object_ref(conn, table_name)
        if not table or name_col not in _object_columns(conn, table):
            continue
        sql = f"""
        SELECT MAX(p.{name_col}), MAX(c.tax_year), COUNT(*)
        FROM {table} p
        JOIN {canonical} c ON c.filing_id = p.filing_id
        WHERE c.ein = ? AND COALESCE(TRIM(p.{name_col}), '') <> ''
        GROUP BY UPPER(TRIM(p.{name_col}))
        ORDER BY MAX(c.tax_year) DESC, COUNT(*) DESC
        LIMIT ?
        """
        try:
            rows = conn.execute(sql, [ein, max(1, min(limit, 12))]).fetchall()
        except Exception:
            continue
        for name, tax_year, occurrences in rows:
            normalized = re.sub(r"[^A-Z0-9]", "", _text(name).upper())
            if len(normalized) < 4:
                continue
            current = by_name.get(normalized)
            candidate = {
                "name": _text(name).strip(),
                "role": role,
                "tax_year": tax_year,
                "occurrences": int(occurrences or 0),
            }
            if current is None or int(tax_year or 0) > int(current.get("tax_year") or 0):
                by_name[normalized] = candidate
    return sorted(
        by_name.values(),
        key=lambda item: (int(item.get("tax_year") or 0), item.get("occurrences") or 0),
        reverse=True,
    )[: max(1, min(limit, 12))]


def _load_public_screening(conn, ein: str, years: List[Dict]) -> Dict:
    latest = years[0] if years else {}
    try:
        irs = lookup_irs_status(ein)
    except Exception:
        irs = {"available": False, "results": [], "coverage": [], "error": "IRS screening lookup failed."}
    try:
        organization = lookup_name_candidates(
            latest.get("org_name") or "",
            city=latest.get("city") or "",
            region=latest.get("state") or "",
            country="US",
            entity_type="organization",
            limit=12,
        )
    except Exception:
        organization = {"available": False, "results": [], "coverage": [], "error": "Organization screening lookup failed."}

    people = []
    if organization.get("available"):
        for person in _recent_people_for_screening(conn, ein):
            try:
                result = lookup_name_candidates(
                    person.get("name") or "",
                    entity_type="individual",
                    limit=6,
                )
            except Exception:
                result = {"available": False, "results": [], "coverage": [], "error": "Person screening lookup failed."}
            if result.get("results"):
                people.append({"subject": person, "lookup": result})
    return {
        "available": bool(irs.get("available") or organization.get("available")),
        "irs": irs,
        "organization": organization,
        "people": people,
    }


def _screening_coverage_keys(screening: Dict) -> Set[str]:
    keys: Set[str] = set()
    lookups = [screening.get("irs") or {}, screening.get("organization") or {}]
    for item in screening.get("people") or []:
        lookups.append(item.get("lookup") or {})
    for lookup in lookups:
        for source in lookup.get("coverage") or []:
            if source.get("dataset_key"):
                keys.add(_text(source.get("dataset_key")))
    return keys


def _screening_indicators(screening: Dict, years: List[Dict], bmf: Optional[Dict] = None) -> List[Dict]:
    indicators: List[Dict] = []
    latest_year = (years[0] if years else {}).get("tax_year")
    irs_rows = (screening.get("irs") or {}).get("results") or []
    coverage_keys = _screening_coverage_keys(screening)
    pub78_covered = "irs_pub78" in coverage_keys
    pub78 = [row for row in irs_rows if row.get("dataset_key") == "irs_pub78"]
    active_revocations = [
        row for row in irs_rows
        if row.get("dataset_key") == "irs_auto_revocation"
        and row.get("status") == "automatically_revoked"
        and not row.get("reinstatement_date")
    ]
    if active_revocations:
        latest = sorted(active_revocations, key=lambda row: _text(row.get("status_date")), reverse=True)[0]
        bmf_current = bool((bmf or {}).get("matched") and _text((bmf or {}).get("status")) == "01")
        if pub78 or bmf_current:
            severity = "Medium"
            category = "External Compliance"
            title = "IRS automatic-revocation status requires review"
            current_sources = []
            if pub78:
                current_sources.append("the current Pub. 78 eligibility snapshot")
            if bmf_current:
                current_sources.append("the EO BMF unconditional-exemption status")
            evidence = (
                f"The exact EIN appears in {' and '.join(current_sources)} and also in an automatic-revocation record "
                f"dated {latest.get('status_date') or 'unknown'} without a reinstatement date in that row."
            )
            why = "The official snapshots conflict or reflect different effective dates; this requires source-level status verification, not an automatic adverse conclusion."
        elif pub78_covered:
            severity = "High"
            category = "External Compliance"
            title = "IRS automatic-revocation status requires review"
            evidence = (
                f"The exact EIN has {len(active_revocations)} automatic-revocation record(s); the latest revocation date is "
                f"{latest.get('status_date') or 'unknown'}, no reinstatement date is shown, and the EIN is absent from the loaded Pub. 78 snapshot."
            )
            why = "An exact-EIN IRS automatic-revocation record without reinstatement is direct tax-status evidence, although monthly snapshot timing and later IRS action must still be checked."
        else:
            severity = "Low"
            category = "External Coverage"
            title = "IRS revocation history found; current Pub. 78 coverage unavailable"
            evidence = (
                f"The exact EIN has {len(active_revocations)} automatic-revocation record(s), latest dated "
                f"{latest.get('status_date') or 'unknown'}, but this partial sidecar does not include Pub. 78 and no current EO BMF status resolves the history."
            )
            why = "The revocation row is real historical evidence, but incomplete current-status coverage cannot support an adverse current-status inference."
        indicators.append(_indicator(
            severity,
            category,
            title,
            latest_year,
            evidence,
            why,
            "Open IRS Tax Exempt Organization Search, confirm the current determination/reinstatement record, effective dates, and whether deductible contributions are currently permitted.",
        ))

    candidate_sets = []
    org_lookup = screening.get("organization") or {}
    if org_lookup.get("results"):
        candidate_sets.append(("organization", org_lookup.get("query", {}).get("name"), org_lookup.get("results") or []))
    for person_item in screening.get("people") or []:
        subject = person_item.get("subject") or {}
        results = (person_item.get("lookup") or {}).get("results") or []
        if results:
            candidate_sets.append((subject.get("role") or "person", subject.get("name"), results))
    if candidate_sets:
        total = sum(len(rows) for _kind, _name, rows in candidate_sets)
        exact_locations = sum(
            1
            for _kind, _name, rows in candidate_sets
            for row in rows
            if (row.get("location_evidence") or {}).get("kind") == "exact"
        )
        datasets = sorted({
            _text(row.get("dataset_key"))
            for _kind, _name, rows in candidate_sets
            for row in rows
            if row.get("dataset_key")
        })
        indicators.append(_indicator(
            "Low",
            "External Lead",
            "OFAC or HHS exclusion name candidates require identity verification",
            latest_year,
            f"Exact conservative name matching returned {total} candidate(s) across {', '.join(datasets)}; {exact_locations} also matched all requested organization location fields.",
            "Even an exact normalized name is not identity proof. Common names, aliases, historical addresses, and list scope can produce false matches; HHS-OIG specifically requires online EIN/SSN verification.",
            "Compare source identifiers, aliases, addresses, dates, program scope, and the original official record before treating any candidate as a match.",
        ))
    return indicators


def _load_indexed_network(ein: str, years: List[Dict]) -> Dict:
    path = risk_network_path()
    base = {"available": False, "path": str(path), "outgoing": [], "incoming": [], "shared_neighbors": [], "sources": [], "build": {}}
    try:
        if not risk_network_available():
            base["error"] = "Indexed network sidecar is not installed or has not completed a build."
            return base
        tax_years = [int(row.get("tax_year")) for row in years if row.get("tax_year") not in (None, "")]
        data = network_for_ein(
            path,
            ein,
            min_tax_year=min(tax_years) if tax_years else None,
            max_tax_year=max(tax_years) if tax_years else None,
            outgoing_limit=500,
            incoming_limit=500,
            shared_target_limit=100,
            shared_edge_limit=500,
        )
        data["available"] = True
        data["path"] = str(path)
        covered_years = set((data.get("coverage") or {}).get("covered_tax_years") or [])
        requested_years = set(tax_years)
        build_scope = _text((data.get("coverage") or {}).get("build_scope"))
        data["coverage_complete"] = (
            build_scope in {"full", "full_plus_incremental"}
            and bool(requested_years)
            and requested_years.issubset(covered_years)
        )
        return data
    except Exception:
        base["error"] = "Indexed network sidecar could not be read."
        return base


def _build_local_analysis(years: List[Dict]) -> Dict:
    conn = connect_ro()
    try:
        _attach_optional_sidecar(conn)
        ein = _text(years[0].get("ein")) if years else ""
        bmf = _load_bmf_profile(conn, ein, years) if ein else {"available": False, "matched": False}
        governance = _load_governance_xml(ein, years) if ein else {"available": False, "records": []}
        network = _build_network(conn, ein, years) if ein else {"connections": [], "paths": [], "metrics": {}}
        indexed_network = _load_indexed_network(ein, years) if ein else {"available": False}
        network["indexed"] = indexed_network
        screening = _load_public_screening(conn, ein, years) if ein else {"available": False}
        indicators = []
        indicators.extend(_financial_indicators(years, bmf))
        indicators.extend(_filing_status_indicators(years))
        indicators.extend(_compensation_indicators(conn, years))
        indicators.extend(_grant_indicators(conn, years))
        indicators.extend(_grant_identity_indicators(conn, years))
        indicators.extend(_contractor_indicators(conn, years))
        indicators.extend(_related_org_indicators(conn, years))
        indicators.extend(_schedule_c_indicators(conn, years, bmf))
        indicators.extend(_schedule_l_indicators(conn, years))
        indicators.extend(_bmf_indicators(bmf, years))
        indicators.extend(_governance_indicators(governance, years))
        indicators.extend(_network_indicators(network, (years[0] if years else {}).get("tax_year")))
        indicators.extend(_screening_indicators(screening, years, bmf))
        _sort_indicators(indicators)
        return {
            "indicators": indicators,
            "bmf": bmf,
            "governance": governance,
            "network": network,
            "screening": screening,
        }
    finally:
        conn.close()


def _build_indicators(years: List[Dict]) -> List[Dict]:
    return _build_local_analysis(years)["indicators"]


def _improvement_notes(screening: Dict, external: Dict, indexed_network: Dict) -> List[Dict]:
    items: List[Dict] = []
    screening_keys = _screening_coverage_keys(screening)
    expected_screening = {"irs_pub78", "irs_auto_revocation", "ofac_sdn", "ofac_consolidated", "hhs_leie"}
    if screening_keys:
        coverage = (screening.get("irs") or {}).get("coverage") or (screening.get("organization") or {}).get("coverage") or []
        newest = max((_text(row.get("source_date") or row.get("retrieved_at")) for row in coverage), default="")
        labels = {
            "irs_pub78": "IRS Pub. 78",
            "irs_auto_revocation": "IRS automatic revocations",
            "ofac_sdn": "OFAC SDN",
            "ofac_consolidated": "OFAC consolidated",
            "hhs_leie": "HHS-OIG LEIE",
        }
        installed = ", ".join(labels[key] for key in sorted(screening_keys) if key in labels)
        missing = ", ".join(labels[key] for key in sorted(expected_screening - screening_keys) if key in labels)
        items.append({
            "title": "Public screening snapshot coverage",
            "body": f"Installed: {installed}{(' (latest source/retrieval ' + newest + ')') if newest else ''}. {('Still missing: ' + missing + '. ') if missing else ''}Refresh complete source groups on schedule and continue to verify every sanctions/exclusion name candidate manually.",
        })
    else:
        items.append({
            "title": "Build public screening snapshots",
            "body": "Run build_screening_sidecar.py --download to install the no-key IRS Pub. 78, automatic-revocation, OFAC, and HHS-OIG LEIE datasets. The dashboard will then perform exact-EIN IRS checks and conservative candidate retrieval.",
        })

    fac = external.get("fac") or {}
    if fac.get("offline_source_as_of_date") or fac.get("offline_coverage"):
        items.append({
            "title": "Integrated Federal Audit Clearinghouse cache",
            "body": "The local FAC audit sidecar is active and augments live results with indexed current and historical audit records. Refresh it when GSA/Census publishes a new release.",
        })
    else:
        items.append({
            "title": "Build the Federal Audit Clearinghouse cache",
            "body": "Import the no-key 2016-present FAC releases and 1998-2015 Census archive into db/fac_audits.db for fast, complete audit history even when the live API is unavailable.",
        })

    if indexed_network.get("available") and indexed_network.get("coverage_complete"):
        built_at = (indexed_network.get("build") or {}).get("completed_at") or (indexed_network.get("build") or {}).get("built_at") or ""
        items.append({
            "title": "Indexed relationship network active",
            "body": f"The precomputed grant, Schedule R, people, address, and contractor edge cache is available{(' (built ' + _text(built_at) + ')') if built_at else ''}. Schedule incremental refreshes after IRS/grant imports and periodically reassess hub thresholds.",
        })
    elif indexed_network.get("available"):
        coverage = indexed_network.get("coverage") or {}
        items.append({
            "title": "Indexed relationship network is only partially built",
            "body": f"The sidecar is valid but does not cover every displayed filing year for this EIN (covered years: {', '.join(str(year) for year in coverage.get('covered_tax_years') or []) or 'none'}; build scope: {coverage.get('build_scope') or 'unknown'}). Keep the bounded on-demand graph authoritative until a full or appropriately scoped refresh completes.",
        })
    else:
        items.append({
            "title": "Build the full indexed relationship network",
            "body": "The bounded on-demand graph remains active. Run the separately documented risk-network build during a maintenance window to enable indexed incoming relationships, shared-target hub suppression, and faster global network work.",
        })

    key_gaps = []
    if (external.get("sam") or {}).get("status") == "not_configured":
        key_gaps.append("a personal SAM.gov key")
    if any((external.get(key) or {}).get("credential") == "shared_demo_key" for key in ("fac", "fec")):
        key_gaps.append("a personal Data.gov key to replace DEMO_KEY")
    if key_gaps:
        items.append({
            "title": "Personal API credentials still recommended",
            "body": "Configure " + " and ".join(key_gaps) + ". The application already degrades source-by-source and never sends keys in rendered request URLs.",
        })

    items.append({
        "title": "Jurisdiction-specific registries remain",
        "body": "State campaign-finance, lobbying, charity-regulator, and corporate-registration coverage still needs source-by-source adapters, refresh schedules, record identifiers, and confidence-reviewed entity matching. Federal FEC/LDA coverage is already wired.",
    })
    return items


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
    external_mode = "live" if (form or {}).get("external_mode") == "live" else "local"
    if (form or {}).get("_action") == "search_org":
        return {
            "search_query": name,
            "search_results": deep._search_orgs_by_name(name),
            "external_mode": external_mode,
            "years": [],
        }

    ein = _parse_ein(form)
    if not ein:
        if name:
            return {
                "search_query": name,
                "search_results": deep._search_orgs_by_name(name),
                "external_mode": external_mode,
                "years": [],
            }
        return {"error": "Enter exactly one valid 9-digit EIN, or search by organization name.", "years": []}

    years = _core_years(ein)
    if not years:
        return {"error": f"No canonical filings found for EIN {ein}.", "years": []}

    local = _build_local_analysis(years)
    external = fetch_external_checks(
        ein,
        years[0].get("org_name") or "",
        years[0].get("state") or "",
        mode=external_mode,
    )
    indicators = list(local["indicators"])
    indicators.extend(_external_indicators(external, years))
    _sort_indicators(indicators)
    counts = _severity_counts(indicators)
    return {
        "ein": ein,
        "org_name": years[0].get("org_name"),
        "years": years,
        "indicators": indicators,
        "bmf": local.get("bmf") or {},
        "governance": local.get("governance") or {},
        "network": local.get("network") or {},
        "screening": local.get("screening") or {},
        "external": external,
        "external_mode": external_mode,
        "counts": counts,
        "risk_score": _risk_score(indicators),
        "improvements": _improvement_notes(
            local.get("screening") or {},
            external,
            (local.get("network") or {}).get("indexed") or {},
        ),
    }


def _cache_key(form) -> str:
    external_mode = "live" if (form or {}).get("external_mode") == "live" else "local"
    action = _text((form or {}).get("_action")).strip() or "analyze"
    name = _org_name_search(form).casefold()
    ein = "".join(ch for ch in ((form or {}).get("ein", "") or (form or {}).get("ein_list", "")) if ch.isdigit())
    if action == "search_org":
        return f"action:search|name:{name}|ein:{ein}|external:{external_mode}"
    if ein:
        return f"action:analyze|ein:{ein}|external:{external_mode}"
    return f"action:search|name:{name}|external:{external_mode}"


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
    external_mode = report.get("external_mode") or "local"
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
                <form method="post" action="/query/fraud_risk_dashboard" style="margin:0;">
                  <input type="hidden" name="qkey" value="fraud_risk_dashboard">
                  <input type="hidden" name="org_search" value="{_h(query)}">
                  <input type="hidden" name="ein" value="{_h(item.get("ein"))}">
                  <input type="hidden" name="external_mode" value="{_h(external_mode)}">
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


def _bmf_panel(bmf: Dict) -> str:
    if not bmf.get("available"):
        content = """
        <div class="source-state source-unavailable"><b>EO BMF sidecar unavailable.</b>
        Configure or rebuild <code>IRS_GRANT_WORK_DB_PATH</code> to enable the loaded IRS snapshot check.</div>
        """
    elif not bmf.get("matched"):
        content = """
        <div class="source-state source-review"><b>No exact EIN was found in the loaded EO BMF snapshot.</b>
        This is an unknown result, not proof of revocation; churches, governmental entities, and other exceptions may not appear.</div>
        """
    else:
        extra = bmf.get("extra") or {}
        fields = [
            ("IRS status", f"{bmf.get('status_label')} (code {bmf.get('status') or 'blank'})"),
            ("Exempt subsection", bmf.get("subsection_label")),
            ("Foundation classification", bmf.get("foundation_label")),
            ("Deductibility", bmf.get("deductibility_label")),
            ("NTEE", bmf.get("ntee_cd")),
            ("Latest BMF tax period", bmf.get("tax_period_label")),
            ("BMF financial snapshot", f"Assets {_money(bmf.get('asset_amt'))}; income {_money(bmf.get('income_amt'))}; revenue {_money(bmf.get('revenue_amt'))}"),
            ("BMF address", bmf.get("bmf_address")),
            ("Latest filing address", bmf.get("filing_address")),
            ("Affiliation / filing requirement", f"Affiliation {extra.get('affiliation') or '—'}; filing requirement {extra.get('filing_req_cd') or '—'}"),
            ("Source", bmf.get("source_detail")),
        ]
        rows = []
        for label, value in fields:
            label_html = _h(label)
            if label == "NTEE":
                label_html += (
                    ' <a class="info-link" href="https://www.irs.gov/instructions/i1023ez" '
                    'target="_blank" rel="noopener" aria-label="Open the IRS NTEE code list" '
                    'title="NTEE is a descriptive mission and purpose classification. It can be imprecise and is not itself a risk finding.">i</a>'
                )
            value_html = _h(value) if value not in (None, "") else '<span class="muted">Not reported</span>'
            rows.append(f"<tr><th>{label_html}</th><td>{value_html}</td></tr>")
        rows = "".join(rows)
        content = f'<table class="risk-table risk-kv"><tbody>{rows}</tbody></table>'
    return f"""
    <section class="risk-panel">
      <div class="panel-title-row">
        <h3>IRS EO BMF Status Snapshot</h3>
        <a href="https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf" target="_blank" rel="noopener">IRS source guide</a>
      </div>
      {content}
      <p class="panel-footnote">The BMF is a current monthly snapshot. NTEE is a descriptive mission/purpose classification and is not itself a risk finding. Pub. 78 eligibility and automatic-revocation history are separate IRS datasets and are not inferred here.</p>
    </section>
    """


def _governance_coverage_text(governance: Dict) -> str:
    records = governance.get("records") or []
    requested_value = governance.get("requested")
    requested = int(_num(requested_value if requested_value is not None else len(records)))
    if requested <= 0:
        return "No canonical filings were requested for XML governance review."

    details = []
    for key, singular, plural in (
        ("missing", "missing source", "missing sources"),
        ("quarantined", "quarantined source", "quarantined sources"),
        ("parse_errors", "parse error", "parse errors"),
        ("empty", "filing with no supported fields", "filings with no supported fields"),
    ):
        count = int(_num(governance.get(key)))
        if count:
            details.append(f"{count} {singular if count == 1 else plural}")

    text = f"{len(records)} of {requested} requested canonical filing(s) yielded supported governance fields"
    if details:
        text += "; " + ", ".join(details)
    return text + "."


def _governance_panel(governance: Dict) -> str:
    records = governance.get("records") or []
    if not governance.get("available") or not records:
        if governance.get("reason") == "xml_inventory_unavailable":
            detail = "Configure the XML source inventory and <code>IRS_XML_ROOT</code> to enable filing-control checks."
        else:
            detail = "No requested filing yielded supported governance fields; review the coverage detail below."
        content = f"""
        <div class="source-state source-unavailable"><b>Governance XML unavailable.</b>
        {detail}</div>
        """
    else:
        latest = records[0]

        def answer(value) -> str:
            if value is True:
                return "Yes"
            if value is False:
                return "No"
            return "Not reported"

        members = latest.get("voting_members")
        independent = latest.get("independent_members")
        fields = [
            ("Tax year", latest.get("tax_year")),
            ("Voting / independent members", f"{members if members is not None else '—'} / {independent if independent is not None else '—'}"),
            ("Family or business relationships", answer(latest.get("family_business_relationships"))),
            ("Material diversion or misuse", answer(latest.get("material_diversion"))),
            ("Governing-body / committee minutes", f"{answer(latest.get('minutes_governing_body'))} / {answer(latest.get('minutes_committees'))}"),
            ("Conflict / whistleblower / retention policies", f"{answer(latest.get('conflict_policy'))} / {answer(latest.get('whistleblower_policy'))} / {answer(latest.get('document_retention_policy'))}"),
            ("Form 990 provided to governing body", answer(latest.get("form990_provided_to_board"))),
            ("Financial statements audited", answer(latest.get("financial_statements_audited"))),
            ("Federal grant audit required / performed", f"{answer(latest.get('federal_grant_audit_required'))} / {answer(latest.get('federal_grant_audit_performed'))}"),
        ]
        rows = "".join(
            f"<tr><th>{_h(label)}</th><td>{_h(value)}</td></tr>"
            for label, value in fields
        )
        content = f'<table class="risk-table risk-kv"><tbody>{rows}</tbody></table>'
    return f"""
    <section class="risk-panel">
      <div class="panel-title-row">
        <h3>Governance &amp; Filing Controls</h3>
        <span class="source-badge">Local Form 990 XML</span>
      </div>
      {content}
      <p class="panel-footnote"><b>XML coverage:</b> {_h(_governance_coverage_text(governance))}</p>
      <p class="panel-footnote">These are filer-reported controls. A “No” response is a review lead, not by itself misconduct or a legal violation. Up to eight recent canonical filings are read from the local XML inventory.</p>
    </section>
    """


_SCREENING_SOURCE_URLS = {
    "irs_pub78": "https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-bulk-data-downloads",
    "irs_auto_revocation": "https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-bulk-data-downloads",
    "ofac_sdn": "https://ofac.treasury.gov/specially-designated-nationals-list-data-formats-data-schemas",
    "ofac_consolidated": "https://ofac.treasury.gov/consolidated-sanctions-list-non-sdn-lists",
    "hhs_leie": "https://oig.hhs.gov/exclusions/exclusions_list.asp",
}


def _screening_coverage_rows(screening: Dict) -> str:
    by_key: Dict[str, Dict] = {}
    lookups = [screening.get("irs") or {}, screening.get("organization") or {}]
    for item in screening.get("people") or []:
        lookups.append(item.get("lookup") or {})
    for lookup in lookups:
        for source in lookup.get("coverage") or []:
            if source.get("dataset_key"):
                by_key[source["dataset_key"]] = source
    rows = []
    for key, source in sorted(by_key.items()):
        url = _SCREENING_SOURCE_URLS.get(key, "")
        label = source.get("title") or key
        linked = f'<a href="{_h(url)}" target="_blank" rel="noopener">{_h(label)}</a>' if url else _h(label)
        rows.append(
            f"<tr><td>{linked}</td><td>{_h(source.get('source_date') or 'Not stated')}</td>"
            f"<td>{_h(source.get('retrieved_at'))}</td><td>{_h(source.get('record_count'))}</td></tr>"
        )
    return "".join(rows)


def _screening_panel(screening: Dict) -> str:
    if not screening.get("available"):
        errors = [
            (screening.get("irs") or {}).get("error"),
            (screening.get("organization") or {}).get("error"),
        ]
        detail = next((error for error in errors if error), "Public-screening sidecar is not installed.")
        return f"""
        <section class="risk-panel">
          <h3>IRS Eligibility, Revocation &amp; Sanctions Screening</h3>
          <div class="source-state source-unavailable"><b>Local public-screening snapshots unavailable.</b> {_h(detail)}
          Run <code>python build_screening_sidecar.py --download</code>; no API key is required.</div>
        </section>
        """

    coverage_keys = _screening_coverage_keys(screening)
    irs_covered = coverage_keys & {"irs_pub78", "irs_auto_revocation"}
    irs_missing = {"irs_pub78", "irs_auto_revocation"} - irs_covered
    irs_rows = (screening.get("irs") or {}).get("results") or []
    if not irs_covered:
        irs_html = '<p class="empty-note">IRS Pub. 78 and automatic-revocation snapshots are not present in this sidecar, so the EIN was not checked against those datasets.</p>'
    elif irs_rows:
        rendered_irs = []
        for row in irs_rows[:30]:
            dataset = row.get("dataset_key")
            if dataset == "irs_pub78":
                status = "Listed as eligible for deductible contributions"
                dates = "Current snapshot"
            else:
                status = "Reinstated after automatic revocation" if row.get("reinstatement_date") else "Automatic revocation; no reinstatement date in snapshot"
                dates = f"Revoked {_text(row.get('status_date')) or '—'}; reinstated {_text(row.get('reinstatement_date')) or '—'}"
            rendered_irs.append(
                f"<tr><td>{_h('Pub. 78' if dataset == 'irs_pub78' else 'Automatic revocation')}</td>"
                f"<td>{_h(row.get('primary_name'))}</td><td>{_h(status)}</td><td>{_h(dates)}</td>"
                f"<td>{_h(row.get('deductibility_code') or row.get('subsection_code'))}</td></tr>"
            )
        missing_note = (
            f'<p class="panel-footnote">Not checked because absent from this sidecar: {_h(", ".join(sorted(irs_missing)))}</p>'
            if irs_missing else ""
        )
        irs_html = f"""
        <div class="table-scroll"><table class="risk-table">
          <thead><tr><th>IRS dataset</th><th>Source name</th><th>Status represented</th><th>Effective dates</th><th>Code</th></tr></thead>
          <tbody>{''.join(rendered_irs)}</tbody>
        </table></div>
        {missing_note}
        """
    else:
        checked = ", ".join(sorted(irs_covered))
        absent = f" Datasets not present and therefore not checked: {', '.join(sorted(irs_missing))}." if irs_missing else ""
        irs_html = f'<p class="empty-note">The exact EIN was checked against { _h(checked) }; no row was returned. Absence is an unknown result, not proof of current status.{_h(absent)}</p>'

    candidate_groups = []
    organization = screening.get("organization") or {}
    if organization.get("results"):
        candidate_groups.append(("Organization", (organization.get("query") or {}).get("name"), organization.get("results") or []))
    for person_item in screening.get("people") or []:
        subject = person_item.get("subject") or {}
        candidate_groups.append((subject.get("role") or "Person", subject.get("name"), (person_item.get("lookup") or {}).get("results") or []))
    candidate_rows = []
    for subject_type, checked_name, results in candidate_groups:
        for row in results[:12]:
            dataset = _text(row.get("dataset_key"))
            source_url = _SCREENING_SOURCE_URLS.get(dataset, "")
            source_label = {
                "ofac_sdn": "OFAC SDN",
                "ofac_consolidated": "OFAC consolidated",
                "hhs_leie": "HHS-OIG LEIE",
            }.get(dataset, dataset)
            source_html = f'<a href="{_h(source_url)}" target="_blank" rel="noopener">{_h(source_label)}</a>' if source_url else _h(source_label)
            location = (row.get("location_evidence") or {}).get("kind") or "not requested"
            candidate_rows.append(
                f"<tr><td>{_h(subject_type)}<div class=\"muted\">{_h(checked_name)}</div></td>"
                f"<td>{source_html}</td><td>{_h(row.get('matched_name') or row.get('primary_name'))}</td>"
                f"<td>{_h((row.get('match_evidence') or {}).get('kind'))}</td><td>{_h(location)}</td>"
                f"<td>{_h(row.get('status') or row.get('exclusion_type') or row.get('program_tags'))}</td>"
                f"<td>{_h(row.get('verification_required'))}</td></tr>"
            )
    name_covered = coverage_keys & {"ofac_sdn", "ofac_consolidated", "hhs_leie"}
    name_missing = {"ofac_sdn", "ofac_consolidated", "hhs_leie"} - name_covered
    if not name_covered:
        candidates_html = '<p class="empty-note">OFAC and HHS-OIG snapshots are not present in this sidecar, so organization/principal names were not checked.</p>'
    elif candidate_rows:
        candidates_html = f"""
        <details open><summary><b>Candidate-only OFAC/HHS results ({len(candidate_rows)})</b></summary>
          <p class="panel-footnote">Exact conservative normalized-name matching retrieves leads only. HHS-OIG requires online EIN/SSN verification; OFAC matches require source identifiers, aliases, location, dates, and list-scope review.</p>
          <div class="table-scroll"><table class="risk-table"><thead><tr><th>Checked subject</th><th>List</th><th>Matched name</th><th>Name evidence</th><th>Location</th><th>Status/scope</th><th>Required verification</th></tr></thead><tbody>{''.join(candidate_rows)}</tbody></table></div>
        </details>
        """
    else:
        missing_note = f" Datasets not present and therefore not checked: {', '.join(sorted(name_missing))}." if name_missing else ""
        candidates_html = f'<p class="empty-note">No exact conservative organization/principal name candidates were returned by the loaded {_h(", ".join(sorted(name_covered)))} snapshot(s).{_h(missing_note)}</p>'

    coverage_rows = _screening_coverage_rows(screening)
    coverage_html = f"""
    <details><summary><b>Snapshot provenance</b></summary>
      <div class="table-scroll"><table class="risk-table"><thead><tr><th>Dataset</th><th>Source date</th><th>Retrieved</th><th>Records</th></tr></thead><tbody>{coverage_rows}</tbody></table></div>
    </details>
    """ if coverage_rows else ""
    return f"""
    <section class="risk-panel">
      <div class="panel-title-row"><h3>IRS Eligibility, Revocation &amp; Sanctions Screening</h3><span class="source-badge">Local official snapshots</span></div>
      {irs_html}
      {candidates_html}
      {coverage_html}
      <p class="panel-footnote">IRS status rows use exact EIN. Sanctions and exclusion results are candidate retrieval, never an automatic fraud determination or final eligibility decision.</p>
    </section>
    """


def _relationship_color(
    relationships: Set[str], scored_relationships: Optional[Set[str]] = None
) -> str:
    effective_signals = relationships if scored_relationships is None else scored_relationships
    if len(effective_signals) >= 2:
        return "#b42318"
    color_relationships = effective_signals or relationships
    if "Schedule R" in color_relationships:
        return "#7048a8"
    if color_relationships & {"Shared officer", "Shared employee/key person", "Shared person name"}:
        return "#176b87"
    if "Shared address" in color_relationships:
        return "#2e7d5b"
    if any(rel.startswith("Grant") for rel in color_relationships):
        return "#b35c00"
    return "#647084"


def _network_label_lines(value, width: int = 14, max_lines: int = 2) -> List[str]:
    text = re.sub(r"\s+", " ", _text(value)).strip() or "Unknown"
    width = max(4, int(width))
    max_lines = max(1, int(max_lines))
    wrapped = textwrap.wrap(
        text,
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=True,
    ) or ["Unknown"]
    if len(wrapped) <= max_lines:
        return wrapped
    lines = wrapped[:max_lines]
    lines[-1] = lines[-1][:(width - 1)].rstrip() + "…"
    return lines


def _network_tspans(lines: List[str], x: float, y: float, line_height: int) -> str:
    rendered = []
    for index, line in enumerate(lines):
        position = f'y="{y:.1f}"' if index == 0 else f'dy="{line_height}"'
        rendered.append(f'<tspan x="{x:.1f}" {position}>{_h(line)}</tspan>')
    return "".join(rendered)


def _network_svg(org_name: str, connections: List[Dict]) -> str:
    nodes = connections[:12]
    if not nodes:
        return '<div class="empty-note">No bounded network connections were found in the available local sources.</div>'
    width, height = 960, 520
    cx, cy, radius = width / 2, height / 2, 200
    pieces = [
        f'<svg class="network-map" viewBox="0 0 {width} {height}" role="img" aria-label="Relationship map centered on {_h(org_name)}">'
    ]
    positions = []
    for index, item in enumerate(nodes):
        angle = -math.pi / 2 + (2 * math.pi * index / max(len(nodes), 1))
        positions.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle), item))
    for x, y, item in positions:
        color = _relationship_color(
            item.get("relationships", set()), _connection_scored_relationships(item)
        )
        title = f"{item.get('name')}: {', '.join(sorted(item.get('relationships', set())))}"
        pieces.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" stroke="{color}" stroke-width="2.5"><title>{_h(title)}</title></line>'
    )
    center_lines = _network_label_lines(org_name, width=18, max_lines=2)
    center_y = cy - (7 if len(center_lines) > 1 else 1)
    center_tspans = _network_tspans(center_lines, cx, center_y, 12)
    pieces.append(
        f'<g><title>{_h(_text(org_name))} · filer</title>'
        f'<circle cx="{cx}" cy="{cy}" r="68" fill="#173f5f"/>'
        f'<text x="{cx}" text-anchor="middle" class="network-center">{center_tspans}</text>'
        f'<text x="{cx}" y="{cy + 28}" text-anchor="middle" class="network-center-sub">filer</text></g>'
    )
    for x, y, item in positions:
        relationships = item.get("relationships", set())
        color = _relationship_color(relationships, _connection_scored_relationships(item))
        full_label = _text(item.get("name") or item.get("ein") or "Unknown")
        label_lines = _network_label_lines(full_label, width=15, max_lines=2)
        label_y = y - (6 if len(label_lines) > 1 else 1)
        label_tspans = _network_tspans(label_lines, x, label_y, 11)
        title = f"{full_label} · {', '.join(sorted(relationships))}"
        pieces.append(
            f'<g><title>{_h(title)}</title><circle cx="{x:.1f}" cy="{y:.1f}" r="46" fill="{color}"/>'
            f'<text x="{x:.1f}" text-anchor="middle" class="network-node">{label_tspans}</text>'
            f'<text x="{x:.1f}" y="{y + (21 if len(label_lines) > 1 else 15):.1f}" text-anchor="middle" class="network-node-sub">{_h(item.get("ein") or "entity")}</text></g>'
        )
    pieces.append("</svg>")
    return "".join(pieces)


def _relationship_badges(relationships: Set[str]) -> str:
    return "".join(
        f'<span class="relation-badge" style="--relation-color:{_relationship_color({relationship})}">{_h(relationship)}</span>'
        for relationship in sorted(relationships)
    )


def _network_rows(connections: List[Dict], subject_ein: str) -> str:
    if not connections:
        return '<tr><td colspan="6" class="muted">No connections found.</td></tr>'
    rows = []
    for item in connections[:30]:
        years = sorted(item.get("years") or [])
        year_text = "" if not years else (str(years[0]) if len(years) == 1 else f"{years[0]}–{years[-1]}")
        amount_lines = [
            f"<div><span class=\"muted\">{_h(relationship)}:</span> {_money(amount)}</div>"
            for relationship, amount in sorted((item.get("amount_by_type") or {}).items())
            if _num(amount)
        ]
        evidence = "<br>".join(_h(value) for value in (item.get("evidence") or [])[:4])
        analyze = ""
        if item.get("ein"):
            analyze = f"""
            <form method="post" action="/query/fraud_risk_dashboard" class="inline-analyze">
              <input type="hidden" name="qkey" value="fraud_risk_dashboard">
              <input type="hidden" name="ein" value="{_h(item.get('ein'))}">
              <input type="hidden" name="external_mode" value="local">
              <button type="submit">Analyze</button>
            </form>
            """
        rows.append(f"""
        <tr>
          <td>{_h(item.get('name'))}<div class="muted">{_h(item.get('ein'))}</div></td>
          <td>{_relationship_badges(item.get('relationships') or set())}</td>
          <td>{_h(year_text)}</td>
          <td>{''.join(amount_lines)}</td>
          <td>{evidence}</td>
          <td>{analyze}</td>
        </tr>
        """)
    return "".join(rows)


def _grant_path_rows(paths: List[Dict], subject_ein: str) -> str:
    if not paths:
        return ""
    rows = []
    for path in paths[:12]:
        if path.get("returns_to_subject") and path.get("chronology_supported"):
            first_years = _year_values_text(path.get("qualifying_first_years"))
            second_years = _year_values_text(path.get("qualifying_second_years"))
            circular = '<span class="badge risk-path-badge">Plausible return</span>'
        elif path.get("returns_to_subject"):
            first_years = _year_values_text(path.get("first_years"))
            second_years = _year_values_text(path.get("second_years"))
            if path.get("second_max_year") and path.get("first_min_year") and path["second_max_year"] < path["first_min_year"]:
                lead_text = "Reverse flow predates first hop"
            else:
                lead_text = "No return within two years"
            circular = f'<span class="relation-badge" style="--relation-color:#647084">{_h(lead_text)}</span>'
        else:
            first_years = _year_values_text(path.get("first_years"))
            second_years = _year_values_text(path.get("second_years"))
            circular = ""
        rows.append(f"""
        <tr>
          <td>{_h(path.get('via_name'))}<div class="muted">{_h(path.get('via_ein'))}</div></td>
          <td>{_h(path.get('target_name'))}<div class="muted">{_h(path.get('target_ein'))}</div></td>
          <td>{_h(first_years)}</td><td>{_h(second_years)}</td><td>{_money(path.get('amount'))}</td><td>{circular}</td>
        </tr>
        """)
    return f"""
    <details class="network-paths" open>
      <summary><b>Potential two-step grant paths ({len(paths)})</b></summary>
      <p class="panel-footnote">These are bounded topology leads, not proof that the same dollars were passed through. Compare dates and purposes.</p>
      <div class="table-scroll"><table class="risk-table">
        <thead><tr><th>First recipient / intermediary</th><th>Its recipient</th><th>First-hop years</th><th>Second-hop years</th><th>Second-hop amount</th><th>Lead</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table></div>
    </details>
    """


def _network_panel(network: Dict, org_name: str, subject_ein: str) -> str:
    metrics = network.get("metrics") or {}
    connections = network.get("connections") or []
    metric_items = [
        ("Connected entities", metrics.get("connected_entities", 0)),
        ("Multi-signal entities", metrics.get("multi_signal_entities", 0)),
        ("Grants paid", _money(metrics.get("grants_paid"))),
        ("Grants received", _money(metrics.get("grants_received"))),
        ("Shared person names", metrics.get("shared_people", 0)),
        ("Common-name hubs hidden", metrics.get("shared_people_hubs_suppressed", 0)),
        ("Shared addresses", metrics.get("shared_addresses", 0)),
        ("Schedule R rows", metrics.get("schedule_r_edges", 0)),
        ("Unrelated partnership rows", metrics.get("schedule_r_unrelated_partnerships", 0)),
        ("Grant match warnings", metrics.get("grant_match_warnings", 0)),
    ]
    metrics_html = "".join(
        f'<div class="risk-metric compact"><span>{_h(label)}</span><strong>{_h(value)}</strong></div>'
        for label, value in metric_items
    )
    truncated = '<p class="panel-footnote">Display is capped at the 50 highest-signal connections.</p>' if network.get("truncated") else ""
    indexed = network.get("indexed") or {}
    if indexed.get("available"):
        indexed_rows = []
        collections = [
            ("Outgoing", indexed.get("outgoing") or []),
            ("Incoming", indexed.get("incoming") or []),
            ("Shared identity", indexed.get("shared_neighbors") or []),
        ]
        for direction, rows in collections:
            for edge in rows[:15]:
                if direction == "Incoming":
                    counterpart = edge.get("source_name") or edge.get("source_ein")
                    counterpart_ein = edge.get("source_ein")
                elif direction == "Shared identity":
                    counterpart = edge.get("source_name") or edge.get("source_ein")
                    counterpart_ein = edge.get("source_ein")
                else:
                    counterpart = edge.get("target_name") or edge.get("target_ein") or edge.get("target_key")
                    counterpart_ein = edge.get("target_ein")
                shared = edge.get("shared_target_name") if direction == "Shared identity" else ""
                indexed_rows.append(
                    f"<tr><td>{_h(direction)}</td><td>{_h(counterpart)}<div class=\"muted\">{_h(counterpart_ein)}</div></td>"
                    f"<td>{_h(edge.get('edge_type'))}{(f'<div class=\"muted\">via {_h(shared)}</div>' if shared else '')}</td>"
                    f"<td>{_h(edge.get('tax_year'))}</td><td>{_money(edge.get('amount'))}</td>"
                    f"<td>{_h(round(_num(edge.get('confidence')), 3))}</td>"
                    f"<td>{_h(edge.get('provenance_table'))}<div class=\"muted\">{_h(edge.get('provenance_row_id'))}</div></td></tr>"
                )
        source_count = sum(1 for source in indexed.get("sources") or [] if int(source.get("available") or 0))
        coverage = indexed.get("coverage") or {}
        if indexed.get("coverage_complete"):
            coverage_note = "All filing years displayed by this dashboard are represented in the indexed filing-state coverage."
        else:
            covered_years = ", ".join(str(year) for year in coverage.get("covered_tax_years") or []) or "none"
            coverage_note = f"Partial sidecar for this EIN: covered years {covered_years}; build scope {_text(coverage.get('build_scope')) or 'unknown'}. Zero edges outside that coverage must not be read as no relationship."
        indexed_html = f"""
        <details><summary><b>Indexed network evidence ({len(indexed.get('outgoing') or [])} outgoing, {len(indexed.get('incoming') or [])} incoming, {len(indexed.get('shared_neighbors') or [])} shared-neighbor rows)</b></summary>
          <p class="panel-footnote">The refreshable sidecar has {source_count} available source group(s). {_h(coverage_note)} It adds indexed reverse traversal, exact source-row provenance, confidence, and precomputed hub suppression. Display below is bounded.</p>
          <div class="table-scroll"><table class="risk-table network-table"><thead><tr><th>Direction</th><th>Counterparty</th><th>Edge</th><th>Year</th><th>Amount</th><th>Confidence</th><th>Provenance</th></tr></thead><tbody>{''.join(indexed_rows) if indexed_rows else '<tr><td colspan="7" class="muted">No indexed edges for this EIN and filing-year window.</td></tr>'}</tbody></table></div>
        </details>
        """
    else:
        indexed_html = '<p class="panel-footnote"><b>Indexed cache:</b> not installed yet. The bounded on-demand graph above remains active; see <code>docs/risk-network.md</code> for the maintenance-window build.</p>'
    return f"""
    <section class="risk-panel">
      <h3>Relationship Network</h3>
      <p class="panel-footnote">Bounded exact-match evidence from resolved grants, Form 990/990-EZ/990-PF officers and key people, normalized addresses, contractors, and Schedule R. On-demand person links are unscored name-only candidates because Form 990 provides no stable person identifier; common-name hubs are hidden across all person tables. Indexed person-name confidence describes match construction, not identity proof, and indexed edges are not used in the score. Shared identities are review leads, not proof of common control.</p>
      <div class="risk-grid network-metrics">{metrics_html}</div>
      <div class="network-layout">
        <div class="network-map-wrap">{_network_svg(org_name, connections)}</div>
        <div class="network-legend">
          <span style="--legend:#7048a8">Schedule R</span><span style="--legend:#176b87">Shared name candidate</span>
          <span style="--legend:#2e7d5b">Shared address</span><span style="--legend:#b35c00">Grant flow</span>
          <span style="--legend:#647084">Contractor</span><span style="--legend:#b42318">Multiple signals</span>
        </div>
      </div>
      <div class="table-scroll"><table class="risk-table network-table">
        <thead><tr><th>Connected entity</th><th>Relationship</th><th>Years</th><th>Amounts by relationship</th><th>Evidence</th><th></th></tr></thead>
        <tbody>{_network_rows(connections, subject_ein)}</tbody>
      </table></div>
      {truncated}
      {_grant_path_rows(network.get('paths') or [], subject_ein)}
      {indexed_html}
    </section>
    """


_EXTERNAL_SOURCE_URLS = {
    "fac": "https://www.fac.gov/data/",
    "usaspending": "https://www.usaspending.gov/",
    "sam": "https://sam.gov/",
    "fec": "https://www.fec.gov/data/",
    "lda": "https://lda.gov/",
}

_EXTERNAL_SOURCE_NAMES = {
    "fac": "Federal Audit Clearinghouse",
    "usaspending": "USAspending",
    "sam": "SAM.gov",
    "fec": "Federal Election Commission",
    "lda": "Lobbying Disclosure Act",
}


def _sam_coverage_details(result: Dict) -> Dict:
    """Normalize quota/coverage metadata for honest SAM result rendering."""
    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    quota = result.get("quota") if isinstance(result.get("quota"), dict) else {}
    queried = list(coverage.get("queried_ueis") or result.get("queried_ueis") or [])
    omitted = list(coverage.get("omitted_ueis") or result.get("omitted_ueis") or [])
    exclusion_queries = coverage.get("exclusion_queries") or []
    page_omissions = []
    for index, query in enumerate(exclusion_queries):
        if not isinstance(query, dict):
            continue
        pages = query.get("pages_omitted")
        if pages is None:
            continue
        label = _text(query.get("uei")).strip() or f"query {index + 1}"
        page_omissions.append(f"{label}: {_text(pages)}")
    truncation_reasons = list(coverage.get("truncation_reasons") or [])
    if not page_omissions and "exclusion_pages_omitted" in truncation_reasons:
        page_omissions.append("unknown")

    coverage_status = _text(
        result.get("coverage_status") or coverage.get("status")
    ).strip().casefold()
    partial = bool(
        result.get("partial")
        or result.get("truncated")
        or coverage.get("partial")
        or coverage.get("truncated")
        or coverage_status == "partial"
        or omitted
    )
    reported = bool(
        coverage
        or quota
        or coverage_status
        or omitted
        or result.get("partial")
        or result.get("truncated")
    )
    return {
        "reported": reported,
        "partial": partial,
        "status": "partial" if partial else (coverage_status or "complete"),
        "queried_ueis": queried,
        "omitted_ueis": omitted,
        "page_omissions": page_omissions,
        "requests_used": quota.get("requests_used"),
        "request_budget": quota.get("request_budget"),
    }


def _external_source_summary(key: str, result: Dict) -> str:
    status = result.get("status") or "error"
    demo_note = " (shared DEMO_KEY; replace for reliable use)" if result.get("credential") == "shared_demo_key" else ""
    if status == "ok":
        if key == "fac":
            as_of = result.get("offline_source_as_of_date") or result.get("source_as_of_date")
            offline_note = f"; local snapshot {_text(as_of)}" if as_of else ""
            return f"{len(result.get('reports') or [])} audit report(s){offline_note}{demo_note}"
        if key == "usaspending":
            return f"{len(result.get('matches') or [])} exact-UEI recipient match(es)"
        if key == "sam":
            coverage = _sam_coverage_details(result)
            coverage_note = "; partial coverage" if coverage["partial"] else ""
            return f"{len(result.get('entities') or [])} registration(s); {len(result.get('exclusions') or [])} exclusion record(s){coverage_note}"
        if key == "fec":
            return f"{len(result.get('candidates') or [])} name/state candidate(s); manual verification required{demo_note}"
        if key == "lda":
            return f"{len(result.get('clients') or [])} exact/strong client match(es)"
    if status == "no_match":
        if key == "sam" and _sam_coverage_details(result)["partial"]:
            return "Partial SAM check; no match within the queried coverage"
        return "Checked; no exact or vetted match" + demo_note
    if status == "not_configured":
        return "API key not configured"
    if status == "blocked":
        reasons = {
            "local_mode": "Skipped in local-data-only mode",
            "requires_fac_uei": "Requires an exact UEI from a primary-EIN FAC match",
            "requires_org_name": "Organization name unavailable",
            "invalid_ein": "EIN unavailable or invalid",
        }
        return reasons.get(result.get("reason"), "Not run")
    return "Source request unavailable; other sources still completed"


def _external_source_cards(external: Dict) -> str:
    cards = []
    for key in ("fac", "usaspending", "sam", "fec", "lda"):
        result = external.get(key) or {}
        status = result.get("status") or "error"
        sam_partial = (
            key == "sam"
            and status in {"ok", "no_match"}
            and _sam_coverage_details(result)["partial"]
        )
        status_label = {
            "ok": "Match",
            "no_match": "No match",
            "not_configured": "Needs key",
            "blocked": "Skipped",
            "error": "Unavailable",
        }.get(status, "Unavailable")
        if sam_partial:
            status_label = "Partial"
        display_status = "partial" if sam_partial else status
        cards.append(f"""
        <div class="source-card source-{_h(display_status)}">
          <div class="source-card-head"><a href="{_EXTERNAL_SOURCE_URLS[key]}" target="_blank" rel="noopener">{_h(_EXTERNAL_SOURCE_NAMES[key])}</a><span class="source-status">{_h(status_label)}</span></div>
          <p>{_h(_external_source_summary(key, result))}</p>
        </div>
        """)
    return "".join(cards)


def _excerpt(value, limit: int = 700) -> str:
    text_value = re.sub(r"\s+", " ", _text(value)).strip()
    return text_value if len(text_value) <= limit else text_value[: limit - 1].rstrip() + "…"


def _fac_audit_rows(reports: List[Dict]) -> str:
    rows = []
    for report in reports:
        general = report.get("general") or {}
        high, medium = _fac_report_issue_labels(report)
        issues = high + medium
        threshold = _single_audit_threshold(general.get("fy_start_date"))
        expended = _num(general.get("total_amount_expended"))
        threshold_state = "Unknown threshold"
        if threshold is not None:
            threshold_state = f"{'At/above' if expended >= threshold else 'Below'} {_money(threshold)} threshold"
        period = " to ".join(
            part for part in [_text(general.get("fy_start_date")), _text(general.get("fy_end_date"))]
            if part
        )
        rows.append(f"""
        <tr>
          <td>{_h(general.get('audit_year'))}<div class="muted">{_h(period)}</div></td>
          <td>{_money(general.get('total_amount_expended'))}</td>
          <td>{_h(threshold_state)}</td>
          <td>{_h(general.get('audit_type'))}<div class="muted">Low-risk auditee: {_h('Yes' if general.get('is_low_risk_auditee') is True else 'No' if general.get('is_low_risk_auditee') is False else 'Unknown')}</div></td>
          <td>{_h(', '.join(issues) if issues else 'No selected issue flags')}</td>
          <td>{_h(report.get('ein_match'))}<div class="muted">{_h(general.get('report_id') or report.get('report_id'))}</div></td>
        </tr>
        """)
    return "".join(rows)


def _fac_finding_rows(reports: List[Dict]) -> str:
    rows = []
    for report in reports:
        general = report.get("general") or {}
        text_by_ref: Dict[str, List[str]] = defaultdict(list)
        action_by_ref: Dict[str, List[str]] = defaultdict(list)
        for item in report.get("findings_text") or []:
            text_by_ref[_text(item.get("finding_ref_number"))].append(_text(item.get("finding_text")))
        for item in report.get("corrective_action_plans") or []:
            action_by_ref[_text(item.get("finding_ref_number"))].append(_text(item.get("planned_action")))
        for finding in report.get("findings") or []:
            reference = _text(finding.get("reference_number"))
            flags = [
                label for field, label in [
                    ("is_modified_opinion", "Modified opinion"),
                    ("is_material_weakness", "Material weakness"),
                    ("is_significant_deficiency", "Significant deficiency"),
                    ("is_questioned_costs", "Questioned costs"),
                    ("is_repeat_finding", "Repeat finding"),
                    ("is_other_findings", "Other finding"),
                ] if finding.get(field) is True
            ]
            narrative = " ".join(text_by_ref.get(reference) or [])
            action = " ".join(action_by_ref.get(reference) or [])
            rows.append(f"""
            <tr>
              <td>{_h(general.get('audit_year'))}</td>
              <td>{_h(reference or finding.get('award_reference'))}</td>
              <td>{_h(finding.get('type_requirement'))}</td>
              <td>{_h(', '.join(flags) if flags else 'Finding reported')}</td>
              <td>{_h(_excerpt(narrative) or 'Narrative not returned')}</td>
              <td>{_h(_excerpt(action) or 'Corrective action not returned')}</td>
            </tr>
            """)
    return "".join(rows)


def _fac_award_rows(reports: List[Dict]) -> str:
    awards = []
    for report in reports:
        year = (report.get("general") or {}).get("audit_year")
        awards.extend((year, award) for award in report.get("federal_awards") or [])
    awards.sort(key=lambda entry: _num(entry[1].get("amount_expended")), reverse=True)
    rows = []
    for year, award in awards[:20]:
        listing = ".".join(part for part in [
            _text(award.get("federal_agency_prefix")),
            _text(award.get("federal_award_extension")),
        ] if part)
        rows.append(f"""
        <tr><td>{_h(year)}</td><td>{_h(listing)}</td><td>{_h(award.get('federal_program_name') or award.get('cluster_name'))}</td>
        <td>{_money(award.get('amount_expended'))}</td><td>{_h('Yes' if award.get('is_major') is True else 'No' if award.get('is_major') is False else '')}</td>
        <td>{_h(award.get('audit_report_type'))}</td><td>{_h(award.get('findings_count'))}</td></tr>
        """)
    return "".join(rows)


def _external_candidate_sections(external: Dict) -> str:
    sections = []
    usa = external.get("usaspending") or {}
    if usa.get("matches"):
        rows = "".join(
            f"<tr><td>{_h(item.get('name'))}</td><td>{_h(item.get('uei'))}</td><td>{_h(item.get('recipient_level'))}</td><td>{_money(item.get('amount'))}</td></tr>"
            for item in (usa.get("matches") or [])[:20]
        )
        sections.append(f"""
        <details open><summary><b>USAspending exact-UEI recipients ({len(usa.get('matches') or [])})</b></summary>
          <p class="panel-footnote">Amount is trailing-12-month transaction activity from the recipient search, not Single Audit federal expenditures.</p>
          <div class="table-scroll"><table class="risk-table"><thead><tr><th>Recipient</th><th>UEI</th><th>Level</th><th>Trailing-12-month amount</th></tr></thead><tbody>{rows}</tbody></table></div>
        </details>
        """)
    sam = external.get("sam") or {}
    if sam.get("status") in {"ok", "no_match"} and (
        sam.get("status") == "ok"
        or sam.get("queried_ueis")
        or sam.get("coverage")
    ):
        entity_rows = []
        for entity in (sam.get("entities") or [])[:10]:
            registration = entity.get("entity_registration") or {}
            entity_rows.append(
                f"<tr><td>{_h(registration.get('legalBusinessName') or registration.get('legal_business_name'))}</td>"
                f"<td>{_h(entity.get('uei'))}</td><td>{_h(registration.get('registrationStatus') or registration.get('registration_status'))}</td>"
                f"<td>{_h(registration.get('registrationExpirationDate') or registration.get('registration_expiration_date'))}</td></tr>"
            )
        exclusion_rows = []
        for exclusion in (sam.get("exclusions") or [])[:25]:
            identification = exclusion.get("exclusionIdentification") or {}
            details = exclusion.get("exclusionDetails") or {}
            actions = ((exclusion.get("exclusionActions") or {}).get("listOfActions") or [])
            action = actions[-1] if actions else {}
            exclusion_rows.append(
                f"<tr><td>{_h(identification.get('entityName') or identification.get('name'))}</td>"
                f"<td>{_h(identification.get('ueiSAM'))}</td><td>{_h(details.get('exclusionType'))}</td>"
                f"<td>{_h(details.get('exclusionProgram'))}</td><td>{_h(details.get('excludingAgencyName') or details.get('excludingAgencyCode'))}</td>"
                f"<td>{_h(action.get('activateDate'))}</td><td>{_h(action.get('terminationDate') or action.get('terminationType'))}</td>"
                f"<td>{_h(action.get('recordStatus'))}</td></tr>"
            )
        coverage = _sam_coverage_details(sam)
        coverage_html = ""
        if coverage["reported"]:
            queried_text = ", ".join(coverage["queried_ueis"]) or "none"
            omitted_text = ", ".join(coverage["omitted_ueis"]) or "none"
            page_text = "; ".join(coverage["page_omissions"]) or "0 reported"
            requests_used = coverage.get("requests_used")
            request_budget = coverage.get("request_budget")
            if requests_used is None and request_budget is None:
                request_text = "not reported"
            elif request_budget is None:
                request_text = f"{_text(requests_used)} used"
            else:
                request_text = f"{_text(requests_used) if requests_used is not None else 'unknown'} of {_text(request_budget)} budgeted"
            coverage_class = "source-state source-review" if coverage["partial"] else "panel-footnote"
            no_match_warning = (
                " <b>No-match applies only to the queried portion; this was not a complete SAM check.</b>"
                if coverage["partial"] and sam.get("status") == "no_match" else ""
            )
            coverage_html = f"""
              <p class="{coverage_class}"><b>{'Partial' if coverage['partial'] else 'Complete'} SAM coverage.</b>
              Queried UEIs: {_h(queried_text)}; omitted UEIs: {_h(omitted_text)};
              exclusion pages omitted: {_h(page_text)}; requests: {_h(request_text)}.{no_match_warning}</p>
            """
        result_summary = (
            f"{len(sam.get('entities') or [])} entity registration(s) and <b>{len(sam.get('exclusions') or [])} exclusion record(s)</b>"
            if sam.get("status") == "ok"
            else "No exact entity-registration or exclusion row was returned within the queried coverage"
        )
        sections.append(f"""
        <details open><summary><b>SAM.gov exact-UEI results</b></summary>
          <p>{result_summary} for UEI(s) {_h(', '.join(sam.get('queried_ueis') or []) or 'none')}.</p>
          {coverage_html}
          {f'<div class="table-scroll"><table class="risk-table"><thead><tr><th>Registered entity</th><th>UEI</th><th>Status</th><th>Expiration</th></tr></thead><tbody>{"".join(entity_rows)}</tbody></table></div>' if entity_rows else ''}
          {f'<div class="table-scroll"><table class="risk-table"><thead><tr><th>Excluded entity</th><th>UEI</th><th>Type</th><th>Program</th><th>Agency</th><th>Activated</th><th>Termination</th><th>Status</th></tr></thead><tbody>{"".join(exclusion_rows)}</tbody></table></div>' if exclusion_rows else ''}
          <p class="panel-footnote">Verify each exclusion's dates, agency, scope, and current applicability in SAM.gov.</p>
        </details>
        """)
    fec = external.get("fec") or {}
    if fec.get("candidates"):
        rows = []
        for item in (fec.get("candidates") or [])[:20]:
            committee_id = re.sub(r"[^A-Za-z0-9]", "", _text(item.get("committee_id")))
            committee = _h(committee_id)
            if committee_id:
                committee = f'<a href="https://www.fec.gov/data/committee/{committee_id}/" target="_blank" rel="noopener">{_h(committee_id)}</a>'
            rows.append(f"<tr><td>{_h(item.get('name'))}</td><td>{committee}</td><td>{_h(item.get('state'))}</td><td>{_h(item.get('committee_type_full') or item.get('committee_type'))}</td><td>{_h(item.get('treasurer_name'))}</td><td>{_h(item.get('last_file_date'))}</td></tr>")
        sections.append(f"""
        <details><summary><b>FEC committee candidates ({len(fec.get('candidates') or [])})</b></summary>
          <p class="panel-footnote">Candidate-only name/state matches. FEC committee data does not offer a dependable nonprofit EIN join.</p>
          <div class="table-scroll"><table class="risk-table"><thead><tr><th>Name</th><th>Committee</th><th>State</th><th>Type</th><th>Treasurer</th><th>Last filed</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        </details>
        """)
    lda = external.get("lda") or {}
    if lda.get("clients"):
        rows = []
        for client in lda.get("clients") or []:
            filing_years = sorted({_text(item.get("filing_year")) for item in client.get("filings") or [] if item.get("filing_year")}, reverse=True)
            reported_income = sum(_num(item.get("income")) for item in client.get("filings") or [])
            reported_expenses = sum(_num(item.get("expenses")) for item in client.get("filings") or [])
            amount_parts = []
            if reported_income:
                amount_parts.append(f"Income {_money(reported_income)}")
            if reported_expenses:
                amount_parts.append(f"Expenses {_money(reported_expenses)}")
            registrant = client.get("registrant") or {}
            if isinstance(registrant, dict):
                registrant = registrant.get("name") or registrant.get("display_name") or registrant.get("id")
            rows.append(f"<tr><td>{_h(client.get('name'))}</td><td>{_h(client.get('state'))}</td><td>{_h(client.get('match_strength'))}</td><td>{_h(registrant)}</td><td>{len(client.get('filings') or [])}</td><td>{_h(', '.join(filing_years[:6]))}</td><td>{_h('; '.join(amount_parts))}</td></tr>")
        sections.append(f"""
        <details><summary><b>LDA client matches ({len(lda.get('clients') or [])})</b></summary>
          <p class="panel-footnote">Only exact/strong client-name matches are shown. A nonprofit may be the client while its lobbying firm is the registrant.</p>
          <div class="table-scroll"><table class="risk-table"><thead><tr><th>Client</th><th>State</th><th>Match</th><th>Registrant</th><th>Recent filings</th><th>Years</th><th>Reported amounts</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        </details>
        """)
    return "".join(sections)


def _external_panel(external: Dict, mode: str) -> str:
    fac = external.get("fac") or {}
    reports = fac.get("reports") or []
    audit_rows = _fac_audit_rows(reports)
    finding_rows = _fac_finding_rows(reports)
    award_rows = _fac_award_rows(reports)
    fac_details = ""
    if reports:
        fac_details = f"""
        <h4>Federal Audit Clearinghouse</h4>
        <p class="panel-footnote">Single Audit applicability is based on federal awards <b>expended</b>, not grants received or Form 990 government-grant revenue. The threshold is $750,000 for fiscal years beginning before October 1, 2024, and $1,000,000 thereafter.</p>
        <div class="table-scroll"><table class="risk-table external-audit-table"><thead><tr><th>Audit year / period</th><th>Federal expenditures</th><th>Period threshold</th><th>Audit type</th><th>Selected issues</th><th>EIN match / report</th></tr></thead><tbody>{audit_rows}</tbody></table></div>
        {f'<details open><summary><b>Audit findings ({sum(len(r.get("findings") or []) for r in reports)})</b></summary><div class="table-scroll"><table class="risk-table external-findings-table"><thead><tr><th>Year</th><th>Reference</th><th>Requirement</th><th>Flags</th><th>Finding narrative</th><th>Corrective action</th></tr></thead><tbody>{finding_rows}</tbody></table></div></details>' if finding_rows else '<p class="empty-note">No FAC finding rows were returned for these reports.</p>'}
        {f'<details><summary><b>Largest federal programs ({sum(len(r.get("federal_awards") or []) for r in reports)})</b></summary><div class="table-scroll"><table class="risk-table"><thead><tr><th>Year</th><th>Assistance listing</th><th>Program</th><th>Expended</th><th>Major</th><th>Opinion</th><th>Findings</th></tr></thead><tbody>{award_rows}</tbody></table></div></details>' if award_rows else ''}
        """
    elif fac.get("status") == "no_match":
        fac_details = '<p class="empty-note">FAC checked both primary and additional EIN fields in the available live and/or local sources and returned no audit match.</p>'
    elif fac.get("status") == "not_configured":
        fac_details = '<p class="empty-note">Set <code>FAC_API_KEY</code> (a free <a href="https://api.data.gov/signup/" target="_blank" rel="noopener">Data.gov API key</a>) or build <code>db/fac_audits.db</code> from the no-key FAC bulk files.</p>'
    mode_note = "Live public-source checks" if mode == "live" else "Local sidecars only; network calls skipped"
    fetched = external.get("fetched_at") or ""
    return f"""
    <section class="risk-panel external-panel">
      <div class="panel-title-row"><h3>Federal Audit &amp; Public-Record Checks</h3><span class="muted">{_h(mode_note)}{(' · ' + _h(fetched)) if fetched else ''}</span></div>
      <div class="source-grid">{_external_source_cards(external)}</div>
      {fac_details}
      {_external_candidate_sections(external)}
    </section>
    """


def _render_report(report: Dict, print_mode: bool = False) -> str:
    indicators = report.get("indicators") or []
    counts = report.get("counts") or {}
    score = int(report.get("risk_score") or 0)
    years = report.get("years") or []
    latest = years[0] if years else {}
    data_quality_count = sum(1 for item in indicators if item.get("category") == "Data Quality")
    disclosure_count = sum(1 for item in indicators if item.get("category") == "Disclosure Context")
    external_lead_count = sum(1 for item in indicators if item.get("category") in {"External Coverage", "External Lead"})
    print_css = """
      @page { size: letter portrait; margin: 0.4in; }
      @media print {
        body { background:#fff; }
        .print-toolbar { display:none !important; }
        .risk-dashboard { font-size: 10px; }
        .risk-grid { grid-template-columns: repeat(4, 1fr); }
        .risk-summary-grid { grid-template-columns: repeat(5, 1fr); }
        .risk-summary-grid .risk-metric strong { font-size:16px; }
        .metric-help { display:none !important; }
        .risk-card, .risk-panel { break-inside: avoid; page-break-inside: avoid; }
        .risk-card { padding: 8px; margin-bottom: 7px; }
        .risk-card h3 { font-size: 12px; }
        .network-table, .external-audit-table, .external-findings-table { min-width:0 !important; font-size:8px; }
        .network-map { min-width:0 !important; }
        .network-map-wrap { overflow:visible !important; }
        .table-scroll { overflow:visible !important; }
        details > * { display:block !important; }
      }
    """ if print_mode else ""
    return f"""
    <style>
      .risk-dashboard {{ --border:#d8dde6; --muted:#647084; --ink:#202733; }}
      .risk-hero {{ border:1px solid var(--border); background:#fff; padding:16px; margin:18px 0; }}
      .risk-hero h2 {{ margin:0 0 4px; }}
      .risk-subtitle {{ color:var(--muted); margin:0; }}
      .risk-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap:10px; margin-top:14px; }}
      .risk-summary-grid {{ grid-template-columns:repeat(5,minmax(135px,1fr)); }}
      .risk-metric {{ border:1px solid var(--border); background:#f7f9fc; padding:10px; }}
      .risk-metric span {{ display:block; color:var(--muted); font-size:12px; }}
      .risk-metric strong {{ display:block; font-size:22px; margin-top:3px; }}
      .metric-label {{ display:flex; align-items:center; gap:6px; color:var(--muted); font-size:12px; }}
      .metric-help {{ position:relative; display:inline-block; }}
      .metric-help summary, .info-link {{ display:inline-grid; place-items:center; width:17px; height:17px; box-sizing:border-box; border:1px solid #8793a3; border-radius:50%; color:#34495e; background:#fff; font:700 11px/1 sans-serif; cursor:pointer; text-decoration:none; list-style:none; }}
      .metric-help summary::-webkit-details-marker {{ display:none; }}
      .metric-help summary:focus-visible, .info-link:focus-visible {{ outline:2px solid #176b87; outline-offset:2px; }}
      .metric-help-card {{ position:absolute; z-index:20; top:23px; left:0; width:285px; padding:9px 10px; border:1px solid var(--border); border-radius:4px; background:#fff; color:var(--ink); box-shadow:0 4px 14px rgba(31,45,61,.16); font-size:12px; font-weight:400; line-height:1.4; }}
      .risk-panel {{ border:1px solid var(--border); background:#fff; padding:14px; margin:16px 0; }}
      .risk-panel h3 {{ margin:0 0 10px; }}
      .panel-title-row {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }}
      .panel-title-row h3 {{ margin:0 0 10px; }}
      .panel-title-row a {{ font-size:12px; }}
      .panel-footnote {{ color:var(--muted); font-size:12px; line-height:1.4; }}
      .risk-table {{ width:100%; border-collapse:collapse; font-size:13px; }}
      .risk-table th, .risk-table td {{ padding:6px 5px; border-bottom:1px solid #eee; text-align:left; white-space:normal; }}
      .risk-table tbody tr:nth-child(odd) {{ background:#f7f7f7; }}
      .risk-kv th {{ width:230px; color:var(--muted); }}
      .table-scroll {{ overflow-x:auto; }}
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
      .source-state {{ padding:12px; border:1px solid var(--border); line-height:1.4; }}
      .source-unavailable {{ background:#f7f9fc; color:var(--muted); }}
      .source-review {{ background:#fff8e7; border-color:#e7c96b; }}
      .source-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(185px,1fr)); gap:8px; margin:10px 0 14px; }}
      .source-card {{ border:1px solid var(--border); border-top:4px solid #647084; padding:9px; background:#fff; }}
      .source-card p {{ margin:7px 0 0; color:var(--muted); font-size:12px; line-height:1.35; }}
      .source-card-head {{ display:flex; justify-content:space-between; gap:7px; align-items:start; }}
      .source-status {{ border-radius:999px; padding:2px 7px; background:#eef1f4; font-size:10px; font-weight:700; white-space:nowrap; }}
      .source-ok {{ border-top-color:#2e7d5b; }}
      .source-no_match {{ border-top-color:#28698f; }}
      .source-partial {{ border-top-color:#b35c00; background:#fff8e7; }}
      .source-error {{ border-top-color:#b42318; }}
      .source-not_configured, .source-blocked {{ border-top-color:#8b8f97; background:#fafbfc; }}
      .external-panel h4 {{ margin:16px 0 7px; }}
      .external-panel details {{ margin:12px 0; }}
      .external-panel summary {{ cursor:pointer; }}
      .external-findings-table td {{ min-width:115px; vertical-align:top; }}
      .screening-note {{ background:#eef6fb; border:1px solid #a8ccdf; padding:10px 12px; margin:12px 0 0; line-height:1.4; }}
      .risk-metric.compact strong {{ font-size:17px; }}
      .network-metrics {{ grid-template-columns:repeat(auto-fit,minmax(135px,1fr)); }}
      .network-layout {{ display:grid; grid-template-columns:minmax(0,1fr); gap:6px; align-items:center; }}
      .network-map-wrap {{ overflow-x:auto; }}
      .network-map {{ display:block; width:100%; height:auto; aspect-ratio:960 / 520; background:#fbfcfe; border:1px solid #edf0f4; }}
      .network-center {{ fill:#fff; font-size:11px; font-weight:700; }}
      .network-center-sub {{ fill:#d7e7f4; font-size:10px; }}
      .network-node {{ fill:#fff; font-size:9px; font-weight:700; }}
      .network-node-sub {{ fill:#eef4f8; font-size:7px; }}
      .network-legend {{ display:flex; gap:8px 14px; flex-wrap:wrap; color:var(--muted); font-size:12px; }}
      .network-legend span::before {{ content:""; display:inline-block; width:10px; height:10px; margin-right:5px; border-radius:50%; background:var(--legend); }}
      .network-table {{ min-width:900px; margin-top:12px; }}
      .relation-badge {{ display:inline-block; border:1px solid var(--relation-color); color:var(--relation-color); border-radius:999px; padding:2px 6px; margin:1px 2px 1px 0; font-size:11px; font-weight:650; }}
      .inline-analyze {{ margin:0; }}
      .inline-analyze button {{ min-height:28px; padding:4px 8px; }}
      .network-paths {{ margin-top:14px; }}
      .network-paths summary {{ cursor:pointer; }}
      .risk-path-badge {{ background:#b42318; }}
      .improvement-list {{ margin:0; padding-left:18px; }}
      .improvement-list li {{ margin:7px 0; line-height:1.35; }}
      @media (max-width: 780px) {{
        .risk-summary-grid {{ grid-template-columns:repeat(2,minmax(135px,1fr)); }}
        .network-map {{ min-width:700px; }}
      }}
      {print_css}
    </style>
    <div class="risk-dashboard">
      <section class="risk-hero">
        <h2>{_h(report.get("org_name"))}</h2>
        <p class="risk-subtitle">EIN {_h(report.get("ein"))} &middot; Latest filing {_h(latest.get("tax_year"))} {_h(latest.get("return_type"))}</p>
        <div class="risk-grid risk-summary-grid">
          <div class="risk-metric">
            <div class="metric-label">Review Priority Score
              <details class="metric-help"><summary aria-label="About the Review Priority Score">i</summary>
                <div class="metric-help-card">Range 0–100. Scores 0–34 are baseline, 35–69 are moderate, and 70–100 are high review priority. This ranks review priority; it is not a fraud probability.</div>
              </details>
            </div>
            <strong>{score}</strong>
          </div>
          <div class="risk-metric"><span>Review Band</span><strong>{_h(_score_band(score))}</strong></div>
          <div class="risk-metric"><span>High</span><strong>{counts.get("High", 0)}</strong></div>
          <div class="risk-metric"><span>Medium</span><strong>{counts.get("Medium", 0)}</strong></div>
          <div class="risk-metric"><span>Low</span><strong>{counts.get("Low", 0)}</strong></div>
        </div>
        <p class="screening-note"><b>Screening result, not a fraud probability or determination.</b> The score ranges from 0–100: 0–34 baseline, 35–69 moderate, and 70–100 high review priority. This is an explainable review-priority heuristic, not a statistically validated model. Repeated yearly observations are grouped so longer filing histories do not score higher merely because they contain more returns. {data_quality_count} data-quality, {disclosure_count} routine disclosure-context, and {external_lead_count} external coverage/lead indicator(s) are displayed but excluded from the score.</p>
      </section>

      {_bmf_panel(report.get("bmf") or {})}

      {_governance_panel(report.get("governance") or {})}

      {_screening_panel(report.get("screening") or {})}

      {_external_panel(report.get("external") or {}, report.get("external_mode") or "local")}

      {_network_panel(report.get("network") or {}, report.get("org_name") or "Organization", report.get("ein") or "")}

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
        <h3>Data Coverage &amp; Remaining Work</h3>
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
