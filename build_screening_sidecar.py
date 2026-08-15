#!/usr/bin/env python3
"""Build a local, replaceable public-screening data sidecar.

The builder intentionally keeps screening data out of the main Form 990
database.  It can download the current public snapshots from IRS, OFAC, and
HHS-OIG, resume interrupted downloads, and rebuild the SQLite sidecar through a
temporary file followed by an atomic replacement.

No source used by this module requires an account or API key.  A name in this
database is a screening lead, not a confirmed identity match.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import os
import re
import sqlite3
import ssl
import sys
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, Mapping, Optional, Sequence, TextIO, Tuple
from urllib.parse import parse_qsl, urlsplit


IRS_SOURCE_PAGE = "https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-bulk-data-downloads"
OFAC_SOURCE_PAGE = "https://ofac.treasury.gov/sanctions-list-service"
HHS_SOURCE_PAGE = "https://oig.hhs.gov/exclusions/leie-database-supplement-downloads/"

PUBLIC_FILES: Mapping[str, Tuple[str, str]] = {
    "irs_pub78": (
        "https://apps.irs.gov/pub/epostcard/data-download-pub78.zip",
        "irs_pub78.zip",
    ),
    "irs_auto_revocation": (
        "https://apps.irs.gov/pub/epostcard/data-download-revocation.zip",
        "irs_auto_revocation.zip",
    ),
    "hhs_leie": (
        "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv",
        "hhs_leie.csv",
    ),
    "ofac_sdn_primary": (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV",
        "ofac_sdn_primary.csv",
    ),
    "ofac_sdn_alias": (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ALT.CSV",
        "ofac_sdn_alias.csv",
    ),
    "ofac_sdn_address": (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADD.CSV",
        "ofac_sdn_address.csv",
    ),
    "ofac_sdn_comments": (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_COMMENTS.CSV",
        "ofac_sdn_comments.csv",
    ),
    "ofac_consolidated_primary": (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_PRIM.CSV",
        "ofac_consolidated_primary.csv",
    ),
    "ofac_consolidated_alias": (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_ALT.CSV",
        "ofac_consolidated_alias.csv",
    ),
    "ofac_consolidated_address": (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_ADD.CSV",
        "ofac_consolidated_address.csv",
    ),
    "ofac_consolidated_comments": (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_COMMENTS.CSV",
        "ofac_consolidated_comments.csv",
    ),
}

GROUP_FILE_KEYS: Mapping[str, Tuple[str, ...]] = {
    "irs": ("irs_pub78", "irs_auto_revocation"),
    "hhs": ("hhs_leie",),
    "ofac": (
        "ofac_sdn_primary",
        "ofac_sdn_alias",
        "ofac_sdn_address",
        "ofac_sdn_comments",
        "ofac_consolidated_primary",
        "ofac_consolidated_alias",
        "ofac_consolidated_address",
        "ofac_consolidated_comments",
    ),
}

USER_AGENT = "irs990-tool/1.0 public-screening-sidecar"
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
SQL_BATCH_SIZE = 20_000

OFAC_PUBLISHED_HOST = (
    "wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com"
)
OFAC_PUBLISHED_NAMES: Mapping[str, str] = {
    "ofac_sdn_primary": "SDN.CSV",
    "ofac_sdn_alias": "ALT.CSV",
    "ofac_sdn_address": "ADD.CSV",
    "ofac_sdn_comments": "SDN_COMMENTS.CSV",
    "ofac_consolidated_primary": "CONS_PRIM.CSV",
    "ofac_consolidated_alias": "CONS_ALT.CSV",
    "ofac_consolidated_address": "CONS_ADD.CSV",
    "ofac_consolidated_comments": "CONS_COMMENTS.CSV",
}
OFAC_SIGNED_QUERY_KEYS = {
    "X-Amz-Algorithm",
    "X-Amz-Credential",
    "X-Amz-Date",
    "X-Amz-Expires",
    "X-Amz-SignedHeaders",
    "X-Amz-Signature",
    "X-Amz-Security-Token",
    "response-content-disposition",
    "response-content-type",
}


@dataclass(frozen=True)
class SourceFile:
    path: Path
    source_url: str
    source_date: Optional[str] = None
    retrieved_at: Optional[str] = None
    sha256: Optional[str] = None


@dataclass(frozen=True)
class OfacSeries:
    primary: SourceFile
    aliases: SourceFile
    addresses: SourceFile
    comments: SourceFile


@dataclass(frozen=True)
class ScreeningInputs:
    irs_pub78: Optional[SourceFile] = None
    irs_auto_revocation: Optional[SourceFile] = None
    hhs_leie: Optional[SourceFile] = None
    ofac_sdn: Optional[OfacSeries] = None
    ofac_consolidated: Optional[OfacSeries] = None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_name(value: object) -> str:
    """Return a conservative deterministic comparison key.

    Legal suffixes and diacritics are deliberately retained.  The key is only
    for candidate generation; a normalized-name collision is never proof that
    two people or organizations are the same.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def normalize_address(*values: object) -> str:
    return normalize_name(" ".join(str(v or "") for v in values))


def normalize_ein(value: object) -> Optional[str]:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 9 else None


def normalize_date(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text or text in {"00000000", "00/00/0000", "-0-"}:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _clean(value: object) -> str:
    # Legacy OFAC flat files retain a DOS Ctrl-Z end-of-file marker.
    text = str(value or "").strip().strip("\ufeff").strip("\x1a").strip()
    return "" if text == "-0-" else text


def _hash_values(*values: object) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = str(value or "").encode("utf-8", "surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso_date_from_http(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _verified_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # Python 3.14 enables OpenSSL strict verification.  Several otherwise valid
    # federal certificate chains currently fail that additional constraint.
    # Keep certificate and hostname verification enabled while relaxing only
    # the new strict-chain flag, matching browsers on those official hosts.
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    return context


def default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=_verified_ssl_context()))


def _metadata_path(path: Path) -> Path:
    return path.with_name(path.name + ".metadata.json")


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _first_nonblank_csv_row(path: Path) -> Optional[list[str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as source:
        return next(
            (row for row in csv.reader(source) if any(_clean(cell) for cell in row)),
            None,
        )


def _validate_download(path: Path, key: str) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"Downloaded file is empty: {path}")
    if key.startswith("irs_"):
        if not zipfile.is_zipfile(path):
            raise RuntimeError(f"IRS download is not a valid ZIP file: {path}")
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise RuntimeError(f"IRS ZIP failed CRC validation at {bad_member}: {path}")
    elif key == "hhs_leie":
        with path.open("rb") as source:
            header = source.readline(4096).upper()
        if b"BUSNAME" not in header or b"EXCLDATE" not in header:
            raise RuntimeError(f"HHS-OIG download has an unexpected header: {path}")
    elif key.startswith("ofac_"):
        first = _first_nonblank_csv_row(path)
        if first is None:
            raise RuntimeError(f"OFAC component is empty: {path}")
        if key.endswith("_primary"):
            valid = len(first) >= 12 and _clean(first[0]).isdigit() and bool(_clean(first[1]))
        elif key.endswith("_alias"):
            valid = (
                len(first) >= 5
                and _clean(first[0]).isdigit()
                and _clean(first[1]).isdigit()
                and bool(_clean(first[3]))
            )
        elif key.endswith("_address"):
            valid = len(first) >= 6 and _clean(first[0]).isdigit() and _clean(first[1]).isdigit()
        elif key.endswith("_comments"):
            valid = len(first) >= 2 and _clean(first[0]).isdigit()
        else:
            valid = False
        if not valid:
            raise RuntimeError(f"OFAC component has an unexpected schema: {path}")


def validate_ofac_series(series: OfacSeries) -> None:
    """Validate all four legacy CSV relations and their parent keys."""

    primary_ids: set[str] = set()
    for row in _iter_ofac_rows(series.primary):
        if not any(_clean(cell) for cell in row):
            continue
        if len(row) < 12 or not _clean(row[0]).isdigit() or not _clean(row[1]):
            raise RuntimeError(f"Invalid OFAC primary row in {series.primary.path}")
        entity_id = _clean(row[0])
        if entity_id in primary_ids:
            raise RuntimeError(f"Duplicate OFAC primary entity key {entity_id}")
        primary_ids.add(entity_id)
    if not primary_ids:
        raise RuntimeError(f"OFAC primary file contains no records: {series.primary.path}")

    child_specs = (
        (series.aliases, 5, 1, "alias"),
        (series.addresses, 6, 1, "address"),
        (series.comments, 2, None, "comments"),
    )
    for source, minimum_columns, child_key_index, label in child_specs:
        seen: set[Tuple[str, str]] = set()
        for row in _iter_ofac_rows(source):
            if not any(_clean(cell) for cell in row):
                continue
            if len(row) < minimum_columns:
                raise RuntimeError(f"Invalid OFAC {label} row in {source.path}")
            entity_id = _clean(row[0])
            if entity_id not in primary_ids:
                raise RuntimeError(
                    f"OFAC {label} row references missing entity key {entity_id}"
                )
            child_id = (
                _clean(row[child_key_index])
                if child_key_index is not None
                else entity_id
            )
            relation_key = (entity_id, child_id)
            if not child_id or relation_key in seen:
                raise RuntimeError(
                    f"Duplicate or blank OFAC {label} key {relation_key}"
                )
            seen.add(relation_key)


def _validate_final_url(key: str, final_url: str) -> None:
    expected = urlsplit(PUBLIC_FILES[key][0])
    actual = urlsplit(final_url)
    try:
        actual_port = actual.port or 443
    except ValueError as exc:
        raise RuntimeError(
            f"Official download redirected to an unapproved target: {final_url}"
        ) from exc
    direct_match = (
        actual.scheme.lower() == "https"
        and actual.hostname == expected.hostname
        and actual_port == (expected.port or 443)
        and actual.path == expected.path
        and not actual.query
        and not actual.fragment
        and not actual.username
        and not actual.password
    )
    if direct_match:
        return

    published_name = OFAC_PUBLISHED_NAMES.get(key)
    query_keys = {name for name, _ in parse_qsl(actual.query, keep_blank_values=True)}
    ofac_path_match = bool(
        published_name
        and re.fullmatch(
            rf"/Published/[0-9a-fA-F-]{{36}}/\d{{4}}-\d{{2}}-\d{{2}}/"
            rf"[0-9a-fA-F-]{{36}}/{re.escape(published_name)}",
            actual.path,
        )
    )
    ofac_signed_match = (
        actual.scheme.lower() == "https"
        and actual.hostname == OFAC_PUBLISHED_HOST
        and actual_port == 443
        and ofac_path_match
        and bool(actual.query)
        and query_keys <= OFAC_SIGNED_QUERY_KEYS
        and "X-Amz-Signature" in query_keys
        and "X-Amz-Credential" in query_keys
        and not actual.fragment
        and not actual.username
        and not actual.password
    )
    if not ofac_signed_match:
        raise RuntimeError(
            f"Official download redirected to an unapproved target: {final_url}"
        )


def _partial_metadata_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".part.metadata.json")


def _load_partial_metadata(path: Path, source_url: str) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("source_url") != source_url:
        return {}
    return value


def _clear_partial(partial: Path, partial_metadata: Path) -> None:
    if partial.exists():
        partial.unlink()
    if partial_metadata.exists():
        partial_metadata.unlink()


def download_public_file(
    key: str,
    destination: Path,
    *,
    refresh: bool = False,
    opener: Optional[urllib.request.OpenerDirector] = None,
    timeout: float = 60.0,
) -> SourceFile:
    """Download one hard-coded official file with safe resume and atomic rename."""

    if key not in PUBLIC_FILES:
        raise ValueError(f"Unsupported public source key: {key}")
    url, _ = PUBLIC_FILES[key]
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = _metadata_path(destination)

    if destination.exists() and not refresh:
        _validate_download(destination, key)
        return source_file_from_path(destination, url)

    partial = destination.with_name(destination.name + ".part")
    partial_metadata = _partial_metadata_path(destination)
    http = opener or default_opener()

    final_url = url
    last_modified: Optional[str] = None
    etag: Optional[str] = None
    completed = False
    for attempt in range(2):
        current_size = partial.stat().st_size if partial.exists() else 0
        partial_state = _load_partial_metadata(partial_metadata, url)
        validator = ""
        if current_size:
            partial_etag = str(partial_state.get("etag") or "")
            partial_modified = str(partial_state.get("last_modified") or "")
            if partial_etag and not partial_etag.startswith("W/"):
                validator = partial_etag
            elif partial_modified:
                validator = partial_modified
            if not validator:
                _clear_partial(partial, partial_metadata)
                current_size = 0
                partial_state = {}

        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if current_size:
            headers["Range"] = f"bytes={current_size}-"
            headers["If-Range"] = validator
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = http.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and current_size and attempt == 0:
                exc.close()
                _clear_partial(partial, partial_metadata)
                continue
            raise

        restart = False
        with response:
            final_url = response.geturl()
            _validate_final_url(key, final_url)
            status = int(getattr(response, "status", response.getcode()))
            resumed_total: Optional[int] = None
            resumed_length: Optional[int] = None
            if current_size and status == 206:
                content_range = str(response.headers.get("Content-Range") or "")
                match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
                response_etag = str(response.headers.get("ETag") or "")
                response_modified = str(response.headers.get("Last-Modified") or "")
                partial_etag = str(partial_state.get("etag") or "")
                used_strong_etag = bool(
                    partial_etag and not partial_etag.startswith("W/")
                )
                validator_changed = (
                    response_etag != partial_etag
                    if used_strong_etag
                    else response_modified
                    != str(partial_state.get("last_modified") or "")
                )
                if match and match.group(3) != "*":
                    range_start = int(match.group(1))
                    range_end = int(match.group(2))
                    resumed_total = int(match.group(3))
                    resumed_length = range_end - range_start + 1
                if (
                    not match
                    or int(match.group(1)) != current_size
                    or match.group(3) == "*"
                    or resumed_length is None
                    or resumed_length <= 0
                    or resumed_total is None
                    or int(match.group(2)) + 1 != resumed_total
                    or validator_changed
                ):
                    restart = True
            elif current_size and status == 200:
                # If-Range correctly caused a full response because the remote
                # object changed. Replace, never append to, the partial file.
                current_size = 0
            elif not current_size and status != 200:
                raise RuntimeError(f"Unexpected HTTP {status} for {url}")
            elif current_size and status not in (200, 206):
                raise RuntimeError(f"Unexpected HTTP {status} while resuming {url}")

            if restart:
                _clear_partial(partial, partial_metadata)
                if attempt == 0:
                    continue
                raise RuntimeError(f"Server returned an invalid resumed response for {url}")

            append = bool(current_size and status == 206)
            content_length = response.headers.get("Content-Length")
            expected_additional = (
                int(content_length)
                if content_length and content_length.isdigit()
                else None
            )
            if append:
                if (
                    expected_additional is not None
                    and expected_additional != resumed_length
                ):
                    raise RuntimeError(
                        f"Resumed Content-Length disagrees with Content-Range for {url}"
                    )
                expected_additional = resumed_length
                expected_total = resumed_total
            else:
                expected_total = expected_additional
            if expected_total is not None and expected_total > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(
                    f"Refusing download larger than {MAX_DOWNLOAD_BYTES:,} bytes: {url}"
                )

            last_modified = response.headers.get("Last-Modified")
            etag = response.headers.get("ETag")
            _atomic_write_json(
                partial_metadata,
                {
                    "source_key": key,
                    "source_url": url,
                    "final_url": final_url,
                    "etag": etag,
                    "last_modified": last_modified,
                    "started_at": utc_now(),
                },
            )
            mode = "ab" if append else "wb"
            written = current_size
            with partial.open(mode) as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError(
                            f"Download exceeded {MAX_DOWNLOAD_BYTES:,} bytes: {url}"
                        )
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if expected_total is not None and written != expected_total:
                raise RuntimeError(
                    f"Incomplete download for {url}: expected {expected_total}, received {written}"
                )
            completed = True
            break

    if not completed:
        raise RuntimeError(f"Could not safely download {url}")

    _validate_download(partial, key)
    digest = sha256_file(partial)
    os.replace(partial, destination)
    metadata = {
        "source_key": key,
        "source_url": url,
        "final_url": final_url,
        "retrieved_at": utc_now(),
        "last_modified": last_modified,
        "source_date": _iso_date_from_http(last_modified),
        "etag": etag,
        "size_bytes": destination.stat().st_size,
        "sha256": digest,
    }
    _atomic_write_json(metadata_path, metadata)
    if partial_metadata.exists():
        partial_metadata.unlink()
    return source_file_from_path(destination, url)


def source_file_from_path(
    path: Path,
    source_url: str,
    *,
    source_date: Optional[str] = None,
) -> SourceFile:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    metadata: Dict[str, object] = {}
    metadata_path = _metadata_path(path)
    if metadata_path.exists():
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("source_url") == source_url:
                metadata = raw
        except (OSError, ValueError):
            metadata = {}
    final_source_date = source_date or str(metadata.get("source_date") or "") or None
    if final_source_date:
        date.fromisoformat(final_source_date)
    return SourceFile(
        path=path,
        source_url=source_url,
        source_date=final_source_date,
        retrieved_at=str(metadata.get("retrieved_at") or "") or None,
        sha256=str(metadata.get("sha256") or "") or None,
    )


@contextmanager
def open_source_text(path: Path) -> Iterator[TextIO]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            candidates = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and not info.filename.startswith("__MACOSX/")
                and Path(info.filename).suffix.lower() in {".txt", ".csv"}
            ]
            if not candidates:
                raise RuntimeError(f"No delimited text member found in {path}")
            member = max(candidates, key=lambda info: info.file_size)
            with archive.open(member, "r") as raw:
                with io.TextIOWrapper(
                    raw,
                    encoding="utf-8-sig",
                    errors="replace",
                    newline="",
                ) as text:
                    yield text
    else:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as text:
            yield text


def _header_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _header_map(header: Sequence[str], fields: Mapping[str, Sequence[str]]) -> Dict[str, int]:
    positions = {_header_key(value): index for index, value in enumerate(header)}
    result: Dict[str, int] = {}
    for logical, candidates in fields.items():
        match = next((positions[_header_key(candidate)] for candidate in candidates if _header_key(candidate) in positions), None)
        if match is None:
            raise RuntimeError(f"Required column {logical!r} not found in header: {header}")
        result[logical] = match
    return result


def _cell(row: Sequence[str], columns: Mapping[str, int], key: str) -> str:
    index = columns[key]
    return _clean(row[index]) if index < len(row) else ""


def _pipe_data_rows(
    text: TextIO,
    fields: Mapping[str, Sequence[str]],
    headerless_columns: Mapping[str, int],
    *,
    label: str,
) -> Tuple[Mapping[str, int], Iterator[list[str]]]:
    """Accept documented headers and the IRS production headerless layout."""

    rows = csv.reader(text, delimiter="|")
    first = next((row for row in rows if any(_clean(cell) for cell in row)), None)
    if first is None:
        raise RuntimeError(f"Empty {label} file")
    if normalize_ein(first[0] if first else None):
        return headerless_columns, itertools.chain((first,), rows)
    return _header_map(first, fields), rows


SCHEMA_SQL = """
PRAGMA foreign_keys=ON;

CREATE TABLE screening_dataset (
  dataset_key TEXT PRIMARY KEY,
  publisher TEXT NOT NULL,
  title TEXT NOT NULL,
  source_page_url TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_date TEXT,
  retrieved_at TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  record_count INTEGER NOT NULL DEFAULT 0,
  complete_snapshot INTEGER NOT NULL CHECK (complete_snapshot IN (0,1)),
  components_json TEXT NOT NULL,
  access_note TEXT NOT NULL
);

CREATE TABLE screening_entity (
  dataset_key TEXT NOT NULL REFERENCES screening_dataset(dataset_key) ON DELETE CASCADE,
  source_record_id TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  primary_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  ein TEXT,
  status TEXT NOT NULL,
  status_date TEXT,
  reinstatement_date TEXT,
  list_name TEXT,
  program_tags TEXT,
  deductibility_code TEXT,
  subsection_code TEXT,
  general_category TEXT,
  specialty TEXT,
  exclusion_type TEXT,
  npi TEXT,
  upin TEXT,
  birth_date TEXT,
  waiver_date TEXT,
  waiver_state TEXT,
  address_line TEXT,
  city TEXT,
  region TEXT,
  postal_code TEXT,
  country TEXT,
  remarks TEXT,
  PRIMARY KEY (dataset_key, source_record_id)
) WITHOUT ROWID;

CREATE TABLE screening_alias (
  dataset_key TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  source_alias_id TEXT NOT NULL,
  alias_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  alias_type TEXT NOT NULL,
  alias_quality TEXT,
  remarks TEXT,
  PRIMARY KEY (dataset_key, source_record_id, source_alias_id),
  FOREIGN KEY (dataset_key, source_record_id)
    REFERENCES screening_entity(dataset_key, source_record_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE screening_address (
  dataset_key TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  source_address_id TEXT NOT NULL,
  address_line TEXT,
  city TEXT,
  region TEXT,
  postal_code TEXT,
  country TEXT,
  normalized_address TEXT NOT NULL,
  remarks TEXT,
  PRIMARY KEY (dataset_key, source_record_id, source_address_id),
  FOREIGN KEY (dataset_key, source_record_id)
    REFERENCES screening_entity(dataset_key, source_record_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE screening_identifier (
  dataset_key TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  identifier_type TEXT NOT NULL,
  identifier_value TEXT NOT NULL,
  PRIMARY KEY (dataset_key, source_record_id, identifier_type, identifier_value),
  FOREIGN KEY (dataset_key, source_record_id)
    REFERENCES screening_entity(dataset_key, source_record_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE VIEW screening_names_v1 AS
SELECT
  e.dataset_key,
  e.source_record_id,
  e.primary_name AS display_name,
  e.normalized_name,
  'primary' AS name_role,
  NULL AS alias_type,
  e.entity_type,
  e.ein,
  e.status,
  d.source_date,
  d.source_url
FROM screening_entity e
JOIN screening_dataset d USING (dataset_key)
UNION ALL
SELECT
  a.dataset_key,
  a.source_record_id,
  a.alias_name,
  a.normalized_name,
  'alias',
  a.alias_type,
  e.entity_type,
  e.ein,
  e.status,
  d.source_date,
  d.source_url
FROM screening_alias a
JOIN screening_entity e USING (dataset_key, source_record_id)
JOIN screening_dataset d USING (dataset_key);
"""

INDEX_SQL = """
CREATE INDEX idx_screening_entity_ein ON screening_entity(ein) WHERE ein IS NOT NULL;
CREATE INDEX idx_screening_entity_name ON screening_entity(normalized_name);
CREATE INDEX idx_screening_entity_status ON screening_entity(dataset_key, status);
CREATE INDEX idx_screening_alias_name ON screening_alias(normalized_name);
CREATE INDEX idx_screening_address_norm ON screening_address(normalized_address);
CREATE INDEX idx_screening_identifier_value ON screening_identifier(identifier_type, identifier_value);
"""


ENTITY_INSERT = """
INSERT OR IGNORE INTO screening_entity (
  dataset_key, source_record_id, entity_type, primary_name, normalized_name,
  ein, status, status_date, reinstatement_date, list_name, program_tags,
  deductibility_code, subsection_code, general_category, specialty,
  exclusion_type, npi, upin, birth_date, waiver_date, waiver_state,
  address_line, city, region, postal_code, country, remarks
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

ALIAS_INSERT = """
INSERT OR IGNORE INTO screening_alias (
  dataset_key, source_record_id, source_alias_id, alias_name, normalized_name,
  alias_type, alias_quality, remarks
) VALUES (?,?,?,?,?,?,?,?)
"""

ADDRESS_INSERT = """
INSERT OR IGNORE INTO screening_address (
  dataset_key, source_record_id, source_address_id, address_line, city, region,
  postal_code, country, normalized_address, remarks
) VALUES (?,?,?,?,?,?,?,?,?,?)
"""


class BatchWriter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.entities: list[Tuple[object, ...]] = []
        self.aliases: list[Tuple[object, ...]] = []
        self.addresses: list[Tuple[object, ...]] = []
        self.identifiers: list[Tuple[object, ...]] = []

    def add_entity(self, row: Tuple[object, ...]) -> None:
        self.entities.append(row)
        if len(self.entities) >= SQL_BATCH_SIZE:
            self.flush_entities()

    def add_alias(self, row: Tuple[object, ...]) -> None:
        self.aliases.append(row)
        if len(self.aliases) >= SQL_BATCH_SIZE:
            self.flush_entities()
            self.flush_aliases()

    def add_address(self, row: Tuple[object, ...]) -> None:
        if row[8]:
            self.addresses.append(row)
        if len(self.addresses) >= SQL_BATCH_SIZE:
            self.flush_entities()
            self.flush_addresses()

    def add_identifier(self, row: Tuple[object, ...]) -> None:
        if row[3]:
            self.identifiers.append(row)
        if len(self.identifiers) >= SQL_BATCH_SIZE:
            self.flush_entities()
            self.flush_identifiers()

    def flush_entities(self) -> None:
        if self.entities:
            self.conn.executemany(ENTITY_INSERT, self.entities)
            self.entities.clear()

    def flush_aliases(self) -> None:
        if self.aliases:
            self.conn.executemany(ALIAS_INSERT, self.aliases)
            self.aliases.clear()

    def flush_addresses(self) -> None:
        if self.addresses:
            self.conn.executemany(ADDRESS_INSERT, self.addresses)
            self.addresses.clear()

    def flush_identifiers(self) -> None:
        if self.identifiers:
            self.conn.executemany(
                "INSERT OR IGNORE INTO screening_identifier VALUES (?,?,?,?)",
                self.identifiers,
            )
            self.identifiers.clear()

    def flush(self) -> None:
        # Entities must exist before child rows are written.
        self.flush_entities()
        self.flush_aliases()
        self.flush_addresses()
        self.flush_identifiers()


def _entity_row(
    dataset_key: str,
    source_record_id: str,
    entity_type: str,
    primary_name: str,
    *,
    ein: Optional[str] = None,
    status: str,
    status_date: Optional[str] = None,
    reinstatement_date: Optional[str] = None,
    list_name: str = "",
    program_tags: str = "",
    deductibility_code: str = "",
    subsection_code: str = "",
    general_category: str = "",
    specialty: str = "",
    exclusion_type: str = "",
    npi: str = "",
    upin: str = "",
    birth_date: Optional[str] = None,
    waiver_date: Optional[str] = None,
    waiver_state: str = "",
    address_line: str = "",
    city: str = "",
    region: str = "",
    postal_code: str = "",
    country: str = "",
    remarks: str = "",
) -> Tuple[object, ...]:
    return (
        dataset_key,
        source_record_id,
        entity_type,
        primary_name,
        normalize_name(primary_name),
        ein,
        status,
        status_date,
        reinstatement_date,
        list_name,
        program_tags,
        deductibility_code,
        subsection_code,
        general_category,
        specialty,
        exclusion_type,
        npi,
        upin,
        birth_date,
        waiver_date,
        waiver_state,
        address_line,
        city,
        region,
        postal_code,
        country,
        remarks,
    )


def _dataset_components(files: Iterable[SourceFile]) -> Tuple[str, str, str, str]:
    components = []
    source_dates = []
    retrieved = []
    combined = hashlib.sha256()
    for source in files:
        digest = source.sha256 or sha256_file(source.path)
        component = {
            "file_name": source.path.name,
            "source_url": source.source_url,
            "source_date": source.source_date,
            "retrieved_at": source.retrieved_at,
            "sha256": digest,
            "size_bytes": source.path.stat().st_size,
        }
        components.append(component)
        combined.update(source.source_url.encode("utf-8"))
        combined.update(bytes.fromhex(digest))
        if source.source_date:
            source_dates.append(source.source_date)
        if source.retrieved_at:
            retrieved.append(source.retrieved_at)
    return (
        max(source_dates) if source_dates else "",
        max(retrieved) if retrieved else utc_now(),
        combined.hexdigest(),
        json.dumps(components, sort_keys=True, separators=(",", ":")),
    )


def _add_dataset(
    conn: sqlite3.Connection,
    *,
    key: str,
    publisher: str,
    title: str,
    page_url: str,
    files: Sequence[SourceFile],
    complete_snapshot: bool,
    access_note: str,
) -> None:
    source_date, retrieved_at, digest, components = _dataset_components(files)
    conn.execute(
        """
        INSERT INTO screening_dataset (
          dataset_key, publisher, title, source_page_url, source_url,
          source_date, retrieved_at, content_sha256, complete_snapshot,
          components_json, access_note
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            key,
            publisher,
            title,
            page_url,
            files[0].source_url,
            source_date or None,
            retrieved_at,
            digest,
            int(complete_snapshot),
            components,
            access_note,
        ),
    )


def _finish_dataset(conn: sqlite3.Connection, key: str) -> int:
    count = int(conn.execute("SELECT COUNT(*) FROM screening_entity WHERE dataset_key=?", (key,)).fetchone()[0])
    conn.execute("UPDATE screening_dataset SET record_count=? WHERE dataset_key=?", (count, key))
    return count


def import_irs_pub78(conn: sqlite3.Connection, source: SourceFile) -> int:
    key = "irs_pub78"
    _add_dataset(
        conn,
        key=key,
        publisher="Internal Revenue Service",
        title="Publication 78 eligible organizations",
        page_url=IRS_SOURCE_PAGE,
        files=[source],
        complete_snapshot=True,
        access_note="Public monthly complete ZIP; no account or API key required.",
    )
    fields = {
        "ein": ("EIN", "TIN", "Taxpayer Identification Number"),
        "name": ("NAME", "Organization Name"),
        "city": ("CITY",),
        "region": ("STATE",),
        "country": ("COUNTRY", "Foreign Country"),
        "deductibility": ("DEDUCTIBILITY_CODE", "Deductibility Code"),
    }
    writer = BatchWriter(conn)
    with open_source_text(source.path) as text:
        columns, rows = _pipe_data_rows(
            text,
            fields,
            {
                "ein": 0,
                "name": 1,
                "city": 2,
                "region": 3,
                "country": 4,
                "deductibility": 5,
            },
            label="IRS Pub. 78",
        )
        for row in rows:
            ein = normalize_ein(_cell(row, columns, "ein"))
            name = _cell(row, columns, "name")
            if not ein or not name:
                continue
            city = _cell(row, columns, "city")
            region = _cell(row, columns, "region")
            country = _cell(row, columns, "country")
            deductibility = _cell(row, columns, "deductibility")
            record_id = _hash_values(ein, name, city, region, country, deductibility)
            writer.add_entity(
                _entity_row(
                    key,
                    record_id,
                    "organization",
                    name,
                    ein=ein,
                    status="eligible_for_deductible_contributions",
                    deductibility_code=deductibility,
                    city=city,
                    region=region,
                    country=country,
                )
            )
    writer.flush()
    return _finish_dataset(conn, key)


def import_irs_auto_revocation(conn: sqlite3.Connection, source: SourceFile) -> int:
    key = "irs_auto_revocation"
    _add_dataset(
        conn,
        key=key,
        publisher="Internal Revenue Service",
        title="Automatic revocation of exemption list",
        page_url=IRS_SOURCE_PAGE,
        files=[source],
        complete_snapshot=True,
        access_note="Public monthly complete ZIP; no account or API key required. Reinstatement date, when present, changes the current status represented here.",
    )
    fields = {
        "ein": ("EIN", "TIN", "Taxpayer Identification Number"),
        "name": ("NAME", "Organization Name"),
        "sort_name": ("NAME2", "Sort Name"),
        "address": ("ADDRESS",),
        "city": ("CITY",),
        "region": ("STATE",),
        "postal": ("ZIP", "ZIP CODE", "Zip Code"),
        "country": ("COUNTRY",),
        "subsection": ("EXEMPTION_TYPE", "SUB_SECTION_CODE", "Sub Section Code"),
        "revocation": ("REVOCATION_DATE", "Revocation Date"),
        "posting": ("REVOCATION_POSTING_DATE", "Revocation Posting Date"),
        "reinstatement": ("EXEMPTION_REINSTATEMENT_DATE", "Exemption Reinstatement Date"),
    }
    writer = BatchWriter(conn)
    with open_source_text(source.path) as text:
        columns, rows = _pipe_data_rows(
            text,
            fields,
            {
                "ein": 0,
                "name": 1,
                "sort_name": 2,
                "address": 3,
                "city": 4,
                "region": 5,
                "postal": 6,
                "country": 7,
                "subsection": 8,
                "revocation": 9,
                "posting": 10,
                "reinstatement": 11,
            },
            label="IRS auto-revocation",
        )
        for row in rows:
            ein = normalize_ein(_cell(row, columns, "ein"))
            name = _cell(row, columns, "name")
            if not ein or not name:
                continue
            sort_name = _cell(row, columns, "sort_name")
            address = _cell(row, columns, "address")
            city = _cell(row, columns, "city")
            region = _cell(row, columns, "region")
            postal = _cell(row, columns, "postal")
            country = _cell(row, columns, "country")
            subsection = _cell(row, columns, "subsection")
            revoked = normalize_date(_cell(row, columns, "revocation"))
            posted = normalize_date(_cell(row, columns, "posting"))
            reinstated = normalize_date(_cell(row, columns, "reinstatement"))
            record_id = _hash_values(ein, name, revoked, posted, reinstated)
            writer.add_entity(
                _entity_row(
                    key,
                    record_id,
                    "organization",
                    name,
                    ein=ein,
                    status="reinstated_after_auto_revocation" if reinstated else "automatically_revoked",
                    status_date=revoked,
                    reinstatement_date=reinstated,
                    subsection_code=subsection,
                    address_line=address,
                    city=city,
                    region=region,
                    postal_code=postal,
                    country=country,
                    remarks=f"IRS posting date: {posted}" if posted else "",
                )
            )
            if sort_name and normalize_name(sort_name) != normalize_name(name):
                writer.add_alias(
                    (key, record_id, "irs_sort_name", sort_name, normalize_name(sort_name), "sort_name", "source_reported", "")
                )
    writer.flush()
    return _finish_dataset(conn, key)


def import_hhs_leie(conn: sqlite3.Connection, source: SourceFile) -> int:
    key = "hhs_leie"
    _add_dataset(
        conn,
        key=key,
        publisher="HHS Office of Inspector General",
        title="List of Excluded Individuals/Entities (active exclusions)",
        page_url=HHS_SOURCE_PAGE,
        files=[source],
        complete_snapshot=True,
        access_note="Public monthly active-only CSV; no account or API key required. OIG requires online verification with SSN or EIN before treating a name lead as confirmed.",
    )
    required = (
        "LASTNAME",
        "FIRSTNAME",
        "MIDNAME",
        "BUSNAME",
        "GENERAL",
        "SPECIALTY",
        "UPIN",
        "NPI",
        "DOB",
        "ADDRESS",
        "CITY",
        "STATE",
        "ZIP",
        "EXCLTYPE",
        "EXCLDATE",
        "REINDATE",
        "WAIVERDATE",
        "WVRSTATE",
    )
    writer = BatchWriter(conn)
    with open_source_text(source.path) as text:
        rows = csv.DictReader(text)
        if not rows.fieldnames:
            raise RuntimeError(f"Empty HHS-OIG LEIE file: {source.path}")
        columns = {_header_key(value): value for value in rows.fieldnames}
        missing = [name for name in required if _header_key(name) not in columns]
        if missing:
            raise RuntimeError(f"HHS-OIG LEIE file is missing columns: {', '.join(missing)}")

        def value(row: Mapping[str, str], name: str) -> str:
            return _clean(row.get(columns[_header_key(name)], ""))

        for row in rows:
            business = value(row, "BUSNAME")
            last = value(row, "LASTNAME")
            first = value(row, "FIRSTNAME")
            middle = value(row, "MIDNAME")
            name = business or " ".join(part for part in (first, middle, last) if part)
            if not name:
                continue
            general = value(row, "GENERAL")
            specialty = value(row, "SPECIALTY")
            upin = value(row, "UPIN")
            npi_raw = value(row, "NPI")
            npi = npi_raw if npi_raw and set(npi_raw) != {"0"} else ""
            dob = normalize_date(value(row, "DOB"))
            address = value(row, "ADDRESS")
            city = value(row, "CITY")
            region = value(row, "STATE")
            postal = value(row, "ZIP")
            exclusion_type = value(row, "EXCLTYPE")
            excluded = normalize_date(value(row, "EXCLDATE"))
            reinstated = normalize_date(value(row, "REINDATE"))
            waiver = normalize_date(value(row, "WAIVERDATE"))
            waiver_state = value(row, "WVRSTATE")
            record_id = _hash_values(
                name,
                general,
                specialty,
                npi,
                upin,
                dob,
                address,
                city,
                region,
                postal,
                exclusion_type,
                excluded,
            )
            writer.add_entity(
                _entity_row(
                    key,
                    record_id,
                    "organization" if business else "individual",
                    name,
                    status="active_exclusion",
                    status_date=excluded,
                    reinstatement_date=reinstated,
                    list_name="LEIE",
                    general_category=general,
                    specialty=specialty,
                    exclusion_type=exclusion_type,
                    npi=npi,
                    upin=upin,
                    birth_date=dob,
                    waiver_date=waiver,
                    waiver_state=waiver_state,
                    address_line=address,
                    city=city,
                    region=region,
                    postal_code=postal,
                    country="US",
                )
            )
            if not business and last and first:
                source_order = f"{last}, {first}{(' ' + middle) if middle else ''}"
                writer.add_alias(
                    (
                        key,
                        record_id,
                        "derived_source_order",
                        source_order,
                        normalize_name(source_order),
                        "source_field_order",
                        "deterministically_derived",
                        "Derived only from the source's LASTNAME/FIRSTNAME/MIDNAME fields.",
                    )
                )
            writer.add_address(
                (
                    key,
                    record_id,
                    "primary",
                    address,
                    city,
                    region,
                    postal,
                    "US",
                    normalize_address(address, city, region, postal, "US"),
                    "",
                )
            )
            if npi:
                writer.add_identifier((key, record_id, "NPI", npi))
            if upin:
                writer.add_identifier((key, record_id, "UPIN", upin))
    writer.flush()
    return _finish_dataset(conn, key)


def _iter_ofac_rows(source: SourceFile) -> Iterator[list[str]]:
    with open_source_text(source.path) as text:
        yield from csv.reader(text)


def import_ofac_series(
    conn: sqlite3.Connection,
    series: OfacSeries,
    *,
    key: str,
    title: str,
    list_name: str,
) -> int:
    validate_ofac_series(series)
    files = [series.primary, series.aliases, series.addresses, series.comments]
    _add_dataset(
        conn,
        key=key,
        publisher="U.S. Department of the Treasury, Office of Foreign Assets Control",
        title=title,
        page_url=OFAC_SOURCE_PAGE,
        files=files,
        complete_snapshot=True,
        access_note="Public current list files; no account or API key required. All four relational CSV components are imported.",
    )
    comments: Dict[str, str] = {}
    for row in _iter_ofac_rows(series.comments):
        if len(row) >= 2:
            comments[_clean(row[0])] = _clean(row[1])

    writer = BatchWriter(conn)
    entity_ids = set()
    for row in _iter_ofac_rows(series.primary):
        if len(row) < 12:
            continue
        entity_number = _clean(row[0])
        name = _clean(row[1])
        if not entity_number or not name:
            continue
        entity_ids.add(entity_number)
        entity_type = _clean(row[2]).lower() or "unknown"
        program = _clean(row[3])
        # The comments file is a direct spill-over of an overlength remarks
        # field and may start in the middle of a word, so do not invent a
        # separator between the two official fragments.
        remarks = _clean(row[11]) + comments.get(entity_number, "")
        writer.add_entity(
            _entity_row(
                key,
                entity_number,
                entity_type,
                name,
                status="sanctions_listed",
                list_name=list_name,
                program_tags=program,
                remarks=remarks,
            )
        )
    writer.flush_entities()

    for row in _iter_ofac_rows(series.aliases):
        if len(row) < 5:
            continue
        entity_number = _clean(row[0])
        alias_number = _clean(row[1])
        alias_type = _clean(row[2]) or "alias"
        alias_name = _clean(row[3])
        if entity_number not in entity_ids or not alias_name:
            continue
        writer.add_alias(
            (
                key,
                entity_number,
                alias_number or _hash_values(alias_type, alias_name),
                alias_name,
                normalize_name(alias_name),
                alias_type,
                None,
                _clean(row[4]),
            )
        )

    for row in _iter_ofac_rows(series.addresses):
        if len(row) < 6:
            continue
        entity_number = _clean(row[0])
        address_number = _clean(row[1])
        address = _clean(row[2])
        city = _clean(row[3])
        country = _clean(row[4])
        remarks = _clean(row[5])
        if entity_number not in entity_ids:
            continue
        normalized = normalize_address(address, city, country)
        if not normalized:
            continue
        writer.add_address(
            (
                key,
                entity_number,
                address_number or _hash_values(address, city, country, remarks),
                address,
                city,
                "",
                "",
                country,
                normalized,
                remarks,
            )
        )
    writer.flush()
    return _finish_dataset(conn, key)


def _build_into(conn: sqlite3.Connection, inputs: ScreeningInputs) -> Dict[str, int]:
    conn.executescript(SCHEMA_SQL)
    counts: Dict[str, int] = {}
    if inputs.irs_pub78:
        counts["irs_pub78"] = import_irs_pub78(conn, inputs.irs_pub78)
    if inputs.irs_auto_revocation:
        counts["irs_auto_revocation"] = import_irs_auto_revocation(conn, inputs.irs_auto_revocation)
    if inputs.hhs_leie:
        counts["hhs_leie"] = import_hhs_leie(conn, inputs.hhs_leie)
    if inputs.ofac_sdn:
        counts["ofac_sdn"] = import_ofac_series(
            conn,
            inputs.ofac_sdn,
            key="ofac_sdn",
            title="Specially Designated Nationals and Blocked Persons list",
            list_name="SDN",
        )
    if inputs.ofac_consolidated:
        counts["ofac_consolidated"] = import_ofac_series(
            conn,
            inputs.ofac_consolidated,
            key="ofac_consolidated",
            title="Consolidated non-SDN sanctions lists",
            list_name="Non-SDN Consolidated",
        )
    if not counts:
        raise RuntimeError("No screening sources were selected")
    conn.executescript(INDEX_SQL)
    conn.execute("ANALYZE")
    conn.execute("PRAGMA optimize")
    return counts


def configured_main_db_path() -> Path:
    configured = os.getenv("IRS_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parent / "db" / "irs990.db").resolve()


def assert_safe_sidecar_destination(db_path: Path) -> Path:
    target = db_path.expanduser().resolve()
    main_db = configured_main_db_path()
    if os.path.normcase(str(target)) == os.path.normcase(str(main_db)):
        raise RuntimeError(
            "Screening sidecar destination must not equal IRS_DB_PATH/main Form 990 database"
        )
    return target


def build_screening_sidecar(db_path: Path, inputs: ScreeningInputs) -> Dict[str, int]:
    """Atomically rebuild ``db_path`` from complete source snapshots."""

    target = assert_safe_sidecar_destination(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(temporary)
        conn.execute("PRAGMA foreign_keys=ON")
        # This is a disposable staging database. Disabling its rollback journal
        # avoids writing every new page twice; the old production sidecar is
        # untouched until the new file has committed and passed quick_check.
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-200000")
        conn.execute("BEGIN IMMEDIATE")
        counts = _build_into(conn, inputs)
        conn.commit()
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {check}")
        conn.close()
        conn = None
        os.replace(temporary, target)
        return counts
    except Exception:
        if conn is not None:
            conn.rollback()
            conn.close()
        raise
    finally:
        if temporary.exists():
            temporary.unlink()


def _source_dates(values: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        key, separator, raw_date = value.partition("=")
        if not separator or key not in {"irs_pub78", "irs_auto_revocation", "hhs_leie", "ofac_sdn", "ofac_consolidated"}:
            raise ValueError(f"Invalid --source-date value: {value}")
        date.fromisoformat(raw_date)
        result[key] = raw_date
    return result


def _source(cache: Path, file_key: str, date_override: Optional[str] = None) -> SourceFile:
    url, filename = PUBLIC_FILES[file_key]
    return source_file_from_path(cache / filename, url, source_date=date_override)


def _group_manifest_path(cache: Path, group: str) -> Path:
    return cache / f"{group}.current.json"


def _active_group_directory(cache: Path, group: str) -> Path:
    manifest_path = _group_manifest_path(cache, group)
    if not manifest_path.exists():
        return cache
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid active-cache manifest: {manifest_path}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("group") != group
        or not isinstance(manifest.get("relative_directory"), str)
    ):
        raise RuntimeError(f"Invalid active-cache manifest: {manifest_path}")
    relative = Path(str(manifest["relative_directory"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Unsafe active-cache path in {manifest_path}")
    versions = (cache / "versions").resolve()
    active = (cache / relative).resolve()
    if not active.is_relative_to(versions) or active.parent != versions:
        raise RuntimeError(f"Active-cache path escapes versions directory: {active}")
    if not active.name.startswith(f"{group}-") or not active.is_dir():
        raise RuntimeError(f"Active-cache version is missing or invalid: {active}")
    return active


def _sources_for_group(directory: Path, group: str) -> Dict[str, SourceFile]:
    return {
        key: _source(directory, key)
        for key in GROUP_FILE_KEYS[group]
    }


def _validate_irs_source_content(source: SourceFile, key: str) -> None:
    if key == "irs_pub78":
        fields = {
            "ein": ("EIN", "TIN", "Taxpayer Identification Number"),
            "name": ("NAME", "Organization Name"),
            "city": ("CITY",),
            "region": ("STATE",),
            "country": ("COUNTRY", "Foreign Country"),
            "deductibility": ("DEDUCTIBILITY_CODE", "Deductibility Code"),
        }
        headerless = {
            "ein": 0,
            "name": 1,
            "city": 2,
            "region": 3,
            "country": 4,
            "deductibility": 5,
        }
    else:
        fields = {
            "ein": ("EIN", "TIN", "Taxpayer Identification Number"),
            "name": ("NAME", "Organization Name"),
            "sort_name": ("NAME2", "Sort Name"),
            "address": ("ADDRESS",),
            "city": ("CITY",),
            "region": ("STATE",),
            "postal": ("ZIP", "ZIP CODE", "Zip Code"),
            "country": ("COUNTRY",),
            "subsection": (
                "EXEMPTION_TYPE",
                "SUB_SECTION_CODE",
                "Sub Section Code",
            ),
            "revocation": ("REVOCATION_DATE", "Revocation Date"),
            "posting": (
                "REVOCATION_POSTING_DATE",
                "Revocation Posting Date",
            ),
            "reinstatement": (
                "EXEMPTION_REINSTATEMENT_DATE",
                "Exemption Reinstatement Date",
            ),
        }
        headerless = {
            "ein": 0,
            "name": 1,
            "sort_name": 2,
            "address": 3,
            "city": 4,
            "region": 5,
            "postal": 6,
            "country": 7,
            "subsection": 8,
            "revocation": 9,
            "posting": 10,
            "reinstatement": 11,
        }
    with open_source_text(source.path) as text:
        columns, rows = _pipe_data_rows(
            text,
            fields,
            headerless,
            label=key,
        )
        first = next((row for row in rows if any(_clean(cell) for cell in row)), None)
        if (
            first is None
            or not normalize_ein(_cell(first, columns, "ein"))
            or not _cell(first, columns, "name")
        ):
            raise RuntimeError(f"IRS source has no valid first record: {source.path}")


def _ofac_publication_identity(source: SourceFile) -> Optional[str]:
    metadata_path = _metadata_path(source.path)
    if not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        final_url = urlsplit(str(metadata.get("final_url") or ""))
    except (OSError, ValueError):
        return None
    if final_url.hostname != OFAC_PUBLISHED_HOST:
        return None
    match = re.fullmatch(
        r"(/Published/[0-9a-fA-F-]{36}/\d{4}-\d{2}-\d{2}/"
        r"[0-9a-fA-F-]{36})/[^/]+",
        final_url.path,
    )
    return match.group(1) if match else None


def _validate_group_sources(group: str, sources: Mapping[str, SourceFile]) -> None:
    expected = set(GROUP_FILE_KEYS[group])
    if set(sources) != expected:
        raise RuntimeError(f"Incomplete {group} source group")
    for key, source in sources.items():
        _validate_download(source.path, key)
    if group == "irs":
        _validate_irs_source_content(sources["irs_pub78"], "irs_pub78")
        _validate_irs_source_content(
            sources["irs_auto_revocation"], "irs_auto_revocation"
        )
    elif group == "ofac":
        for label, keys in (
            (
                "SDN",
                (
                    "ofac_sdn_primary",
                    "ofac_sdn_alias",
                    "ofac_sdn_address",
                    "ofac_sdn_comments",
                ),
            ),
            (
                "consolidated",
                (
                    "ofac_consolidated_primary",
                    "ofac_consolidated_alias",
                    "ofac_consolidated_address",
                    "ofac_consolidated_comments",
                ),
            ),
        ):
            series_dates = {
                sources[key].source_date
                for key in keys
                if sources[key].source_date
            }
            if len(series_dates) > 1:
                raise RuntimeError(
                    f"OFAC {label} components have inconsistent source dates: "
                    f"{sorted(series_dates)}"
                )
            publication_ids = [
                _ofac_publication_identity(sources[key]) for key in keys
            ]
            known_publication_ids = {
                identity for identity in publication_ids if identity
            }
            if known_publication_ids and (
                len(known_publication_ids) != 1
                or any(identity is None for identity in publication_ids)
            ):
                raise RuntimeError(
                    f"OFAC {label} components do not share one publication version"
                )
        validate_ofac_series(
            OfacSeries(
                primary=sources["ofac_sdn_primary"],
                aliases=sources["ofac_sdn_alias"],
                addresses=sources["ofac_sdn_address"],
                comments=sources["ofac_sdn_comments"],
            )
        )
        validate_ofac_series(
            OfacSeries(
                primary=sources["ofac_consolidated_primary"],
                aliases=sources["ofac_consolidated_alias"],
                addresses=sources["ofac_consolidated_address"],
                comments=sources["ofac_consolidated_comments"],
            )
        )


def inputs_from_cache(cache: Path, groups: Sequence[str], date_overrides: Mapping[str, str]) -> ScreeningInputs:
    selected = set(groups)
    kwargs: Dict[str, object] = {}
    if "irs" in selected:
        irs_cache = _active_group_directory(cache, "irs")
        kwargs["irs_pub78"] = _source(irs_cache, "irs_pub78", date_overrides.get("irs_pub78"))
        kwargs["irs_auto_revocation"] = _source(
            irs_cache,
            "irs_auto_revocation",
            date_overrides.get("irs_auto_revocation"),
        )
    if "hhs" in selected:
        hhs_cache = _active_group_directory(cache, "hhs")
        kwargs["hhs_leie"] = _source(
            hhs_cache, "hhs_leie", date_overrides.get("hhs_leie")
        )
    if "ofac" in selected:
        ofac_cache = _active_group_directory(cache, "ofac")
        sdn_date = date_overrides.get("ofac_sdn")
        consolidated_date = date_overrides.get("ofac_consolidated")
        kwargs["ofac_sdn"] = OfacSeries(
            primary=_source(ofac_cache, "ofac_sdn_primary", sdn_date),
            aliases=_source(ofac_cache, "ofac_sdn_alias", sdn_date),
            addresses=_source(ofac_cache, "ofac_sdn_address", sdn_date),
            comments=_source(ofac_cache, "ofac_sdn_comments", sdn_date),
        )
        kwargs["ofac_consolidated"] = OfacSeries(
            primary=_source(
                ofac_cache, "ofac_consolidated_primary", consolidated_date
            ),
            aliases=_source(
                ofac_cache, "ofac_consolidated_alias", consolidated_date
            ),
            addresses=_source(
                ofac_cache, "ofac_consolidated_address", consolidated_date
            ),
            comments=_source(
                ofac_cache, "ofac_consolidated_comments", consolidated_date
            ),
        )
    return ScreeningInputs(**kwargs)


def download_groups(cache: Path, groups: Sequence[str], refresh: bool) -> Dict[str, SourceFile]:
    downloaded: Dict[str, SourceFile] = {}
    cache.mkdir(parents=True, exist_ok=True)
    opener = default_opener()
    for group in groups:
        manifest_path = _group_manifest_path(cache, group)
        if manifest_path.exists() and not refresh:
            active_sources = _sources_for_group(
                _active_group_directory(cache, group),
                group,
            )
            _validate_group_sources(group, active_sources)
            downloaded.update(active_sources)
            continue

        staging = (cache / "staging" / group).resolve()
        staging.mkdir(parents=True, exist_ok=True)
        group_sources: Dict[str, SourceFile] = {}
        for key in GROUP_FILE_KEYS[group]:
            _, filename = PUBLIC_FILES[key]
            destination = staging / filename
            # Completed files in an unpublished staging group may be from an
            # earlier source generation. Re-fetch them together; retain only
            # validator-bound partials so interrupted transfers can resume.
            if destination.exists():
                destination.unlink()
            metadata = _metadata_path(destination)
            if metadata.exists():
                metadata.unlink()
            print(f"[download] {key} -> staged {destination}", flush=True)
            group_sources[key] = download_public_file(
                key,
                destination,
                refresh=False,
                opener=opener,
            )
        _validate_group_sources(group, group_sources)

        versions = (cache / "versions").resolve()
        versions.mkdir(parents=True, exist_ok=True)
        version = versions / f"{group}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
        os.replace(staging, version)
        relative_version = version.relative_to(cache.resolve()).as_posix()
        _atomic_write_json(
            manifest_path,
            {
                "group": group,
                "relative_directory": relative_version,
                "published_at": utc_now(),
                "file_keys": list(GROUP_FILE_KEYS[group]),
            },
        )
        active_sources = _sources_for_group(version, group)
        _validate_group_sources(group, active_sources)
        downloaded.update(active_sources)
    return downloaded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the IRS/OFAC/HHS public-screening SQLite sidecar.")
    parser.add_argument(
        "--db",
        default=os.getenv("IRS_SCREENING_DB_PATH", str(Path("db") / "screening_data.db")),
        help="Output SQLite sidecar (default: IRS_SCREENING_DB_PATH or db/screening_data.db).",
    )
    parser.add_argument(
        "--cache-dir",
        default=os.getenv("IRS_SCREENING_CACHE_DIR", str(Path("downloads") / "screening")),
        help="Ignored local download cache (default: downloads/screening).",
    )
    parser.add_argument(
        "--source",
        choices=tuple(GROUP_FILE_KEYS),
        action="append",
        help="Source group to include; repeat as needed. Defaults to irs, hhs, and ofac.",
    )
    parser.add_argument("--download", action="store_true", help="Download missing official files before building.")
    parser.add_argument(
        "--refresh-downloads",
        action="store_true",
        help="Re-download selected official snapshots, replacing cached copies only after validation.",
    )
    parser.add_argument("--download-only", action="store_true", help="Download/validate sources without building SQLite.")
    parser.add_argument(
        "--source-date",
        action="append",
        default=[],
        metavar="SOURCE=YYYY-MM-DD",
        help="Override missing release date metadata for a manually supplied cache file.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    groups = tuple(dict.fromkeys(args.source or ("irs", "hhs", "ofac")))
    cache = Path(args.cache_dir).resolve()
    try:
        assert_safe_sidecar_destination(Path(args.db))
        date_overrides = _source_dates(args.source_date)
        if args.download or args.refresh_downloads or args.download_only:
            download_groups(cache, groups, bool(args.refresh_downloads))
        if args.download_only:
            print("[done] downloads validated; sidecar not changed", flush=True)
            return 0
        inputs = inputs_from_cache(cache, groups, date_overrides)
        counts = build_screening_sidecar(Path(args.db), inputs)
        print(f"[done] sidecar={Path(args.db).resolve()}", flush=True)
        for key, count in counts.items():
            print(f"[rows] {key}={count:,}", flush=True)
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
