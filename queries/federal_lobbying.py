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

from common import connect_ro, normalize_eins


META = {
    "key": "federal_lobbying",
    "name": "Federal Lobbyist Registrations & Reports",
    "description": (
        "Enter one EIN to resolve its IRS organization name and find related "
        "registrations, quarterly activity reports, amendments, and terminations "
        "through the official LDA.gov API."
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
META["headers"] = HEADERS

HIDE_PREVIEW_LIMIT = True
DISABLE_ROW_LIMIT = True
RUN_BUTTON_LABEL = "Search LDA.gov"

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


def _h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_fields(form) -> str:
    ein = (form or {}).get("ein", "")
    return f"""
    <div class="row">
      <label for="ein"><b>EIN (Federal Tax ID):</b></label><br>
      <input id="ein" name="ein" type="text" inputmode="numeric"
             autocomplete="off" value="{_h(ein)}" placeholder="e.g. 52-1234567"
             maxlength="12" style="width:190px;">
      <div style="color:#666; font-size:90%; margin-top:4px; max-width:780px;">
        Enter one 9-digit EIN. The tool resolves the latest IRS organization name,
        verifies matching registrant and client names through LDA.gov, and then
        retrieves all matching LD-1 and LD-2 filings. LDA.gov does not publish EINs.
      </div>
    </div>
    """


def _parse_ein(form) -> str:
    eins = normalize_eins((form or {}).get("ein", ""))
    if len(eins) != 1:
        raise ValueError("Enter exactly one valid 9-digit EIN.")
    return eins[0]


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
    """Return current-to-old IRS legal/DBA names for an EIN."""
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
        for value in (org_name, dba_name):
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


def _fetch_collection(endpoint: str, params: Mapping[str, object]) -> List[Mapping[str, object]]:
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
        results.extend(item for item in page_results if isinstance(item, dict))

        next_url = payload.get("next")
        url = _validate_api_url(next_url) if next_url else ""
    return results


def _official_names(endpoint: str, field: str, source_name: str) -> List[str]:
    names: List[str] = []
    seen = set()
    for item in _fetch_collection(endpoint, {field: source_name}):
        name = str(item.get("name") or "").strip()
        key = name.casefold()
        if _names_equivalent(source_name, name) and key not in seen:
            seen.add(key)
            names.append(name)
    return names


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


def _find_filings(candidates: Sequence[str]) -> List[Tuple]:
    for source_name in candidates:
        registrant_names = _official_names(
            _REGISTRANTS_URL, "registrant_name", source_name
        )
        client_names = _official_names(_CLIENTS_URL, "client_name", source_name)
        if not registrant_names and not client_names:
            continue

        filings: Dict[str, Mapping[str, object]] = {}
        anonymous_rows: List[Mapping[str, object]] = []
        searches = [
            ("registrant_name", name) for name in registrant_names
        ] + [("client_name", name) for name in client_names]
        for field, official_name in searches:
            for filing in _fetch_collection(
                _FILINGS_URL,
                {field: official_name, "ordering": "-dt_posted"},
            ):
                role = filing.get("registrant" if field == "registrant_name" else "client")
                role = role if isinstance(role, dict) else {}
                if not _names_equivalent(official_name, role.get("name")):
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
    return []


def _cache_get(ein: str) -> List[Tuple] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(ein)
        if entry is None:
            return None
        created_at, cached_rows = entry
        if now - created_at > _CACHE_TTL_SECONDS:
            del _CACHE[ein]
            return None
        _CACHE.move_to_end(ein)
        return list(cached_rows)


def _cache_put(ein: str, rows: Sequence[Tuple]) -> None:
    with _CACHE_LOCK:
        _CACHE[ein] = (time.monotonic(), tuple(rows))
        _CACHE.move_to_end(ein)
        while len(_CACHE) > _CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)


def run(form):
    ein = _parse_ein(form)
    cached = _cache_get(ein)
    if cached is not None:
        return HEADERS, cached

    candidates = _resolve_name_candidates(ein)
    if not candidates:
        raise ValueError(f"No IRS organization name was found for EIN {ein}.")
    rows = _find_filings(candidates)
    _cache_put(ein, rows)
    return HEADERS, rows


def export_rows(form) -> Iterable[Tuple]:
    return run(form)[1]


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


def render_results(form, headers, rows) -> str:
    if not rows:
        return """
        <p><b>No exact LDA.gov registrant or client match was found for this EIN's
        IRS organization name.</b></p>
        <p class="description">LDA.gov does not publish EINs, so name differences
        or an absence of federal lobbying filings can produce no results.</p>
        """

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
      Found <b>{len(rows):,}</b> filing{'s' if len(rows) != 1 else ''}.
      Select any column heading to sort; select it again to reverse the order.
      Click a registrant name to open the related filing on LDA.gov.
    </p>
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
