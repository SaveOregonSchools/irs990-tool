"""Portable identity and snapshot metadata for risk-network source databases.

The database and revision UUIDs live inside the main IRS database, so an exact
checkpointed copy retains its identity on another path, filesystem, or OS.
Filesystem identity and modification time remain useful as same-process build
guards, but they are deliberately excluded from this portable contract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote


IDENTITY_TABLE = "app_dataset_identity"
IDENTITY_NAME = "risk_network_source"
IDENTITY_VERSION = 1
SOURCE_IDENTITY_SCHEME = "portable_v1"

PORTABLE_META_KEYS = frozenset({
    "source_identity_scheme",
    "source_identity_version",
    "source_database_id",
    "source_risk_revision",
    "source_header_sha256",
    "source_snapshot_id",
})
PORTABLE_REQUIRED_META_KEYS = PORTABLE_META_KEYS | {"source_file_size"}

IDENTITY_DDL = f"""
CREATE TABLE IF NOT EXISTS {IDENTITY_TABLE} (
  identity_name TEXT PRIMARY KEY,
  identity_version INTEGER NOT NULL,
  database_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  adopted_legacy_lineage_id TEXT,
  adopted_legacy_file_size INTEGER,
  adopted_legacy_file_mtime_ns INTEGER,
  adopted_at_revision_id TEXT
) WITHOUT ROWID
"""


class RiskSourceIdentityError(RuntimeError):
    """Raised when portable identity metadata is absent, partial, or invalid."""


@dataclass(frozen=True)
class RiskSourceIdentity:
    identity_version: int
    database_id: str
    revision_id: str
    created_at: str
    updated_at: str
    adopted_legacy_lineage_id: Optional[str] = None
    adopted_legacy_file_size: Optional[int] = None
    adopted_legacy_file_mtime_ns: Optional[int] = None
    adopted_at_revision_id: Optional[str] = None


@dataclass(frozen=True)
class PortableSourceStamp:
    identity_scheme: str
    identity_version: int
    database_id: str
    revision_id: str
    file_size: int
    header_sha256: str
    snapshot_id: str

    def metadata(self) -> dict[str, str]:
        return {
            "source_identity_scheme": self.identity_scheme,
            "source_identity_version": str(self.identity_version),
            "source_database_id": self.database_id,
            "source_risk_revision": self.revision_id,
            "source_file_size": str(self.file_size),
            "source_header_sha256": self.header_sha256,
            "source_snapshot_id": self.snapshot_id,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_uuid(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    try:
        canonical = str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError):
        raise RiskSourceIdentityError(f"Invalid {label}: {value!r}") from None
    if text != canonical:
        raise RiskSourceIdentityError(
            f"Invalid {label}: UUID must use canonical lowercase form"
        )
    return canonical


def _optional_int(value: Any, label: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RiskSourceIdentityError(f"Invalid {label}: {value!r}") from None
    if parsed < 0:
        raise RiskSourceIdentityError(f"Invalid {label}: {value!r}")
    return parsed


def identity_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (IDENTITY_TABLE,),
    ).fetchone() is not None


def read_risk_source_identity(
    conn: sqlite3.Connection, *, required: bool = False
) -> Optional[RiskSourceIdentity]:
    if not identity_table_exists(conn):
        if required:
            raise RiskSourceIdentityError(
                "Main database lacks portable risk-source identity metadata"
            )
        return None
    try:
        row = conn.execute(
            f"""SELECT identity_version,database_id,revision_id,created_at,updated_at,
                       adopted_legacy_lineage_id,adopted_legacy_file_size,
                       adopted_legacy_file_mtime_ns,adopted_at_revision_id
                  FROM {IDENTITY_TABLE} WHERE identity_name=?""",
            (IDENTITY_NAME,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RiskSourceIdentityError(
            f"Portable risk-source identity table is incompatible: {exc}"
        ) from exc
    if row is None:
        if required:
            raise RiskSourceIdentityError(
                "Main database has no risk-network source identity row"
            )
        return None
    try:
        version = int(row[0])
    except (TypeError, ValueError):
        raise RiskSourceIdentityError("Invalid risk-source identity version") from None
    if version != IDENTITY_VERSION:
        raise RiskSourceIdentityError(
            f"Unsupported risk-source identity version: {version}"
        )
    return RiskSourceIdentity(
        identity_version=version,
        database_id=_canonical_uuid(row[1], "database_id"),
        revision_id=_canonical_uuid(row[2], "revision_id"),
        created_at=str(row[3] or ""),
        updated_at=str(row[4] or ""),
        adopted_legacy_lineage_id=(
            str(row[5]).strip() if row[5] not in (None, "") else None
        ),
        adopted_legacy_file_size=_optional_int(
            row[6], "adopted legacy file size"
        ),
        adopted_legacy_file_mtime_ns=_optional_int(
            row[7], "adopted legacy file mtime"
        ),
        adopted_at_revision_id=(
            _canonical_uuid(row[8], "adopted_at_revision_id")
            if row[8] not in (None, "")
            else None
        ),
    )


def ensure_risk_source_identity(
    conn: sqlite3.Connection,
    *,
    adopted_legacy_lineage_id: Optional[str] = None,
    adopted_legacy_file_size: Optional[int] = None,
    adopted_legacy_file_mtime_ns: Optional[int] = None,
) -> RiskSourceIdentity:
    """Create the singleton identity when absent without committing the caller."""

    adoption_values = (
        adopted_legacy_lineage_id,
        adopted_legacy_file_size,
        adopted_legacy_file_mtime_ns,
    )
    if any(value is not None for value in adoption_values) and not all(
        value is not None for value in adoption_values
    ):
        raise RiskSourceIdentityError(
            "Legacy adoption requires lineage, file size, and file mtime together"
        )
    conn.execute(IDENTITY_DDL)
    existing = read_risk_source_identity(conn)
    if existing is not None:
        existing_values = (
            existing.adopted_legacy_lineage_id,
            existing.adopted_legacy_file_size,
            existing.adopted_legacy_file_mtime_ns,
        )
        if any(value is not None for value in adoption_values) and adoption_values != existing_values:
            raise RiskSourceIdentityError(
                "Existing risk-source identity has different legacy adoption metadata"
            )
        return existing

    now = utc_now()
    database_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    adopted_at_revision_id = (
        revision_id if all(value is not None for value in adoption_values) else None
    )
    conn.execute(
        f"""INSERT INTO {IDENTITY_TABLE}(
               identity_name,identity_version,database_id,revision_id,
               created_at,updated_at,adopted_legacy_lineage_id,
               adopted_legacy_file_size,adopted_legacy_file_mtime_ns,
               adopted_at_revision_id
             ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            IDENTITY_NAME,
            IDENTITY_VERSION,
            database_id,
            revision_id,
            now,
            now,
            adopted_legacy_lineage_id,
            adopted_legacy_file_size,
            adopted_legacy_file_mtime_ns,
            adopted_at_revision_id,
        ),
    )
    identity = read_risk_source_identity(conn, required=True)
    assert identity is not None
    return identity


def rotate_risk_source_revision(conn: sqlite3.Connection) -> RiskSourceIdentity:
    """Invalidate prior network snapshots before a supported source mutation."""

    ensure_risk_source_identity(conn)
    conn.execute(
        f"""UPDATE {IDENTITY_TABLE}
               SET revision_id=?, updated_at=?
             WHERE identity_name=?""",
        (str(uuid.uuid4()), utc_now(), IDENTITY_NAME),
    )
    identity = read_risk_source_identity(conn, required=True)
    assert identity is not None
    return identity


def sqlite_header_sha256(source_path: Path) -> tuple[int, str]:
    resolved = source_path.expanduser().resolve()
    stat = resolved.stat()
    if stat.st_size < 100:
        raise RiskSourceIdentityError("SQLite source file is smaller than its header")
    with resolved.open("rb") as handle:
        header = handle.read(100)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
        raise RiskSourceIdentityError("Source is not a valid SQLite 3 database file")
    return int(stat.st_size), hashlib.sha256(header).hexdigest()


def portable_source_stamp(
    source_path: Path, identity: RiskSourceIdentity
) -> PortableSourceStamp:
    file_size, header_sha256 = sqlite_header_sha256(source_path)
    payload = {
        "source_identity_scheme": SOURCE_IDENTITY_SCHEME,
        "source_identity_version": identity.identity_version,
        "source_database_id": identity.database_id,
        "source_risk_revision": identity.revision_id,
        "source_file_size": file_size,
        "source_header_sha256": header_sha256,
    }
    snapshot_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return PortableSourceStamp(
        identity_scheme=SOURCE_IDENTITY_SCHEME,
        identity_version=identity.identity_version,
        database_id=identity.database_id,
        revision_id=identity.revision_id,
        file_size=file_size,
        header_sha256=header_sha256,
        snapshot_id=snapshot_id,
    )


def parse_portable_metadata(
    meta: Mapping[str, Any],
) -> Optional[PortableSourceStamp]:
    """Return None for a wholly legacy sidecar; reject partial portable data."""

    present = PORTABLE_META_KEYS.intersection(meta)
    if not present:
        return None
    missing = sorted(PORTABLE_REQUIRED_META_KEYS.difference(meta))
    if missing:
        raise RiskSourceIdentityError(
            "Portable risk-network metadata is incomplete; missing: "
            + ", ".join(missing)
        )
    scheme = str(meta.get("source_identity_scheme") or "")
    if scheme != SOURCE_IDENTITY_SCHEME:
        raise RiskSourceIdentityError(
            f"Unsupported risk-network source identity scheme: {scheme!r}"
        )
    try:
        version = int(meta.get("source_identity_version"))
        file_size = int(meta.get("source_file_size"))
    except (TypeError, ValueError):
        raise RiskSourceIdentityError(
            "Portable risk-network metadata has invalid numeric values"
        ) from None
    if version != IDENTITY_VERSION or file_size < 100:
        raise RiskSourceIdentityError(
            "Portable risk-network metadata has unsupported identity values"
        )
    header_sha256 = str(meta.get("source_header_sha256") or "").lower()
    snapshot_id = str(meta.get("source_snapshot_id") or "").lower()
    if (
        len(header_sha256) != 64
        or len(snapshot_id) != 64
        or any(ch not in "0123456789abcdef" for ch in header_sha256 + snapshot_id)
    ):
        raise RiskSourceIdentityError(
            "Portable risk-network metadata has invalid SHA-256 values"
        )
    stamp = PortableSourceStamp(
        identity_scheme=scheme,
        identity_version=version,
        database_id=_canonical_uuid(meta.get("source_database_id"), "source_database_id"),
        revision_id=_canonical_uuid(meta.get("source_risk_revision"), "source_risk_revision"),
        file_size=file_size,
        header_sha256=header_sha256,
        snapshot_id=snapshot_id,
    )
    expected = portable_source_stamp_from_values(
        stamp.database_id,
        stamp.revision_id,
        stamp.file_size,
        stamp.header_sha256,
    )
    if stamp.snapshot_id != expected.snapshot_id:
        raise RiskSourceIdentityError(
            "Portable risk-network snapshot ID does not match its metadata"
        )
    return stamp


def portable_source_stamp_from_values(
    database_id: str,
    revision_id: str,
    file_size: int,
    header_sha256: str,
) -> PortableSourceStamp:
    database_id = _canonical_uuid(database_id, "database_id")
    revision_id = _canonical_uuid(revision_id, "revision_id")
    payload = {
        "source_identity_scheme": SOURCE_IDENTITY_SCHEME,
        "source_identity_version": IDENTITY_VERSION,
        "source_database_id": database_id,
        "source_risk_revision": revision_id,
        "source_file_size": int(file_size),
        "source_header_sha256": str(header_sha256).lower(),
    }
    snapshot_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return PortableSourceStamp(
        identity_scheme=SOURCE_IDENTITY_SCHEME,
        identity_version=IDENTITY_VERSION,
        database_id=database_id,
        revision_id=revision_id,
        file_size=int(file_size),
        header_sha256=str(header_sha256).lower(),
        snapshot_id=snapshot_id,
    )


def _readonly_identity_connection(source_path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(source_path.resolve().as_posix(), safe="/:") + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def validate_portable_source(
    meta: Mapping[str, Any], source_path: Path
) -> PortableSourceStamp:
    expected = parse_portable_metadata(meta)
    if expected is None:
        raise RiskSourceIdentityError(
            "Risk-network sidecar does not contain portable source metadata"
        )
    with closing(_readonly_identity_connection(source_path)) as conn:
        identity = read_risk_source_identity(conn, required=True)
    assert identity is not None
    current = portable_source_stamp(source_path, identity)
    if current != expected:
        raise RiskSourceIdentityError(
            "Risk-network sidecar is stale for the current portable source snapshot"
        )
    return current
