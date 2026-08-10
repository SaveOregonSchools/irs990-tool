from typing import Iterable, Tuple

from queries._olms_common import iter_query, preview_limit, query_rows
from queries._olms_payments import build_sql, headers, render_fields as payment_fields

META = {
    "key": "olms_vendors_paid",
    "name": "OLMS Vendors / Contractors / Payees",
    "description": "Union-reported vendors, consultants, service providers, and other payees; these are not necessarily legally classified contractors.",
}
HEADERS = headers({})
META["headers"] = HEADERS


def render_fields(form) -> str:
    return payment_fields(form, grants=False)


def export_headers(form):
    return headers(form)


def run(form):
    sql, params = build_sql(form, grants=False)
    return headers(form), query_rows(sql, params, preview_limit(form))


def export_rows(form) -> Iterable[Tuple]:
    sql, params = build_sql(form, grants=False)
    return iter_query(sql, params)
