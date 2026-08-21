from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest

import migrate_risk_network_portability as migration
from build_risk_network import source_lineage_id
from queries import _risk_network
from risk_source_identity import (
    RiskSourceIdentityError,
    ensure_risk_source_identity,
    parse_portable_metadata,
    portable_source_stamp_from_values,
    read_risk_source_identity,
    rotate_risk_source_revision,
    validate_portable_source,
)


def _checkpoint_and_close(conn: sqlite3.Connection) -> None:
    conn.commit()
    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert result[0] == 0
    conn.close()


def _legacy_meta(source: Path) -> dict[str, str]:
    source_stat = source.stat()
    return {
        "source_lineage_id": source_lineage_id(source),
        "source_file_size": str(source_stat.st_size),
        "source_file_mtime_ns": str(source_stat.st_mtime_ns),
    }


def _read_meta(sidecar: Path) -> dict[str, str]:
    with closing(sqlite3.connect(sidecar)) as conn:
        return dict(conn.execute("SELECT key,value FROM risk_network_build_meta"))


def _read_identity(source: Path):
    with closing(sqlite3.connect(source)) as conn:
        return read_risk_source_identity(conn)


def _counts(sidecar: Path) -> dict[str, int]:
    with closing(sqlite3.connect(sidecar)) as conn:
        return {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in migration.COUNT_TABLES
        }


def _create_pair(root: Path) -> tuple[Path, Path, dict[str, str]]:
    source = root / "main.db"
    sidecar = root / "risk_network.db"

    source_conn = sqlite3.connect(source)
    assert source_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    source_conn.executescript(
        """
        CREATE TABLE returns(filing_id TEXT PRIMARY KEY, ein TEXT NOT NULL);
        INSERT INTO returns VALUES ('F1','123456789');
        """
    )
    _checkpoint_and_close(source_conn)
    legacy = _legacy_meta(source)

    sidecar_conn = sqlite3.connect(sidecar)
    sidecar_conn.executescript(
        """
        CREATE TABLE risk_network_build_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE risk_network_edge(edge_id TEXT PRIMARY KEY);
        CREATE TABLE risk_network_filing_state(filing_id TEXT PRIMARY KEY);
        CREATE TABLE risk_network_node_stats(target_key TEXT PRIMARY KEY);
        CREATE TABLE risk_network_source_status(source_name TEXT PRIMARY KEY);
        INSERT INTO risk_network_edge VALUES ('E1');
        INSERT INTO risk_network_filing_state VALUES ('F1');
        INSERT INTO risk_network_node_stats VALUES ('N1');
        INSERT INTO risk_network_source_status VALUES ('addresses');
        """
    )
    meta = {
        "schema_version": "1",
        "build_status": "complete",
        "build_scope": "full",
        "edge_count_written": "1",
        "selected_filing_count": "1",
        **legacy,
    }
    sidecar_conn.executemany(
        "INSERT INTO risk_network_build_meta(key,value) VALUES (?,?)",
        sorted(meta.items()),
    )
    sidecar_conn.commit()
    sidecar_conn.close()
    return source, sidecar, legacy


def _apply(
    source: Path, sidecar: Path, receipt: Path
) -> Path:
    return migration.apply_migration(
        source, sidecar, yes=True, receipt_path=str(receipt)
    )


def test_plan_is_read_only_and_reports_initial_migration(tmp_path, capsys):
    source, sidecar, _legacy = _create_pair(tmp_path)
    source_before = source.read_bytes()
    sidecar_before = sidecar.read_bytes()

    assert migration.main(
        ["plan", "--db", str(source), "--sidecar", str(sidecar)]
    ) == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["action"] == "initial_migration"
    assert plan["changes_planned"] is True
    assert plan["source_journal_mode"] == "wal"
    assert plan["sidecar_journal_mode"] == "delete"
    assert source.read_bytes() == source_before
    assert sidecar.read_bytes() == sidecar_before


def test_apply_requires_explicit_yes_before_preflight_or_receipt(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    receipt = tmp_path / "receipt.json"
    source_before = source.read_bytes()
    sidecar_before = sidecar.read_bytes()

    with pytest.raises(migration.PortabilityMigrationError, match="explicit --yes"):
        migration.apply_migration(
            source, sidecar, yes=False, receipt_path=str(receipt)
        )

    assert not receipt.exists()
    assert source.read_bytes() == source_before
    assert sidecar.read_bytes() == sidecar_before


def test_initial_apply_writes_receipt_first_and_preserves_network_rows(
    tmp_path, monkeypatch
):
    source, sidecar, legacy = _create_pair(tmp_path)
    receipt = tmp_path / "receipt.json"
    counts_before = _counts(sidecar)
    original_phase_one = migration._create_or_resume_identity

    def asserting_phase_one(source_path, legacy_stamp):
        prepared = json.loads(receipt.read_text(encoding="utf-8"))
        assert prepared["status"] == "prepared"
        assert prepared["sidecar_counts_before"] == counts_before
        return original_phase_one(source_path, legacy_stamp)

    monkeypatch.setattr(
        migration, "_create_or_resume_identity", asserting_phase_one
    )

    assert _apply(source, sidecar, receipt) == receipt.absolute()

    identity = _read_identity(source)
    assert identity is not None
    assert identity.adopted_legacy_lineage_id == legacy["source_lineage_id"]
    assert identity.adopted_legacy_file_size == int(legacy["source_file_size"])
    assert identity.adopted_legacy_file_mtime_ns == int(
        legacy["source_file_mtime_ns"]
    )
    meta = _read_meta(sidecar)
    assert parse_portable_metadata(meta) is not None
    validate_portable_source(meta, source)
    assert meta["source_lineage_id"] == source_lineage_id(source)
    assert meta["source_file_size"] == str(source.stat().st_size)
    assert meta["source_file_mtime_ns"] == str(source.stat().st_mtime_ns)
    assert _counts(sidecar) == counts_before
    assert _risk_network.available(
        main_db_path=str(source),
        environ={
            "IRS_DB_PATH": str(source),
            "IRS_RISK_NETWORK_DB_PATH": str(sidecar),
        },
    )
    assert not Path(str(source) + "-wal").exists() or Path(
        str(source) + "-wal"
    ).stat().st_size == 0
    assert not Path(str(sidecar) + "-journal").exists()
    with closing(sqlite3.connect(source)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with closing(sqlite3.connect(sidecar)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    audit = json.loads(receipt.read_text(encoding="utf-8"))
    assert audit["status"] == "complete"
    assert audit["sidecar_counts_after"] == counts_before
    assert audit["identity_before"] is None
    assert "source_database_id" in audit["portable_meta_after"]


def test_resume_after_main_identity_phase_finishes_legacy_sidecar(tmp_path):
    source, sidecar, legacy = _create_pair(tmp_path)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        expected_identity = ensure_risk_source_identity(
            conn,
            adopted_legacy_lineage_id=legacy["source_lineage_id"],
            adopted_legacy_file_size=int(legacy["source_file_size"]),
            adopted_legacy_file_mtime_ns=int(legacy["source_file_mtime_ns"]),
        )
        conn.commit()
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    pre = migration.preflight(source, sidecar)
    assert pre.action == "resume_after_main_identity"
    receipt = tmp_path / "resume-receipt.json"
    _apply(source, sidecar, receipt)

    assert _read_identity(source) == expected_identity
    validate_portable_source(_read_meta(sidecar), source)
    assert json.loads(receipt.read_text(encoding="utf-8"))["action"] == (
        "resume_after_main_identity"
    )


def test_resume_refuses_identity_with_different_legacy_adoption(tmp_path):
    source, sidecar, legacy = _create_pair(tmp_path)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_risk_source_identity(
            conn,
            adopted_legacy_lineage_id="0" * 64,
            adopted_legacy_file_size=int(legacy["source_file_size"]),
            adopted_legacy_file_mtime_ns=int(legacy["source_file_mtime_ns"]),
        )
        conn.commit()
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    with pytest.raises(
        migration.PortabilityMigrationError, match="does not adopt"
    ):
        migration.preflight(source, sidecar)


def test_resume_refuses_revision_rotated_after_legacy_adoption(tmp_path):
    source, sidecar, legacy = _create_pair(tmp_path)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        adopted = ensure_risk_source_identity(
            conn,
            adopted_legacy_lineage_id=legacy["source_lineage_id"],
            adopted_legacy_file_size=int(legacy["source_file_size"]),
            adopted_legacy_file_mtime_ns=int(legacy["source_file_mtime_ns"]),
        )
        conn.commit()
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        conn.execute("BEGIN IMMEDIATE")
        rotated = rotate_risk_source_revision(conn)
        conn.commit()
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    assert rotated.revision_id != adopted.revision_id
    assert rotated.adopted_at_revision_id == adopted.revision_id
    with pytest.raises(
        migration.PortabilityMigrationError, match="revision changed"
    ):
        migration.preflight(source, sidecar)
    assert parse_portable_metadata(_read_meta(sidecar)) is None


def test_apply_is_idempotent_and_second_receipt_is_no_op(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    _apply(source, sidecar, tmp_path / "first.json")
    identity_before = _read_identity(source)
    source_before = source.read_bytes()
    sidecar_before = sidecar.read_bytes()

    second_receipt = tmp_path / "second.json"
    _apply(source, sidecar, second_receipt)

    assert _read_identity(source) == identity_before
    assert source.read_bytes() == source_before
    assert sidecar.read_bytes() == sidecar_before
    audit = json.loads(second_receipt.read_text(encoding="utf-8"))
    assert audit["status"] == "no_op"
    assert audit["action"] == "no_op"


def test_phase_two_refuses_sidecar_metadata_changed_after_preflight(
    tmp_path, monkeypatch
):
    source, sidecar, _legacy = _create_pair(tmp_path)
    receipt = tmp_path / "tamper.json"
    original_phase_one = migration._create_or_resume_identity

    def phase_one_then_tamper(source_path, legacy_stamp):
        identity = original_phase_one(source_path, legacy_stamp)
        with closing(sqlite3.connect(sidecar)) as conn:
            conn.execute(
                "UPDATE risk_network_build_meta SET value='tampered' "
                "WHERE key='build_scope'"
            )
            conn.commit()
        return identity

    monkeypatch.setattr(
        migration, "_create_or_resume_identity", phase_one_then_tamper
    )
    with pytest.raises(
        migration.PortabilityMigrationError, match="changed after preflight"
    ):
        _apply(source, sidecar, receipt)

    assert parse_portable_metadata(_read_meta(sidecar)) is None
    assert _counts(sidecar)["risk_network_edge"] == 1
    assert json.loads(receipt.read_text(encoding="utf-8"))["status"] == "failed"


def test_phase_two_refuses_same_count_edge_update_after_preflight(
    tmp_path, monkeypatch
):
    source, sidecar, _legacy = _create_pair(tmp_path)
    receipt = tmp_path / "edge-update.json"
    original_phase_one = migration._create_or_resume_identity

    def phase_one_then_update_edge(source_path, legacy_stamp):
        identity = original_phase_one(source_path, legacy_stamp)
        with closing(sqlite3.connect(sidecar)) as conn:
            conn.execute(
                "UPDATE risk_network_edge SET edge_id='E1-CHANGED' "
                "WHERE edge_id='E1'"
            )
            conn.commit()
        return identity

    monkeypatch.setattr(
        migration, "_create_or_resume_identity", phase_one_then_update_edge
    )
    with pytest.raises(
        migration.PortabilityMigrationError, match="physical file changed"
    ):
        _apply(source, sidecar, receipt)

    assert parse_portable_metadata(_read_meta(sidecar)) is None
    assert _counts(sidecar)["risk_network_edge"] == 1
    with closing(sqlite3.connect(sidecar)) as conn:
        assert conn.execute("SELECT edge_id FROM risk_network_edge").fetchone()[0] == (
            "E1-CHANGED"
        )
    audit = json.loads(receipt.read_text(encoding="utf-8"))
    assert audit["status"] == "failed"
    assert audit["sidecar_physical_guard_before"]["header_sha256"]


def test_phase_two_rolls_back_if_metadata_trigger_changes_network_rows(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    with closing(sqlite3.connect(sidecar)) as conn:
        conn.execute(
            """
            CREATE TRIGGER inject_edge_during_portability
            AFTER INSERT ON risk_network_build_meta
            WHEN NEW.key='source_identity_scheme'
            BEGIN
              INSERT INTO risk_network_edge VALUES ('E-INJECTED');
            END
            """
        )
        conn.commit()

    with pytest.raises(
        migration.PortabilityMigrationError,
        match="row counts changed inside metadata transaction",
    ):
        _apply(source, sidecar, tmp_path / "trigger.json")

    assert parse_portable_metadata(_read_meta(sidecar)) is None
    assert _counts(sidecar)["risk_network_edge"] == 1


def test_postcommit_validation_failure_invalidates_sidecar(tmp_path, monkeypatch):
    source, sidecar, _legacy = _create_pair(tmp_path)

    def fail_postcommit(*_args, **_kwargs):
        raise migration.PortabilityMigrationError("simulated postcommit failure")

    monkeypatch.setattr(migration, "_validate_completed_apply", fail_postcommit)
    with pytest.raises(
        migration.PortabilityMigrationError, match="simulated postcommit"
    ):
        _apply(source, sidecar, tmp_path / "postcommit.json")

    meta = _read_meta(sidecar)
    assert meta["build_status"] == "portability_migration_failed"
    assert not _risk_network.available(
        main_db_path=str(source),
        environ={
            "IRS_DB_PATH": str(source),
            "IRS_RISK_NETWORK_DB_PATH": str(sidecar),
        },
    )


def test_migrated_pair_is_valid_after_copy_to_new_paths_and_mtime(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    _apply(source, sidecar, tmp_path / "apply.json")
    copied = tmp_path / "linux-layout"
    copied.mkdir()
    copied_source = copied / "irs990.db"
    copied_sidecar = copied / "network.db"
    shutil.copyfile(source, copied_source)
    shutil.copyfile(sidecar, copied_sidecar)

    assert source_lineage_id(copied_source) != _read_meta(copied_sidecar)[
        "source_lineage_id"
    ]
    assert migration.preflight(copied_source, copied_sidecar).action == "no_op"
    assert _risk_network.available(
        main_db_path=str(copied_source),
        environ={
            "IRS_DB_PATH": str(copied_source),
            "IRS_RISK_NETWORK_DB_PATH": str(copied_sidecar),
        },
    )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("build_status", "building", "build_status"),
        ("schema_version", "2", "schema_version"),
        ("source_lineage_id", "0" * 64, "does not match"),
        ("source_file_size", "101", "does not match"),
        ("source_file_mtime_ns", "1", "does not match"),
    ],
)
def test_preflight_refuses_invalid_or_mismatched_legacy_meta(
    tmp_path, key, value, message
):
    source, sidecar, _legacy = _create_pair(tmp_path)
    with closing(sqlite3.connect(sidecar)) as conn:
        conn.execute(
            "UPDATE risk_network_build_meta SET value=? WHERE key=?", (value, key)
        )
        conn.commit()

    with pytest.raises(migration.PortabilityMigrationError, match=message):
        migration.preflight(source, sidecar)


def test_preflight_refuses_partial_portable_metadata(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    with closing(sqlite3.connect(sidecar)) as conn:
        conn.execute(
            "INSERT INTO risk_network_build_meta VALUES "
            "('source_identity_scheme','portable_v1')"
        )
        conn.commit()

    with pytest.raises(migration.PortabilityMigrationError, match="incomplete"):
        migration.preflight(source, sidecar)


@pytest.mark.parametrize(
    ("key", "value"),
    [("edge_count_written", "2"), ("selected_filing_count", "2")],
)
def test_preflight_refuses_declared_count_mismatch(tmp_path, key, value):
    source, sidecar, _legacy = _create_pair(tmp_path)
    with closing(sqlite3.connect(sidecar)) as conn:
        conn.execute(
            "UPDATE risk_network_build_meta SET value=? WHERE key=?", (value, key)
        )
        conn.commit()

    with pytest.raises(migration.PortabilityMigrationError, match="does not match"):
        migration.preflight(source, sidecar)


def test_preflight_refuses_portable_sidecar_without_main_identity(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    # Build a self-consistent portable stamp without installing its UUIDs in main.
    from risk_source_identity import sqlite_header_sha256

    size, header_hash = sqlite_header_sha256(source)
    portable = portable_source_stamp_from_values(
        str(uuid.uuid4()), str(uuid.uuid4()), size, header_hash
    )
    with closing(sqlite3.connect(sidecar)) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO risk_network_build_meta VALUES (?,?)",
            sorted(portable.metadata().items()),
        )
        conn.commit()

    with pytest.raises(migration.PortabilityMigrationError, match="no identity"):
        migration.preflight(source, sidecar)


@pytest.mark.parametrize(
    ("database", "suffix"),
    [
        ("source", "-wal"),
        ("source", "-journal"),
        ("sidecar", "-wal"),
        ("sidecar", "-journal"),
    ],
)
def test_preflight_refuses_populated_auxiliary_files(tmp_path, database, suffix):
    source, sidecar, _legacy = _create_pair(tmp_path)
    target = source if database == "source" else sidecar
    Path(str(target) + suffix).write_bytes(b"populated")

    with pytest.raises(migration.PortabilityMigrationError, match="populated"):
        migration.preflight(source, sidecar)


def test_apply_handles_wal_mode_sidecar_and_checkpoints_portable_metadata(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    with closing(sqlite3.connect(sidecar)) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    assert migration.preflight(source, sidecar).sidecar_journal_mode == "wal"
    _apply(source, sidecar, tmp_path / "wal-sidecar.json")

    assert not Path(str(sidecar) + "-wal").exists() or Path(
        str(sidecar) + "-wal"
    ).stat().st_size == 0
    with closing(sqlite3.connect(sidecar)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    validate_portable_source(_read_meta(sidecar), source)
    assert migration.preflight(source, sidecar).action == "no_op"


def test_preflight_refuses_same_file_hardlink_and_nonfile(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    with pytest.raises(migration.PortabilityMigrationError, match="distinct"):
        migration.preflight(source, source)

    hardlink = tmp_path / "hardlink.db"
    os.link(source, hardlink)
    with pytest.raises(migration.PortabilityMigrationError, match="distinct"):
        migration.preflight(source, hardlink)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(migration.PortabilityMigrationError, match="regular file"):
        migration.preflight(directory, sidecar)


def test_preflight_refuses_symlink(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    symlink = tmp_path / "linked.db"
    try:
        symlink.symlink_to(sidecar)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this Windows host")
    with pytest.raises(migration.PortabilityMigrationError, match="symlink"):
        migration.preflight(source, symlink)


def test_mark_changed_requires_yes_rotates_revision_and_invalidates_sidecar(tmp_path):
    source, sidecar, _legacy = _create_pair(tmp_path)
    _apply(source, sidecar, tmp_path / "apply.json")
    identity_before = _read_identity(source)
    assert identity_before is not None
    sidecar_meta = _read_meta(sidecar)
    refused_receipt = tmp_path / "refused.json"

    with pytest.raises(migration.PortabilityMigrationError, match="explicit --yes"):
        migration.mark_risk_source_changed(
            source, yes=False, receipt_path=str(refused_receipt)
        )
    assert not refused_receipt.exists()
    assert _read_identity(source) == identity_before

    receipt = tmp_path / "mark.json"
    migration.mark_risk_source_changed(
        source, yes=True, receipt_path=str(receipt)
    )
    identity_after = _read_identity(source)
    assert identity_after is not None
    assert identity_after.database_id == identity_before.database_id
    assert identity_after.revision_id != identity_before.revision_id
    with pytest.raises(RiskSourceIdentityError, match="stale"):
        validate_portable_source(sidecar_meta, source)
    assert not _risk_network.available(
        main_db_path=str(source),
        environ={
            "IRS_DB_PATH": str(source),
            "IRS_RISK_NETWORK_DB_PATH": str(sidecar),
        },
    )
    assert json.loads(receipt.read_text(encoding="utf-8"))["status"] == "complete"


def test_mark_changed_refuses_source_without_identity(tmp_path):
    source, _sidecar, _legacy = _create_pair(tmp_path)
    with pytest.raises(migration.PortabilityMigrationError, match="no portable"):
        migration.mark_risk_source_changed(
            source,
            yes=True,
            receipt_path=str(tmp_path / "unused.json"),
        )


def test_initialize_identity_requires_yes_then_is_idempotent(tmp_path):
    source, _sidecar, _legacy = _create_pair(tmp_path)
    refused = tmp_path / "initialize-refused.json"
    with pytest.raises(migration.PortabilityMigrationError, match="explicit --yes"):
        migration.initialize_risk_source_identity(
            source, yes=False, receipt_path=str(refused)
        )
    assert not refused.exists()
    assert _read_identity(source) is None

    first_receipt = tmp_path / "initialize.json"
    migration.initialize_risk_source_identity(
        source, yes=True, receipt_path=str(first_receipt)
    )
    identity = _read_identity(source)
    assert identity is not None
    assert identity.adopted_legacy_lineage_id is None
    assert identity.adopted_legacy_file_size is None
    assert identity.adopted_legacy_file_mtime_ns is None
    assert identity.adopted_at_revision_id is None
    assert json.loads(first_receipt.read_text(encoding="utf-8"))["status"] == (
        "complete"
    )
    source_before = source.read_bytes()

    second_receipt = tmp_path / "initialize-again.json"
    migration.initialize_risk_source_identity(
        source, yes=True, receipt_path=str(second_receipt)
    )
    assert source.read_bytes() == source_before
    assert _read_identity(source) == identity
    assert json.loads(second_receipt.read_text(encoding="utf-8"))["status"] == (
        "no_op"
    )


def test_initialize_identity_rejects_populated_wal(tmp_path):
    source, _sidecar, _legacy = _create_pair(tmp_path)
    Path(str(source) + "-wal").write_bytes(b"populated")
    with pytest.raises(migration.PortabilityMigrationError, match="populated"):
        migration.initialize_risk_source_identity(
            source,
            yes=True,
            receipt_path=str(tmp_path / "unused-initialize.json"),
        )


def test_default_receipt_is_under_gitignored_exports():
    path = migration._default_receipt_path("apply")
    assert path.parent.name == "exports"
    assert path.suffix == ".json"
