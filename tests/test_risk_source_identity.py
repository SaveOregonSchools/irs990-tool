from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from risk_source_identity import (
    RiskSourceIdentityError,
    ensure_risk_source_identity,
    parse_portable_metadata,
    portable_source_stamp,
    read_risk_source_identity,
    rotate_risk_source_revision,
    validate_portable_source,
)


def _create_source(path: Path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample(value TEXT NOT NULL)")
        identity = ensure_risk_source_identity(conn)
        conn.commit()
    return identity


def test_checkpointed_copy_keeps_portable_identity_across_path_and_mtime(tmp_path):
    source = tmp_path / "windows-source.db"
    identity = _create_source(source)
    metadata = portable_source_stamp(source, identity).metadata()

    relocated = tmp_path / "linux" / "irs990.db"
    relocated.parent.mkdir()
    shutil.copyfile(source, relocated)
    os.utime(relocated, (source.stat().st_atime + 120, source.stat().st_mtime + 120))

    assert validate_portable_source(metadata, relocated).metadata() == metadata


def test_rotating_revision_invalidates_previous_snapshot(tmp_path):
    source = tmp_path / "source.db"
    identity = _create_source(source)
    metadata = portable_source_stamp(source, identity).metadata()

    with sqlite3.connect(source) as conn:
        rotated = rotate_risk_source_revision(conn)
        conn.commit()

    assert rotated.database_id == identity.database_id
    assert rotated.revision_id != identity.revision_id
    with pytest.raises(RiskSourceIdentityError, match="stale"):
        validate_portable_source(metadata, source)


def test_rollback_journal_source_write_without_revision_change_is_detected(tmp_path):
    source = tmp_path / "source.db"
    identity = _create_source(source)
    metadata = portable_source_stamp(source, identity).metadata()

    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO sample(value) VALUES ('changed')")
        conn.commit()

    with pytest.raises(RiskSourceIdentityError, match="stale"):
        validate_portable_source(metadata, source)


def test_independent_database_cannot_reuse_snapshot_metadata(tmp_path):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    first_identity = _create_source(first)
    _create_source(second)

    with pytest.raises(RiskSourceIdentityError, match="stale"):
        validate_portable_source(
            portable_source_stamp(first, first_identity).metadata(), second
        )


def test_partial_portable_metadata_fails_closed():
    with pytest.raises(RiskSourceIdentityError, match="incomplete"):
        parse_portable_metadata({"source_database_id": "missing-the-other-fields"})


def test_legacy_adoption_is_idempotent_but_cannot_be_changed(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as conn:
        first = ensure_risk_source_identity(
            conn,
            adopted_legacy_lineage_id="legacy-hash",
            adopted_legacy_file_size=123,
            adopted_legacy_file_mtime_ns=456,
        )
        second = ensure_risk_source_identity(
            conn,
            adopted_legacy_lineage_id="legacy-hash",
            adopted_legacy_file_size=123,
            adopted_legacy_file_mtime_ns=456,
        )
        assert first == second
        assert first.adopted_at_revision_id == first.revision_id

        with pytest.raises(RiskSourceIdentityError, match="different legacy"):
            ensure_risk_source_identity(
                conn,
                adopted_legacy_lineage_id="different-hash",
                adopted_legacy_file_size=123,
                adopted_legacy_file_mtime_ns=456,
            )


def test_partial_legacy_adoption_is_rejected(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as conn:
        with pytest.raises(RiskSourceIdentityError, match="requires lineage"):
            ensure_risk_source_identity(
                conn,
                adopted_legacy_lineage_id="legacy-hash",
                adopted_legacy_file_size=123,
            )


def test_revision_rotation_retains_the_original_adoption_revision(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as conn:
        adopted = ensure_risk_source_identity(
            conn,
            adopted_legacy_lineage_id="legacy-hash",
            adopted_legacy_file_size=123,
            adopted_legacy_file_mtime_ns=456,
        )
        rotated = rotate_risk_source_revision(conn)

    assert rotated.revision_id != adopted.revision_id
    assert rotated.adopted_at_revision_id == adopted.revision_id


def test_identity_creation_obeys_callers_transaction(tmp_path):
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("BEGIN")
        ensure_risk_source_identity(conn)
        conn.rollback()
        assert read_risk_source_identity(conn) is None
    finally:
        conn.close()
