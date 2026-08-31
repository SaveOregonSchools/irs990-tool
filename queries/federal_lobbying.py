from __future__ import annotations

import html
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from flask import has_request_context, url_for

from common import connect_ro, normalize_eins
from queries._links import query_url


META = {
    "key": "federal_lobbying",
    "name": "Federal Lobbyist Registrations & Reports",
    "description": (
        "Enter an EIN or organization name, select the correct LDA.gov registrant "
        "or client match, and view its registrations, quarterly activity reports, "
        "amendments, and terminations."
    ),
}

HEADERS = [
    "Registrant Name",
    "Client Name",
    "Report Type",
    "Amount Reported",
    "Filing Year",
    "Posted Date",
]
MATCH_HEADERS = [
    "LDA Role",
    "Organization Name",
    "State",
    "Associated Registrant",
]
META["headers"] = HEADERS

HIDE_PREVIEW_LIMIT = True
HIDE_CSV_EXPORT = True
DISABLE_ROW_LIMIT = True
EXPORTS_REQUIRE_RESULTS = True
RUN_BUTTON_LABEL = "Find LDA.gov Matches"

_LDA_BASE_URL = "https://lda.gov/api/v1/"
_REGISTRANTS_URL = _LDA_BASE_URL + "registrants/"
_CLIENTS_URL = _LDA_BASE_URL + "clients/"
_FILINGS_URL = _LDA_BASE_URL + "filings/"
_ALLOWED_API_PATHS = {
    "/api/v1/registrants/",
    "/api/v1/clients/",
    "/api/v1/filings/",
}
_PAGE_SIZE = 25  # LDA.gov's documented maximum.
_MAX_MATCHES_PER_ROLE = 50
_MAX_NAME_CANDIDATES = 5  # Keep a no-match lookup below the anonymous rate limit.
_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_NAME_TOKEN_RE = re.compile(r"[A-Z0-9]+")
_LEGAL_SUFFIXES = {
    "CO",
    "COMPANY",
    "CORP",
    "CORPORATION",
    "INC",
    "INCORPORATED",
    "LLC",
    "LP",
    "LLP",
    "LTD",
    "LIMITED",
    "PLLC",
}

_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 32
_CACHE_LOCK = threading.Lock()
_CACHE: "OrderedDict[str, Tuple[float, Tuple[Tuple, ...]]]" = OrderedDict()


class FilingRegistrant(str):
    """A display-safe string that carries its filing link for custom rendering."""

    def __new__(cls, name: str, filing_url: str):
        value = str.__new__(cls, name)
        value.filing_url = filing_url
        return value


class MatchName(str):
    """An LDA organization name carrying the selected API entity identifier."""

    def __new__(cls, name: str, role: str, entity_id: int):
        value = str.__new__(cls, name)
        value.match_role = role
        value.entity_id = entity_id
        return value


def _h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_fields(form) -> str:
    ein = (form or {}).get("ein", "")
    organization_name = (form or {}).get("organization_name", "")
    return f"""
    <div class="row" style="display:flex; gap:22px; flex-wrap:wrap; align-items:flex-start;">
      <div>
        <label for="ein"><b>EIN (Federal Tax ID)</b></label><br>
        <input id="ein" name="ein" type="text" inputmode="numeric"
               autocomplete="off" value="{_h(ein)}" placeholder="e.g. 52-1198450"
               maxlength="12" style="width:190px;">
      </div>
      <div style="padding-top:28px; color:#666; font-weight:650;">or</div>
      <div style="flex:1 1 420px;">
        <label for="organization_name"><b>Organization name</b></label><br>
        <input id="organization_name" name="organization_name" type="text"
               autocomplete="organization" value="{_h(organization_name)}"
               placeholder="e.g. Institute for Educational Leadership"
               maxlength="200" style="width:min(100%, 520px);">
      </div>
    </div>
    <div style="color:#666; font-size:90%; margin:6px 0 12px; max-width:820px;">
      Enter either one 9-digit EIN or an organization name. First, the tool shows
      possible registrant and client names from LDA.gov. Select the best match to
      retrieve its LD-1 and LD-2 filings. LDA.gov does not publish EINs.
    </div>
    """


def _parse_ein(form) -> str:
    eins = normalize_eins((form or {}).get("ein", ""))
    if len(eins) != 1:
        raise ValueError("Enter exactly one valid 9-digit EIN.")
    return eins[0]


def _parse_search(form) -> Tuple[str, List[str]]:
    raw_ein = str((form or {}).get("ein") or "").strip()
    organization_name = str((form or {}).get("organization_name") or "").strip()
    if raw_ein and organization_name:
        raise ValueError("Enter either an EIN or an organization name, not both.")
    if raw_ein:
        ein = _parse_ein(form)
        candidates = _resolve_name_candidates(ein)
        if not candidates:
            raise ValueError(f"No IRS organization name was found for EIN {ein}.")
        return ein, candidates
    if not organization_name:
        raise ValueError("Enter either one valid 9-digit EIN or an organization name.")
    if len(organization_name) < 2:
        raise ValueError("Enter at least two characters of an organization name.")
    return "", [organization_name]


def _name_key(value: object) -> str:
    text = str(value or "").upper().replace("&", " AND ")
    tokens = _NAME_TOKEN_RE.findall(text)
    if tokens[-3:] == ["L", "L", "C"]:
        tokens[-3:] = ["LLC"]
    elif tokens[-3:] == ["L", "L", "P"]:
        tokens[-3:] = ["LLP"]
    elif tokens[-2:] == ["L", "P"]:
        tokens[-2:] = ["LP"]
    if tokens and tokens[0] == "THE":
        tokens = tokens[1:]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _names_equivalent(left: object, right: object) -> bool:
    left_key = _name_key(left)
    return bool(left_key and left_key == _name_key(right))


def _resolve_name_candidates(ein: str) -> List[str]:
    """Return current-to-old IRS name combinations and individual fields."""
    conn = connect_ro()
    try:
        rows = conn.execute(
            """
            SELECT r.org_name, r.dba_name
            FROM canonical_by_ein_year c
            JOIN returns r ON r.filing_id = c.filing_id
            WHERE c.ein = ?
            ORDER BY c.tax_year DESC, c.filing_id DESC
            """,
            (ein,),
        ).fetchall()
    finally:
        conn.close()

    candidates: List[str] = []
    seen = set()
    for org_name, dba_name in rows:
        org_name = str(org_name or "").strip()
        dba_name = str(dba_name or "").strip()
        combined = " ".join(value for value in (org_name, dba_name) if value)
        for value in (combined, org_name, dba_name):
            name = str(value or "").strip()
            key = _name_key(name)
            if name and key and key not in seen:
                seen.add(key)
                candidates.append(name)
                if len(candidates) >= _MAX_NAME_CANDIDATES:
                    return candidates
    return candidates


def _timeout_seconds() -> float:
    try:
        value = float(os.getenv("LDA_API_TIMEOUT", "30"))
    except (TypeError, ValueError):
        value = 30.0
    return max(5.0, min(value, 120.0))


def _request_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "IRS990-Research-Tool/1.0 "
            "(+https://github.com/SaveOregonSchools/irs990-tool)"
        ),
    }
    token = (os.getenv("LDA_API_TOKEN") or os.getenv("LDA_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = token if token.startswith("Token ") else f"Token {token}"
    return headers


def _validate_api_url(url: object) -> str:
    value = str(url or "")
    parsed = urllib.parse.urlsplit(value)
    try:
        unsafe = (
            parsed.scheme != "https"
            or parsed.hostname != "lda.gov"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in _ALLOWED_API_PATHS
        )
    except ValueError:
        unsafe = True
    if unsafe:
        raise RuntimeError("LDA.gov returned an unsafe pagination URL.")
    return value


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow API redirects only when they stay on an approved LDA.gov endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_api_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _lda_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # Python 3.14 enables strict RFC 5280 validation by default. LDA.gov's
    # otherwise-valid federal certificate chain currently needs the pre-3.14
    # strictness setting, while certificate and hostname validation remain on.
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    return context


def _open_api_request(request: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(
        _SameOriginRedirectHandler(),
        urllib.request.HTTPSHandler(context=_lda_tls_context()),
    )
    return opener.open(request, timeout=timeout)


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(4096)
        payload = json.loads(raw.decode("utf-8"))
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if detail:
            return str(detail)
    except Exception:
        pass
    return str(exc.reason or "HTTP error")


def _api_get_json(url: str) -> Mapping[str, object]:
    safe_url = _validate_api_url(url)
    request = urllib.request.Request(safe_url, headers=_request_headers(), method="GET")
    try:
        with _open_api_request(request, _timeout_seconds()) as response:
            geturl = getattr(response, "geturl", None)
            if callable(geturl):
                final_url = geturl()
                if final_url:
                    _validate_api_url(final_url)
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if exc.code == 429:
            raise RuntimeError(
                "LDA.gov rate limit reached. Try again after the server's retry "
                "window, or configure LDA_API_TOKEN for the higher registered limit. "
                f"LDA.gov response: {detail}"
            ) from exc
        raise RuntimeError(f"LDA.gov API request failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the LDA.gov API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("The LDA.gov API request timed out.") from exc

    if len(body) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("LDA.gov returned an unexpectedly large response.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("LDA.gov returned an invalid JSON response.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("LDA.gov returned an unexpected response shape.")
    return payload


def _collection_url(endpoint: str, params: Mapping[str, object]) -> str:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value != ""})
    return f"{endpoint}?{query}"


def _fetch_collection(
    endpoint: str,
    params: Mapping[str, object],
    *,
    max_results: int | None = None,
) -> List[Mapping[str, object]]:
    query = dict(params)
    query["page_size"] = _PAGE_SIZE
    url = _collection_url(endpoint, query)
    results: List[Mapping[str, object]] = []
    seen_urls = set()

    while url:
        safe_url = _validate_api_url(url)
        if safe_url in seen_urls:
            raise RuntimeError("LDA.gov returned a pagination loop.")
        seen_urls.add(safe_url)

        payload = _api_get_json(safe_url)
        page_results = payload.get("results")
        if not isinstance(page_results, list):
            raise RuntimeError("LDA.gov returned results in an unexpected format.")
        valid_results = [item for item in page_results if isinstance(item, dict)]
        if max_results is not None:
            valid_results = valid_results[: max(0, max_results - len(results))]
        results.extend(valid_results)
        if max_results is not None and len(results) >= max_results:
            break

        next_url = payload.get("next")
        url = _validate_api_url(next_url) if next_url else ""
    return results


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 < parsed < 2**63 else None


def _match_rank(source_name: str, official_name: object) -> Tuple[int, str]:
    source_key = _name_key(source_name)
    official_key = _name_key(official_name)
    if source_key and source_key == official_key:
        return 0, official_key
    if source_key and official_key and (
        source_key in official_key or official_key in source_key
    ):
        return 1, official_key
    return 2, official_key


def _find_matches(candidates: Sequence[str]) -> List[Tuple]:
    """Return possible official LDA registrant/client choices for one IRS name."""
    for source_name in candidates:
        rows: List[Tuple] = []
        seen = set()
        registrants = _fetch_collection(
            _REGISTRANTS_URL,
            {"registrant_name": source_name},
            max_results=_MAX_MATCHES_PER_ROLE,
        )
        clients = _fetch_collection(
            _CLIENTS_URL,
            {"client_name": source_name},
            max_results=_MAX_MATCHES_PER_ROLE,
        )

        for role, items in (("registrant", registrants), ("client", clients)):
            for item in items:
                entity_id = _positive_int(item.get("id"))
                name = str(item.get("name") or "").strip()
                if entity_id is None or not name or (role, entity_id) in seen:
                    continue
                seen.add((role, entity_id))
                registrant = item.get("registrant")
                registrant = registrant if isinstance(registrant, dict) else {}
                state = str(item.get("state_display") or item.get("state") or "")
                rows.append(
                    (
                        role.title(),
                        MatchName(name, role, entity_id),
                        state,
                        str(registrant.get("name") or "") if role == "client" else "",
                    )
                )
        if rows:
            rows.sort(
                key=lambda row: (
                    *_match_rank(source_name, row[1]),
                    0 if row[0] == "Registrant" else 1,
                    str(row[3]).casefold(),
                )
            )
            return rows
    return []


def _filing_url(filing_uuid: object) -> str:
    value = str(filing_uuid or "").strip()
    if not _UUID_RE.fullmatch(value):
        return ""
    return f"https://lda.gov/filings/public/filing/{value}/print/"


def _amount_reported(filing: Mapping[str, object]) -> str:
    value = filing.get("income")
    if value in (None, ""):
        value = filing.get("expenses")
    if value in (None, ""):
        return ""
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def _filing_row(filing: Mapping[str, object]) -> Tuple:
    registrant = filing.get("registrant")
    client = filing.get("client")
    registrant = registrant if isinstance(registrant, dict) else {}
    client = client if isinstance(client, dict) else {}
    registrant_name = str(registrant.get("name") or "")
    filing_link = _filing_url(filing.get("filing_uuid"))
    try:
        filing_year: object = int(filing.get("filing_year"))
    except (TypeError, ValueError):
        filing_year = ""
    return (
        FilingRegistrant(registrant_name, filing_link),
        str(client.get("name") or ""),
        str(filing.get("filing_type_display") or filing.get("filing_type") or ""),
        _amount_reported(filing),
        filing_year,
        str(filing.get("dt_posted") or ""),
    )


def _posted_timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return float("-inf")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _parse_selected_match(form) -> Tuple[str, int, str]:
    role = str((form or {}).get("selected_role") or "").strip().lower()
    if role not in {"registrant", "client"}:
        raise ValueError("Select a valid LDA.gov registrant or client match.")
    entity_id = _positive_int((form or {}).get("selected_id"))
    if entity_id is None:
        raise ValueError("Select a valid LDA.gov organization match.")
    name = str((form or {}).get("selected_name") or "").strip()
    if not name or len(name) > 300:
        raise ValueError("Select a valid LDA.gov organization name.")
    return role, entity_id, name


def _filings_for_match(role: str, entity_id: int, selected_name: str) -> List[Tuple]:
    id_field = "registrant_id" if role == "registrant" else "client_id"
    role_field = "registrant" if role == "registrant" else "client"
    filings: Dict[str, Mapping[str, object]] = {}
    anonymous_rows: List[Mapping[str, object]] = []
    for filing in _fetch_collection(
        _FILINGS_URL,
        {id_field: entity_id, "ordering": "-dt_posted"},
    ):
        selected_entity = filing.get(role_field)
        selected_entity = selected_entity if isinstance(selected_entity, dict) else {}
        if not _names_equivalent(selected_name, selected_entity.get("name")):
            continue
        filing_uuid = str(filing.get("filing_uuid") or "").strip()
        if filing_uuid:
            filings[filing_uuid] = filing
        else:
            anonymous_rows.append(filing)

    rows = [_filing_row(item) for item in filings.values()]
    rows.extend(_filing_row(item) for item in anonymous_rows)
    rows.sort(key=lambda row: _posted_timestamp(row[5]), reverse=True)
    return rows


def _cache_get(cache_key: str) -> List[Tuple] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(cache_key)
        if entry is None:
            return None
        created_at, cached_rows = entry
        if now - created_at > _CACHE_TTL_SECONDS:
            del _CACHE[cache_key]
            return None
        _CACHE.move_to_end(cache_key)
        return list(cached_rows)


def _cache_put(cache_key: str, rows: Sequence[Tuple]) -> None:
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic(), tuple(rows))
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > _CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)


def run(form):
    if (form or {}).get("action") == "load_filings":
        role, entity_id, selected_name = _parse_selected_match(form)
        cache_key = f"filings:{role}:{entity_id}:{_name_key(selected_name)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return HEADERS, cached
        rows = _filings_for_match(role, entity_id, selected_name)
        _cache_put(cache_key, rows)
        return HEADERS, rows

    _ein, candidates = _parse_search(form)
    cache_key = "matches:" + "\x1f".join(_name_key(name) for name in candidates)
    cached = _cache_get(cache_key)
    if cached is not None:
        return MATCH_HEADERS, cached
    rows = _find_matches(candidates)
    _cache_put(cache_key, rows)
    return MATCH_HEADERS, rows


def export_rows(form) -> Iterable[Tuple]:
    return run(form)[1]


def export_headers(form) -> List[str]:
    return HEADERS if (form or {}).get("action") == "load_filings" else MATCH_HEADERS


def _format_amount(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"${Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def _format_posted(value: object) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%m/%d/%Y @ %I:%M %p")
    except ValueError:
        return text


def _safe_filing_link(value: object) -> str:
    url = str(value or "")
    parsed = urllib.parse.urlsplit(url)
    path_match = re.fullmatch(
        r"/filings/public/filing/([0-9a-fA-F-]+)/print/", parsed.path
    )
    try:
        safe_origin = (
            parsed.scheme == "https"
            and parsed.hostname == "lda.gov"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        safe_origin = False
    if not safe_origin or not path_match or not _UUID_RE.fullmatch(path_match.group(1)):
        return ""
    return url


def _route_url(endpoint: str, fallback: str) -> str:
    return url_for(endpoint) if has_request_context() else fallback


def _hidden(name: str, value: object) -> str:
    return f'<input type="hidden" name="{_h(name)}" value="{_h(value)}">'


def _render_match_results(form, rows) -> str:
    if not rows:
        return """
        <p><b>No possible LDA.gov registrant or client names were found.</b></p>
        <p class="description">Try a shorter or alternate organization name. If
        you searched by EIN, you can also search directly by organization name.</p>
        """

    action_url = query_url(META["key"])
    source_ein = (form or {}).get("ein", "")
    source_name = (form or {}).get("organization_name", "")
    body_rows = []
    for role_label, organization_name, state, associated_registrant in rows:
        role = getattr(organization_name, "match_role", str(role_label).lower())
        entity_id = getattr(organization_name, "entity_id", "")
        selection_form = (
            f'<form method="post" action="{_h(action_url)}" '
            'style="margin:0;" onsubmit="document.body.classList.add(\'is-running\')">'
            + _hidden("qkey", META["key"])
            + _hidden("action", "load_filings")
            + _hidden("selected_role", role)
            + _hidden("selected_id", entity_id)
            + _hidden("selected_name", organization_name)
            + _hidden("ein", source_ein)
            + _hidden("organization_name", source_name)
            + f'<button type="submit" class="lda-match-button">{_h(organization_name)}</button>'
            + "</form>"
        )
        body_rows.append(
            "<tr>"
            f"<td>{_h(role_label)}</td>"
            f"<td>{selection_form}</td>"
            f"<td>{_h(state)}</td>"
            f'<td title="{_h(associated_registrant)}">{_h(associated_registrant)}</td>'
            "</tr>"
        )

    return f"""
    <style>
      .lda-match-button {{
        min-height:0; padding:0; border:0; color:var(--primary);
        background:transparent; font:inherit; font-weight:700; text-align:left;
        justify-content:flex-start; text-decoration:underline;
      }}
      .lda-match-button:hover, .lda-match-button:focus-visible {{
        color:var(--primary-dark); background:transparent;
      }}
    </style>
    <p><b>Possible LDA.gov matches</b></p>
    <p class="description">Select the official organization name that best matches
    your search. Client choices show the registrant associated with that specific
    LDA registration.</p>
    <div style="overflow:auto; max-height:60vh; border:1px solid #ddd;">
      <table>
        <thead><tr>{''.join(f'<th scope="col">{_h(label)}</th>' for label in MATCH_HEADERS)}</tr></thead>
        <tbody>{''.join(body_rows)}</tbody>
      </table>
    </div>
    """


def render_results(form, headers, rows) -> str:
    if list(headers or []) == MATCH_HEADERS:
        return _render_match_results(form, rows)
    if not rows:
        return (
            f"<p><b>No LDA.gov filings were found for the selected "
            f"{_h((form or {}).get('selected_role', 'organization'))}: "
            f"{_h((form or {}).get('selected_name', ''))}.</b></p>"
        )

    body_rows = []
    for row in rows:
        registrant, client, report_type, amount, filing_year, posted = row
        filing_link = _safe_filing_link(getattr(registrant, "filing_url", ""))
        if filing_link:
            registrant_html = (
                f'<a href="{_h(filing_link)}" target="_blank" '
                f'rel="noopener noreferrer">{_h(registrant)}</a>'
            )
        else:
            registrant_html = _h(registrant)
        posted_sort = _posted_timestamp(posted)
        posted_sort_value = "" if posted_sort == float("-inf") else str(posted_sort)
        try:
            amount_sort = str(Decimal(str(amount))) if amount not in (None, "") else ""
        except (InvalidOperation, ValueError):
            amount_sort = ""
        body_rows.append(
            "<tr>"
            f'<td data-sort="{_h(_name_key(registrant))}" title="{_h(registrant)}">{registrant_html}</td>'
            f'<td data-sort="{_h(_name_key(client))}" title="{_h(client)}">{_h(client)}</td>'
            f'<td data-sort="{_h(report_type)}" title="{_h(report_type)}">{_h(report_type)}</td>'
            f'<td data-sort="{_h(amount_sort)}" title="{_h(amount)}">{_h(_format_amount(amount))}</td>'
            f'<td data-sort="{_h(filing_year)}">{_h(filing_year)}</td>'
            f'<td data-sort="{_h(posted_sort_value)}" title="{_h(posted)}">{_h(_format_posted(posted))}</td>'
            "</tr>"
        )

    header_cells = []
    kinds = ("text", "text", "text", "number", "number", "date")
    for index, (label, kind) in enumerate(zip(HEADERS, kinds)):
        initial_sort = ' aria-sort="descending"' if index == 5 else ' aria-sort="none"'
        indicator = "&#9660;" if index == 5 else ""
        header_cells.append(
            f'<th scope="col"{initial_sort}>'
            f'<button type="button" class="lda-sort-button" data-index="{index}" '
            f'data-kind="{kind}">{_h(label)} '
            f'<span class="lda-sort-indicator" aria-hidden="true">{indicator}</span>'
            "</button></th>"
        )

    selected_role = str((form or {}).get("selected_role") or "organization").title()
    selected_name = (form or {}).get("selected_name", "")
    export_form = (
        f'<form method="post" action="{_h(_route_url("export", "/export"))}" '
        'style="margin:0 0 10px;">'
        + _hidden("qkey", META["key"])
        + _hidden("action", "load_filings")
        + _hidden("selected_role", (form or {}).get("selected_role", ""))
        + _hidden("selected_id", (form or {}).get("selected_id", ""))
        + _hidden("selected_name", selected_name)
        + '<button type="submit" class="secondary">Export CSV (full result)</button>'
        + "</form>"
    )

    return f"""
    <style>
      .lda-results-note {{ margin: 14px 0 8px; }}
      .lda-sort-button {{
        min-height: 0; padding: 0; border: 0; border-radius: 0;
        color: inherit; background: transparent; font: inherit; font-weight: 700;
        text-align: left; justify-content: flex-start;
      }}
      .lda-sort-button:hover, .lda-sort-button:focus-visible {{
        color: var(--primary-dark); background: transparent; text-decoration: underline;
      }}
      .lda-sort-indicator {{ display:inline-block; min-width:0.9em; }}
    </style>
    <p class="lda-results-note">
      Selected <b>{_h(selected_role)}</b>: <b>{_h(selected_name)}</b>.<br>
      Found <b>{len(rows):,}</b> filing{'s' if len(rows) != 1 else ''}.
      Select any column heading to sort; select it again to reverse the order.
      Click a registrant name to open the related filing on LDA.gov.
    </p>
    {export_form}
    <div style="overflow:auto; max-height:65vh; border:1px solid #ddd;">
      <table id="lda-results-table">
        <thead><tr>{''.join(header_cells)}</tr></thead>
        <tbody>{''.join(body_rows)}</tbody>
      </table>
    </div>
    <script>
      (function() {{
        const table = document.getElementById("lda-results-table");
        if (!table) return;
        const tbody = table.tBodies[0];
        const buttons = Array.from(table.querySelectorAll(".lda-sort-button"));
        const collator = new Intl.Collator(undefined, {{numeric: true, sensitivity: "base"}});
        let activeIndex = 5;
        let ascending = false;

        function compare(left, right, kind) {{
          const leftEmpty = left === "";
          const rightEmpty = right === "";
          if (leftEmpty && rightEmpty) return 0;
          if (leftEmpty) return 1;
          if (rightEmpty) return -1;
          if (kind === "number" || kind === "date") {{
            return Number(left) - Number(right);
          }}
          return collator.compare(left, right);
        }}

        buttons.forEach(function(button) {{
          button.addEventListener("click", function() {{
            const index = Number(button.dataset.index);
            const kind = button.dataset.kind;
            ascending = index === activeIndex ? !ascending : true;
            activeIndex = index;
            const ordered = Array.from(tbody.rows).map(function(row, originalIndex) {{
              return {{row: row, originalIndex: originalIndex}};
            }});
            ordered.sort(function(a, b) {{
              const left = a.row.cells[index].dataset.sort || "";
              const right = b.row.cells[index].dataset.sort || "";
              const leftEmpty = left === "";
              const rightEmpty = right === "";
              let result;
              if (leftEmpty || rightEmpty) {{
                result = compare(left, right, kind);
              }} else {{
                result = compare(left, right, kind);
                if (!ascending) result = -result;
              }}
              return result || (a.originalIndex - b.originalIndex);
            }});
            ordered.forEach(function(item) {{ tbody.appendChild(item.row); }});

            buttons.forEach(function(other) {{
              const th = other.closest("th");
              const indicator = other.querySelector(".lda-sort-indicator");
              if (other === button) {{
                th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
                indicator.textContent = ascending ? "\u25B2" : "\u25BC";
              }} else {{
                th.setAttribute("aria-sort", "none");
                indicator.textContent = "";
              }}
            }});
          }});
        }});
      }})();
    </script>
    """
