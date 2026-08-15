"""Build and query a local Federal Audit Clearinghouse SQLite sidecar.

The builder consumes official FAC CSV exports without loading a complete file
into memory.  It understands the current GSA exports (2016-present) and the
older Census ZIP/CSV layout (1998-2015).  A persistent staging database makes
the build resumable; the destination is replaced only after validation.

Application code should use :func:`lookup_fac_by_ein`, which opens the finished
sidecar read-only.  The import path is deliberately separate from the primary
IRS 990 database.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import ssl
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping, Optional, Sequence


SCHEMA_VERSION = "1"
APP_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - installed by the application requirements
    load_dotenv = None

if load_dotenv:
    load_dotenv(APP_ROOT / ".env")

DEFAULT_FAC_DB_PATH = Path(
    os.getenv("FAC_DB_PATH", APP_ROOT / "db" / "fac_audits.db")
).expanduser().resolve()

CURRENT_DOWNLOAD_PAGE = "https://www.fac.gov/data/download/current/"
HISTORIC_DOWNLOAD_PAGE = "https://www.fac.gov/data/download/historic/"
CURRENT_DICTIONARY_URL = "https://www.fac.gov/data/download/current-dictionary/"
HISTORIC_DICTIONARY_URL = "https://www.fac.gov/data/download/historic-dictionary/"
CURRENT_DOWNLOAD_ROOT = (
    "https://app.fac.gov/dissemination/public-data/gsa/full/"
)
HISTORIC_DOWNLOAD_ROOT = (
    "https://app.fac.gov/dissemination/public-data/census/csv/"
)

CURRENT_TABLES = (
    "general",
    "federal_awards",
    "findings",
    "findings_text",
    "corrective_action_plans",
    "additional_eins",
    "additional_ueis",
)
CURRENT_DOWNLOAD_URLS = tuple(
    CURRENT_DOWNLOAD_ROOT + table + ".csv" for table in CURRENT_TABLES
)
HISTORIC_DOWNLOAD_URL = HISTORIC_DOWNLOAD_ROOT + "census-1998-2015.zip"
HISTORIC_SHA1_URL = HISTORIC_DOWNLOAD_ROOT + "census-1998-2015.sha1"

DEFAULT_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024 * 1024
MIN_CLI_DOWNLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_USER_AGENT = "irs990-tool-fac-bulk/1.0"
_OFFICIAL_DOWNLOAD_FILENAMES = {
    **{
        url: url.rsplit("/", 1)[-1]
        for url in CURRENT_DOWNLOAD_URLS
    },
    HISTORIC_DOWNLOAD_URL: "census-1998-2015.zip",
    HISTORIC_SHA1_URL: "census-1998-2015.sha1",
}
_ALLOWED_RESPONSE_HOSTS = frozenset(
    {"app.fac.gov", "s3-us-gov-west-1.amazonaws.com"}
)
_OFFICIAL_S3_PATH_PREFIX = (
    "/cg-ac8bf271-4c6d-4ee0-bd36-1415b839a93c/public-data/"
)
_CURRENT_REQUIRED_HEADERS = {
    "general.csv": {"report_id", "audit_year", "auditee_ein"},
    "federal_awards.csv": {"report_id", "amount_expended"},
    "findings.csv": {"report_id"},
    "findings_text.csv": {"report_id", "finding_text"},
    "corrective_action_plans.csv": {"report_id", "planned_action"},
    "additional_eins.csv": {"report_id", "additional_ein"},
    "additional_ueis.csv": {"report_id", "additional_uei"},
}

_CURRENT_NAME_RE = re.compile(
    r"^(general|federal_awards|findings|findings_text|corrective_action_plans|"
    r"additional_eins|additional_ueis)(?:-(?:ay|fy|ffy)-?(\d{4}))?\.csv$",
    re.IGNORECASE,
)
_HISTORIC_FILES = {
    "elecauditheader.csv": "general",
    "elecaudits.csv": "federal_awards",
    "elecauditfindings.csv": "findings",
    "eleceins.csv": "additional_eins",
    "elecueis.csv": "additional_ueis",
}
_IMPORT_ORDER = {
    "general": 0,
    "additional_eins": 1,
    "additional_ueis": 2,
    "federal_awards": 3,
    "findings": 4,
    "findings_text": 5,
    "corrective_action_plans": 6,
}
_IMPORT_CHECKPOINT_ROWS = 25_000


@dataclass(frozen=True)
class SourceCandidate:
    path: Path
    member: Optional[str]
    source_era: str
    logical_table: str
    audit_year_hint: Optional[int]
    source_url: str

    @property
    def display_name(self) -> str:
        return str(self.path) + ("::" + self.member if self.member else "")

    @property
    def source_key(self) -> str:
        identity = str(self.path.resolve()) + "\x1f" + (self.member or "")
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceFingerprint:
    candidate: SourceCandidate
    sha256: str
    size_bytes: int
    mtime_ns: int
    archive_sha1: Optional[str]
    official_sha1_verified: bool


class FacDownloadError(RuntimeError):
    """A detail-light error suitable for CLI display without signed redirect URLs."""


class _AllowlistedFacRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not _allowed_response_url(newurl):
            raise FacDownloadError("FAC download redirected to a non-allowlisted host or path")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fac_download_ssl_context() -> ssl.SSLContext:
    """Return a verified TLS context compatible with FAC on Python 3.14.

    Python 3.14 enables OpenSSL's strict X.509 mode in its default context.
    The FAC redirect chain currently fails that additional strict-mode check on
    this Windows runtime.  Clearing only that flag retains CA validation,
    certificate validation, and hostname verification.
    """

    context = ssl.create_default_context()
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    return context


def _allowed_response_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        return False
    if parsed.scheme.casefold() != "https" or parsed.hostname not in _ALLOWED_RESPONSE_HOSTS:
        return False
    if parsed.hostname == "app.fac.gov":
        return parsed.path.startswith("/dissemination/public-data/")
    return parsed.path.startswith(_OFFICIAL_S3_PATH_PREFIX)


def _validate_official_download(url: str, destination: Path) -> str:
    filename = _OFFICIAL_DOWNLOAD_FILENAMES.get(url)
    if not filename:
        raise ValueError("URL is not in the fixed FAC official-download allowlist")
    if destination.name.casefold() != filename.casefold():
        raise ValueError(
            f"Official FAC URL must be saved as {filename}, not {destination.name}"
        )
    return filename


def _part_paths(destination: Path) -> tuple[Path, Path]:
    return Path(str(destination) + ".part"), Path(str(destination) + ".part.json")


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    temporary = Path(str(path) + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(dict(value), stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _load_part_metadata(path: Path) -> Optional[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _header_value(headers: Any, name: str) -> Optional[str]:
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        return None
    return str(value).strip() if value is not None and str(value).strip() else None


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    try:
        return int(status)
    except (TypeError, ValueError):
        return 200


def _open_fac_request(
    request: urllib.request.Request,
    *,
    context: ssl.SSLContext,
    opener: Optional[Callable[..., Any]],
    timeout: float,
) -> Any:
    if opener is not None:
        return opener(request, timeout=timeout, context=context)
    transport = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        _AllowlistedFacRedirectHandler(),
    )
    return transport.open(request, timeout=timeout)


def _parse_content_range(value: Optional[str]) -> Optional[tuple[int, int, int]]:
    if not value:
        return None
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", value.strip(), re.IGNORECASE)
    if not match:
        return None
    start, end, total = (int(part) for part in match.groups())
    if end < start or total <= end:
        return None
    return start, end, total


def _parse_sha1_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise FacDownloadError("Historic FAC checksum file is unavailable") from exc
    match = re.search(r"(?i)\b([0-9a-f]{40})\b", text)
    if not match:
        raise FacDownloadError("Historic FAC checksum file is invalid")
    return match.group(1).casefold()


def _validate_completed_download(
    url: str,
    path: Path,
    *,
    historic_checksum_path: Optional[Path] = None,
) -> None:
    filename = _OFFICIAL_DOWNLOAD_FILENAMES[url]
    if not path.exists() or path.stat().st_size <= 0:
        raise FacDownloadError(f"Downloaded FAC file is empty: {filename}")

    required_headers = _CURRENT_REQUIRED_HEADERS.get(filename)
    if required_headers:
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
                headers = next(csv.reader(stream))
        except (OSError, StopIteration, csv.Error) as exc:
            raise FacDownloadError(f"Downloaded FAC CSV is invalid: {filename}") from exc
        normalized = {_normalize_column(header) for header in headers}
        if not required_headers.issubset(normalized):
            raise FacDownloadError(
                f"Downloaded FAC CSV has an unexpected header: {filename}"
            )
        return

    if filename.endswith(".sha1"):
        _parse_sha1_file(path)
        return

    if url == HISTORIC_DOWNLOAD_URL:
        checksum_path = historic_checksum_path or path.with_suffix(".sha1")
        expected = _parse_sha1_file(checksum_path)
        try:
            with path.open("rb") as stream:
                actual, _ = _hash_stream(stream, "sha1")
        except OSError as exc:
            raise FacDownloadError("Historic FAC archive could not be hashed") from exc
        if actual.casefold() != expected:
            raise FacDownloadError("Historic FAC archive failed its published SHA1 check")
        try:
            with zipfile.ZipFile(path) as archive:
                names = {Path(info.filename).name.casefold() for info in archive.infolist()}
        except (OSError, zipfile.BadZipFile) as exc:
            raise FacDownloadError("Historic FAC archive is not a valid ZIP") from exc
        if not {"elecauditheader.csv", "elecaudits.csv"}.issubset(names):
            raise FacDownloadError("Historic FAC archive is missing required Census tables")


def _publish_download(part_path: Path, metadata_path: Path, destination: Path) -> None:
    os.replace(part_path, destination)
    try:
        metadata_path.unlink()
    except FileNotFoundError:
        pass


def _completed_part_total(metadata: Optional[Mapping[str, Any]]) -> Optional[int]:
    if not metadata:
        return None
    try:
        value = int(metadata.get("expected_total_bytes"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def download_official_fac_file(
    url: str,
    destination: Path | str,
    *,
    refresh: bool = False,
    restart_partial: bool = False,
    max_bytes: int = DEFAULT_DOWNLOAD_MAX_BYTES,
    timeout: float = 60.0,
    opener: Optional[Callable[..., Any]] = None,
    progress: Optional[Callable[[str, int, Optional[int]], None]] = None,
    publish: bool = True,
    historic_checksum_path: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Safely stage and optionally publish one allowlisted official FAC file.

    Existing finished files are skipped unless ``refresh`` is true.  A refresh
    writes only to ``.part`` until the response length and file-specific
    validation succeed, so a failed refresh cannot remove or truncate the
    previous finished file.  The injected opener hook exists for deterministic
    tests; normal calls always use the verified allowlisted transport above.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    destination_path = Path(destination).expanduser().resolve()
    filename = _validate_official_download(url, destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    part_path, metadata_path = _part_paths(destination_path)
    if restart_partial:
        for stale in (part_path, metadata_path, Path(str(metadata_path) + ".tmp")):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
    if destination_path.exists() and not refresh:
        return {
            "status": "existing",
            "url": url,
            "path": str(destination_path),
            "bytes": destination_path.stat().st_size,
        }

    metadata = _load_part_metadata(metadata_path)
    part_size = part_path.stat().st_size if part_path.exists() else 0
    if part_size > max_bytes:
        raise FacDownloadError(f"FAC partial file exceeds the configured size cap: {filename}")
    try:
        checkpoint_size = int(metadata.get("part_size_at_checkpoint", -1)) if metadata else -1
    except (TypeError, ValueError):
        checkpoint_size = -1
    metadata_matches = bool(
        metadata
        and metadata.get("url") == url
        and 0 <= checkpoint_size <= part_size
    )
    validator = None
    if metadata_matches:
        validator = metadata.get("etag") or metadata.get("last_modified")
    resume_at = part_size if metadata_matches and validator else 0
    expected_total = _completed_part_total(metadata) if metadata_matches else None

    checksum_path = (
        Path(historic_checksum_path).expanduser().resolve()
        if historic_checksum_path is not None
        else None
    )
    if part_size and expected_total == part_size:
        _validate_completed_download(
            url, part_path, historic_checksum_path=checksum_path
        )
        if publish:
            _publish_download(part_path, metadata_path, destination_path)
            status = "downloaded"
            result_path = destination_path
        else:
            status = "staged"
            result_path = part_path
        return {
            "status": status,
            "url": url,
            "path": str(destination_path),
            "staged_path": str(result_path),
            "bytes": part_size,
            "resumed": True,
        }

    headers = {
        "Accept": "text/csv, application/zip, text/plain, application/octet-stream",
        "Accept-Encoding": "identity",
        "User-Agent": _DOWNLOAD_USER_AGENT,
    }
    if resume_at:
        headers["Range"] = f"bytes={resume_at}-"
        headers["If-Range"] = str(validator)
    request = urllib.request.Request(url, headers=headers, method="GET")
    context = fac_download_ssl_context()
    try:
        response = _open_fac_request(
            request, context=context, opener=opener, timeout=timeout
        )
    except urllib.error.HTTPError as exc:
        raise FacDownloadError(f"FAC download returned HTTP {exc.code}: {filename}") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise FacDownloadError(f"FAC download request failed: {filename}") from None

    try:
        with response:
            response_url = response.geturl() if hasattr(response, "geturl") else url
            if not _allowed_response_url(response_url):
                raise FacDownloadError("FAC response came from a non-allowlisted host or path")
            status = _response_status(response)
            content_length = _integer(_header_value(response.headers, "Content-Length"))
            content_range = _parse_content_range(
                _header_value(response.headers, "Content-Range")
            )
            if status == 206:
                if not resume_at or not content_range or content_range[0] != resume_at:
                    raise FacDownloadError(f"FAC resume response was inconsistent: {filename}")
                write_mode = "ab"
                downloaded = resume_at
                expected_total = content_range[2]
                if content_length is not None and content_length != expected_total - resume_at:
                    raise FacDownloadError(f"FAC resume length was inconsistent: {filename}")
            elif status == 200:
                write_mode = "wb"
                downloaded = 0
                expected_total = content_length
            else:
                raise FacDownloadError(f"FAC download returned HTTP {status}: {filename}")

            if expected_total is not None and expected_total > max_bytes:
                raise FacDownloadError(f"FAC file exceeds the configured size cap: {filename}")
            etag = _header_value(response.headers, "ETag")
            last_modified = _header_value(response.headers, "Last-Modified")
            _atomic_json_write(
                metadata_path,
                {
                    "url": url,
                    "etag": etag,
                    "last_modified": last_modified,
                    "expected_total_bytes": expected_total,
                    "part_size_at_checkpoint": downloaded,
                    "updated_at_utc": _utc_now(),
                },
            )
            if progress:
                progress(filename, downloaded, expected_total)
            with part_path.open(write_mode) as stream:
                while True:
                    chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    next_total = downloaded + len(chunk)
                    if next_total > max_bytes:
                        raise FacDownloadError(
                            f"FAC file exceeds the configured size cap: {filename}"
                        )
                    stream.write(chunk)
                    downloaded = next_total
                    if progress:
                        progress(filename, downloaded, expected_total)
                stream.flush()
                os.fsync(stream.fileno())
    except FacDownloadError:
        raise
    except (OSError, TimeoutError):
        raise FacDownloadError(f"FAC download stream failed: {filename}") from None

    if expected_total is not None and downloaded != expected_total:
        raise FacDownloadError(
            f"FAC download ended before the advertised length: {filename}"
        )
    _atomic_json_write(
        metadata_path,
        {
            "url": url,
            "etag": etag,
            "last_modified": last_modified,
            "expected_total_bytes": downloaded,
            "part_size_at_checkpoint": downloaded,
            "updated_at_utc": _utc_now(),
        },
    )
    _validate_completed_download(
        url, part_path, historic_checksum_path=checksum_path
    )
    if publish:
        _publish_download(part_path, metadata_path, destination_path)
        result_status = "downloaded"
        result_path = destination_path
    else:
        result_status = "staged"
        result_path = part_path
    return {
        "status": result_status,
        "url": url,
        "path": str(destination_path),
        "staged_path": str(result_path),
        "bytes": downloaded,
        "resumed": bool(resume_at),
    }


def download_official_fac_sources(
    root: Path | str,
    *,
    include_current: bool = False,
    include_historic: bool = False,
    refresh: bool = False,
    restart_partials: bool = False,
    max_bytes: int = DEFAULT_DOWNLOAD_MAX_BYTES,
    timeout: float = 60.0,
    opener: Optional[Callable[..., Any]] = None,
    progress: Optional[Callable[[str, int, Optional[int]], None]] = None,
) -> dict[str, Any]:
    """Download selected official source groups, validating all before publish."""

    if not include_current and not include_historic:
        raise ValueError("Select current and/or historic FAC downloads")
    root_path = Path(root).expanduser().resolve()
    plan: list[tuple[str, Path]] = []
    if include_current:
        plan.extend(
            (url, root_path / "current" / _OFFICIAL_DOWNLOAD_FILENAMES[url])
            for url in CURRENT_DOWNLOAD_URLS
        )
    if include_historic:
        # Checksum first so the staged archive can be authenticated before publish.
        plan.extend(
            [
                (HISTORIC_SHA1_URL, root_path / "historic" / "census-1998-2015.sha1"),
                (HISTORIC_DOWNLOAD_URL, root_path / "historic" / "census-1998-2015.zip"),
            ]
        )

    results: list[dict[str, Any]] = []
    staged: list[tuple[Path, Path, Path]] = []
    staged_by_destination: dict[Path, Path] = {}
    historic_checksum_stage: Optional[Path] = None
    for url, destination in plan:
        part_path, metadata_path = _part_paths(destination)
        checksum_for_zip: Optional[Path] = None
        if url == HISTORIC_DOWNLOAD_URL:
            checksum_for_zip = historic_checksum_stage or destination.with_suffix(".sha1")
        result = download_official_fac_file(
            url,
            destination,
            refresh=refresh,
            restart_partial=restart_partials,
            max_bytes=max_bytes,
            timeout=timeout,
            opener=opener,
            progress=progress,
            publish=False,
            historic_checksum_path=checksum_for_zip,
        )
        results.append(result)
        if result["status"] == "staged":
            staged.append((part_path, metadata_path, destination))
            staged_by_destination[destination] = part_path
            if url == HISTORIC_SHA1_URL:
                historic_checksum_stage = part_path

    if include_historic:
        checksum_destination = root_path / "historic" / "census-1998-2015.sha1"
        archive_destination = root_path / "historic" / "census-1998-2015.zip"
        checksum_source = staged_by_destination.get(
            checksum_destination, checksum_destination
        )
        archive_source = staged_by_destination.get(
            archive_destination, archive_destination
        )
        _validate_completed_download(
            HISTORIC_DOWNLOAD_URL,
            archive_source,
            historic_checksum_path=checksum_source,
        )

    # No finished member of the selected source group changes until every new
    # or refreshed member has downloaded and validated.
    for part_path, metadata_path, destination in staged:
        _publish_download(part_path, metadata_path, destination)
    for result in results:
        if result["status"] == "staged":
            result["status"] = "downloaded"
            result["staged_path"] = result["path"]
    return {
        "root": str(root_path),
        "current_directory": str(root_path / "current") if include_current else None,
        "historic_directory": str(root_path / "historic") if include_historic else None,
        "files": results,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_column(value: Any) -> str:
    text = str(value or "").lstrip("\ufeff").strip().casefold()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _clean_row(row: Mapping[Any, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        normalized = _normalize_column(key)
        if not normalized:
            continue
        if isinstance(value, list):
            text = ",".join(str(item) for item in value)
        else:
            text = "" if value is None else str(value)
        cleaned[normalized] = text.strip()
    return cleaned


def _value(row: Mapping[str, str], *names: str) -> Optional[str]:
    for name in names:
        value = row.get(_normalize_column(name))
        if value is not None and value.strip() != "":
            return value.strip()
    return None


def _clean_ein(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", "" if value is None else str(value))
    return digits if len(digits) == 9 else None


def _clean_uei(value: Any) -> Optional[str]:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", "" if value is None else str(value)).upper()
    return cleaned if len(cleaned) == 12 else None


def _integer(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(Decimal(str(value).strip().replace(",", "")))
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _number(value: Any) -> Optional[int | float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text.casefold() in {"n/a", "na", "none", "null"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if number == number.to_integral_value():
        integer = int(number)
        if -(2**63) <= integer < 2**63:
            return integer
    try:
        return float(number)
    except (ValueError, OverflowError):
        return None


def _boolean(value: Any) -> Optional[int]:
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    if normalized in {"y", "yes", "true", "t", "1", "x"}:
        return 1
    if normalized in {"n", "no", "false", "f", "0"}:
        return 0
    return None


def _date_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return text


def _raw_json(row: Mapping[str, str]) -> str:
    return json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_key(logical_table: str, report_id: str, row: Mapping[str, str]) -> str:
    payload = logical_table + "\x1f" + report_id + "\x1f" + _raw_json(row)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _year_hint(*values: str) -> Optional[int]:
    for value in values:
        matches = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", value)
        for match in matches:
            year = int(match)
            if 1998 <= year <= 2200:
                return year
    return None


def _classify(path: Path, member: Optional[str]) -> Optional[SourceCandidate]:
    name = Path(member).name if member else path.name
    current = _CURRENT_NAME_RE.match(name)
    if current:
        logical_table = current.group(1).casefold()
        year = int(current.group(2)) if current.group(2) else _year_hint(member or "", str(path))
        direct_url = (
            CURRENT_DOWNLOAD_ROOT + logical_table + ".csv"
            if name.casefold() == logical_table + ".csv"
            else CURRENT_DOWNLOAD_PAGE
        )
        return SourceCandidate(
            path=path.resolve(),
            member=member,
            source_era="current",
            logical_table=logical_table,
            audit_year_hint=year,
            source_url=direct_url,
        )

    logical_table = _HISTORIC_FILES.get(name.casefold())
    if not logical_table:
        return None
    year = _year_hint(member or "", str(path))
    archive_name = path.name.casefold()
    if archive_name == "census-1998-2015.zip":
        source_url = HISTORIC_DOWNLOAD_URL
    elif year and path.suffix.casefold() == ".zip":
        source_url = HISTORIC_DOWNLOAD_ROOT + f"census-{year}.zip"
    else:
        source_url = HISTORIC_DOWNLOAD_PAGE
    return SourceCandidate(
        path=path.resolve(),
        member=member,
        source_era="historic",
        logical_table=logical_table,
        audit_year_hint=year,
        source_url=source_url,
    )


def discover_sources(input_paths: Sequence[Path | str]) -> list[SourceCandidate]:
    """Find recognized FAC CSVs and CSV members in ZIP files."""

    files: list[Path] = []
    for raw_path in input_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"FAC input path not found: {path}")
        if path.is_dir():
            files.extend(
                child for child in path.rglob("*")
                if child.is_file() and child.suffix.casefold() in {".csv", ".zip"}
            )
        else:
            files.append(path)

    candidates: list[SourceCandidate] = []
    seen: set[tuple[str, Optional[str]]] = set()
    for path in sorted(set(files), key=lambda item: str(item).casefold()):
        if path.suffix.casefold() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    members = sorted(
                        (info.filename for info in archive.infolist() if not info.is_dir()),
                        key=str.casefold,
                    )
            except zipfile.BadZipFile as exc:
                raise RuntimeError(f"Invalid FAC ZIP archive: {path}") from exc
            for member in members:
                candidate = _classify(path, member)
                if candidate and (str(path), member) not in seen:
                    candidates.append(candidate)
                    seen.add((str(path), member))
        elif path.suffix.casefold() == ".csv":
            candidate = _classify(path, None)
            if candidate and (str(path), None) not in seen:
                candidates.append(candidate)
                seen.add((str(path), None))

    if not candidates:
        supported = ", ".join(CURRENT_TABLES) + ", ELECAUDITHEADER/ELECAUDITS/..."
        raise RuntimeError(f"No recognized FAC CSV sources found. Supported tables: {supported}")
    return sorted(
        candidates,
        key=lambda item: (
            0 if item.source_era == "current" else 1,
            _IMPORT_ORDER[item.logical_table],
            item.audit_year_hint or 0,
            item.display_name.casefold(),
        ),
    )


@contextmanager
def _open_binary(candidate: SourceCandidate) -> Iterator[BinaryIO]:
    if candidate.member:
        with zipfile.ZipFile(candidate.path) as archive:
            with archive.open(candidate.member, "r") as stream:
                yield stream
    else:
        with candidate.path.open("rb") as stream:
            yield stream


def _hash_stream(stream: BinaryIO, algorithm: str = "sha256") -> tuple[str, int]:
    digest = hashlib.new(algorithm)
    size = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _expected_sha1(zip_path: Path) -> Optional[str]:
    checksum_path = zip_path.with_suffix(".sha1")
    if not checksum_path.exists():
        return None
    match = re.search(r"(?i)\b([0-9a-f]{40})\b", checksum_path.read_text(encoding="utf-8", errors="replace"))
    if not match:
        raise RuntimeError(f"Could not parse SHA1 checksum file: {checksum_path}")
    return match.group(1).casefold()


def fingerprint_sources(candidates: Sequence[SourceCandidate]) -> list[SourceFingerprint]:
    """Hash source bytes and verify an adjacent official legacy .sha1, if present."""

    archive_hashes: dict[Path, tuple[str, bool]] = {}
    fingerprints: list[SourceFingerprint] = []
    for candidate in candidates:
        archive_sha1: Optional[str] = None
        verified = False
        if candidate.path.suffix.casefold() == ".zip":
            if candidate.path not in archive_hashes:
                expected = _expected_sha1(candidate.path)
                if expected:
                    with candidate.path.open("rb") as stream:
                        actual, _ = _hash_stream(stream, "sha1")
                    if actual.casefold() != expected:
                        raise RuntimeError(
                            f"SHA1 mismatch for {candidate.path}: expected {expected}, got {actual}"
                        )
                    archive_hashes[candidate.path] = (actual, True)
                else:
                    archive_hashes[candidate.path] = ("", False)
            archive_sha1, verified = archive_hashes[candidate.path]
            archive_sha1 = archive_sha1 or None

        with _open_binary(candidate) as stream:
            sha256, size = _hash_stream(stream, "sha256")
        stat = candidate.path.stat()
        fingerprints.append(
            SourceFingerprint(
                candidate=candidate,
                sha256=sha256,
                size_bytes=size,
                mtime_ns=stat.st_mtime_ns,
                archive_sha1=archive_sha1,
                official_sha1_verified=verified,
            )
        )
    return fingerprints


_SCHEMA_SQL = """
CREATE TABLE fac_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE fac_source_files (
    source_key TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_member TEXT,
    source_url TEXT NOT NULL,
    source_era TEXT NOT NULL CHECK(source_era IN ('current', 'historic')),
    logical_table TEXT NOT NULL,
    audit_year_hint INTEGER,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    archive_sha1 TEXT,
    official_sha1_verified INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    source_as_of_date TEXT NOT NULL,
    imported_at_utc TEXT NOT NULL
);

CREATE TABLE fac_reports (
    report_id TEXT PRIMARY KEY,
    source_era TEXT NOT NULL,
    audit_year INTEGER,
    historic_dbkey TEXT,
    auditee_ein TEXT,
    auditee_uei TEXT,
    auditee_name TEXT,
    entity_type TEXT,
    fy_start_date TEXT,
    fy_end_date TEXT,
    audit_type TEXT,
    fac_accepted_date TEXT,
    submitted_date TEXT,
    total_amount_expended NUMERIC,
    gaap_results TEXT,
    is_going_concern_included INTEGER,
    is_internal_control_material_weakness_disclosed INTEGER,
    is_internal_control_deficiency_disclosed INTEGER,
    is_material_noncompliance_disclosed INTEGER,
    is_low_risk_auditee INTEGER,
    agencies_with_prior_findings TEXT,
    auditor_firm_name TEXT,
    is_public INTEGER,
    resubmission_version INTEGER,
    resubmission_status TEXT,
    raw_json TEXT NOT NULL,
    source_key TEXT NOT NULL
);

CREATE TABLE fac_additional_eins (
    record_key TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    audit_year INTEGER,
    additional_ein TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    source_key TEXT NOT NULL
);

CREATE TABLE fac_additional_ueis (
    record_key TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    audit_year INTEGER,
    additional_uei TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    source_key TEXT NOT NULL
);

CREATE TABLE fac_awards (
    record_key TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    audit_year INTEGER,
    award_reference TEXT,
    legacy_award_id TEXT,
    federal_agency_prefix TEXT,
    federal_award_extension TEXT,
    additional_award_identification TEXT,
    federal_program_name TEXT,
    amount_expended NUMERIC,
    cluster_name TEXT,
    state_cluster_name TEXT,
    federal_program_total NUMERIC,
    cluster_total NUMERIC,
    is_direct INTEGER,
    is_passthrough_award INTEGER,
    passthrough_amount NUMERIC,
    is_major INTEGER,
    audit_report_type TEXT,
    is_loan INTEGER,
    loan_balance NUMERIC,
    findings_count INTEGER,
    raw_json TEXT NOT NULL,
    source_key TEXT NOT NULL
);

CREATE TABLE fac_findings (
    record_key TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    audit_year INTEGER,
    award_reference TEXT,
    legacy_award_id TEXT,
    reference_number TEXT,
    type_requirement TEXT,
    is_modified_opinion INTEGER,
    is_other_matters INTEGER,
    is_material_weakness INTEGER,
    is_significant_deficiency INTEGER,
    is_other_findings INTEGER,
    is_questioned_costs INTEGER,
    is_repeat_finding INTEGER,
    prior_finding_ref_numbers TEXT,
    raw_json TEXT NOT NULL,
    source_key TEXT NOT NULL
);

CREATE TABLE fac_findings_text (
    record_key TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    audit_year INTEGER,
    finding_ref_number TEXT,
    finding_text TEXT,
    contains_chart_or_table INTEGER,
    raw_json TEXT NOT NULL,
    source_key TEXT NOT NULL
);

CREATE TABLE fac_corrective_action_plans (
    record_key TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    audit_year INTEGER,
    finding_ref_number TEXT,
    planned_action TEXT,
    contains_chart_or_table INTEGER,
    raw_json TEXT NOT NULL,
    source_key TEXT NOT NULL
);

CREATE TABLE fac_rejected_rows (
    source_key TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    logical_table TEXT NOT NULL,
    reason TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY(source_key, source_row_number)
);

CREATE TABLE fac_import_progress (
    source_key TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    last_source_row_number INTEGER NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    updated_at_utc TEXT NOT NULL
);
"""


_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_fac_reports_ein_year
    ON fac_reports(auditee_ein, audit_year DESC);
CREATE INDEX IF NOT EXISTS idx_fac_reports_uei_year
    ON fac_reports(auditee_uei, audit_year DESC);
CREATE INDEX IF NOT EXISTS idx_fac_reports_year
    ON fac_reports(audit_year DESC);
CREATE INDEX IF NOT EXISTS idx_fac_reports_period
    ON fac_reports(fy_end_date DESC, fac_accepted_date DESC);
CREATE INDEX IF NOT EXISTS idx_fac_additional_eins_lookup
    ON fac_additional_eins(additional_ein, report_id);
CREATE INDEX IF NOT EXISTS idx_fac_additional_ueis_lookup
    ON fac_additional_ueis(additional_uei, report_id);
CREATE INDEX IF NOT EXISTS idx_fac_awards_report
    ON fac_awards(report_id, award_reference);
CREATE INDEX IF NOT EXISTS idx_fac_awards_year
    ON fac_awards(audit_year DESC);
CREATE INDEX IF NOT EXISTS idx_fac_findings_report
    ON fac_findings(report_id, reference_number);
CREATE INDEX IF NOT EXISTS idx_fac_findings_year
    ON fac_findings(audit_year DESC);
CREATE INDEX IF NOT EXISTS idx_fac_findings_text_report
    ON fac_findings_text(report_id, finding_ref_number);
CREATE INDEX IF NOT EXISTS idx_fac_caps_report
    ON fac_corrective_action_plans(report_id, finding_ref_number);

CREATE VIEW IF NOT EXISTS fac_report_eins AS
SELECT report_id, auditee_ein AS ein, 'primary_ein' AS match_type
FROM fac_reports
WHERE auditee_ein IS NOT NULL
UNION ALL
SELECT report_id, additional_ein AS ein, 'additional_ein' AS match_type
FROM fac_additional_eins;
"""


def _set_metadata(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO fac_metadata(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _historic_report_id(row: Mapping[str, str], year_hint: Optional[int]) -> tuple[Optional[str], Optional[int], Optional[str]]:
    year = _integer(_value(row, "audit_year", "audityear")) or year_hint
    dbkey = _value(row, "dbkey")
    if year is None or not dbkey:
        return None, year, dbkey
    return f"historic:{year}:{dbkey}", year, dbkey


def _report_identity(
    row: Mapping[str, str], source_era: str, year_hint: Optional[int]
) -> tuple[Optional[str], Optional[int], Optional[str]]:
    if source_era == "historic":
        return _historic_report_id(row, year_hint)
    report_id = _value(row, "report_id")
    year = _integer(_value(row, "audit_year")) or year_hint
    return report_id, year, None


def _insert_report(
    conn: sqlite3.Connection,
    row: Mapping[str, str],
    fingerprint: SourceFingerprint,
) -> Optional[str]:
    candidate = fingerprint.candidate
    report_id, audit_year, dbkey = _report_identity(row, candidate.source_era, candidate.audit_year_hint)
    if not report_id:
        return "missing report_id, or historic AUDITYEAR/DBKEY"

    values = {
        "report_id": report_id,
        "source_era": candidate.source_era,
        "audit_year": audit_year,
        "historic_dbkey": dbkey,
        "auditee_ein": _clean_ein(_value(row, "auditee_ein", "ein")),
        "auditee_uei": _clean_uei(_value(row, "auditee_uei", "uei")),
        "auditee_name": _value(row, "auditee_name", "auditeename"),
        "entity_type": _value(row, "entity_type", "typeofentity"),
        "fy_start_date": _date_text(_value(row, "fy_start_date", "fystartdate")),
        "fy_end_date": _date_text(_value(row, "fy_end_date", "fyenddate")),
        "audit_type": _value(row, "audit_type", "audittype"),
        "fac_accepted_date": _date_text(_value(row, "fac_accepted_date", "fac accepted date", "completed_on")),
        "submitted_date": _date_text(_value(row, "submitted_date", "form date received", "initial date received")),
        "total_amount_expended": _number(_value(row, "total_amount_expended", "totfedexpend")),
        "gaap_results": _value(row, "gaap_results", "typereport_fs"),
        "is_going_concern_included": _boolean(_value(row, "is_going_concern_included", "goingconcern")),
        "is_internal_control_material_weakness_disclosed": _boolean(_value(row, "is_internal_control_material_weakness_disclosed", "materialweakness")),
        "is_internal_control_deficiency_disclosed": _boolean(_value(row, "is_internal_control_deficiency_disclosed", "reportablecondition/significantdeficiency", "significantdeficiency")),
        "is_material_noncompliance_disclosed": _boolean(_value(row, "is_material_noncompliance_disclosed", "materialnoncompliance")),
        "is_low_risk_auditee": _boolean(_value(row, "is_low_risk_auditee", "lowrisk")),
        "agencies_with_prior_findings": _value(row, "agencies_with_prior_findings", "pyschedule"),
        "auditor_firm_name": _value(row, "auditor_firm_name", "cpafirmname"),
        "is_public": _boolean(_value(row, "is_public")),
        "resubmission_version": _integer(_value(row, "resubmission_version")),
        "resubmission_status": _value(row, "resubmission_status"),
        "raw_json": _raw_json(row),
        "source_key": candidate.source_key,
    }
    columns = ", ".join(values)
    placeholders = ", ".join(":" + name for name in values)
    updates = ", ".join(
        f"{name}=excluded.{name}" for name in values if name != "report_id"
    )
    conn.execute(
        f"INSERT INTO fac_reports ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(report_id) DO UPDATE SET {updates}",
        values,
    )
    return None


def _insert_additional_identifier(
    conn: sqlite3.Connection,
    row: Mapping[str, str],
    fingerprint: SourceFingerprint,
    *,
    kind: str,
) -> Optional[str]:
    candidate = fingerprint.candidate
    report_id, audit_year, _ = _report_identity(row, candidate.source_era, candidate.audit_year_hint)
    raw_value = _value(row, f"additional_{kind}", kind)
    value = _clean_ein(raw_value) if kind == "ein" else _clean_uei(raw_value)
    if not report_id or not value:
        return f"missing report identity or valid additional {kind.upper()}"
    table = "fac_additional_eins" if kind == "ein" else "fac_additional_ueis"
    column = "additional_ein" if kind == "ein" else "additional_uei"
    conn.execute(
        f"INSERT OR IGNORE INTO {table} "
        f"(record_key, report_id, audit_year, {column}, raw_json, source_key) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            _record_key(candidate.logical_table, report_id, row),
            report_id,
            audit_year,
            value,
            _raw_json(row),
            candidate.source_key,
        ),
    )
    return None


def _insert_award(
    conn: sqlite3.Connection,
    row: Mapping[str, str],
    fingerprint: SourceFingerprint,
) -> Optional[str]:
    candidate = fingerprint.candidate
    report_id, audit_year, _ = _report_identity(row, candidate.source_era, candidate.audit_year_hint)
    if not report_id:
        return "missing report identity"
    cfda = _value(row, "cfda") or ""
    prefix, extension = (cfda.split(".", 1) + [None])[:2] if cfda else (None, None)
    values = (
        _record_key(candidate.logical_table, report_id, row),
        report_id,
        audit_year,
        _value(row, "award_reference", "findingrefnums"),
        _value(row, "elecauditsid"),
        _value(row, "federal_agency_prefix") or prefix,
        _value(row, "federal_award_extension") or extension,
        _value(row, "additional_award_identification", "awardidentification"),
        _value(row, "federal_program_name", "federalprogramname", "cfdaprogramname"),
        _number(_value(row, "amount_expended", "amount")),
        _value(row, "cluster_name", "clustername", "otherclustername"),
        _value(row, "state_cluster_name", "stateclustername"),
        _number(_value(row, "federal_program_total", "programtotal")),
        _number(_value(row, "cluster_total", "clustertotal")),
        _boolean(_value(row, "is_direct", "direct")),
        _boolean(_value(row, "is_passthrough_award", "passthroughaward")),
        _number(_value(row, "passthrough_amount", "passthroughamount")),
        _boolean(_value(row, "is_major", "majorprogram")),
        _value(row, "audit_report_type", "typereport_mp"),
        _boolean(_value(row, "is_loan", "loans")),
        _number(_value(row, "loan_balance", "loanbalance")),
        _integer(_value(row, "findings_count", "findingscount")),
        _raw_json(row),
        candidate.source_key,
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO fac_awards (
            record_key, report_id, audit_year, award_reference, legacy_award_id,
            federal_agency_prefix, federal_award_extension,
            additional_award_identification, federal_program_name,
            amount_expended, cluster_name, state_cluster_name,
            federal_program_total, cluster_total, is_direct,
            is_passthrough_award, passthrough_amount, is_major,
            audit_report_type, is_loan, loan_balance, findings_count,
            raw_json, source_key
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        values,
    )
    return None


def _insert_finding(
    conn: sqlite3.Connection,
    row: Mapping[str, str],
    fingerprint: SourceFingerprint,
) -> Optional[str]:
    candidate = fingerprint.candidate
    report_id, audit_year, _ = _report_identity(row, candidate.source_era, candidate.audit_year_hint)
    if not report_id:
        return "missing report identity"
    conn.execute(
        """
        INSERT OR IGNORE INTO fac_findings (
            record_key, report_id, audit_year, award_reference, legacy_award_id,
            reference_number, type_requirement, is_modified_opinion,
            is_other_matters, is_material_weakness, is_significant_deficiency,
            is_other_findings, is_questioned_costs, is_repeat_finding,
            prior_finding_ref_numbers, raw_json, source_key
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _record_key(candidate.logical_table, report_id, row),
            report_id,
            audit_year,
            _value(row, "award_reference"),
            _value(row, "elecauditsid"),
            _value(row, "reference_number", "findingrefnums"),
            _value(row, "type_requirement", "typerequirement"),
            _boolean(_value(row, "is_modified_opinion", "modifiedopinion")),
            _boolean(_value(row, "is_other_matters", "othernoncompliance")),
            _boolean(_value(row, "is_material_weakness", "materialweakness")),
            _boolean(_value(row, "is_significant_deficiency", "significantdeficiency")),
            _boolean(_value(row, "is_other_findings", "otherfindings")),
            _boolean(_value(row, "is_questioned_costs", "qcosts")),
            _boolean(_value(row, "is_repeat_finding", "repeatfinding")),
            _value(row, "prior_finding_ref_numbers", "priorfindingrefnums"),
            _raw_json(row),
            candidate.source_key,
        ),
    )
    return None


def _insert_long_text(
    conn: sqlite3.Connection,
    row: Mapping[str, str],
    fingerprint: SourceFingerprint,
    *,
    corrective_action: bool,
) -> Optional[str]:
    candidate = fingerprint.candidate
    report_id, audit_year, _ = _report_identity(row, candidate.source_era, candidate.audit_year_hint)
    if not report_id:
        return "missing report identity"
    table = "fac_corrective_action_plans" if corrective_action else "fac_findings_text"
    text_column = "planned_action" if corrective_action else "finding_text"
    text_value = _value(row, text_column)
    conn.execute(
        f"INSERT OR IGNORE INTO {table} "
        f"(record_key, report_id, audit_year, finding_ref_number, {text_column}, "
        "contains_chart_or_table, raw_json, source_key) VALUES (?,?,?,?,?,?,?,?)",
        (
            _record_key(candidate.logical_table, report_id, row),
            report_id,
            audit_year,
            _value(row, "finding_ref_number"),
            text_value,
            _boolean(_value(row, "contains_chart_or_table")),
            _raw_json(row),
            candidate.source_key,
        ),
    )
    return None


def _insert_row(
    conn: sqlite3.Connection, row: Mapping[str, str], fingerprint: SourceFingerprint
) -> Optional[str]:
    table = fingerprint.candidate.logical_table
    if table == "general":
        return _insert_report(conn, row, fingerprint)
    if table == "additional_eins":
        return _insert_additional_identifier(conn, row, fingerprint, kind="ein")
    if table == "additional_ueis":
        return _insert_additional_identifier(conn, row, fingerprint, kind="uei")
    if table == "federal_awards":
        return _insert_award(conn, row, fingerprint)
    if table == "findings":
        return _insert_finding(conn, row, fingerprint)
    if table == "findings_text":
        return _insert_long_text(conn, row, fingerprint, corrective_action=False)
    if table == "corrective_action_plans":
        return _insert_long_text(conn, row, fingerprint, corrective_action=True)
    return "unsupported logical table"


def _detect_csv(candidate: SourceCandidate) -> tuple[str, csv.Dialect]:
    with _open_binary(candidate) as stream:
        sample = stream.read(128 * 1024)
    try:
        sample_text = sample.decode("utf-8-sig")
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        sample_text = sample.decode("cp1252", errors="replace")
        encoding = "cp1252"
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=",\t|")
    except csv.Error:
        dialect = csv.excel
    return encoding, dialect


def _iter_csv_rows(candidate: SourceCandidate) -> Iterator[tuple[int, dict[str, str]]]:
    encoding, dialect = _detect_csv(candidate)
    csv.field_size_limit(64 * 1024 * 1024)
    with _open_binary(candidate) as binary:
        with io.TextIOWrapper(binary, encoding=encoding, errors="replace", newline="") as text_stream:
            reader = csv.DictReader(text_stream, dialect=dialect)
            if not reader.fieldnames:
                raise RuntimeError(f"FAC source has no CSV header: {candidate.display_name}")
            for row_number, raw_row in enumerate(reader, start=2):
                yield row_number, _clean_row(raw_row)


def _import_candidate(
    conn: sqlite3.Connection,
    fingerprint: SourceFingerprint,
    source_as_of_date: str,
) -> tuple[int, int]:
    candidate = fingerprint.candidate
    existing = conn.execute(
        "SELECT sha256, size_bytes, row_count, rejected_count FROM fac_source_files WHERE source_key=?",
        (candidate.source_key,),
    ).fetchone()
    if existing:
        if existing[0] != fingerprint.sha256 or int(existing[1]) != fingerprint.size_bytes:
            raise RuntimeError(
                f"A source changed since the staged import: {candidate.display_name}. "
                "Use --restart to rebuild staging from the new inputs."
            )
        return int(existing[2]), int(existing[3])

    progress = conn.execute(
        "SELECT sha256, size_bytes, last_source_row_number, accepted_count, rejected_count "
        "FROM fac_import_progress WHERE source_key=?",
        (candidate.source_key,),
    ).fetchone()
    if progress and (
        progress[0] != fingerprint.sha256 or int(progress[1]) != fingerprint.size_bytes
    ):
        raise RuntimeError(
            f"A partially imported source changed: {candidate.display_name}. "
            "Use --restart to rebuild staging from the new inputs."
        )
    last_committed_row = int(progress[2]) if progress else 1
    accepted = int(progress[3]) if progress else 0
    rejected = int(progress[4]) if progress else 0
    rows_since_checkpoint = 0
    try:
        for row_number, row in _iter_csv_rows(candidate):
            if row_number <= last_committed_row:
                continue
            reason = _insert_row(conn, row, fingerprint)
            if reason:
                rejected += 1
                conn.execute(
                    "INSERT INTO fac_rejected_rows "
                    "(source_key, source_row_number, logical_table, reason, raw_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        candidate.source_key,
                        row_number,
                        candidate.logical_table,
                        reason,
                        _raw_json(row),
                    ),
                )
            else:
                accepted += 1
            rows_since_checkpoint += 1
            if rows_since_checkpoint >= _IMPORT_CHECKPOINT_ROWS:
                conn.execute(
                    """
                    INSERT INTO fac_import_progress (
                        source_key, sha256, size_bytes, last_source_row_number,
                        accepted_count, rejected_count, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_key) DO UPDATE SET
                        sha256=excluded.sha256,
                        size_bytes=excluded.size_bytes,
                        last_source_row_number=excluded.last_source_row_number,
                        accepted_count=excluded.accepted_count,
                        rejected_count=excluded.rejected_count,
                        updated_at_utc=excluded.updated_at_utc
                    """,
                    (
                        candidate.source_key,
                        fingerprint.sha256,
                        fingerprint.size_bytes,
                        row_number,
                        accepted,
                        rejected,
                        _utc_now(),
                    ),
                )
                conn.commit()
                last_committed_row = row_number
                rows_since_checkpoint = 0
        conn.execute(
            """
            INSERT INTO fac_source_files (
                source_key, source_path, source_member, source_url, source_era,
                logical_table, audit_year_hint, size_bytes, mtime_ns, sha256,
                archive_sha1, official_sha1_verified, row_count, rejected_count,
                source_as_of_date, imported_at_utc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate.source_key,
                str(candidate.path),
                candidate.member,
                candidate.source_url,
                candidate.source_era,
                candidate.logical_table,
                candidate.audit_year_hint,
                fingerprint.size_bytes,
                fingerprint.mtime_ns,
                fingerprint.sha256,
                fingerprint.archive_sha1,
                int(fingerprint.official_sha1_verified),
                accepted,
                rejected,
                source_as_of_date,
                _utc_now(),
            ),
        )
        conn.execute(
            "DELETE FROM fac_import_progress WHERE source_key=?",
            (candidate.source_key,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return accepted, rejected


def _manifest_signature(fingerprints: Sequence[SourceFingerprint]) -> str:
    manifest = [
        {
            "source_key": item.candidate.source_key,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "logical_table": item.candidate.logical_table,
            "source_era": item.candidate.source_era,
        }
        for item in fingerprints
    ]
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _staging_path(destination: Path) -> Path:
    return destination.with_name(destination.stem + ".building" + destination.suffix)


def _remove_staging(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-journal"), Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            candidate.unlink()


def _validate_as_of(value: Optional[str]) -> tuple[str, str]:
    if value:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("--source-as-of must use YYYY-MM-DD") from exc
        return parsed.isoformat(), "operator_supplied_download_or_publication_date"
    return date.today().isoformat(), "build_date_not_publisher_date"


def _open_build_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _initialize_or_validate_staging(
    conn: sqlite3.Connection,
    *,
    manifest_signature: str,
    source_as_of_date: str,
    as_of_basis: str,
) -> bool:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='fac_metadata'"
    ).fetchone()
    if not table_exists:
        conn.executescript(_SCHEMA_SQL)
        _set_metadata(conn, "schema_version", SCHEMA_VERSION)
        _set_metadata(conn, "build_status", "building")
        _set_metadata(conn, "build_started_at_utc", _utc_now())
        _set_metadata(conn, "input_manifest_sha256", manifest_signature)
        _set_metadata(conn, "source_as_of_date", source_as_of_date)
        _set_metadata(conn, "source_as_of_basis", as_of_basis)
        _set_metadata(conn, "current_download_page", CURRENT_DOWNLOAD_PAGE)
        _set_metadata(conn, "historic_download_page", HISTORIC_DOWNLOAD_PAGE)
        _set_metadata(conn, "current_dictionary", CURRENT_DICTIONARY_URL)
        _set_metadata(conn, "historic_dictionary", HISTORIC_DICTIONARY_URL)
        conn.commit()
        return False

    metadata = dict(conn.execute("SELECT key, value FROM fac_metadata"))
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Staged FAC database uses a different schema; rerun with --restart")
    if metadata.get("build_status") not in {"building", "complete"}:
        raise RuntimeError("Staged FAC database is not resumable; rerun with --restart")
    if metadata.get("input_manifest_sha256") != manifest_signature:
        raise RuntimeError("FAC input set changed since staging began; rerun with --restart")
    if metadata.get("source_as_of_date") != source_as_of_date:
        raise RuntimeError("--source-as-of changed since staging began; rerun with --restart")
    return True


def _coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for era in ("current", "historic"):
        row = conn.execute(
            "SELECT MIN(audit_year), MAX(audit_year), COUNT(*) FROM fac_reports WHERE source_era=?",
            (era,),
        ).fetchone()
        result[era] = {
            "min_audit_year": row[0],
            "max_audit_year": row[1],
            "report_count": row[2],
        }
    for table in (
        "fac_awards",
        "fac_findings",
        "fac_findings_text",
        "fac_corrective_action_plans",
        "fac_additional_eins",
        "fac_additional_ueis",
        "fac_rejected_rows",
    ):
        result[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    result["orphan_detail_rows"] = {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} d "
            "WHERE NOT EXISTS (SELECT 1 FROM fac_reports r WHERE r.report_id=d.report_id)"
        ).fetchone()[0]
        for table in (
            "fac_awards",
            "fac_findings",
            "fac_findings_text",
            "fac_corrective_action_plans",
            "fac_additional_eins",
            "fac_additional_ueis",
        )
    }
    return result


def build_fac_database(
    input_paths: Sequence[Path | str],
    destination: Path | str = DEFAULT_FAC_DB_PATH,
    *,
    source_as_of_date: Optional[str] = None,
    replace: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    """Build a resumable FAC sidecar and atomically publish it on success."""

    destination_path = Path(destination).expanduser().resolve()
    main_db_path = Path(
        os.getenv("IRS_DB_PATH", APP_ROOT / "db" / "irs990.db")
    ).expanduser().resolve()
    same_as_main = destination_path == main_db_path
    if not same_as_main and destination_path.exists() and main_db_path.exists():
        try:
            same_as_main = os.path.samefile(destination_path, main_db_path)
        except OSError:
            same_as_main = False
    if same_as_main:
        raise RuntimeError(
            "FAC sidecar destination must be separate from the IRS source database."
        )
    staging_path = _staging_path(destination_path)
    if destination_path.exists() and not replace:
        raise FileExistsError(
            f"FAC sidecar already exists: {destination_path}. Use --replace to publish a refreshed build."
        )
    if restart and staging_path.exists():
        _remove_staging(staging_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    candidates = discover_sources(input_paths)
    fingerprints = fingerprint_sources(candidates)
    as_of_date, as_of_basis = _validate_as_of(source_as_of_date)
    signature = _manifest_signature(fingerprints)

    conn = _open_build_connection(staging_path)
    resumed = False
    imported_sources = 0
    skipped_sources = 0
    total_accepted = 0
    total_rejected = 0
    try:
        resumed = _initialize_or_validate_staging(
            conn,
            manifest_signature=signature,
            source_as_of_date=as_of_date,
            as_of_basis=as_of_basis,
        )
        for fingerprint in fingerprints:
            existed = conn.execute(
                "SELECT 1 FROM fac_source_files WHERE source_key=? "
                "UNION ALL SELECT 1 FROM fac_import_progress WHERE source_key=? LIMIT 1",
                (fingerprint.candidate.source_key, fingerprint.candidate.source_key),
            ).fetchone() is not None
            accepted, rejected = _import_candidate(conn, fingerprint, as_of_date)
            total_accepted += accepted
            total_rejected += rejected
            if existed:
                skipped_sources += 1
            else:
                imported_sources += 1

        conn.executescript(_INDEX_SQL)
        if conn.execute("SELECT COUNT(*) FROM fac_import_progress").fetchone()[0]:
            raise RuntimeError("FAC build finished with unexpected partial-import checkpoints")
        coverage = _coverage(conn)
        _set_metadata(conn, "coverage_json", json.dumps(coverage, sort_keys=True))
        _set_metadata(conn, "build_completed_at_utc", _utc_now())
        _set_metadata(conn, "build_status", "complete")
        conn.execute("ANALYZE")
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"FAC staging database failed integrity_check: {integrity}")
    except Exception:
        conn.close()
        raise
    else:
        conn.close()

    os.replace(staging_path, destination_path)
    summary = {
        "database": str(destination_path),
        "source_as_of_date": as_of_date,
        "source_as_of_basis": as_of_basis,
        "resumed": resumed,
        "sources_discovered": len(fingerprints),
        "sources_imported_this_run": imported_sources,
        "sources_resumed_from_staging": skipped_sources,
        "accepted_source_rows": total_accepted,
        "rejected_source_rows": total_rejected,
        "coverage": coverage,
    }
    return summary


def connect_fac_readonly(path: Path | str = DEFAULT_FAC_DB_PATH) -> sqlite3.Connection:
    """Open a completed FAC sidecar with SQLite-enforced read-only/query-only access."""

    db_path = Path(path).expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"FAC sidecar not found: {db_path}")
    conn = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro&immutable=1",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        metadata = dict(conn.execute("SELECT key, value FROM fac_metadata"))
    except Exception:
        conn.close()
        raise
    if metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("build_status") != "complete":
        conn.close()
        raise RuntimeError("FAC sidecar is incomplete or uses an unsupported schema")
    return conn


def _bool_value(value: Any) -> Optional[bool]:
    return None if value is None else bool(value)


def _rows_by_report(
    conn: sqlite3.Connection,
    table: str,
    report_ids: Sequence[str],
    *,
    limit_per_report: int,
) -> dict[str, list[dict[str, Any]]]:
    grouped = {report_id: [] for report_id in report_ids}
    if not report_ids:
        return grouped
    placeholders = ",".join("?" for _ in report_ids)
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE report_id IN ({placeholders}) ORDER BY rowid",
        list(report_ids),
    )
    excluded = {"record_key", "raw_json", "source_key", "legacy_award_id"}
    boolean_fields = {
        "is_direct", "is_passthrough_award", "is_major", "is_loan",
        "is_modified_opinion", "is_other_matters", "is_material_weakness",
        "is_significant_deficiency", "is_other_findings", "is_questioned_costs",
        "is_repeat_finding", "contains_chart_or_table",
    }
    for row in rows:
        report_id = row["report_id"]
        if len(grouped[report_id]) >= limit_per_report:
            continue
        item = {key: row[key] for key in row.keys() if key not in excluded}
        for key in boolean_fields.intersection(item):
            item[key] = _bool_value(item[key])
        grouped[report_id].append(item)
    return grouped


def lookup_fac_by_ein(
    ein: str,
    path: Path | str = DEFAULT_FAC_DB_PATH,
    *,
    max_reports: int = 5,
    max_findings: int = 250,
    max_awards: int = 500,
    max_text_reports: int = 2,
    max_text_rows: int = 50,
) -> dict[str, Any]:
    """Return a bounded FAC result shaped like the dashboard's live adapter."""

    clean_ein = _clean_ein(ein)
    if not clean_ein:
        return {"status": "blocked", "reason": "invalid_ein", "reports": [], "ueis": []}
    try:
        conn = connect_fac_readonly(path)
    except FileNotFoundError:
        return {"status": "not_configured", "reason": "missing_sidecar", "reports": [], "ueis": []}
    except (RuntimeError, sqlite3.Error):
        return {"status": "error", "error": "invalid_sidecar", "reports": [], "ueis": []}

    try:
        metadata = dict(conn.execute("SELECT key, value FROM fac_metadata"))
        rows = conn.execute(
            """
            SELECT r.*,
                   CASE WHEN r.auditee_ein=? THEN 'primary_ein' ELSE 'additional_ein' END AS ein_match
            FROM fac_reports r
            WHERE r.auditee_ein=?
               OR EXISTS (
                    SELECT 1 FROM fac_additional_eins ae
                    WHERE ae.report_id=r.report_id AND ae.additional_ein=?
               )
            ORDER BY COALESCE(r.fy_end_date, printf('%04d', r.audit_year)) DESC,
                     COALESCE(r.fac_accepted_date, r.submitted_date, '') DESC,
                     COALESCE(r.resubmission_version, 0) DESC,
                     r.report_id DESC
            LIMIT ?
            """,
            (clean_ein, clean_ein, clean_ein, max(max_reports * 20, max_reports)),
        ).fetchall()
        selected: list[sqlite3.Row] = []
        periods: set[tuple[Any, ...]] = set()
        for row in rows:
            if row["fy_start_date"] or row["fy_end_date"]:
                period = (
                    row["auditee_ein"], row["auditee_uei"],
                    row["fy_start_date"], row["fy_end_date"],
                )
            else:
                period = ("report", row["report_id"])
            if period in periods:
                continue
            periods.add(period)
            selected.append(row)
            if len(selected) >= max_reports:
                break

        report_ids = [row["report_id"] for row in selected]
        awards = _rows_by_report(conn, "fac_awards", report_ids, limit_per_report=max_awards)
        findings = _rows_by_report(conn, "fac_findings", report_ids, limit_per_report=max_findings)
        text_ids = [report_id for report_id in report_ids if findings.get(report_id)][:max_text_reports]
        finding_text = _rows_by_report(conn, "fac_findings_text", text_ids, limit_per_report=max_text_rows)
        caps = _rows_by_report(conn, "fac_corrective_action_plans", text_ids, limit_per_report=max_text_rows)

        reports: list[dict[str, Any]] = []
        for row in selected:
            report_id = row["report_id"]
            general_fields = (
                "report_id", "audit_year", "fy_start_date", "fy_end_date",
                "submitted_date", "fac_accepted_date", "auditee_ein", "auditee_uei",
                "auditee_name", "entity_type", "audit_type", "total_amount_expended",
                "gaap_results", "is_going_concern_included",
                "is_internal_control_material_weakness_disclosed",
                "is_internal_control_deficiency_disclosed",
                "is_material_noncompliance_disclosed", "is_low_risk_auditee",
                "agencies_with_prior_findings", "auditor_firm_name", "is_public",
                "resubmission_version", "resubmission_status", "source_era",
            )
            general = {field: row[field] for field in general_fields}
            for field in (
                "is_going_concern_included",
                "is_internal_control_material_weakness_disclosed",
                "is_internal_control_deficiency_disclosed",
                "is_material_noncompliance_disclosed",
                "is_low_risk_auditee",
                "is_public",
            ):
                general[field] = _bool_value(general[field])
            reports.append(
                {
                    "report_id": report_id,
                    "ein_match": row["ein_match"],
                    "general": general,
                    "findings": findings.get(report_id, []),
                    "findings_text": finding_text.get(report_id, []),
                    "corrective_action_plans": caps.get(report_id, []),
                    "federal_awards": awards.get(report_id, []),
                    "findings_status": "ok",
                    "findings_text_status": "ok" if report_id in text_ids else "not_requested",
                    "corrective_action_plans_status": "ok" if report_id in text_ids else "not_requested",
                    "federal_awards_status": "ok",
                }
            )

        ueis = sorted(
            {
                row["auditee_uei"]
                for row in selected
                if row["ein_match"] == "primary_ein" and row["auditee_uei"]
            }
        )[:3]
        coverage = json.loads(metadata.get("coverage_json", "{}"))
        return {
            "status": "ok" if reports else "no_match",
            "reports": reports,
            "ueis": ueis,
            "report_count": len(reports),
            "source": "offline_fac_sidecar",
            "source_as_of_date": metadata.get("source_as_of_date"),
            "source_as_of_basis": metadata.get("source_as_of_basis"),
            "coverage": coverage,
        }
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return {"status": "error", "error": "invalid_sidecar", "reports": [], "ueis": []}
    finally:
        conn.close()


__all__ = [
    "DEFAULT_DOWNLOAD_MAX_BYTES",
    "MIN_CLI_DOWNLOAD_MAX_BYTES",
    "CURRENT_DOWNLOAD_URLS",
    "CURRENT_DOWNLOAD_PAGE",
    "DEFAULT_FAC_DB_PATH",
    "HISTORIC_DOWNLOAD_URL",
    "HISTORIC_SHA1_URL",
    "build_fac_database",
    "connect_fac_readonly",
    "discover_sources",
    "download_official_fac_file",
    "download_official_fac_sources",
    "fac_download_ssl_context",
    "lookup_fac_by_ein",
]
