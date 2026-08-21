import argparse
import sqlite3
from pathlib import Path

import pytest

import grant_ai_assist_v1 as grant_ai


@pytest.fixture(autouse=True)
def _restore_grant_work_configuration():
    names = (
        "GRANT_WORK_DB_PATH",
        "GRANT_WORK_SIDECAR_ENABLED",
        "ORG_IDENTITY_TABLE",
        "ORG_TOKEN_TABLE",
        "ORG_IDENTITY_FTS_TABLE",
        "SIG_TABLE",
        "SIG_GRANT_TABLE",
        "CAND_TABLE",
        "CAND_RUN_TABLE",
    )
    prior = {name: getattr(grant_ai, name) for name in names}
    try:
        yield
    finally:
        for name, value in prior.items():
            setattr(grant_ai, name, value)


def _build_main(path: Path, *, include_resolved: bool = True) -> None:
    current_candidate_hash = grant_ai.candidate_set_fingerprint(
        [
            {
                "candidate_id": "cand:1",
                "ein": "111111111",
                "candidate_score": 95,
            }
        ]
    )
    conn = sqlite3.connect(path)
    try:
        if include_resolved:
            conn.executescript(
                """
                CREATE TABLE grant_recipient_resolved (
                  grant_id INTEGER PRIMARY KEY,
                  resolved_ein TEXT,
                  resolved_org_name TEXT,
                  confidence NUMERIC
                );
                INSERT INTO grant_recipient_resolved VALUES
                  (1, NULL, NULL, 0.0),
                  (900, '900000000', 'Old deterministic row', 0.9);
                """
            )
        conn.executescript(
            """
            CREATE TABLE grant_recipient_ai_decision (
              signature_hash TEXT PRIMARY KEY,
              decision TEXT,
              selected_candidate_id TEXT,
              selected_ein TEXT,
              selected_name TEXT,
              confidence NUMERIC,
              auto_accept INTEGER,
              validation_status TEXT,
              model TEXT,
              candidate_set_hash TEXT
            );

            CREATE TABLE grant_recipient_ai_applied (
              grant_id INTEGER PRIMARY KEY,
              signature_hash TEXT NOT NULL,
              selected_ein TEXT NOT NULL,
              selected_name TEXT,
              ai_confidence NUMERIC,
              ai_decision TEXT,
              model TEXT,
              applied_at TEXT
            );
            INSERT INTO grant_recipient_ai_applied VALUES
              (900, 'SIG_OLD', '900000000', 'Old visible recipient', 0.9,
               'SELECT_CANDIDATE', 'old-model', 'old-timestamp');

            CREATE VIEW grant_recipient_resolved_plus_ai_v1 AS
            SELECT grant_id, selected_ein AS final_resolved_ein
            FROM grant_recipient_ai_applied;
            """
        )
        conn.execute(
            "INSERT INTO grant_recipient_ai_decision VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "SIG_CURRENT",
                "SELECT_CANDIDATE",
                "cand:1",
                "111111111",
                "Current recipient",
                0.97,
                1,
                "ok",
                "reviewed-model",
                current_candidate_hash,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _build_work(path: Path, *, include_mapping: bool = True) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE grant_recipient_signature (
              signature_hash TEXT PRIMARY KEY,
              reported_ein TEXT,
              ai_queue_status TEXT,
              updated_at TEXT
            );
            INSERT INTO grant_recipient_signature VALUES
              ('SIG_CURRENT', '', 'adjudicated', 'current-timestamp');

            CREATE TABLE grant_recipient_ai_candidate (
              signature_hash TEXT NOT NULL,
              candidate_id TEXT NOT NULL,
              candidate_rank INTEGER NOT NULL,
              ein TEXT NOT NULL,
              candidate_score NUMERIC,
              PRIMARY KEY (signature_hash, candidate_id)
            );
            INSERT INTO grant_recipient_ai_candidate VALUES
              ('SIG_CURRENT', 'cand:1', 1, '111111111', 95);
            CREATE INDEX idx_fixture_candidate_sig_rank
              ON grant_recipient_ai_candidate(signature_hash, candidate_rank);
            """
        )
        if include_mapping:
            conn.executescript(
                """
                CREATE TABLE grant_recipient_signature_grant (
                  signature_hash TEXT NOT NULL,
                  grant_id INTEGER NOT NULL,
                  PRIMARY KEY (signature_hash, grant_id)
                );
                INSERT INTO grant_recipient_signature_grant VALUES ('SIG_CURRENT', 1);
                """
            )
        conn.commit()
    finally:
        conn.close()


def _args(main: Path, work: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db=str(main),
        work_db=str(work),
        full_refresh=True,
        min_confidence=0.0,
        batch_size=1,
    )


def _visible_layer(path: Path):
    conn = sqlite3.connect(path)
    try:
        applied = conn.execute(
            "SELECT grant_id,signature_hash,selected_ein,selected_name,model,applied_at "
            "FROM grant_recipient_ai_applied ORDER BY grant_id"
        ).fetchall()
        view_rows = conn.execute(
            "SELECT grant_id,final_resolved_ein "
            "FROM grant_recipient_resolved_plus_ai_v1 ORDER BY grant_id"
        ).fetchall()
        return applied, view_rows
    finally:
        conn.close()


def test_apply_full_refresh_refuses_typo_without_creating_work_db(tmp_path):
    main = tmp_path / "main.db"
    missing_work = tmp_path / "grant-work-typo.db"
    _build_main(main)
    before = _visible_layer(main)

    with pytest.raises(FileNotFoundError, match="Grant work database file does not exist"):
        grant_ai.cmd_apply_decisions(_args(main, missing_work))

    assert not missing_work.exists()
    assert _visible_layer(main) == before


def test_apply_full_refresh_refuses_non_file_work_path(tmp_path):
    main = tmp_path / "main.db"
    work_directory = tmp_path / "work-directory"
    work_directory.mkdir()
    _build_main(main)
    before = _visible_layer(main)

    with pytest.raises(RuntimeError, match="Grant work database path is not a regular file"):
        grant_ai.cmd_apply_decisions(_args(main, work_directory))

    assert _visible_layer(main) == before


def test_apply_full_refresh_checks_work_objects_before_touching_visible_layer(tmp_path):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main)
    _build_work(work, include_mapping=False)
    before = _visible_layer(main)

    with pytest.raises(RuntimeError, match="grant_recipient_signature_grant"):
        grant_ai.cmd_apply_decisions(_args(main, work))

    assert _visible_layer(main) == before


def test_apply_full_refresh_checks_main_objects_before_touching_visible_layer(tmp_path):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main, include_resolved=False)
    _build_work(work)
    before = _visible_layer(main)

    with pytest.raises(RuntimeError, match="grant_recipient_resolved"):
        grant_ai.cmd_apply_decisions(_args(main, work))

    assert _visible_layer(main) == before


def test_apply_refuses_incremental_mode_before_touching_visible_layer(tmp_path):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main)
    _build_work(work)
    before = _visible_layer(main)
    args = _args(main, work)
    args.full_refresh = False

    with pytest.raises(RuntimeError, match="requires --full-refresh.*stale or revoked"):
        grant_ai.cmd_apply_decisions(args)

    assert _visible_layer(main) == before


def test_apply_parser_requires_explicit_full_refresh_flag():
    parser = grant_ai.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["apply-decisions"])


def test_apply_rejects_stale_candidate_set_before_touching_visible_layer(tmp_path):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main)
    _build_work(work)
    before = _visible_layer(main)
    conn = sqlite3.connect(work)
    try:
        conn.execute(
            "INSERT INTO grant_recipient_ai_candidate VALUES (?,?,?,?,?)",
            ("SIG_CURRENT", "cand:2", 2, "222222222", 94),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="stale/invalid candidate_set_hash"):
        grant_ai.cmd_apply_decisions(_args(main, work))

    assert _visible_layer(main) == before
    conn = sqlite3.connect(main)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?",
            (grant_ai.APPLIED_STAGING_TABLE,),
        ).fetchone() is None
    finally:
        conn.close()


def test_apply_allows_only_audited_legacy_nonselection_candidate_prefix(tmp_path):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main)
    _build_work(work)
    prefix_hash = grant_ai.candidate_set_fingerprint(
        [{"candidate_id": "C1", "ein": "222222222", "candidate_score": 91}]
    )

    conn = sqlite3.connect(main)
    try:
        conn.execute(
            "INSERT INTO grant_recipient_resolved VALUES (?,?,?,?)",
            (2, None, None, 0.0),
        )
        conn.execute(
            "INSERT INTO grant_recipient_ai_decision VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "SIG_LEGACY_REVIEW",
                "HUMAN_REVIEW",
                "",
                "",
                "",
                0.0,
                0,
                "ok",
                "rule:reported_ein_no_ai_review",
                prefix_hash,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(work)
    try:
        conn.execute(
            "INSERT INTO grant_recipient_signature VALUES (?,?,?,?)",
            ("SIG_LEGACY_REVIEW", "222222222", "adjudicated", "legacy"),
        )
        conn.execute(
            "INSERT INTO grant_recipient_signature_grant VALUES (?,?)",
            ("SIG_LEGACY_REVIEW", 2),
        )
        conn.executemany(
            "INSERT INTO grant_recipient_ai_candidate VALUES (?,?,?,?,?)",
            [
                ("SIG_LEGACY_REVIEW", "C1", 1, "222222222", 91),
                ("SIG_LEGACY_REVIEW", "C2", 2, "333333333", 88),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    grant_ai.cmd_apply_decisions(_args(main, work))

    conn = sqlite3.connect(main)
    try:
        assert conn.execute(
            "SELECT signature_hash FROM grant_recipient_ai_applied ORDER BY signature_hash"
        ).fetchall() == [("SIG_CURRENT",)]
    finally:
        conn.close()


def test_apply_rejects_candidate_prefix_for_other_nonselection_models(tmp_path):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main)
    _build_work(work)
    before = _visible_layer(main)
    conn = sqlite3.connect(main)
    try:
        conn.execute(
            """
            UPDATE grant_recipient_ai_decision
            SET decision='HUMAN_REVIEW', selected_candidate_id='', selected_ein='',
                selected_name='', confidence=0, auto_accept=0,
                model='rule:candidate_evidence'
            WHERE signature_hash='SIG_CURRENT'
            """
        )
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(work)
    try:
        conn.execute(
            "INSERT INTO grant_recipient_ai_candidate VALUES (?,?,?,?,?)",
            ("SIG_CURRENT", "cand:2", 2, "222222222", 94),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="stale/invalid candidate_set_hash"):
        grant_ai.cmd_apply_decisions(_args(main, work))

    assert _visible_layer(main) == before


def test_apply_candidate_freshness_scans_are_index_ordered_without_temp_sort(tmp_path):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main)
    _build_work(work)
    grant_ai.configure_grant_work_sidecar(str(main), str(work))
    conn = grant_ai.connect(str(main), readonly=True)
    try:
        for sql in (
            grant_ai._apply_decision_freshness_sql(),
            grant_ai._apply_candidate_freshness_sql(),
        ):
            details = [
                str(row[3]).upper()
                for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
            ]
            assert not any("TEMP B-TREE" in detail for detail in details), details
            assert any("INDEX" in detail for detail in details), details
    finally:
        conn.close()


def test_apply_min_confidence_is_inclusive_and_full_refresh_removes_excluded_rows(tmp_path):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main)
    _build_work(work)
    args = _args(main, work)
    args.min_confidence = 0.97

    grant_ai.cmd_apply_decisions(args)

    assert [row[0] for row in _visible_layer(main)[0]] == [1]

    args.min_confidence = 0.98

    grant_ai.cmd_apply_decisions(args)

    applied, view_rows = _visible_layer(main)
    assert applied == []
    assert view_rows == [(1, None), (900, "900000000")]


def test_apply_full_refresh_rolls_back_visible_swap_on_view_failure(tmp_path, monkeypatch):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main)
    _build_work(work)
    before = _visible_layer(main)

    def fail_view_creation(_conn):
        raise RuntimeError("forced final-view failure")

    monkeypatch.setattr(grant_ai, "_create_final_view", fail_view_creation)
    with pytest.raises(RuntimeError, match="forced final-view failure"):
        grant_ai.cmd_apply_decisions(_args(main, work))

    assert _visible_layer(main) == before


def test_apply_full_refresh_atomically_installs_staged_layer(tmp_path):
    main = tmp_path / "main.db"
    work = tmp_path / "work.db"
    _build_main(main)
    _build_work(work)

    grant_ai.cmd_apply_decisions(_args(main, work))

    conn = sqlite3.connect(main)
    try:
        assert conn.execute(
            "SELECT grant_id,signature_hash,selected_ein,selected_name,ai_confidence,"
            "ai_decision,model FROM grant_recipient_ai_applied"
        ).fetchall() == [
            (1, "SIG_CURRENT", "111111111", "Current recipient", 0.97, "SELECT_CANDIDATE", "reviewed-model")
        ]
        assert conn.execute(
            "SELECT grant_id,final_resolved_ein,final_match_source "
            "FROM grant_recipient_resolved_plus_ai_v1 ORDER BY grant_id"
        ).fetchall() == [
            (1, "111111111", "ai_assisted"),
            (900, "900000000", "deterministic"),
        ]
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?",
            (grant_ai.APPLIED_STAGING_TABLE,),
        ).fetchone() is None

        leading_columns = set()
        for index_row in conn.execute("PRAGMA index_list(grant_recipient_ai_applied)"):
            index_name = index_row[1].replace("'", "''")
            columns = conn.execute(f"PRAGMA index_info('{index_name}')").fetchall()
            if columns:
                leading_columns.add(columns[0][2])
        assert {"selected_ein", "signature_hash"}.issubset(leading_columns)
    finally:
        conn.close()
