from typing import Iterable, Tuple

from queries._olms_common import iter_query, preview_limit, query_rows
from queries._olms_payments import build_sql, headers, render_fields as payment_fields

META = {
    "key": "olms_grants_paid",
    "name": "OLMS Grants / Contributions Paid",
    "description": "Annual payee totals or itemized transactions reported under OLMS disbursement code 503.",
}
HEADERS = headers({})
META["headers"] = HEADERS


def render_fields(form) -> str:
    return payment_fields(form, grants=True)


def export_headers(form):
    return headers(form)


def run(form):
    sql, params = build_sql(form, grants=True)
    return headers(form), query_rows(sql, params, preview_limit(form))


def export_rows(form) -> Iterable[Tuple]:
    sql, params = build_sql(form, grants=True)
    return iter_query(sql, params)
