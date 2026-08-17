from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Mapping

try:
    from fac_bulk import lookup_fac_by_ein as _lookup_offline_fac
except ImportError:  # pragma: no cover - permits isolated query-module reuse
    _lookup_offline_fac = None


_USER_AGENT = "irs990-tool-risk-dashboard/1.0"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 128
_MAX_FAC_REPORTS = 5
_MAX_FAC_ADDITIONAL_REPORTS = 10
_MAX_FAC_FINDINGS = 250
_MAX_FAC_AWARDS = 500
# FAC finding narratives can each be roughly 20 KiB. Keep the combined response
# comfortably below the transport's 4 MiB safety ceiling while still retaining a
# generous number of findings for the two most recent finding-bearing reports.
_MAX_FAC_TEXT_ROWS = 50
_MAX_FAC_TEXT_REPORTS = 2
_MAX_COMBINED_FAC_REPORTS = 25
_MAX_UEIS = 3
_DEFAULT_SAM_MAX_UEIS = 1
_DEFAULT_SAM_REQUEST_BUDGET = 3
_MAX_SAM_REQUEST_BUDGET = 10
_SAM_PAGE_SIZE = 10
_SAM_LOWEST_PUBLISHED_DAILY_QUOTA = 10
_MAX_FEC_CANDIDATES = 20
_MAX_LDA_CLIENTS = 3
_MAX_LDA_FILINGS = 10

# Python 3.14 enables OpenSSL's strict RFC 5280 checks by default. These fixed
# official federal hosts currently serve otherwise-valid chains whose CA Basic
# Constraints extension is not marked critical. For only this allowlist, retain
# certificate and hostname verification while using the pre-3.14 strictness.
_FEDERAL_TLS_COMPAT_HOSTS = {
    "api.fac.gov",
    "api.open.fec.gov",
    "api.sam.gov",
    "api.usaspending.gov",
    "lda.gov",
}
_FEDERAL_API_PATH_PREFIXES = {
    "api.fac.gov": ("/",),
    "api.open.fec.gov": ("/v1/",),
    "api.sam.gov": ("/entity-information/",),
    "api.usaspending.gov": ("/api/v2/",),
    "lda.gov": ("/api/v1/",),
}

_CACHE: dict[tuple[str, str, str, str, str], tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.RLock()

_SOURCE_INFO = {
    "fac": {
        "name": "Federal Audit Clearinghouse",
        "url": "https://www.fac.gov/data/",
    },
    "usaspending": {
        "name": "USAspending",
        "url": "https://www.usaspending.gov/",
    },
    "sam": {
        "name": "SAM.gov",
        "url": "https://sam.gov/",
    },
    "fec": {
        "name": "Federal Election Commission",
        "url": "https://www.fec.gov/data/",
    },
    "lda": {
        "name": "Lobbying Disclosure Act database",
        "url": "https://lda.gov/",
    },
}

_FAC_GENERAL_FIELDS = (
    "report_id",
    "audit_year",
    "fy_start_date",
    "fy_end_date",
    "submitted_date",
    "fac_accepted_date",
    "auditee_ein",
    "auditee_uei",
    "auditee_name",
    "entity_type",
    "audit_type",
    "total_amount_expended",
    "gaap_results",
    "is_going_concern_included",
    "is_internal_control_material_weakness_disclosed",
    "is_internal_control_deficiency_disclosed",
    "is_material_noncompliance_disclosed",
    "is_low_risk_auditee",
    "agencies_with_prior_findings",
    "auditor_firm_name",
    "is_public",
    "resubmission_version",
    "resubmission_status",
)

_FAC_ADDITIONAL_EIN_FIELDS = (
    "report_id",
    "audit_year",
    "fac_accepted_date",
    "auditee_uei",
    "additional_ein",
)

_FAC_FINDING_FIELDS = (
    "report_id",
    "audit_year",
    "fac_accepted_date",
    "auditee_uei",
    "award_reference",
    "reference_number",
    "type_requirement",
    "is_modified_opinion",
    "is_other_matters",
    "is_material_weakness",
    "is_significant_deficiency",
    "is_other_findings",
    "is_questioned_costs",
    "is_repeat_finding",
    "prior_finding_ref_numbers",
)

_FAC_AWARD_FIELDS = (
    "report_id",
    "audit_year",
    "fac_accepted_date",
    "auditee_uei",
    "federal_agency_prefix",
    "federal_award_extension",
    "additional_award_identification",
    "federal_program_name",
    "amount_expended",
    "cluster_name",
    "state_cluster_name",
    "federal_program_total",
    "cluster_total",
    "is_direct",
    "is_passthrough_award",
    "passthrough_amount",
    "is_major",
    "audit_report_type",
    "is_loan",
    "loan_balance",
    "findings_count",
    "award_reference",
)

_FAC_GENERAL_BOOLEAN_FIELDS = (
    "is_going_concern_included",
    "is_internal_control_material_weakness_disclosed",
    "is_internal_control_deficiency_disclosed",
    "is_material_noncompliance_disclosed",
    "is_low_risk_auditee",
    "is_public",
)

_FAC_FINDING_BOOLEAN_FIELDS = (
    "is_modified_opinion",
    "is_other_matters",
    "is_material_weakness",
    "is_significant_deficiency",
    "is_other_findings",
    "is_questioned_costs",
    "is_repeat_finding",
)

_FAC_FINDING_TEXT_FIELDS = (
    "report_id",
    "audit_year",
    "fac_accepted_date",
    "auditee_uei",
    "finding_ref_number",
    "finding_text",
    "contains_chart_or_table",
)

_FAC_CORRECTIVE_ACTION_FIELDS = (
    "report_id",
    "audit_year",
    "fac_accepted_date",
    "auditee_uei",
    "finding_ref_number",
    "planned_action",
    "contains_chart_or_table",
)

_FAC_AWARD_BOOLEAN_FIELDS = (
    "is_direct",
    "is_passthrough_award",
    "is_major",
    "is_loan",
)

_FEC_FIELDS = (
    "committee_id",
    "name",
    "affiliated_committee_name",
    "committee_type",
    "committee_type_full",
    "designation",
    "designation_full",
    "organization_type",
    "organization_type_full",
    "party",
    "party_full",
    "state",
    "treasurer_name",
    "filing_frequency",
    "cycles",
    "first_file_date",
    "last_file_date",
)

_LDA_CLIENT_FIELDS = (
    "id",
    "client_id",
    "url",
    "name",
    "general_description",
    "client_government_entity",
    "client_self_select",
    "state",
    "country",
    "ppb_state",
    "ppb_country",
    "effective_date",
    "registrant",
)

_LDA_FILING_FIELDS = (
    "filing_uuid",
    "filing_type",
    "filing_type_display",
    "filing_year",
    "filing_period",
    "filing_period_display",
    "filing_document_url",
    "income",
    "expenses",
    "dt_posted",
    "termination_date",
    "registrant",
    "client",
    "lobbying_activities",
    "conviction_disclosures",
    "foreign_entities",
    "affiliated_organizations",
)


class _ExternalRequestError(RuntimeError):
    """An intentionally detail-free error safe to expose in result metadata."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _validate_federal_api_url(url: str, *, origin_url: str | None = None) -> None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or host not in _FEDERAL_API_PATH_PREFIXES:
        raise _ExternalRequestError("redirect_not_allowed" if origin_url else "request_not_allowed")
    if not any(parsed.path.startswith(prefix) for prefix in _FEDERAL_API_PATH_PREFIXES[host]):
        raise _ExternalRequestError("redirect_not_allowed" if origin_url else "request_not_allowed")
    if origin_url:
        origin = urllib.parse.urlsplit(origin_url)
        origin_port = origin.port or 443
        target_port = parsed.port or 443
        if (origin.hostname or "").casefold() != host or origin_port != target_port:
            raise _ExternalRequestError("redirect_not_allowed")


def _federal_tls_context(host: str) -> ssl.SSLContext | None:
    if host.casefold() not in _FEDERAL_TLS_COMPAT_HOSTS:
        return None
    context = ssl.create_default_context()
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    return context


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow only HTTPS redirects that retain the original API origin."""

    def __init__(self, origin_url: str):
        super().__init__()
        self.origin_url = origin_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_federal_api_url(target, origin_url=self.origin_url)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _clear_cache() -> None:
    """Clear the small process-local cache (primarily useful to deterministic tests)."""

    with _CACHE_LOCK:
        _CACHE.clear()


def _clean_ein(value: Any) -> str:
    digits = re.sub(r"\D", "", "" if value is None else str(value))
    return digits if len(digits) == 9 else ""


def _clean_uei(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", "" if value is None else str(value)).upper()
    # GSA_MIGRATION is a FAC legacy-data annotation, not a UEI.  Stripping its
    # underscore produces exactly 12 characters, so a length check alone would
    # send a fabricated identifier to downstream federal APIs.
    if cleaned == "GSAMIGRATION":
        return ""
    return cleaned if len(cleaned) == 12 else ""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _ordered_ueis(values: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, (list, tuple)):
        return ordered
    for value in values:
        uei = _clean_uei(value)
        if uei and uei not in seen:
            seen.add(uei)
            ordered.append(uei)
    return ordered


def _normalize_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "t", "yes", "y", "x"}:
        return True
    if normalized in {"0", "false", "f", "no", "n"}:
        return False
    return None


def _normalize_booleans(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result = dict(row)
    for field in fields:
        if field in result:
            result[field] = _normalize_boolean(result[field])
    return result


def _project(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {}
    return {field: row.get(field) for field in fields if field in row}


def _status(status: str, reason: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if reason:
        result["reason"] = reason
    return result


def _config_fingerprint(environ: Mapping[str, str]) -> str:
    # The digest prevents credentials from becoming cache keys or appearing in repr/debug output.
    values = (
        environ.get("FAC_API_KEY") or environ.get("API_GOV_KEY") or "",
        environ.get("SAM_API_KEY") or "",
        environ.get("FEC_API_KEY") or "",
        environ.get("LDA_API_TOKEN") or environ.get("LDA_API_KEY") or "",
        environ.get("SAM_MAX_UEIS") or "",
        environ.get("SAM_REQUEST_BUDGET") or "",
    )
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def _cache_get(key: tuple[str, str, str, str, str]) -> dict[str, Any] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= now:
            _CACHE.pop(key, None)
            return None
        return copy.deepcopy(value)


def _cache_put(key: tuple[str, str, str, str, str], value: dict[str, Any]) -> None:
    now = time.monotonic()
    with _CACHE_LOCK:
        expired = [cache_key for cache_key, (expiry, _) in _CACHE.items() if expiry <= now]
        for cache_key in expired:
            _CACHE.pop(cache_key, None)
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            oldest_key = min(_CACHE, key=lambda cache_key: _CACHE[cache_key][0])
            _CACHE.pop(oldest_key, None)
        _CACHE[key] = (now + _CACHE_TTL_SECONDS, copy.deepcopy(value))


def _request_json(
    base_url: str,
    *,
    params: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    opener: Any = None,
    timeout: float = 5.0,
) -> Any:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = base_url + (("?" + query) if query else "")
    _validate_federal_api_url(url)
    request_headers = {
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    request_headers.update(headers or {})
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)

    try:
        if opener is None:
            host = (urllib.parse.urlsplit(url).hostname or "").casefold()
            handlers: list[Any] = [_SameOriginRedirectHandler(url)]
            context = _federal_tls_context(host)
            if context is not None:
                handlers.append(urllib.request.HTTPSHandler(context=context))
            response = urllib.request.build_opener(*handlers).open(request, timeout=timeout)
        elif hasattr(opener, "open"):
            response = opener.open(request, timeout=timeout)
        else:
            response = opener(request, timeout=timeout)
        try:
            geturl = getattr(response, "geturl", None)
            if callable(geturl):
                final_url = geturl()
                if final_url:
                    _validate_federal_api_url(final_url, origin_url=url)
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except _ExternalRequestError:
        raise
    except (TimeoutError, urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        if isinstance(exc, TimeoutError):
            raise _ExternalRequestError("timeout") from None
        raise _ExternalRequestError("request_failed") from None
    except Exception:
        # Injected openers and unusual transports must not break the dashboard.
        raise _ExternalRequestError("request_failed") from None

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise _ExternalRequestError("response_too_large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise _ExternalRequestError("invalid_json") from None


def _fac_get(
    endpoint: str,
    params: Mapping[str, Any],
    api_key: str,
    *,
    opener: Any,
    timeout: float,
) -> list[dict[str, Any]]:
    payload = _request_json(
        "https://api.fac.gov/" + endpoint,
        params=params,
        headers={"X-Api-Key": api_key},
        opener=opener,
        timeout=timeout,
    )
    if not isinstance(payload, list):
        raise _ExternalRequestError("invalid_response")
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def _fac_sort_key(row: Mapping[str, Any]) -> tuple[str, str, int, str]:
    try:
        version = int(row.get("resubmission_version") or 0)
    except (TypeError, ValueError):
        version = 0
    return (
        str(row.get("fy_end_date") or ""),
        str(row.get("fac_accepted_date") or row.get("submitted_date") or ""),
        version,
        str(row.get("report_id") or ""),
    )


def _dedupe_latest_fac_reports(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_report_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        report_id = str(row.get("report_id") or "").strip()
        if not report_id:
            continue
        previous = by_report_id.get(report_id)
        if previous is None or _fac_sort_key(row) > _fac_sort_key(previous):
            by_report_id[report_id] = row

    by_period: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in by_report_id.values():
        fiscal_start = str(row.get("fy_start_date") or "")
        fiscal_end = str(row.get("fy_end_date") or "")
        if fiscal_start or fiscal_end:
            group = (
                _clean_ein(row.get("auditee_ein")),
                _clean_uei(row.get("auditee_uei")),
                fiscal_start,
                fiscal_end,
            )
        else:
            # Sparse migrated rows should not be collapsed merely because their year matches.
            group = ("report", str(row.get("report_id") or ""))
        previous = by_period.get(group)
        if previous is None or _fac_sort_key(row) > _fac_sort_key(previous):
            by_period[group] = row

    return sorted(by_period.values(), key=_fac_sort_key, reverse=True)[:_MAX_FAC_REPORTS]


def _fetch_fac(
    ein: str,
    api_key: str,
    *,
    opener: Any,
    timeout: float,
) -> dict[str, Any]:
    if not api_key:
        return _status("not_configured", "missing_api_key")
    if not ein:
        return _status("blocked", "invalid_ein")

    try:
        # Neither exact-EIN lookup depends on the other. Running them together
        # keeps live dashboard latency bounded by one request timeout for this
        # stage instead of two.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="risk-fac-index") as pool:
            primary_future = pool.submit(
                _fac_get,
                "general",
                {
                    "auditee_ein": "eq." + ein,
                    "select": ",".join(_FAC_GENERAL_FIELDS),
                    "order": "fy_end_date.desc",
                    "limit": 100,
                },
                api_key,
                opener=opener,
                timeout=timeout,
            )
            additional_future = pool.submit(
                _fac_get,
                "additional_eins",
                {
                    "additional_ein": "eq." + ein,
                    "select": ",".join(_FAC_ADDITIONAL_EIN_FIELDS),
                    "order": "fac_accepted_date.desc",
                    "limit": 100,
                },
                api_key,
                opener=opener,
                timeout=timeout,
            )
            primary_rows = primary_future.result()
            additional_rows = additional_future.result()
    except _ExternalRequestError as exc:
        return {"status": "error", "error": exc.code, "reports": [], "ueis": []}

    match_by_report: dict[str, str] = {}
    general_rows: list[dict[str, Any]] = []
    for row in primary_rows:
        if _clean_ein(row.get("auditee_ein")) != ein:
            continue
        report_id = str(row.get("report_id") or "")
        if report_id:
            match_by_report[report_id] = "primary_ein"
        general_rows.append(row)

    known_report_ids = {str(row.get("report_id") or "") for row in general_rows}
    additional_ids: list[str] = []
    for row in additional_rows:
        if _clean_ein(row.get("additional_ein")) != ein:
            continue
        report_id = str(row.get("report_id") or "").strip()
        if not report_id:
            continue
        match_by_report.setdefault(report_id, "additional_ein")
        if report_id not in known_report_ids and report_id not in additional_ids:
            additional_ids.append(report_id)

    partial_errors = 0
    safe_additional_ids = [
        report_id for report_id in additional_ids[:_MAX_FAC_ADDITIONAL_REPORTS]
        if re.fullmatch(r"[A-Za-z0-9_-]+", report_id)
    ]
    if safe_additional_ids:
        try:
            rows = _fac_get(
                "general",
                {
                    "report_id": "in.(" + ",".join(safe_additional_ids) + ")",
                    "select": ",".join(_FAC_GENERAL_FIELDS),
                    "limit": len(safe_additional_ids),
                },
                api_key,
                opener=opener,
                timeout=timeout,
            )
            general_rows.extend(
                row for row in rows
                if str(row.get("report_id") or "") in safe_additional_ids
            )
        except _ExternalRequestError:
            partial_errors += 1

    reports: list[dict[str, Any]] = []
    for raw_general in _dedupe_latest_fac_reports(general_rows):
        general = _normalize_booleans(
            _project(raw_general, _FAC_GENERAL_FIELDS), _FAC_GENERAL_BOOLEAN_FIELDS
        )
        report_id = str(general.get("report_id") or "")
        report: dict[str, Any] = {
            "report_id": report_id,
            "ein_match": match_by_report.get(report_id, "unknown"),
            "general": general,
            "findings": [],
            "findings_text": [],
            "corrective_action_plans": [],
            "federal_awards": [],
            "findings_status": "ok",
            "findings_text_status": "not_requested",
            "corrective_action_plans_status": "not_requested",
            "federal_awards_status": "ok",
        }
        reports.append(report)

    report_by_id = {
        report["report_id"]: report
        for report in reports
        if re.fullmatch(r"[A-Za-z0-9_-]+", report.get("report_id") or "")
    }
    report_ids = list(report_by_id)
    if report_ids:
        report_filter = "in.(" + ",".join(report_ids) + ")"
        # Findings and award rows are independent and are the two largest FAC
        # detail queries, so fetch them concurrently. Narrative/CAP rows follow
        # only when the findings response proves they are relevant.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="risk-fac-detail") as pool:
            findings_future = pool.submit(
                _fac_get,
                "findings",
                {
                    "report_id": report_filter,
                    "select": ",".join(_FAC_FINDING_FIELDS),
                    "limit": _MAX_FAC_FINDINGS * len(report_ids),
                },
                api_key,
                opener=opener,
                timeout=timeout,
            )
            awards_future = pool.submit(
                _fac_get,
                "federal_awards",
                {
                    "report_id": report_filter,
                    "select": ",".join(_FAC_AWARD_FIELDS),
                    "limit": _MAX_FAC_AWARDS * len(report_ids),
                },
                api_key,
                opener=opener,
                timeout=timeout,
            )

            try:
                findings = findings_future.result()
            except _ExternalRequestError:
                for report in reports:
                    report["findings_status"] = "error"
                partial_errors += 1
                findings = []

            for row in findings:
                target = report_by_id.get(str(row.get("report_id") or ""))
                if target is not None and len(target["findings"]) < _MAX_FAC_FINDINGS:
                    target["findings"].append(_normalize_booleans(
                        _project(row, _FAC_FINDING_FIELDS), _FAC_FINDING_BOOLEAN_FIELDS
                    ))

            try:
                awards = awards_future.result()
            except _ExternalRequestError:
                for report in reports:
                    report["federal_awards_status"] = "error"
                partial_errors += 1
                awards = []

            for row in awards:
                target = report_by_id.get(str(row.get("report_id") or ""))
                if target is not None and len(target["federal_awards"]) < _MAX_FAC_AWARDS:
                    target["federal_awards"].append(_normalize_booleans(
                        _project(row, _FAC_AWARD_FIELDS), _FAC_AWARD_BOOLEAN_FIELDS
                    ))

        text_report_ids = [
            report["report_id"] for report in reports
            if report["findings"] and report["report_id"] in report_by_id
        ][:_MAX_FAC_TEXT_REPORTS]
        if text_report_ids:
            text_filter = "in.(" + ",".join(text_report_ids) + ")"
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="risk-fac-text") as pool:
                text_future = pool.submit(
                    _fac_get,
                    "findings_text",
                    {
                        "report_id": text_filter,
                        "select": ",".join(_FAC_FINDING_TEXT_FIELDS),
                        "limit": _MAX_FAC_TEXT_ROWS * len(text_report_ids),
                    },
                    api_key,
                    opener=opener,
                    timeout=timeout,
                )
                corrective_action_future = pool.submit(
                    _fac_get,
                    "corrective_action_plans",
                    {
                        "report_id": text_filter,
                        "select": ",".join(_FAC_CORRECTIVE_ACTION_FIELDS),
                        "limit": _MAX_FAC_TEXT_ROWS * len(text_report_ids),
                    },
                    api_key,
                    opener=opener,
                    timeout=timeout,
                )

                try:
                    findings_text = text_future.result()
                except _ExternalRequestError:
                    for report_id in text_report_ids:
                        report_by_id[report_id]["findings_text_status"] = "error"
                    partial_errors += 1
                    findings_text = []
                else:
                    for report_id in text_report_ids:
                        report_by_id[report_id]["findings_text_status"] = "ok"

                for report_id in text_report_ids:
                    report = report_by_id[report_id]
                    report["findings_text"] = []
                for row in findings_text:
                    target = report_by_id.get(str(row.get("report_id") or ""))
                    if (
                        target is not None
                        and target["report_id"] in text_report_ids
                        and len(target["findings_text"]) < _MAX_FAC_TEXT_ROWS
                    ):
                        target["findings_text"].append(_project(row, _FAC_FINDING_TEXT_FIELDS))

                try:
                    corrective_actions = corrective_action_future.result()
                except _ExternalRequestError:
                    for report_id in text_report_ids:
                        report_by_id[report_id]["corrective_action_plans_status"] = "error"
                    partial_errors += 1
                    corrective_actions = []
                else:
                    for report_id in text_report_ids:
                        report_by_id[report_id]["corrective_action_plans_status"] = "ok"

                for row in corrective_actions:
                    target = report_by_id.get(str(row.get("report_id") or ""))
                    if (
                        target is not None
                        and target["report_id"] in text_report_ids
                        and len(target["corrective_action_plans"]) < _MAX_FAC_TEXT_ROWS
                    ):
                        target["corrective_action_plans"].append(
                            _project(row, _FAC_CORRECTIVE_ACTION_FIELDS)
                        )

    # A FAC report matched through an additional EIN does not prove that the primary
    # auditee UEI belongs to the queried EIN. Only bridge primary EIN matches onward.
    ueis = sorted(
        {
            _clean_uei(report["general"].get("auditee_uei"))
            for report in reports
            if report.get("ein_match") == "primary_ein"
            and _clean_ein(report["general"].get("auditee_ein")) == ein
            and _clean_uei(report["general"].get("auditee_uei"))
        }
    )[:_MAX_UEIS]

    if not reports:
        result = {"status": "no_match", "reports": [], "ueis": []}
    else:
        result = {"status": "ok", "reports": reports, "ueis": ueis}
    result["report_count"] = len(reports)
    if partial_errors:
        result["partial_errors"] = partial_errors
    return result


def _fetch_fac_offline(ein: str) -> dict[str, Any]:
    if _lookup_offline_fac is None:
        return _status("not_configured", "offline_module_unavailable")
    try:
        result = _lookup_offline_fac(ein, max_reports=_MAX_COMBINED_FAC_REPORTS)
    except Exception:
        return {"status": "error", "error": "offline_lookup_failed", "reports": [], "ueis": []}
    return result if isinstance(result, dict) else {
        "status": "error",
        "error": "invalid_offline_response",
        "reports": [],
        "ueis": [],
    }


def _fac_result_sort_key(report: Mapping[str, Any]) -> tuple[str, str, str]:
    general = report.get("general") if isinstance(report.get("general"), Mapping) else {}
    return (
        str(general.get("fy_end_date") or general.get("audit_year") or ""),
        str(general.get("fac_accepted_date") or general.get("submitted_date") or ""),
        str(report.get("report_id") or general.get("report_id") or ""),
    )


def _merge_fac_report(live_report: Mapping[str, Any], offline_report: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a duplicate report without discarding offline details after a partial live failure."""
    merged = copy.deepcopy(dict(offline_report))
    for key, value in live_report.items():
        if key not in {
            "general", "findings", "federal_awards", "findings_text",
            "corrective_action_plans",
        }:
            merged[key] = copy.deepcopy(value)

    general = copy.deepcopy(dict(offline_report.get("general") or {}))
    for key, value in (live_report.get("general") or {}).items():
        if value not in (None, ""):
            general[key] = copy.deepcopy(value)
    merged["general"] = general

    detail_statuses = {
        "findings": "findings_status",
        "federal_awards": "federal_awards_status",
        "findings_text": "findings_text_status",
        "corrective_action_plans": "corrective_action_plans_status",
    }
    fallbacks = []
    for detail, status_key in detail_statuses.items():
        live_status = str(live_report.get(status_key) or "")
        live_rows = live_report.get(detail) or []
        offline_rows = offline_report.get(detail) or []
        if live_status in {"error", "not_requested"} and offline_rows:
            merged[detail] = copy.deepcopy(offline_rows)
            merged[status_key] = "offline_fallback_after_" + live_status
            fallbacks.append(detail)
        else:
            merged[detail] = copy.deepcopy(live_rows)
            if status_key in live_report:
                merged[status_key] = live_report.get(status_key)
    if fallbacks:
        merged["offline_detail_fallbacks"] = fallbacks
        merged["partial_coverage"] = True
    return merged


def _combine_fac_results(live: dict[str, Any], offline: dict[str, Any]) -> dict[str, Any]:
    """Prefer fresh live rows while retaining local history and offline fallback."""
    live_status = str(live.get("status") or "error")
    offline_status = str(offline.get("status") or "error")
    live_usable = live_status in {"ok", "no_match"}
    offline_usable = offline_status in {"ok", "no_match"}

    if not offline_usable:
        result = copy.deepcopy(live)
        result["offline_status"] = offline_status
        result["uses_live"] = live_usable
        return result
    if not live_usable:
        result = copy.deepcopy(offline)
        result["source"] = "offline_fac_sidecar_fallback"
        result["live_status"] = live_status
        result["uses_live"] = False
        return result

    by_report: dict[str, dict[str, Any]] = {}
    for report in offline.get("reports") or []:
        report_id = str(report.get("report_id") or (report.get("general") or {}).get("report_id") or "")
        if report_id:
            by_report[report_id] = copy.deepcopy(report)
    # Live fields win for duplicate IDs, but a partial live endpoint failure must
    # not erase locally available findings, award, narrative, or CAP evidence.
    for report in live.get("reports") or []:
        report_id = str(report.get("report_id") or (report.get("general") or {}).get("report_id") or "")
        if report_id:
            if report_id in by_report:
                by_report[report_id] = _merge_fac_report(report, by_report[report_id])
            else:
                by_report[report_id] = copy.deepcopy(report)
    reports = sorted(by_report.values(), key=_fac_result_sort_key, reverse=True)[
        :_MAX_COMBINED_FAC_REPORTS
    ]
    ueis = sorted({
        _clean_uei(uei)
        for value in (live.get("ueis") or [], offline.get("ueis") or [])
        for uei in value
        if _clean_uei(uei)
    })[:_MAX_UEIS]
    result = copy.deepcopy(live)
    result.update({
        "status": "ok" if reports else "no_match",
        "reports": reports,
        "report_count": len(reports),
        "ueis": ueis,
        "source": "live_and_offline_fac",
        "uses_live": True,
        "offline_status": offline_status,
        "offline_source_as_of_date": offline.get("source_as_of_date"),
        "offline_coverage": offline.get("coverage") or {},
    })
    return result


def _fac_primary_ueis(fac: Mapping[str, Any], ein: str) -> list[str]:
    """Return primary-EIN UEIs in FAC report recency order.

    Additional-EIN matches are intentionally excluded because they do not prove
    that the report's auditee UEI belongs to the dashboard subject.  The FAC
    adapters sort reports newest first, making the first value the safest
    default when a low-quota SAM key can support only one UEI.
    """

    ordered: list[str] = []
    seen: set[str] = set()
    reports = fac.get("reports") if isinstance(fac, Mapping) else []
    if isinstance(reports, list):
        for report in reports:
            if not isinstance(report, Mapping) or report.get("ein_match") != "primary_ein":
                continue
            general = report.get("general")
            if not isinstance(general, Mapping):
                continue
            if _clean_ein(general.get("auditee_ein")) != ein:
                continue
            uei = _clean_uei(general.get("auditee_uei"))
            if uei and uei not in seen:
                seen.add(uei)
                ordered.append(uei)

    # Older/offline adapters already constrain this field to primary-EIN UEIs.
    # Use it only as a fallback or to append safe values absent from report rows.
    for uei in _ordered_ueis(fac.get("ueis") if isinstance(fac, Mapping) else []):
        if uei not in seen:
            seen.add(uei)
            ordered.append(uei)
    return ordered[:_MAX_UEIS]


def _fetch_usaspending(ueis: list[str], *, opener: Any, timeout: float) -> dict[str, Any]:
    exact_ueis = sorted({_clean_uei(uei) for uei in ueis if _clean_uei(uei)})[:_MAX_UEIS]
    if not exact_ueis:
        return _status("blocked", "requires_fac_uei")

    matches: list[dict[str, Any]] = []
    error_count = 0

    def fetch_uei(uei: str) -> tuple[list[dict[str, Any]], bool]:
        try:
            payload = _request_json(
                "https://api.usaspending.gov/api/v2/recipient/",
                payload={
                    "keyword": uei,
                    "award_type": "grants",
                    "sort": "amount",
                    "order": "desc",
                    "page": 1,
                    "limit": 50,
                },
                opener=opener,
                timeout=timeout,
            )
            if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
                raise _ExternalRequestError("invalid_response")
            rows: list[dict[str, Any]] = []
            for row in payload["results"][:50]:
                if not isinstance(row, Mapping) or _clean_uei(row.get("uei")) != uei:
                    continue
                rows.append(
                    _project(
                        row,
                        ("id", "name", "uei", "duns", "recipient_level", "amount"),
                    )
                )
            return rows, False
        except _ExternalRequestError:
            return [], True

    with ThreadPoolExecutor(
        max_workers=len(exact_ueis), thread_name_prefix="risk-usaspending"
    ) as pool:
        futures = [pool.submit(fetch_uei, uei) for uei in exact_ueis]
        for future in futures:
            rows, failed = future.result()
            matches.extend(rows)
            error_count += int(failed)

    if matches:
        result: dict[str, Any] = {
            "status": "ok",
            "matches": matches,
            "queried_ueis": exact_ueis,
            "amount_basis": "trailing_12_month_transactions_not_single_audit_expenditures",
        }
        if error_count:
            result["partial_errors"] = error_count
        return result
    if error_count:
        return {
            "status": "error",
            "error": "request_failed",
            "matches": [],
            "queried_ueis": exact_ueis,
        }
    return {"status": "no_match", "matches": [], "queried_ueis": exact_ueis}


def _record_uei(record: Mapping[str, Any]) -> str:
    candidates = [record.get("ueiSAM"), record.get("uei")]
    registration = record.get("entityRegistration")
    if isinstance(registration, Mapping):
        candidates.extend((registration.get("ueiSAM"), registration.get("uei")))
    entity = record.get("entity")
    if isinstance(entity, Mapping):
        candidates.extend((entity.get("ueiSAM"), entity.get("uei")))
    exclusion_identification = record.get("exclusionIdentification")
    if isinstance(exclusion_identification, Mapping):
        candidates.extend((
            exclusion_identification.get("ueiSAM"),
            exclusion_identification.get("uei"),
        ))
    for value in candidates:
        normalized = _clean_uei(value)
        if normalized:
            return normalized
    return ""


def _compact_sam_entity(row: Mapping[str, Any]) -> dict[str, Any]:
    registration = row.get("entityRegistration")
    core_data = row.get("coreData")
    return {
        "uei": _record_uei(row),
        "entity_registration": dict(registration) if isinstance(registration, Mapping) else {},
        "core_data": dict(core_data) if isinstance(core_data, Mapping) else {},
        "integrity_information": row.get("integrityInformation") or {},
    }


def _fetch_sam(
    ueis: list[str],
    api_key: str,
    *,
    opener: Any,
    timeout: float,
    max_ueis: int = _DEFAULT_SAM_MAX_UEIS,
    request_budget: int = _DEFAULT_SAM_REQUEST_BUDGET,
) -> dict[str, Any]:
    if not api_key:
        return _status("not_configured", "missing_api_key")
    candidate_ueis = _ordered_ueis(ueis)[:_MAX_UEIS]
    if not candidate_ueis:
        return _status("blocked", "requires_fac_uei")

    bounded_max_ueis = _bounded_int(
        max_ueis, _DEFAULT_SAM_MAX_UEIS, 1, _MAX_UEIS
    )
    bounded_budget = _bounded_int(
        request_budget,
        _DEFAULT_SAM_REQUEST_BUDGET,
        2,
        _MAX_SAM_REQUEST_BUDGET,
    )
    # Reserve one entity and one exclusion request for every selected UEI. A
    # configured UEI count that cannot fit in the request budget is reduced and
    # reported explicitly rather than producing misleading one-sided coverage.
    selected_count = min(
        len(candidate_ueis), bounded_max_ueis, max(1, bounded_budget // 2)
    )
    exact_ueis = candidate_ueis[:selected_count]
    omitted_ueis = candidate_ueis[selected_count:]

    # After reserving entity requests, distribute the exclusion-page allowance
    # round-robin. The safest (newest primary-EIN) UEI receives the first extra
    # page when the budget does not divide evenly.
    remaining_exclusion_requests = bounded_budget - len(exact_ueis)
    exclusion_budgets = {uei: 1 for uei in exact_ueis}
    remaining_exclusion_requests -= len(exact_ueis)
    allocation_index = 0
    while remaining_exclusion_requests > 0:
        uei = exact_ueis[allocation_index % len(exact_ueis)]
        exclusion_budgets[uei] += 1
        allocation_index += 1
        remaining_exclusion_requests -= 1

    entities: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    error_count = 0

    def total_records(payload: Mapping[str, Any]) -> int | None:
        value = payload.get("totalRecords")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return max(0, parsed)

    def has_next_link(payload: Mapping[str, Any]) -> bool:
        links = payload.get("links")
        return bool(isinstance(links, Mapping) and links.get("nextLink"))

    def fetch_entities(
        uei: str,
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
        common = {"api_key": api_key, "ueiSAM": uei}
        try:
            payload = _request_json(
                "https://api.sam.gov/entity-information/v4/entities",
                params={
                    **common,
                    "includeSections": "entityRegistration,coreData,integrityInformation",
                    "page": 0,
                    "size": _SAM_PAGE_SIZE,
                },
                opener=opener,
                timeout=timeout,
            )
            if not isinstance(payload, Mapping):
                raise _ExternalRequestError("invalid_response")
            rows = payload.get("entityData") or payload.get("results") or []
            if not isinstance(rows, list):
                raise _ExternalRequestError("invalid_response")
            reported = total_records(payload)
            truncated = bool(
                has_next_link(payload)
                or (reported is not None and reported > len(rows))
                or (reported is None and len(rows) >= _SAM_PAGE_SIZE)
            )
            matched_entities = [
                _compact_sam_entity(row)
                for row in rows[:_SAM_PAGE_SIZE]
                if isinstance(row, Mapping) and _record_uei(row) == uei
            ]
            return matched_entities, False, {
                "uei": uei,
                "requests_used": 1,
                "records_reported": reported,
                "records_returned": len(matched_entities),
                "truncated": truncated,
            }
        except _ExternalRequestError:
            return [], True, {
                "uei": uei,
                "requests_used": 1,
                "records_reported": None,
                "records_returned": 0,
                "truncated": True,
                "error": "request_failed",
            }

    def fetch_exclusions(
        uei: str, page_budget: int
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
        common = {"api_key": api_key, "ueiSAM": uei}
        matched_rows: list[dict[str, Any]] = []
        requests_used = 0
        pages_fetched = 0
        reported_total: int | None = None
        truncated = False
        failed = False
        for page in range(page_budget):
            requests_used += 1
            try:
                payload = _request_json(
                    "https://api.sam.gov/entity-information/v4/exclusions",
                    params={
                        **common,
                        "page": page,
                        "size": _SAM_PAGE_SIZE,
                    },
                    opener=opener,
                    timeout=timeout,
                )
                if not isinstance(payload, Mapping):
                    raise _ExternalRequestError("invalid_response")
                rows = (
                    payload.get("excludedEntity")
                    or payload.get("exclusionData")
                    or payload.get("results")
                    or []
                )
                if not isinstance(rows, list):
                    raise _ExternalRequestError("invalid_response")
            except _ExternalRequestError:
                failed = True
                truncated = True
                break

            pages_fetched += 1
            page_total = total_records(payload)
            if page_total is not None:
                reported_total = page_total
            matched_rows.extend(
                dict(row)
                for row in rows[:_SAM_PAGE_SIZE]
                if isinstance(row, Mapping) and _record_uei(row) == uei
            )
            more_records = (
                has_next_link(payload)
                or (
                    reported_total is not None
                    and (page + 1) * _SAM_PAGE_SIZE < reported_total
                )
                or (reported_total is None and len(rows) >= _SAM_PAGE_SIZE)
            )
            if not more_records:
                break
            if page + 1 >= page_budget:
                truncated = True

        total_pages = (
            (reported_total + _SAM_PAGE_SIZE - 1) // _SAM_PAGE_SIZE
            if reported_total is not None
            else None
        )
        omitted_pages = (
            max(0, total_pages - pages_fetched)
            if total_pages is not None
            else (None if not truncated else "unknown")
        )
        return matched_rows, failed, {
            "uei": uei,
            "page_size": _SAM_PAGE_SIZE,
            "page_budget": page_budget,
            "requests_used": requests_used,
            "pages_fetched": pages_fetched,
            "pages_omitted": omitted_pages,
            "records_reported": reported_total,
            "records_returned": len(matched_rows),
            "truncated": truncated,
            **({"error": "request_failed"} if failed else {}),
        }

    entity_coverage: list[dict[str, Any]] = []
    exclusion_coverage: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=2 * len(exact_ueis), thread_name_prefix="risk-sam"
    ) as pool:
        futures = [
            (
                uei,
                pool.submit(fetch_entities, uei),
                pool.submit(fetch_exclusions, uei, exclusion_budgets[uei]),
            )
            for uei in exact_ueis
        ]
        for _uei, entity_future, exclusion_future in futures:
            entity_rows, entity_failed, entity_meta = entity_future.result()
            exclusion_rows, exclusion_failed, exclusion_meta = exclusion_future.result()
            entities.extend(entity_rows)
            exclusions.extend(exclusion_rows)
            entity_coverage.append(entity_meta)
            exclusion_coverage.append(exclusion_meta)
            error_count += int(entity_failed) + int(exclusion_failed)

    requests_used = sum(
        int(item.get("requests_used") or 0)
        for item in entity_coverage + exclusion_coverage
    )
    page_truncated = any(item.get("truncated") for item in exclusion_coverage)
    entity_truncated = any(item.get("truncated") for item in entity_coverage)
    truncated = bool(omitted_ueis or page_truncated or entity_truncated)
    partial = bool(truncated or error_count)
    truncation_reasons = []
    if omitted_ueis:
        truncation_reasons.append(
            "uei_limit" if len(exact_ueis) >= bounded_max_ueis else "request_budget"
        )
    if page_truncated:
        truncation_reasons.append("exclusion_pages_omitted")
    if entity_truncated:
        truncation_reasons.append("entity_pages_omitted")
    if error_count:
        truncation_reasons.append("request_errors")

    coverage = {
        "status": "partial" if partial else "complete",
        "partial": partial,
        "truncated": truncated,
        "candidate_ueis": candidate_ueis,
        "queried_ueis": exact_ueis,
        "omitted_ueis": omitted_ueis,
        "truncation_reasons": truncation_reasons,
        "entity_queries": entity_coverage,
        "exclusion_queries": exclusion_coverage,
    }
    common_result: dict[str, Any] = {
        "entities": entities,
        "exclusions": exclusions,
        "queried_ueis": exact_ueis,
        "omitted_ueis": omitted_ueis,
        "coverage_status": coverage["status"],
        "partial": partial,
        "truncated": truncated,
        "coverage": coverage,
        "quota": {
            "published_lowest_daily_limit": _SAM_LOWEST_PUBLISHED_DAILY_QUOTA,
            "request_budget": bounded_budget,
            "requests_used": requests_used,
            "configured_max_ueis": bounded_max_ueis,
        },
        "cache_policy": {
            "scope": "process_local",
            "ttl_seconds": _CACHE_TTL_SECONDS,
            "persistent": False,
            "limitation": (
                "Repeated checks after cache expiry or a process restart consume "
                "SAM.gov quota again."
            ),
        },
    }
    if entities or exclusions:
        result: dict[str, Any] = {"status": "ok", **common_result}
        if error_count:
            result["partial_errors"] = error_count
        return result
    if error_count:
        return {
            "status": "error",
            "error": "request_failed",
            **common_result,
        }
    return {
        "status": "no_match",
        **common_result,
    }


def _fetch_fec(
    org_name: str,
    state: str,
    api_key: str,
    *,
    opener: Any,
    timeout: float,
) -> dict[str, Any]:
    if not api_key:
        return _status("not_configured", "missing_api_key")
    if not org_name:
        return _status("blocked", "requires_org_name")

    params: dict[str, Any] = {
        "q": org_name,
        "page": 1,
        "per_page": _MAX_FEC_CANDIDATES,
        "api_key": api_key,
    }
    if state:
        params["state"] = state
    try:
        payload = _request_json(
            "https://api.open.fec.gov/v1/committees/",
            params=params,
            opener=opener,
            timeout=timeout,
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
            raise _ExternalRequestError("invalid_response")
    except _ExternalRequestError as exc:
        return {"status": "error", "error": exc.code, "candidates": []}

    candidates = [
        _project(row, _FEC_FIELDS)
        for row in payload["results"][:_MAX_FEC_CANDIDATES]
        if isinstance(row, Mapping)
    ]
    if not candidates:
        return {"status": "no_match", "candidates": [], "match_type": "candidate_only"}
    return {
        "status": "ok",
        "candidates": candidates,
        "match_type": "candidate_only",
        "requires_manual_verification": True,
    }


_LEGAL_SUFFIXES = {
    "CO",
    "COMPANY",
    "CORP",
    "CORPORATION",
    "INC",
    "INCORPORATED",
    "LLC",
    "LTD",
    "THE",
}


def _name_tokens(value: Any) -> tuple[str, ...]:
    tokens = re.findall(r"[A-Z0-9]+", str(value or "").upper())
    return tuple(token for token in tokens if token not in _LEGAL_SUFFIXES)


def _lda_name_strength(query: str, candidate: Any, state: str, candidate_state: Any) -> str:
    query_tokens = _name_tokens(query)
    candidate_tokens = _name_tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return ""
    if state and candidate_state and str(candidate_state).upper() != state:
        return ""
    if query_tokens == candidate_tokens:
        return "exact"
    query_set = set(query_tokens)
    candidate_set = set(candidate_tokens)
    overlap = len(query_set & candidate_set) / max(len(query_set), len(candidate_set))
    query_text = " ".join(query_tokens)
    candidate_text = " ".join(candidate_tokens)
    similarity = SequenceMatcher(None, query_text, candidate_text).ratio()
    if (overlap >= 0.75 and min(len(query_set), len(candidate_set)) >= 2) or similarity >= 0.92:
        return "strong"
    return ""


def _fetch_lda(
    org_name: str,
    state: str,
    token: str,
    *,
    opener: Any,
    timeout: float,
) -> dict[str, Any]:
    if not org_name:
        return _status("blocked", "requires_org_name")
    headers = {"Authorization": "Token " + token} if token else {}
    params: dict[str, Any] = {
        "client_name": org_name,
        "page_size": 25,
        "page": 1,
    }
    if state:
        params["client_state"] = state
    try:
        payload = _request_json(
            "https://lda.gov/api/v1/clients/",
            params=params,
            headers=headers,
            opener=opener,
            timeout=timeout,
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
            raise _ExternalRequestError("invalid_response")
    except _ExternalRequestError as exc:
        return {"status": "error", "error": exc.code, "clients": []}

    matched: list[tuple[dict[str, Any], str]] = []
    for row in payload["results"][:25]:
        if not isinstance(row, Mapping):
            continue
        strength = _lda_name_strength(org_name, row.get("name"), state, row.get("state"))
        if strength:
            matched.append((dict(row), strength))
    matched.sort(key=lambda item: 0 if item[1] == "exact" else 1)

    if not matched:
        return {
            "status": "no_match",
            "clients": [],
            "candidate_count": len(payload["results"]),
        }

    clients: list[dict[str, Any]] = []
    partial_errors = 0

    def expand_client(item: tuple[dict[str, Any], str]) -> tuple[dict[str, Any], bool]:
        raw_client, strength = item
        client = _project(raw_client, _LDA_CLIENT_FIELDS)
        client["match_strength"] = strength
        client["filings"] = []
        client_id = raw_client.get("client_id") or raw_client.get("id")
        if client_id is None:
            client["filings_status"] = "error"
            return client, True
        try:
            filing_payload = _request_json(
                "https://lda.gov/api/v1/filings/",
                params={
                    "client_id": client_id,
                    "ordering": "-dt_posted",
                    "page_size": _MAX_LDA_FILINGS,
                    "page": 1,
                },
                headers=headers,
                opener=opener,
                timeout=timeout,
            )
            if not isinstance(filing_payload, Mapping) or not isinstance(
                filing_payload.get("results"), list
            ):
                raise _ExternalRequestError("invalid_response")
            client["filings"] = [
                _project(row, _LDA_FILING_FIELDS)
                for row in filing_payload["results"][:_MAX_LDA_FILINGS]
                if isinstance(row, Mapping)
            ]
            client["filings_status"] = "ok"
        except _ExternalRequestError:
            client["filings_status"] = "error"
            return client, True
        return client, False

    selected_matches = matched[:_MAX_LDA_CLIENTS]
    with ThreadPoolExecutor(
        max_workers=len(selected_matches), thread_name_prefix="risk-lda"
    ) as pool:
        futures = [pool.submit(expand_client, item) for item in selected_matches]
        for future in futures:
            client, failed = future.result()
            clients.append(client)
            partial_errors += int(failed)

    result: dict[str, Any] = {
        "status": "ok",
        "clients": clients,
        "authenticated": bool(token),
    }
    if partial_errors:
        result["partial_errors"] = partial_errors
    return result


def _build_sources(results: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    return {
        key: {
            **metadata,
            "status": str(results.get(key, {}).get("status") or "error"),
        }
        for key, metadata in _SOURCE_INFO.items()
    }


def _safe_fetch(callable_obj: Any) -> dict[str, Any]:
    """Keep an unexpected source-specific failure from breaking the dashboard."""
    try:
        result = callable_obj()
        return result if isinstance(result, dict) else _status("error", "invalid_response")
    except Exception:
        return {"status": "error", "error": "internal_error"}


def fetch_external_checks(
    ein: str,
    org_name: str,
    state: str = "",
    mode: str = "live",
    environ: Mapping[str, str] | None = None,
    opener: Any = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Fetch bounded, read-only public risk context from supported federal sources.

    External matches are investigative indicators, not determinations of fraud.  In
    ``local`` mode this function performs no HTTP calls. Results are cached briefly
    in-process using only normalized public identifiers and a one-way configuration
    fingerprint; API keys are never returned or stored in cache keys. SAM.gov checks
    default to one newest FAC primary-EIN UEI and three requests per call. Advanced
    users can raise the bounded limits with ``SAM_MAX_UEIS`` (maximum 3) and
    ``SAM_REQUEST_BUDGET`` (maximum 10); the result reports any omitted coverage.
    The cache is not persistent, so repeated checks after five minutes or a process
    restart consume quota again.
    """

    env = os.environ if environ is None else environ
    normalized_ein = _clean_ein(ein)
    normalized_name = re.sub(r"\s+", " ", str(org_name or "")).strip()[:300]
    normalized_state = re.sub(r"[^A-Za-z]", "", str(state or "")).upper()[:2]
    normalized_mode = str(mode or "live").strip().casefold()
    if normalized_mode not in {"live", "local"}:
        normalized_mode = "local"
    try:
        bounded_timeout = max(0.1, min(float(timeout), 30.0))
    except (TypeError, ValueError):
        bounded_timeout = 5.0

    cache_key = (
        normalized_ein,
        normalized_name.casefold(),
        normalized_state,
        normalized_mode,
        _config_fingerprint(env),
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if normalized_mode == "local":
        results = {
            key: _status("blocked", "local_mode")
            for key in ("fac", "usaspending", "sam", "fec", "lda")
        }
        offline_fac = _safe_fetch(lambda: _fetch_fac_offline(normalized_ein))
        if offline_fac.get("status") in {"ok", "no_match"}:
            results["fac"] = offline_fac
    else:
        fac_key = env.get("FAC_API_KEY") or env.get("API_GOV_KEY") or ""
        sam_key = env.get("SAM_API_KEY") or ""
        sam_max_ueis = _bounded_int(
            env.get("SAM_MAX_UEIS"),
            _DEFAULT_SAM_MAX_UEIS,
            1,
            _MAX_UEIS,
        )
        sam_request_budget = _bounded_int(
            env.get("SAM_REQUEST_BUDGET"),
            _DEFAULT_SAM_REQUEST_BUDGET,
            2,
            _MAX_SAM_REQUEST_BUDGET,
        )
        fec_key = env.get("FEC_API_KEY") or ""
        lda_token = env.get("LDA_API_TOKEN") or env.get("LDA_API_KEY") or ""

        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="risk-source") as pool:
            fac_future = pool.submit(_safe_fetch, lambda: _fetch_fac(
                normalized_ein,
                fac_key,
                opener=opener,
                timeout=bounded_timeout,
            ))
            fec_future = pool.submit(_safe_fetch, lambda: _fetch_fec(
                normalized_name,
                normalized_state,
                fec_key,
                opener=opener,
                timeout=bounded_timeout,
            ))
            lda_future = pool.submit(_safe_fetch, lambda: _fetch_lda(
                normalized_name,
                normalized_state,
                lda_token,
                opener=opener,
                timeout=bounded_timeout,
            ))
            offline_fac_future = pool.submit(
                _safe_fetch, lambda: _fetch_fac_offline(normalized_ein)
            )
            fac = _combine_fac_results(fac_future.result(), offline_fac_future.result())
            fac_ueis = _fac_primary_ueis(fac, normalized_ein)
            usa_future = pool.submit(_safe_fetch, lambda: _fetch_usaspending(
                fac_ueis,
                opener=opener,
                timeout=bounded_timeout,
            ))
            sam_future = pool.submit(_safe_fetch, lambda: _fetch_sam(
                fac_ueis,
                sam_key,
                opener=opener,
                timeout=bounded_timeout,
                max_ueis=sam_max_ueis,
                request_budget=sam_request_budget,
            ))
            results = {
                "fac": fac,
                "usaspending": usa_future.result(),
                "sam": sam_future.result(),
                "fec": fec_future.result(),
                "lda": lda_future.result(),
            }
        if fac_key == "DEMO_KEY" and results["fac"].get("uses_live"):
            results["fac"]["credential"] = "shared_demo_key"
        if fec_key == "DEMO_KEY":
            results["fec"]["credential"] = "shared_demo_key"

    result: dict[str, Any] = {
        "fetched_at": fetched_at,
        "sources": _build_sources(results),
        **results,
    }
    _cache_put(cache_key, result)
    return copy.deepcopy(result)


__all__ = ["fetch_external_checks"]
