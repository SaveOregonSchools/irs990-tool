from __future__ import annotations

from urllib.parse import quote, urlencode

from flask import has_request_context, url_for


def query_url(qkey: str, **values: object) -> str:
    """Build an internal query URL that honors the app's mounted URL prefix."""
    if has_request_context():
        return url_for("query_page", qkey=qkey, **values)

    path = f"/query/{quote(str(qkey), safe='')}"
    query = urlencode({key: value for key, value in values.items() if value is not None})
    return f"{path}?{query}" if query else path
