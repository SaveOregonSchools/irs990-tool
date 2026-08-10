from __future__ import annotations

import html
import re
from typing import Iterable, List, Optional, Sequence, Tuple

from common import connect_olms_ro


EDUCATION_SCOPES = ("likely_education", "education_or_mixed", "manual_include")


def h(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def truthy(form, key: str, default: bool = False) -> bool:
    value = (form or {}).get(key)
    return default if value is None else value in (True, "true", "on", "1", "yes", "y")


def to_int(value, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_float(value, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fnums(value: str) -> List[int]:
    result = []
    seen = set()
    for token in re.split(r"[,;\s]+", value or ""):
        digits = re.sub(r"\D", "", token)
        if digits:
            number = int(digits)
            if number not in seen:
                result.append(number)
                seen.add(number)
    return result


def clean_ein(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) == 9 else ""


def preview_limit(form, default: int = 500) -> int:
    return max(1, min(to_int((form or {}).get("_limit"), default) or default, 5000))


def scope_clause(scope: str, alias: str = "o") -> Tuple[str, List[object]]:
    scope = (scope or "education").strip().lower()
    if scope == "all":
        return "", []
    if scope == "nea_aft":
        return f"UPPER(COALESCE({alias}.affiliation,'')) IN ('NEA','AFT')", []
    placeholders = ",".join("?" for _ in EDUCATION_SCOPES)
    return f"{alias}.education_scope IN ({placeholders})", list(EDUCATION_SCOPES)


def scope_select(form) -> str:
    value = ((form or {}).get("scope") or "education").lower()
    options = [
        ("education", "Education-related OLMS organizations"),
        ("nea_aft", "NEA/AFT organizations"),
        ("all", "All OLMS organizations"),
    ]
    return "<select name='scope'>" + "".join(
        f'<option value="{key}"{" selected" if key == value else ""}>{h(label)}</option>'
        for key, label in options
    ) + "</select>"


def mode_select(form, summary_label: str = "Annual payee summary") -> str:
    value = ((form or {}).get("mode") or "summary").lower()
    return (
        '<select name="mode">'
        f'<option value="summary"{" selected" if value == "summary" else ""}>{h(summary_label)}</option>'
        f'<option value="transactions"{" selected" if value == "transactions" else ""}>Itemized transaction detail</option>'
        '</select>'
    )


def query_rows(sql: str, params: Sequence[object], limit: Optional[int] = None) -> List[Tuple]:
    conn = connect_olms_ro()
    try:
        if limit is not None:
            sql = sql.rstrip().rstrip(";") + " LIMIT ?"
            params = list(params) + [limit]
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def iter_query(sql: str, params: Sequence[object]) -> Iterable[Tuple]:
    def generate():
        conn = connect_olms_ro()
        try:
            for row in conn.execute(sql, params):
                yield row
        finally:
            conn.close()

    return generate()


def add_like(clauses: List[str], params: List[object], expression: str, value: str) -> None:
    value = (value or "").strip()
    if value:
        clauses.append(f"UPPER(COALESCE({expression},'')) LIKE ?")
        params.append("%" + value.upper() + "%")


def year_filters(
    clauses: List[str], params: List[object], expression: str, min_year, max_year
) -> None:
    low = to_int(min_year)
    high = to_int(max_year)
    if low is not None and high is None:
        high = low
    if high is not None and low is None:
        low = high
    if low is not None and high is not None:
        if low > high:
            low, high = high, low
        clauses.append(f"CAST(strftime('%Y',{expression}) AS INTEGER) BETWEEN ? AND ?")
        params.extend([low, high])


def where_sql(clauses: Sequence[str]) -> str:
    return " WHERE " + " AND ".join(clauses) if clauses else ""
