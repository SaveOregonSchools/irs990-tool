import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import grant_ai_assist_v1 as gai


def candidate_args(main_db: Path, work_db: Path, *, mode: str, full_refresh: bool):
    return SimpleNamespace(
        db=str(main_db),
        work_db=str(work_db),
        full_refresh=full_refresh,
        regenerate=False,
        state=None,
        min_total_amount=None,
        queue_status=None if mode == "fast" else "no_candidates",
        limit=None,
        max_candidates=20,
        min_candidate_score=45.0,
        candidate_mode=mode,
        enough_candidates=8,
        token_limit=50,
        no_fts=False,
        commit_every=1,
        status_update_every=1,
    )


def create_candidate_fixture(root: Path, signature_count: int = 2):
    main_db = root / "main.db"
    work_db = root / "work.db"
    sqlite3.connect(main_db).close()
    gai.configure_grant_work_sidecar(str(main_db), str(work_db))
    conn = gai.connect(str(main_db), readonly=False)
    conn.execute(
        f"CREATE TABLE {gai.ORG_IDENTITY_TABLE} (identity_id INTEGER PRIMARY KEY)"
    )
    gai.create_signature_schema(conn, full_refresh=True)
    for index in range(signature_count):
        conn.execute(
            f"""
            INSERT INTO {gai.SIG_TABLE} (
              signature_hash, reported_ein, recipient_name, recipient_name_norm,
              street_norm, city, state, zip5, country, grant_count, total_amount,
              sample_grantor_ein, candidate_count, ai_queue_status,
              candidate_generation_phase
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"SIG_{index:03d}",
                "",
                f"Fixture {index}",
                f"FIXTURE {index}",
                "",
                "",
                "CA",
                "",
                "US",
                1,
                1000 - index,
                "999999990",
                0,
                "new",
                "pending",
            ),
        )
    conn.commit()
    conn.close()
    return main_db, work_db


class CandidateGenerationProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.saved_globals = {
            name: getattr(gai, name)
            for name in (
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
        }
        self.original_identity_lookup = gai.get_candidate_identity_rows

    def tearDown(self):
        gai.get_candidate_identity_rows = self.original_identity_lookup
        for name, value in self.saved_globals.items():
            setattr(gai, name, value)

    def test_completed_fast_and_balanced_runs_certify_every_zero_result_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            main_db, work_db = create_candidate_fixture(Path(tmp), signature_count=2)
            gai.get_candidate_identity_rows = lambda *args, **kwargs: []

            gai.cmd_generate_candidates(
                candidate_args(main_db, work_db, mode="fast", full_refresh=True)
            )
            gai.cmd_generate_candidates(
                candidate_args(main_db, work_db, mode="balanced", full_refresh=False)
            )

            conn = gai.connect(str(main_db), readonly=True)
            runs = list(
                conn.execute(
                    f"SELECT workflow_phase, run_status, selected_count, "
                    f"processed_count FROM {gai.CAND_RUN_TABLE} ORDER BY run_seq"
                )
            )
            self.assertEqual(
                [tuple(row) for row in runs],
                [
                    ("fast_full", "completed", 2, 2),
                    ("balanced_no_candidates", "completed", 2, 2),
                ],
            )
            signatures = list(
                conn.execute(
                    f"SELECT candidate_count, ai_queue_status, "
                    f"candidate_generation_phase, candidate_generation_version "
                    f"FROM {gai.SIG_TABLE} ORDER BY signature_hash"
                )
            )
            self.assertEqual(
                [tuple(row) for row in signatures],
                [
                    (0, "no_candidates", "balanced", gai.CANDIDATE_GENERATION_VERSION),
                    (0, "no_candidates", "balanced", gai.CANDIDATE_GENERATION_VERSION),
                ],
            )
            fast, balanced = gai._require_candidate_generation_run_chain(conn)
            gai._require_candidate_generation_consistency(conn, fast, balanced)
            conn.close()

    def test_generator_bootstraps_provenance_columns_on_legacy_signature_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            main_db, work_db = create_candidate_fixture(Path(tmp), signature_count=1)
            conn = gai.connect(str(main_db), readonly=False)
            for column in gai.CANDIDATE_PROVENANCE_COLUMNS:
                conn.execute(f"ALTER TABLE {gai.SIG_TABLE} DROP COLUMN {column}")
            conn.commit()

            gai._ensure_candidate_generation_provenance_schema(conn)

            columns = gai._table_columns(conn, gai.SIG_TABLE)
            self.assertTrue(set(gai.CANDIDATE_PROVENANCE_COLUMNS) <= columns)
            row = conn.execute(
                f"SELECT candidate_generation_phase, "
                f"candidate_generation_run_id, candidate_generation_version "
                f"FROM {gai.SIG_TABLE}"
            ).fetchone()
            self.assertEqual(tuple(row), ("pending", None, None))
            self.assertTrue(gai.table_exists(conn, gai.CAND_RUN_TABLE))
            conn.close()

    def test_standard_scope_and_run_iteration_plans_do_not_materialize_or_sort(self):
        with tempfile.TemporaryDirectory() as tmp:
            main_db, work_db = create_candidate_fixture(Path(tmp), signature_count=2)
            conn = gai.connect(str(main_db), readonly=False)
            gai.create_candidate_schema(conn, full_refresh=False)
            for args in (
                candidate_args(main_db, work_db, mode="fast", full_refresh=True),
                candidate_args(main_db, work_db, mode="balanced", full_refresh=False),
            ):
                sql, params = gai._candidate_scope_update_statement(args, "RUN_PLAN")
                details = [
                    str(row[3]).upper()
                    for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
                ]
                self.assertFalse(
                    any(
                        "TEMP B-TREE" in detail or "LIST SUBQUERY" in detail
                        for detail in details
                    ),
                    details,
                )
            for after in (None, "SIG_000"):
                sql, params = gai._candidate_run_batch_statement(
                    "RUN_PLAN",
                    after,
                    5000,
                )
                details = [
                    str(row[3]).upper()
                    for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
                ]
                self.assertFalse(
                    any("TEMP B-TREE" in detail for detail in details),
                    details,
                )
                self.assertTrue(any("INDEX" in detail for detail in details), details)
            conn.close()

    def test_interrupted_run_cannot_certify_and_requires_new_full_fast_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            main_db, work_db = create_candidate_fixture(Path(tmp), signature_count=2)
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("fixture interruption")
                return []

            gai.get_candidate_identity_rows = fail_second
            with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
                gai.cmd_generate_candidates(
                    candidate_args(main_db, work_db, mode="fast", full_refresh=True)
                )

            conn = gai.connect(str(main_db), readonly=True)
            failed = conn.execute(
                f"SELECT run_status, selected_count, processed_count "
                f"FROM {gai.CAND_RUN_TABLE} ORDER BY run_seq DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(tuple(failed), ("failed", 2, 1))
            with self.assertRaisesRegex(RuntimeError, "Latest candidate-generation run"):
                gai._require_candidate_generation_run_chain(conn)
            conn.close()

            gai.get_candidate_identity_rows = lambda *args, **kwargs: []
            with self.assertRaisesRegex(RuntimeError, "Rerun fast with --full-refresh"):
                gai.cmd_generate_candidates(
                    candidate_args(main_db, work_db, mode="balanced", full_refresh=False)
                )

            gai.cmd_generate_candidates(
                candidate_args(main_db, work_db, mode="fast", full_refresh=True)
            )
            gai.cmd_generate_candidates(
                candidate_args(main_db, work_db, mode="balanced", full_refresh=False)
            )
            conn = gai.connect(str(main_db), readonly=True)
            fast, balanced = gai._require_candidate_generation_run_chain(conn)
            gai._require_candidate_generation_consistency(conn, fast, balanced)
            conn.close()


if __name__ == "__main__":
    unittest.main()
