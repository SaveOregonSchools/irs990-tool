#!/usr/bin/env python3
"""Safely add portable source lineage to an existing risk-network pair.

The migration is deliberately metadata-only.  It first adopts the existing
physical-file lineage into the main database, then publishes the corresponding
portable stamp in ``risk_network_build_meta``.  A receipt is durably written
before either database is changed so an interrupted first phase can be resumed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat as stat_module
import sys
import tempfile
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote

from build_risk_network import source_lineage_id
from risk_source_identity import (
    PortableSourceStamp,
    RiskSourceIdentity,
    RiskSourceIdentityError,
    ensure_risk_source_identity,
    parse_portable_metadata,
    portable_source_stamp,
    read_risk_source_identity,
    rotate_risk_source_revision,
    sqlite_header_sha256,
    validate_portable_source,
)


EXPECTED_SIDECAR_SCHEMA_VERSION = "1"
COUNT_TABLES = (
    "risk_network_edge",
    "risk_network_filing_state",
    "risk_network_node_stats",
    "risk_network_source_status",
)


class PortabilityMigrationError(RuntimeError):
    """Raised when a migration precondition or postcondition fails."""


@dataclass(frozen=True)
class LegacySourceStamp:
    lineage_id: str
    file_size: int
    file_mtime_ns: int

    def metadata(self) -> dict[str, str]:
        return {
            "source_lineage_id": self.lineage_id,
            "source_file_size": str(self.file_size),
            "source_file_mtime_ns": str(self.file_mtime_ns),
        }


@dataclass(frozen=True)
class LocalSidecarGuard:
    lineage_id: str
    file_size: int
    file_mtime_ns: int
    header_sha256: str


@dataclass(frozen=True)
class MigrationPreflight:
    action: str
    source_path: Path
    sidecar_path: Path
    source_journal_mode: str
    sidecar_journal_mode: str
    sidecar_meta: dict[str, str]
    counts: dict[str, int]
    sidecar_guard: LocalSidecarGuard
    legacy_stamp: Optional[LegacySourceStamp]
    identity: Optional[RiskSourceIdentity]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _existing_regular_file(raw_path: str | Path, label: str) -> Path:
    path = Path(raw_path).expanduser()
    if _is_link_like(path):
        raise PortabilityMigrationError(f"{label} must not be a symlink or junction: {path}")
    try:
        file_stat = path.stat()
    except FileNotFoundError:
        raise PortabilityMigrationError(f"{label} does not exist: {path}") from None
    except OSError as exc:
        raise PortabilityMigrationError(f"Cannot inspect {label}: {path}: {exc}") from exc
    if not stat_module.S_ISREG(file_stat.st_mode):
        raise PortabilityMigrationError(f"{label} is not a regular file: {path}")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise PortabilityMigrationError(f"Cannot resolve {label}: {path}: {exc}") from exc


def _require_distinct_files(source_path: Path, sidecar_path: Path) -> None:
    same = os.path.normcase(str(source_path)) == os.path.normcase(str(sidecar_path))
    if not same:
        try:
            same = os.path.samefile(source_path, sidecar_path)
        except OSError as exc:
            raise PortabilityMigrationError(
                f"Cannot prove source and sidecar are distinct files: {exc}"
            ) from exc
    if same:
        raise PortabilityMigrationError(
            "Main source database and risk-network sidecar must be distinct files"
        )


def _auxiliary_state(database_path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for suffix in ("-wal", "-journal"):
        auxiliary = Path(str(database_path) + suffix)
        try:
            size = int(auxiliary.stat().st_size)
        except FileNotFoundError:
            size = 0
        except OSError as exc:
            raise PortabilityMigrationError(
                f"Cannot inspect SQLite auxiliary file {auxiliary}: {exc}"
            ) from exc
        result[suffix[1:]] = size
    return result


def _assert_auxiliaries_clear(database_path: Path, label: str) -> None:
    populated = {
        name: size
        for name, size in _auxiliary_state(database_path).items()
        if size > 0
    }
    if populated:
        detail = ", ".join(f"{name}={size:,} bytes" for name, size in populated.items())
        raise PortabilityMigrationError(
            f"{label} has populated SQLite auxiliary data ({detail}); stop writers, "
            "checkpoint/recover the database, and retry"
        )


def _file_uri(path: Path, mode: str) -> str:
    return (
        "file:"
        + quote(path.resolve().as_posix(), safe="/:")
        + f"?mode={mode}"
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_file_uri(path, "ro"), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def _connect_writable(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _file_uri(path, "rw"), uri=True, timeout=30.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def _journal_mode(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()


def _read_sidecar_meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT key,value FROM risk_network_build_meta ORDER BY key"
        ).fetchall()
    except sqlite3.Error as exc:
        raise PortabilityMigrationError(
            f"Risk-network sidecar lacks compatible build metadata: {exc}"
        ) from exc
    meta = {str(row[0]): str(row[1] or "") for row in rows}
    if meta.get("build_status") != "complete":
        raise PortabilityMigrationError(
            "Risk-network sidecar build_status must be 'complete'"
        )
    if meta.get("schema_version") != EXPECTED_SIDECAR_SCHEMA_VERSION:
        raise PortabilityMigrationError(
            "Risk-network sidecar schema_version must be "
            + EXPECTED_SIDECAR_SCHEMA_VERSION
        )
    return meta


def _read_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in COUNT_TABLES:
        try:
            counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        except sqlite3.Error as exc:
            raise PortabilityMigrationError(
                f"Risk-network sidecar lacks required table {table}: {exc}"
            ) from exc
    return counts


def _validate_declared_counts(
    meta: Mapping[str, str], counts: Mapping[str, int]
) -> None:
    declared_keys = {
        "edge_count_written": "risk_network_edge",
        "selected_filing_count": "risk_network_filing_state",
    }
    for meta_key, table in declared_keys.items():
        value = str(meta.get(meta_key, "")).strip()
        try:
            declared = int(value)
        except (TypeError, ValueError):
            raise PortabilityMigrationError(
                f"Risk-network sidecar has invalid declared {meta_key}: {value!r}"
            ) from None
        if declared < 0 or declared != int(counts[table]):
            raise PortabilityMigrationError(
                f"Risk-network declared {meta_key}={declared:,} does not match "
                f"{table} count={int(counts[table]):,}"
            )


def _quick_check_metadata_conn(conn: sqlite3.Connection) -> None:
    for table in ("risk_network_build_meta", "risk_network_source_status"):
        result = [
            str(row[0])
            for row in conn.execute(f"PRAGMA quick_check('{table}')")
        ]
        if result != ["ok"]:
            raise PortabilityMigrationError(
                f"Sidecar metadata quick_check failed for {table}: {result!r}"
            )


def _parse_legacy_meta(meta: Mapping[str, str]) -> LegacySourceStamp:
    required = ("source_lineage_id", "source_file_size", "source_file_mtime_ns")
    missing = [key for key in required if str(meta.get(key, "")).strip() == ""]
    if missing:
        raise PortabilityMigrationError(
            "Legacy risk-network source metadata is incomplete; missing: "
            + ", ".join(missing)
        )
    try:
        file_size = int(meta["source_file_size"])
        file_mtime_ns = int(meta["source_file_mtime_ns"])
    except (TypeError, ValueError):
        raise PortabilityMigrationError(
            "Legacy risk-network source size/mtime metadata is invalid"
        ) from None
    lineage_id = str(meta["source_lineage_id"]).strip().lower()
    if (
        file_size < 100
        or file_mtime_ns < 0
        or len(lineage_id) != 64
        or any(ch not in "0123456789abcdef" for ch in lineage_id)
    ):
        raise PortabilityMigrationError("Legacy risk-network source metadata is invalid")
    return LegacySourceStamp(lineage_id, file_size, file_mtime_ns)


def _current_legacy_stamp(source_path: Path) -> LegacySourceStamp:
    source_stat = source_path.stat()
    return LegacySourceStamp(
        lineage_id=source_lineage_id(source_path),
        file_size=int(source_stat.st_size),
        file_mtime_ns=int(source_stat.st_mtime_ns),
    )


def _current_sidecar_guard(sidecar_path: Path) -> LocalSidecarGuard:
    before = sidecar_path.stat()
    lineage_id = source_lineage_id(sidecar_path)
    file_size, header_sha256 = sqlite_header_sha256(sidecar_path)
    after = sidecar_path.stat()
    before_state = (
        int(before.st_size),
        int(before.st_mtime_ns),
        int(getattr(before, "st_dev", 0)),
        int(getattr(before, "st_ino", 0)),
    )
    after_state = (
        int(after.st_size),
        int(after.st_mtime_ns),
        int(getattr(after, "st_dev", 0)),
        int(getattr(after, "st_ino", 0)),
    )
    if before_state != after_state or int(file_size) != int(after.st_size):
        raise PortabilityMigrationError(
            "Risk-network sidecar changed while its local physical guard was read"
        )
    return LocalSidecarGuard(
        lineage_id=lineage_id,
        file_size=int(file_size),
        file_mtime_ns=int(after.st_mtime_ns),
        header_sha256=header_sha256,
    )


def _validate_legacy_source(meta: Mapping[str, str], source_path: Path) -> LegacySourceStamp:
    expected = _parse_legacy_meta(meta)
    current = _current_legacy_stamp(source_path)
    if expected != current:
        raise PortabilityMigrationError(
            "Legacy risk-network metadata does not match the current main database"
        )
    return expected


def _read_identity(source_path: Path) -> Optional[RiskSourceIdentity]:
    try:
        with closing(_connect_readonly(source_path)) as conn:
            return read_risk_source_identity(conn)
    except (sqlite3.Error, RiskSourceIdentityError) as exc:
        raise PortabilityMigrationError(str(exc)) from exc


def _require_identity_adopts_legacy(
    identity: RiskSourceIdentity, legacy: LegacySourceStamp
) -> None:
    adopted_tuple_matches = (
        identity.adopted_legacy_lineage_id == legacy.lineage_id
        and identity.adopted_legacy_file_size == legacy.file_size
        and identity.adopted_legacy_file_mtime_ns == legacy.file_mtime_ns
    )
    if not adopted_tuple_matches:
        raise PortabilityMigrationError(
            "Existing main-database identity does not adopt this sidecar's "
            "legacy source metadata"
        )
    if identity.adopted_at_revision_id != identity.revision_id:
        raise PortabilityMigrationError(
            "Main risk-source revision changed after legacy adoption; refusing "
            "to bless the legacy sidecar"
        )


def preflight(source: str | Path, sidecar: str | Path) -> MigrationPreflight:
    source_path = _existing_regular_file(source, "Main source database")
    sidecar_path = _existing_regular_file(sidecar, "Risk-network sidecar")
    _require_distinct_files(source_path, sidecar_path)
    _assert_auxiliaries_clear(source_path, "Main source database")
    _assert_auxiliaries_clear(sidecar_path, "Risk-network sidecar")
    sidecar_guard_before = _current_sidecar_guard(sidecar_path)

    try:
        with closing(_connect_readonly(source_path)) as source_conn:
            source_conn.execute("BEGIN")
            source_conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
            source_journal_mode = _journal_mode(source_conn)
            identity = read_risk_source_identity(source_conn)
            source_conn.commit()
        with closing(_connect_readonly(sidecar_path)) as sidecar_conn:
            sidecar_conn.execute("BEGIN")
            sidecar_conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
            sidecar_journal_mode = _journal_mode(sidecar_conn)
            meta = _read_sidecar_meta(sidecar_conn)
            counts = _read_counts(sidecar_conn)
            _validate_declared_counts(meta, counts)
            _quick_check_metadata_conn(sidecar_conn)
            sidecar_conn.commit()
    except (sqlite3.Error, RiskSourceIdentityError) as exc:
        raise PortabilityMigrationError(str(exc)) from exc
    _assert_auxiliaries_clear(source_path, "Main source database")
    _assert_auxiliaries_clear(sidecar_path, "Risk-network sidecar")
    sidecar_guard_after = _current_sidecar_guard(sidecar_path)
    if sidecar_guard_after != sidecar_guard_before:
        raise PortabilityMigrationError(
            "Risk-network sidecar changed during migration preflight"
        )

    try:
        portable = parse_portable_metadata(meta)
    except RiskSourceIdentityError as exc:
        raise PortabilityMigrationError(str(exc)) from exc

    if portable is not None:
        if identity is None:
            raise PortabilityMigrationError(
                "Sidecar has portable metadata but the main database has no identity"
            )
        try:
            validate_portable_source(meta, source_path)
        except RiskSourceIdentityError as exc:
            raise PortabilityMigrationError(str(exc)) from exc
        action = "no_op"
        legacy_stamp: Optional[LegacySourceStamp] = None
    else:
        legacy_stamp = _parse_legacy_meta(meta)
        if identity is None:
            _validate_legacy_source(meta, source_path)
            action = "initial_migration"
        else:
            _require_identity_adopts_legacy(identity, legacy_stamp)
            action = "resume_after_main_identity"

    return MigrationPreflight(
        action=action,
        source_path=source_path,
        sidecar_path=sidecar_path,
        source_journal_mode=source_journal_mode,
        sidecar_journal_mode=sidecar_journal_mode,
        sidecar_meta=meta,
        counts=counts,
        sidecar_guard=sidecar_guard_after,
        legacy_stamp=legacy_stamp,
        identity=identity,
    )


def _identity_dict(identity: Optional[RiskSourceIdentity]) -> Optional[dict[str, Any]]:
    return asdict(identity) if identity is not None else None


def _preflight_receipt(pre: MigrationPreflight) -> dict[str, Any]:
    return {
        "format": "risk_network_portability_migration_v1",
        "command": "apply",
        "status": "prepared",
        "prepared_at": utc_now(),
        "action": pre.action,
        "source_path": str(pre.source_path),
        "sidecar_path": str(pre.sidecar_path),
        "source_journal_mode": pre.source_journal_mode,
        "sidecar_journal_mode": pre.sidecar_journal_mode,
        "source_auxiliary_sizes": _auxiliary_state(pre.source_path),
        "sidecar_auxiliary_sizes": _auxiliary_state(pre.sidecar_path),
        "identity_before": _identity_dict(pre.identity),
        "legacy_stamp_before": (
            asdict(pre.legacy_stamp) if pre.legacy_stamp is not None else None
        ),
        "sidecar_counts_before": pre.counts,
        "sidecar_physical_guard_before": asdict(pre.sidecar_guard),
        "sidecar_meta_before": pre.sidecar_meta,
    }


def _default_receipt_path(command: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return Path(__file__).resolve().parent / "exports" / (
        f"risk-network-portability-{command}-{stamp}-{suffix}.json"
    )


def _new_receipt_path(raw_path: Optional[str], command: str) -> Path:
    path = Path(raw_path).expanduser() if raw_path else _default_receipt_path(command)
    path = path.absolute()
    if path.exists():
        raise PortabilityMigrationError(f"Receipt path already exists: {path}")
    if _is_link_like(path):
        raise PortabilityMigrationError(f"Receipt path must not be a link: {path}")
    return path


def _write_receipt(path: Path, receipt: Mapping[str, Any], *, first: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if first and path.exists():
        raise PortabilityMigrationError(f"Receipt path already exists: {path}")
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(receipt), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _commit_and_checkpoint(
    conn: sqlite3.Connection, database_path: Path, label: str
) -> None:
    mode = _journal_mode(conn)
    conn.commit()
    if mode == "wal":
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise PortabilityMigrationError(
                f"{label} WAL checkpoint did not complete: {checkpoint!r}"
            )
    _assert_auxiliaries_clear(database_path, label)


def _create_or_resume_identity(
    source_path: Path, legacy: LegacySourceStamp
) -> RiskSourceIdentity:
    _assert_auxiliaries_clear(source_path, "Main source database")
    with closing(_connect_writable(source_path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            identity = ensure_risk_source_identity(
                conn,
                adopted_legacy_lineage_id=legacy.lineage_id,
                adopted_legacy_file_size=legacy.file_size,
                adopted_legacy_file_mtime_ns=legacy.file_mtime_ns,
            )
            _commit_and_checkpoint(conn, source_path, "Main source database")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    return identity


def _write_sidecar_portable_meta(
    source_path: Path,
    sidecar_path: Path,
    expected_identity: RiskSourceIdentity,
    expected_stamp: PortableSourceStamp,
    expected_preflight_meta: Mapping[str, str],
    expected_counts: Mapping[str, int],
    expected_sidecar_guard: LocalSidecarGuard,
    legacy_before: LegacySourceStamp,
    legacy_after: LegacySourceStamp,
) -> tuple[dict[str, str], dict[str, int]]:
    _assert_auxiliaries_clear(sidecar_path, "Risk-network sidecar")
    values = dict(expected_stamp.metadata())
    values.update(legacy_after.metadata())
    with closing(_connect_writable(sidecar_path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            if _current_sidecar_guard(sidecar_path) != expected_sidecar_guard:
                raise PortabilityMigrationError(
                    "Risk-network sidecar physical file changed after preflight"
                )
            current = _read_sidecar_meta(conn)
            if current != dict(expected_preflight_meta):
                raise PortabilityMigrationError(
                    "Risk-network sidecar metadata changed after preflight"
                )
            if parse_portable_metadata(current) is not None:
                raise PortabilityMigrationError(
                    "Sidecar portable metadata appeared during migration; refusing overwrite"
                )
            if _parse_legacy_meta(current) != legacy_before:
                raise PortabilityMigrationError(
                    "Risk-network legacy metadata changed after preflight"
                )
            counts_before = _read_counts(conn)
            if counts_before != dict(expected_counts):
                raise PortabilityMigrationError(
                    "Risk-network row counts changed after preflight"
                )
            _validate_declared_counts(current, counts_before)
            _quick_check_metadata_conn(conn)

            _assert_auxiliaries_clear(source_path, "Main source database")
            current_identity = _read_identity(source_path)
            if current_identity != expected_identity:
                raise PortabilityMigrationError(
                    "Main risk-source identity changed before sidecar publication"
                )
            _require_identity_adopts_legacy(expected_identity, legacy_before)
            if portable_source_stamp(source_path, expected_identity) != expected_stamp:
                raise PortabilityMigrationError(
                    "Main portable source stamp changed before sidecar publication"
                )
            if _current_legacy_stamp(source_path) != legacy_after:
                raise PortabilityMigrationError(
                    "Main legacy source stamp changed before sidecar publication"
                )

            conn.executemany(
                "INSERT OR REPLACE INTO risk_network_build_meta(key,value) VALUES (?,?)",
                sorted(values.items()),
            )
            updated = _read_sidecar_meta(conn)
            parsed = parse_portable_metadata(updated)
            if parsed != expected_stamp:
                raise PortabilityMigrationError(
                    "Sidecar portable metadata failed transactional verification"
                )
            if _parse_legacy_meta(updated) != legacy_after:
                raise PortabilityMigrationError(
                    "Sidecar legacy compatibility metadata failed verification"
                )
            counts_after = _read_counts(conn)
            if counts_after != counts_before:
                raise PortabilityMigrationError(
                    "Risk-network row counts changed inside metadata transaction"
                )
            _validate_declared_counts(updated, counts_after)
            _quick_check_metadata_conn(conn)
            try:
                validate_portable_source(updated, source_path)
            except RiskSourceIdentityError as exc:
                raise PortabilityMigrationError(str(exc)) from exc

            # Recheck the other database at the last safe point. If a writer
            # races after this point, the runtime's revision/header/WAL checks
            # still fail closed instead of accepting an incorrect pair.
            _assert_auxiliaries_clear(source_path, "Main source database")
            if _read_identity(source_path) != expected_identity:
                raise PortabilityMigrationError(
                    "Main risk-source identity changed during sidecar transaction"
                )
            if portable_source_stamp(source_path, expected_identity) != expected_stamp:
                raise PortabilityMigrationError(
                    "Main portable source stamp changed during sidecar transaction"
                )
            _commit_and_checkpoint(conn, sidecar_path, "Risk-network sidecar")
            return updated, counts_after
        except RiskSourceIdentityError as exc:
            if conn.in_transaction:
                conn.rollback()
            raise PortabilityMigrationError(str(exc)) from exc
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _validate_runtime(source_path: Path, sidecar_path: Path) -> None:
    # Import lazily so this migration remains usable by maintenance tooling
    # without initializing Flask or the query registry.
    from queries._risk_network import available

    env = {
        "IRS_DB_PATH": str(source_path),
        "IRS_RISK_NETWORK_DB_PATH": str(sidecar_path),
    }
    if not available(main_db_path=str(source_path), environ=env):
        raise PortabilityMigrationError(
            "Updated application runtime does not accept the migrated portable pair"
        )


def _invalidate_committed_sidecar(
    sidecar_path: Path, expected_stamp: PortableSourceStamp, reason: str
) -> None:
    """Fail closed if a post-commit invariant unexpectedly fails."""

    with closing(_connect_writable(sidecar_path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            meta = _read_sidecar_meta(conn)
            try:
                current = parse_portable_metadata(meta)
            except RiskSourceIdentityError:
                current = None
            if current != expected_stamp:
                raise PortabilityMigrationError(
                    "Cannot safely invalidate sidecar because its portable stamp changed"
                )
            conn.executemany(
                "INSERT OR REPLACE INTO risk_network_build_meta(key,value) VALUES (?,?)",
                (
                    ("build_status", "portability_migration_failed"),
                    ("portability_migration_failure", reason[:1000]),
                ),
            )
            _commit_and_checkpoint(conn, sidecar_path, "Risk-network sidecar")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _validate_completed_apply(
    source_path: Path,
    sidecar_path: Path,
    expected_counts: Mapping[str, int],
    *,
    require_local_legacy: bool,
) -> tuple[dict[str, str], dict[str, int]]:
    _assert_auxiliaries_clear(source_path, "Main source database")
    _assert_auxiliaries_clear(sidecar_path, "Risk-network sidecar")
    with closing(_connect_readonly(sidecar_path)) as conn:
        conn.execute("BEGIN")
        meta = _read_sidecar_meta(conn)
        counts = _read_counts(conn)
        _validate_declared_counts(meta, counts)
        _quick_check_metadata_conn(conn)
        conn.commit()
    if counts != dict(expected_counts):
        raise PortabilityMigrationError(
            f"Risk-network row counts changed during metadata migration: "
            f"before={dict(expected_counts)!r}, after={counts!r}"
        )
    try:
        validate_portable_source(meta, source_path)
    except RiskSourceIdentityError as exc:
        raise PortabilityMigrationError(str(exc)) from exc
    if require_local_legacy:
        _validate_legacy_source(meta, source_path)
    _validate_runtime(source_path, sidecar_path)
    return meta, counts


def apply_migration(
    source: str | Path,
    sidecar: str | Path,
    *,
    yes: bool,
    receipt_path: Optional[str] = None,
) -> Path:
    if not yes:
        raise PortabilityMigrationError("apply requires explicit --yes")
    pre = preflight(source, sidecar)
    receipt_file = _new_receipt_path(receipt_path, "apply")
    receipt = _preflight_receipt(pre)
    _write_receipt(receipt_file, receipt, first=True)

    if pre.action == "no_op":
        meta, counts = _validate_completed_apply(
            pre.source_path,
            pre.sidecar_path,
            pre.counts,
            require_local_legacy=False,
        )
        receipt.update(
            {
                "status": "no_op",
                "completed_at": utc_now(),
                "sidecar_counts_after": counts,
                "portable_meta_after": {
                    key: value
                    for key, value in meta.items()
                    if key.startswith("source_")
                },
            }
        )
        _write_receipt(receipt_file, receipt)
        return receipt_file

    assert pre.legacy_stamp is not None
    try:
        if pre.action == "initial_migration":
            # Repeat the physical-file check immediately before the first write.
            _assert_auxiliaries_clear(pre.source_path, "Main source database")
            _validate_legacy_source(pre.sidecar_meta, pre.source_path)
            identity = _create_or_resume_identity(pre.source_path, pre.legacy_stamp)
        else:
            assert pre.identity is not None
            identity = pre.identity

        current_identity = _read_identity(pre.source_path)
        if current_identity is None or current_identity != identity:
            raise PortabilityMigrationError(
                "Main source identity changed while the migration was running"
            )
        stamp = portable_source_stamp(pre.source_path, identity)
        legacy_after = _current_legacy_stamp(pre.source_path)
        receipt.update(
            {
                "status": "main_identity_committed",
                "main_identity_committed_at": utc_now(),
                "identity_after_main_phase": _identity_dict(identity),
                "portable_stamp_after_main_phase": stamp.metadata(),
                "legacy_stamp_after_main_phase": asdict(legacy_after),
            }
        )
        _write_receipt(receipt_file, receipt)

        # For a resumed phase, reconfirm that the persisted adoption still
        # identifies precisely the untouched legacy sidecar.
        _require_identity_adopts_legacy(identity, pre.legacy_stamp)
        _assert_auxiliaries_clear(pre.source_path, "Main source database")
        transactional_meta, transactional_counts = _write_sidecar_portable_meta(
            pre.source_path,
            pre.sidecar_path,
            identity,
            stamp,
            pre.sidecar_meta,
            pre.counts,
            pre.sidecar_guard,
            pre.legacy_stamp,
            legacy_after,
        )

        try:
            meta_after, counts_after = _validate_completed_apply(
                pre.source_path,
                pre.sidecar_path,
                pre.counts,
                require_local_legacy=True,
            )
            if meta_after != transactional_meta or counts_after != transactional_counts:
                raise PortabilityMigrationError(
                    "Committed sidecar differs from its transactionally validated state"
                )
        except Exception as postcommit_error:
            try:
                _invalidate_committed_sidecar(
                    pre.sidecar_path, stamp, str(postcommit_error)
                )
            except Exception as invalidation_error:
                raise PortabilityMigrationError(
                    f"Post-commit validation failed ({postcommit_error}); fail-closed "
                    f"invalidation also failed ({invalidation_error})"
                ) from postcommit_error
            raise
        receipt.update(
            {
                "status": "complete",
                "completed_at": utc_now(),
                "source_auxiliary_sizes_after": _auxiliary_state(pre.source_path),
                "sidecar_auxiliary_sizes_after": _auxiliary_state(pre.sidecar_path),
                "sidecar_counts_after": counts_after,
                "portable_meta_after": {
                    key: value
                    for key, value in meta_after.items()
                    if key.startswith("source_")
                },
            }
        )
        _write_receipt(receipt_file, receipt)
        return receipt_file
    except Exception as exc:
        receipt.update(
            {
                "status": "failed",
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        try:
            _write_receipt(receipt_file, receipt)
        except Exception:
            pass
        raise


def initialize_risk_source_identity(
    source: str | Path,
    *,
    yes: bool,
    receipt_path: Optional[str] = None,
) -> Path:
    """Initialize a portable identity for a source that has no legacy sidecar."""

    if not yes:
        raise PortabilityMigrationError(
            "initialize-risk-source-identity requires explicit --yes"
        )
    source_path = _existing_regular_file(source, "Main source database")
    _assert_auxiliaries_clear(source_path, "Main source database")
    with closing(_connect_readonly(source_path)) as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
        journal_mode = _journal_mode(conn)
        identity_before = read_risk_source_identity(conn)
        conn.commit()
    _assert_auxiliaries_clear(source_path, "Main source database")

    receipt_file = _new_receipt_path(receipt_path, "initialize")
    receipt: dict[str, Any] = {
        "format": "risk_network_portability_migration_v1",
        "command": "initialize-risk-source-identity",
        "status": "prepared",
        "prepared_at": utc_now(),
        "source_path": str(source_path),
        "source_journal_mode": journal_mode,
        "source_auxiliary_sizes": _auxiliary_state(source_path),
        "identity_before": _identity_dict(identity_before),
    }
    _write_receipt(receipt_file, receipt, first=True)

    if identity_before is not None:
        stamp = portable_source_stamp(source_path, identity_before)
        receipt.update(
            {
                "status": "no_op",
                "completed_at": utc_now(),
                "identity_after": _identity_dict(identity_before),
                "portable_stamp_after": stamp.metadata(),
            }
        )
        _write_receipt(receipt_file, receipt)
        return receipt_file

    try:
        _assert_auxiliaries_clear(source_path, "Main source database")
        with closing(_connect_writable(source_path)) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                identity_after = ensure_risk_source_identity(conn)
                _commit_and_checkpoint(
                    conn, source_path, "Main source database"
                )
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
        persisted = _read_identity(source_path)
        if persisted != identity_after:
            raise PortabilityMigrationError(
                "Initialized risk-source identity did not persist exactly"
            )
        if any(
            value is not None
            for value in (
                identity_after.adopted_legacy_lineage_id,
                identity_after.adopted_legacy_file_size,
                identity_after.adopted_legacy_file_mtime_ns,
                identity_after.adopted_at_revision_id,
            )
        ):
            raise PortabilityMigrationError(
                "Standalone identity initialization unexpectedly adopted legacy metadata"
            )
        stamp = portable_source_stamp(source_path, identity_after)
        receipt.update(
            {
                "status": "complete",
                "completed_at": utc_now(),
                "identity_after": _identity_dict(identity_after),
                "portable_stamp_after": stamp.metadata(),
                "source_auxiliary_sizes_after": _auxiliary_state(source_path),
            }
        )
        _write_receipt(receipt_file, receipt)
        return receipt_file
    except Exception as exc:
        receipt.update(
            {
                "status": "failed",
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        try:
            _write_receipt(receipt_file, receipt)
        except Exception:
            pass
        raise


def mark_risk_source_changed(
    source: str | Path,
    *,
    yes: bool,
    receipt_path: Optional[str] = None,
) -> Path:
    if not yes:
        raise PortabilityMigrationError(
            "mark-risk-source-changed requires explicit --yes"
        )
    source_path = _existing_regular_file(source, "Main source database")
    _assert_auxiliaries_clear(source_path, "Main source database")
    identity_before = _read_identity(source_path)
    if identity_before is None:
        raise PortabilityMigrationError(
            "Main database has no portable risk-source identity to rotate"
        )
    stamp_before = portable_source_stamp(source_path, identity_before)
    receipt_file = _new_receipt_path(receipt_path, "mark-changed")
    receipt: dict[str, Any] = {
        "format": "risk_network_portability_migration_v1",
        "command": "mark-risk-source-changed",
        "status": "prepared",
        "prepared_at": utc_now(),
        "source_path": str(source_path),
        "source_auxiliary_sizes": _auxiliary_state(source_path),
        "identity_before": _identity_dict(identity_before),
        "portable_stamp_before": stamp_before.metadata(),
    }
    _write_receipt(receipt_file, receipt, first=True)
    try:
        with closing(_connect_writable(source_path)) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                identity_after = rotate_risk_source_revision(conn)
                _commit_and_checkpoint(
                    conn, source_path, "Main source database"
                )
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
        if (
            identity_after.database_id != identity_before.database_id
            or identity_after.revision_id == identity_before.revision_id
        ):
            raise PortabilityMigrationError(
                "Risk-source revision rotation did not produce the expected identity"
            )
        persisted = _read_identity(source_path)
        if persisted != identity_after:
            raise PortabilityMigrationError(
                "Rotated risk-source identity did not persist exactly"
            )
        stamp_after = portable_source_stamp(source_path, identity_after)
        receipt.update(
            {
                "status": "complete",
                "completed_at": utc_now(),
                "identity_after": _identity_dict(identity_after),
                "portable_stamp_after": stamp_after.metadata(),
                "source_auxiliary_sizes_after": _auxiliary_state(source_path),
            }
        )
        _write_receipt(receipt_file, receipt)
        return receipt_file
    except Exception as exc:
        receipt.update(
            {
                "status": "failed",
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        try:
            _write_receipt(receipt_file, receipt)
        except Exception:
            pass
        raise


def _plan_output(pre: MigrationPreflight) -> dict[str, Any]:
    return {
        "action": pre.action,
        "source_path": str(pre.source_path),
        "sidecar_path": str(pre.sidecar_path),
        "source_journal_mode": pre.source_journal_mode,
        "sidecar_journal_mode": pre.sidecar_journal_mode,
        "sidecar_counts": pre.counts,
        "identity_present": pre.identity is not None,
        "portable_sidecar_present": pre.action == "no_op",
        "changes_planned": pre.action != "no_op",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate risk-network source lineage to a portable identity"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser("plan", help="Read-only migration preflight")
    plan_parser.add_argument("--db", required=True, help="Main IRS SQLite database")
    plan_parser.add_argument("--sidecar", required=True, help="Risk-network SQLite sidecar")

    apply_parser = sub.add_parser("apply", help="Apply the metadata-only migration")
    apply_parser.add_argument("--db", required=True, help="Main IRS SQLite database")
    apply_parser.add_argument("--sidecar", required=True, help="Risk-network SQLite sidecar")
    apply_parser.add_argument("--receipt", default=None, help="New JSON receipt path")
    apply_parser.add_argument("--yes", action="store_true", help="Confirm database writes")

    mark_parser = sub.add_parser(
        "mark-risk-source-changed",
        help="Rotate the source revision before supported source changes",
    )
    mark_parser.add_argument("--db", required=True, help="Main IRS SQLite database")
    mark_parser.add_argument("--receipt", default=None, help="New JSON receipt path")
    mark_parser.add_argument("--yes", action="store_true", help="Confirm database write")

    initialize_parser = sub.add_parser(
        "initialize-risk-source-identity",
        help="Initialize identity for a main database without a legacy sidecar",
    )
    initialize_parser.add_argument(
        "--db", required=True, help="Main IRS SQLite database"
    )
    initialize_parser.add_argument(
        "--receipt", default=None, help="New JSON receipt path"
    )
    initialize_parser.add_argument(
        "--yes", action="store_true", help="Confirm database write"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        print(json.dumps(_plan_output(preflight(args.db, args.sidecar)), indent=2, sort_keys=True))
        return 0
    if args.command == "apply":
        receipt = apply_migration(
            args.db,
            args.sidecar,
            yes=bool(args.yes),
            receipt_path=args.receipt,
        )
        print(f"Portability migration receipt: {receipt}")
        return 0
    if args.command == "mark-risk-source-changed":
        receipt = mark_risk_source_changed(
            args.db,
            yes=bool(args.yes),
            receipt_path=args.receipt,
        )
        print(f"Risk-source revision receipt: {receipt}")
        return 0
    if args.command == "initialize-risk-source-identity":
        receipt = initialize_risk_source_identity(
            args.db,
            yes=bool(args.yes),
            receipt_path=args.receipt,
        )
        print(f"Risk-source initialization receipt: {receipt}")
        return 0
    raise PortabilityMigrationError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
