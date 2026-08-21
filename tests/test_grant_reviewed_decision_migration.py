import csv
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import grant_ai_assist_v1 as gai


TARGET_EIN = "472772048"


def create_empty_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker(value TEXT)")
    conn.execute("CREATE TABLE grant_recipient_resolved(grant_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


def create_work_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE grant_recipient_signature (
          signature_hash TEXT PRIMARY KEY,
          reported_ein TEXT,
          recipient_name TEXT,
          recipient_name_norm TEXT,
          street_norm TEXT,
          city TEXT,
          state TEXT,
          zip5 TEXT,
          country TEXT,
          sample_grantor_ein TEXT,
          candidate_count INTEGER,
          ai_queue_status TEXT,
          candidate_generation_phase TEXT,
          candidate_generation_run_id TEXT,
          candidate_generation_version TEXT,
          updated_at TEXT
        );
        CREATE TABLE grant_recipient_ai_candidate (
          signature_hash TEXT NOT NULL,
          candidate_id TEXT NOT NULL,
          candidate_rank INTEGER NOT NULL,
          ein TEXT NOT NULL,
          candidate_name TEXT,
          candidate_score NUMERIC,
          PRIMARY KEY(signature_hash, candidate_id)
        );
        CREATE TABLE grant_recipient_signature_grant (
          signature_hash TEXT NOT NULL,
          grant_id INTEGER NOT NULL,
          PRIMARY KEY(signature_hash, grant_id)
        );
        """
    )
    conn.commit()
    conn.close()


def add_signature(
    target_db: Path,
    work_db: Path,
    signature_hash: str,
    *,
    reported_ein: str = "",
    candidate_id: str = "C1",
    candidate_ein: str = TARGET_EIN,
    candidate_score: int = 95,
) -> None:
    conn = sqlite3.connect(work_db)
    conn.execute(
        """
        INSERT INTO grant_recipient_signature (
          signature_hash, reported_ein, recipient_name, recipient_name_norm,
          street_norm, city, state, zip5, country, sample_grantor_ein,
          candidate_count, ai_queue_status, candidate_generation_phase,
          candidate_generation_run_id, candidate_generation_version, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            signature_hash,
            reported_ein,
            "Learning Policy Institute",
            "LEARNING POLICY INSTITUTE",
            "",
            "",
            "",
            "",
            "US",
            "999999990",
            1,
            "candidates_ready",
            "pending",
            None,
            None,
            "fixture",
        ),
    )
    conn.execute(
        "INSERT INTO grant_recipient_ai_candidate VALUES (?,?,?,?,?,?)",
        (signature_hash, candidate_id, 1, candidate_ein, "Learning Policy Institute", candidate_score),
    )
    grant_id = conn.execute("SELECT COALESCE(MAX(grant_id),0)+1 FROM grant_recipient_signature_grant").fetchone()[0]
    conn.execute("INSERT INTO grant_recipient_signature_grant VALUES (?,?)", (signature_hash, grant_id))
    conn.commit()
    conn.close()
    target = sqlite3.connect(target_db)
    target.execute("INSERT INTO grant_recipient_resolved(grant_id) VALUES (?)", (grant_id,))
    target.commit()
    target.close()


def add_signature_without_candidate(
    target_db: Path,
    work_db: Path,
    signature_hash: str,
    *,
    candidate_count=0,
    queue_status: str = "no_candidates",
) -> None:
    conn = sqlite3.connect(work_db)
    conn.execute(
        """
        INSERT INTO grant_recipient_signature (
          signature_hash, reported_ein, recipient_name, recipient_name_norm,
          street_norm, city, state, zip5, country, sample_grantor_ein,
          candidate_count, ai_queue_status, candidate_generation_phase,
          candidate_generation_run_id, candidate_generation_version, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            signature_hash,
            "",
            "No Candidate Fixture",
            "NO CANDIDATE FIXTURE",
            "",
            "",
            "",
            "",
            "US",
            "999999990",
            candidate_count,
            queue_status,
            "pending",
            None,
            None,
            "fixture",
        ),
    )
    grant_id = conn.execute(
        "SELECT COALESCE(MAX(grant_id),0)+1 FROM grant_recipient_signature_grant"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO grant_recipient_signature_grant VALUES (?,?)",
        (signature_hash, grant_id),
    )
    conn.commit()
    conn.close()
    target = sqlite3.connect(target_db)
    target.execute(
        "INSERT INTO grant_recipient_resolved(grant_id) VALUES (?)",
        (grant_id,),
    )
    target.commit()
    target.close()


def certify_candidate_runs(target_db: Path, work_db: Path) -> None:
    """Create a tiny valid fast->balanced provenance chain for migration fixtures."""
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS grant_work", (str(work_db),))
    gai._ensure_candidate_generation_provenance_schema(conn)
    signature_count, signature_fingerprint = gai.candidate_signature_population_fingerprint(conn)
    defaults = dict(
        regenerate=False,
        state=None,
        min_total_amount=None,
        limit=None,
        max_candidates=20,
        min_candidate_score=45.0,
        enough_candidates=8,
        token_limit=50,
        no_fts=False,
    )
    fast_args = SimpleNamespace(
        **defaults,
        full_refresh=True,
        queue_status=None,
        candidate_mode="fast",
    )
    balanced_args = SimpleNamespace(
        **defaults,
        full_refresh=False,
        queue_status="no_candidates",
        candidate_mode="balanced",
    )
    fast_scope, fast_config, fast_config_hash = gai._candidate_run_config(fast_args)
    balanced_scope, balanced_config, balanced_config_hash = gai._candidate_run_config(
        balanced_args
    )
    no_candidate_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {gai.SIG_TABLE} s "
            f"WHERE NOT EXISTS (SELECT 1 FROM {gai.CAND_TABLE} c "
            "WHERE c.signature_hash=s.signature_hash)"
        ).fetchone()[0]
    )
    with_candidate_count = signature_count - no_candidate_count
    conn.execute(f"DELETE FROM {gai.CAND_RUN_TABLE}")
    run_columns = (
        "run_id, generator_version, workflow_phase, candidate_mode, run_status, "
        "full_refresh, regenerate, scope_json, config_json, config_fingerprint, "
        "signature_count, signature_fingerprint, selected_count, processed_count, "
        "with_candidates_count, base_fast_run_id, started_at, completed_at, error_message"
    )
    conn.execute(
        f"INSERT INTO {gai.CAND_RUN_TABLE} ({run_columns}) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "RUN_FAST",
            gai.CANDIDATE_GENERATION_VERSION,
            "fast_full",
            "fast",
            "completed",
            1,
            0,
            fast_scope,
            fast_config,
            fast_config_hash,
            signature_count,
            signature_fingerprint,
            signature_count,
            signature_count,
            with_candidate_count,
            None,
            "fixture",
            "fixture",
            None,
        ),
    )
    conn.execute(
        f"INSERT INTO {gai.CAND_RUN_TABLE} ({run_columns}) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "RUN_BALANCED",
            gai.CANDIDATE_GENERATION_VERSION,
            "balanced_no_candidates",
            "balanced",
            "completed",
            0,
            0,
            balanced_scope,
            balanced_config,
            balanced_config_hash,
            signature_count,
            signature_fingerprint,
            no_candidate_count,
            no_candidate_count,
            0,
            "RUN_FAST",
            "fixture",
            "fixture",
            None,
        ),
    )
    conn.execute(
        f"""
        UPDATE {gai.SIG_TABLE}
        SET candidate_generation_phase=CASE
              WHEN EXISTS (
                SELECT 1 FROM {gai.CAND_TABLE} c
                WHERE c.signature_hash={gai.SIG_BASE}.signature_hash
              ) THEN 'fast' ELSE 'balanced' END,
            candidate_generation_run_id=CASE
              WHEN EXISTS (
                SELECT 1 FROM {gai.CAND_TABLE} c
                WHERE c.signature_hash={gai.SIG_BASE}.signature_hash
              ) THEN 'RUN_FAST' ELSE 'RUN_BALANCED' END,
            candidate_generation_version=?
        """,
        (gai.CANDIDATE_GENERATION_VERSION,),
    )
    conn.commit()
    conn.close()


def create_source_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    gai.create_decision_schema(conn)
    conn.close()


def decision_tuple(
    signature_hash: str,
    *,
    decision: str = "SELECT_CANDIDATE",
    candidate_id: str = "C1",
    selected_ein: str = TARGET_EIN,
    candidate_ein: str = TARGET_EIN,
    candidate_score: int = 95,
    model: str = "external:test-model",
    auto_accept: int = 1,
    validation_status: str = "ok",
    output_override=None,
):
    source_candidates = [
        {"candidate_id": candidate_id, "ein": candidate_ein, "candidate_score": candidate_score}
    ]
    if decision in {"NO_MATCH", "AMBIGUOUS", "HUMAN_REVIEW"}:
        candidate_id = ""
        selected_ein = ""
        auto_accept = 0
    output = output_override or {
        "decision": decision,
        "candidate_id": candidate_id,
        "confidence": 0.95 if auto_accept else 0.0,
        "confidence_label": "high" if auto_accept else "none",
        "reason_codes": ["fixture"],
        "explanation": "Fixture reviewed decision.",
        "needs_human_review": decision in {"AMBIGUOUS", "HUMAN_REVIEW"},
    }
    input_obj = {
        "grant_recipient_signature": {"signature_hash": signature_hash},
        "candidates": source_candidates,
    }
    input_json = json.dumps(input_obj, sort_keys=True)
    output_json = json.dumps(output, sort_keys=True)
    return (
        signature_hash,
        decision,
        candidate_id,
        selected_ein,
        "Original reviewed name",
        0.95 if auto_accept else 0.0,
        "high" if auto_accept else "none",
        json.dumps(["fixture"]),
        "Fixture reviewed decision.",
        1 if decision in {"AMBIGUOUS", "HUMAN_REVIEW"} else 0,
        auto_accept,
        validation_status,
        "" if validation_status == "ok" else "source_invalid",
        model,
        json.dumps({"num_ctx": 8192, "temperature": 0.0}, sort_keys=True),
        gai.stable_hash([input_json], "PROMPT_"),
        gai.candidate_set_fingerprint(source_candidates),
        input_json,
        output_json,
        "2026-07-01 12:34:56",
    )


def insert_source_decision(source_db: Path, row) -> None:
    conn = sqlite3.connect(source_db)
    placeholders = ",".join("?" for _ in gai.DECISION_COLUMNS)
    conn.execute(
        f"INSERT INTO grant_recipient_ai_decision ({','.join(gai.DECISION_COLUMNS)}) VALUES ({placeholders})",
        row,
    )
    conn.commit()
    conn.close()


def migration_args(
    root: Path,
    source_db: Path,
    target_db: Path,
    work_db: Path,
    *,
    apply: bool = False,
    replace_existing_reviewed: bool = False,
    suffix: str = "run",
):
    return SimpleNamespace(
        source_db=str(source_db),
        db=str(target_db),
        work_db=str(work_db),
        apply=apply,
        audit_csv=str(root / f"audit-{suffix}.csv"),
        quarantine_jsonl=str(root / f"quarantine-{suffix}.jsonl"),
        overwrite_audit=False,
        replace_existing_reviewed=replace_existing_reviewed,
        batch_size=1,
        max_candidates=20,
        progress_every=0,
    )


class ReviewedDecisionMigrationTests(unittest.TestCase):
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

    def tearDown(self):
        for name, value in self.saved_globals.items():
            setattr(gai, name, value)

    def configure(
        self,
        target_db: Path,
        work_db: Path,
        *,
        certify: bool = True,
    ) -> None:
        gai.configure_grant_work_sidecar(str(target_db), str(work_db))
        if certify:
            certify_candidate_runs(target_db, work_db)

    def test_dry_run_is_read_only_and_excludes_rule_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            insert_source_decision(source_db, decision_tuple("SIG_RULE", model="rule:candidate_evidence"))
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db)
            source_mtime = source_db.stat().st_mtime_ns
            target_mtime = target_db.stat().st_mtime_ns
            work_mtime = work_db.stat().st_mtime_ns

            gai.cmd_migrate_reviewed_decisions(args)

            target = sqlite3.connect(target_db)
            self.assertIsNone(
                target.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='grant_recipient_ai_decision'"
                ).fetchone()
            )
            target.close()
            self.assertEqual(source_db.stat().st_mtime_ns, source_mtime)
            self.assertEqual(target_db.stat().st_mtime_ns, target_mtime)
            self.assertEqual(work_db.stat().st_mtime_ns, work_mtime)
            with Path(args.audit_csv).open(newline="", encoding="utf-8-sig") as fh:
                audit_rows = list(csv.DictReader(fh))
            self.assertEqual(len(audit_rows), 1)
            self.assertEqual(audit_rows[0]["signature_hash"], "SIG_VALID")
            self.assertEqual(audit_rows[0]["status"], "eligible")
            self.assertEqual(Path(args.quarantine_jsonl).read_text(encoding="utf-8"), "")

    def test_blank_model_is_quarantined_as_insufficient_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_BLANK_MODEL")
            insert_source_decision(source_db, decision_tuple("SIG_BLANK_MODEL", model=""))
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, suffix="blank-model")

            gai.cmd_migrate_reviewed_decisions(args)

            with Path(args.audit_csv).open(newline="", encoding="utf-8-sig") as fh:
                audit = next(csv.DictReader(fh))
            self.assertEqual(audit["status"], "quarantined")
            self.assertIn("missing_source_model_provenance", audit["reasons"])

    def test_apply_preserves_provenance_and_updates_work_queue_transactionally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            source_row = decision_tuple("SIG_VALID")
            insert_source_decision(source_db, source_row)
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, apply=True)

            gai.cmd_migrate_reviewed_decisions(args)

            target = sqlite3.connect(target_db)
            target.row_factory = sqlite3.Row
            migrated = target.execute("SELECT * FROM grant_recipient_ai_decision").fetchone()
            target.close()
            self.assertEqual(migrated["signature_hash"], "SIG_VALID")
            self.assertEqual(migrated["selected_candidate_id"], "C1")
            self.assertEqual(migrated["selected_ein"], TARGET_EIN)
            self.assertEqual(migrated["selected_name"], "Learning Policy Institute")
            self.assertEqual(migrated["model"], "external:test-model")
            self.assertEqual(migrated["model_options_json"], source_row[14])
            self.assertEqual(migrated["input_json"], source_row[17])
            self.assertEqual(migrated["output_json"], source_row[18])
            self.assertEqual(migrated["created_at"], source_row[19])
            work = sqlite3.connect(work_db)
            self.assertEqual(
                work.execute(
                    "SELECT ai_queue_status FROM grant_recipient_signature WHERE signature_hash='SIG_VALID'"
                ).fetchone()[0],
                "adjudicated",
            )
            work.close()

    def test_orphan_changed_select_and_keep_semantics_are_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_CHANGED", candidate_id="C2", candidate_ein="111111112")
            add_signature(target_db, work_db, "SIG_SELECT")
            add_signature(target_db, work_db, "SIG_KEEP", reported_ein=TARGET_EIN)
            insert_source_decision(source_db, decision_tuple("SIG_ORPHAN"))
            insert_source_decision(source_db, decision_tuple("SIG_CHANGED"))
            insert_source_decision(
                source_db,
                decision_tuple("SIG_SELECT", selected_ein="111111112"),
            )
            insert_source_decision(
                source_db,
                decision_tuple(
                    "SIG_KEEP",
                    decision="KEEP_REPORTED_EIN",
                    selected_ein="111111112",
                    candidate_ein=TARGET_EIN,
                ),
            )
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, suffix="quarantine")

            gai.cmd_migrate_reviewed_decisions(args)

            with Path(args.audit_csv).open(newline="", encoding="utf-8-sig") as fh:
                audit = {row["signature_hash"]: row for row in csv.DictReader(fh)}
            self.assertEqual(set(audit), {"SIG_ORPHAN", "SIG_CHANGED", "SIG_SELECT", "SIG_KEEP"})
            self.assertIn("orphan_signature_not_found", audit["SIG_ORPHAN"]["reasons"])
            self.assertIn("candidate_set_changed", audit["SIG_CHANGED"]["reasons"])
            self.assertIn("select_candidate_ein_changed", audit["SIG_SELECT"]["reasons"])
            self.assertIn("keep_reported_ein_value_changed", audit["SIG_KEEP"]["reasons"])
            quarantine_rows = [
                json.loads(line)
                for line in Path(args.quarantine_jsonl).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(quarantine_rows), 4)

    def test_existing_review_conflict_requires_explicit_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID", model="external:source"))
            target = sqlite3.connect(target_db)
            gai.create_decision_schema(target)
            placeholders = ",".join("?" for _ in gai.DECISION_COLUMNS)
            target.execute(
                f"INSERT INTO grant_recipient_ai_decision ({','.join(gai.DECISION_COLUMNS)}) VALUES ({placeholders})",
                decision_tuple("SIG_VALID", model="external:target"),
            )
            target.commit()
            target.close()
            self.configure(target_db, work_db)

            dry_args = migration_args(root, source_db, target_db, work_db, suffix="conflict")
            gai.cmd_migrate_reviewed_decisions(dry_args)
            with Path(dry_args.audit_csv).open(newline="", encoding="utf-8-sig") as fh:
                row = next(csv.DictReader(fh))
            self.assertIn("target_reviewed_decision_conflict", row["reasons"])
            conflict_record = json.loads(
                Path(dry_args.quarantine_jsonl).read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(
                set(conflict_record["preexisting_target_decision"]),
                set(gai.DECISION_COLUMNS),
            )
            self.assertEqual(
                conflict_record["preexisting_target_decision"]["model"],
                "external:target",
            )

            apply_args = migration_args(
                root,
                source_db,
                target_db,
                work_db,
                apply=True,
                replace_existing_reviewed=True,
                suffix="replace",
            )
            gai.cmd_migrate_reviewed_decisions(apply_args)
            target = sqlite3.connect(target_db)
            self.assertEqual(
                target.execute(
                    "SELECT model FROM grant_recipient_ai_decision WHERE signature_hash='SIG_VALID'"
                ).fetchone()[0],
                "external:source",
            )
            target.close()
            replacement_records = [
                json.loads(line)
                for line in Path(apply_args.quarantine_jsonl).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(replacement_records), 1)
            self.assertEqual(replacement_records[0]["event_type"], "preexisting_target_replacement")
            self.assertEqual(
                set(replacement_records[0]["preexisting_target_decision"]),
                set(gai.DECISION_COLUMNS),
            )
            self.assertEqual(
                replacement_records[0]["preexisting_target_decision"]["model"],
                "external:target",
            )

    def test_rule_target_is_replaced_without_promoting_historical_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_REVIEW")
            insert_source_decision(
                source_db,
                decision_tuple("SIG_REVIEW", model="manual:reviewed", auto_accept=0),
            )
            target = sqlite3.connect(target_db)
            gai.create_decision_schema(target)
            placeholders = ",".join("?" for _ in gai.DECISION_COLUMNS)
            target.execute(
                f"INSERT INTO grant_recipient_ai_decision ({','.join(gai.DECISION_COLUMNS)}) VALUES ({placeholders})",
                decision_tuple("SIG_REVIEW", model="rule:candidate_evidence", auto_accept=1),
            )
            target.commit()
            target.close()
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, apply=True, suffix="rule-replace")

            gai.cmd_migrate_reviewed_decisions(args)

            target = sqlite3.connect(target_db)
            migrated = target.execute(
                "SELECT model, auto_accept FROM grant_recipient_ai_decision WHERE signature_hash='SIG_REVIEW'"
            ).fetchone()
            target.close()
            self.assertEqual(migrated, ("manual:reviewed", 0))

    def test_refuses_same_source_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_work_db(work_db)
            self.configure(source_db, work_db)
            args = migration_args(root, source_db, source_db, work_db)
            with self.assertRaisesRegex(RuntimeError, "same file"):
                gai.cmd_migrate_reviewed_decisions(args)

    def test_apply_rolls_back_all_rows_when_an_insert_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            for signature_hash in ("SIG_ONE", "SIG_TWO"):
                add_signature(target_db, work_db, signature_hash)
                insert_source_decision(source_db, decision_tuple(signature_hash))
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, apply=True, suffix="rollback")
            original = gai._upsert_migrated_decision
            calls = 0

            def fail_second(conn, row):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("fixture insert failure")
                return original(conn, row)

            gai._upsert_migrated_decision = fail_second
            try:
                with self.assertRaisesRegex(RuntimeError, "fixture insert failure"):
                    gai.cmd_migrate_reviewed_decisions(args)
            finally:
                gai._upsert_migrated_decision = original

            target = sqlite3.connect(target_db)
            self.assertIsNone(
                target.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='grant_recipient_ai_decision'"
                ).fetchone()
            )
            target.close()
            work = sqlite3.connect(work_db)
            self.assertEqual(
                work.execute(
                    "SELECT COUNT(*) FROM grant_recipient_signature WHERE ai_queue_status='candidates_ready'"
                ).fetchone()[0],
                2,
            )
            work.close()
            self.assertFalse(Path(args.audit_csv).exists())
            self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_commit_failure_rolls_back_and_publishes_no_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, apply=True, suffix="commit-fail")
            original = gai._commit_migration_transaction

            def fail_commit(_conn):
                raise sqlite3.OperationalError("fixture commit failure")

            gai._commit_migration_transaction = fail_commit
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "fixture commit failure"):
                    gai.cmd_migrate_reviewed_decisions(args)
            finally:
                gai._commit_migration_transaction = original

            target = sqlite3.connect(target_db)
            self.assertIsNone(
                target.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='grant_recipient_ai_decision'"
                ).fetchone()
            )
            target.close()
            work = sqlite3.connect(work_db)
            self.assertEqual(
                work.execute(
                    "SELECT ai_queue_status FROM grant_recipient_signature WHERE signature_hash='SIG_VALID'"
                ).fetchone()[0],
                "candidates_ready",
            )
            work.close()
            self.assertFalse(Path(args.audit_csv).exists())
            self.assertFalse(Path(args.quarantine_jsonl).exists())
            self.assertEqual(list(root.glob(".*commit-fail*.tmp")), [])

    def test_post_commit_publish_failure_retains_named_recovery_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, apply=True, suffix="publish-fail")
            original = gai._publish_staged_migration_outputs

            def fail_publish(temp_paths):
                first_final, first_temp = next(iter(temp_paths.items()))
                os.replace(first_temp, first_final)
                raise OSError("fixture publish failure")

            gai._publish_staged_migration_outputs = fail_publish
            try:
                with self.assertRaisesRegex(RuntimeError, "DATABASE COMMITTED") as raised:
                    gai.cmd_migrate_reviewed_decisions(args)
            finally:
                gai._publish_staged_migration_outputs = original

            target = sqlite3.connect(target_db)
            self.assertEqual(
                target.execute("SELECT COUNT(*) FROM grant_recipient_ai_decision").fetchone()[0],
                1,
            )
            target.close()
            work = sqlite3.connect(work_db)
            self.assertEqual(
                work.execute(
                    "SELECT ai_queue_status FROM grant_recipient_signature WHERE signature_hash='SIG_VALID'"
                ).fetchone()[0],
                "adjudicated",
            )
            work.close()
            self.assertTrue(Path(args.audit_csv).exists())
            self.assertFalse(Path(args.quarantine_jsonl).exists())
            staged = sorted(root.glob(".*publish-fail*.tmp"))
            self.assertEqual(len(staged), 1)
            for path in staged:
                self.assertIn(str(path), str(raised.exception))
            self.assertIn(str(Path(args.audit_csv)), str(raised.exception))

    def test_rerun_is_normalized_and_does_not_rewrite_current_rows_or_work_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            self.configure(target_db, work_db)
            first_args = migration_args(root, source_db, target_db, work_db, apply=True, suffix="first")
            gai.cmd_migrate_reviewed_decisions(first_args)

            target = sqlite3.connect(target_db)
            target.execute(
                "UPDATE grant_recipient_ai_decision SET "
                "model_options_json=?, reason_codes_json=? WHERE signature_hash='SIG_VALID'",
                ('{"temperature":0.0,"num_ctx":8192}', '[ "fixture" ]'),
            )
            target.commit()
            before_target = target.execute(
                f"SELECT {','.join(gai.DECISION_COLUMNS)} FROM grant_recipient_ai_decision "
                "WHERE signature_hash='SIG_VALID'"
            ).fetchone()
            target.close()
            work = sqlite3.connect(work_db)
            work.execute(
                "UPDATE grant_recipient_signature SET updated_at='do-not-rewrite' "
                "WHERE signature_hash='SIG_VALID'"
            )
            work.commit()
            work.close()

            second_args = migration_args(root, source_db, target_db, work_db, apply=True, suffix="second")
            gai.cmd_migrate_reviewed_decisions(second_args)

            with Path(second_args.audit_csv).open(newline="", encoding="utf-8-sig") as fh:
                audit = next(csv.DictReader(fh))
            self.assertEqual(audit["status"], "already_current")
            self.assertEqual(audit["target_action"], "already_current")
            target = sqlite3.connect(target_db)
            after_target = target.execute(
                f"SELECT {','.join(gai.DECISION_COLUMNS)} FROM grant_recipient_ai_decision "
                "WHERE signature_hash='SIG_VALID'"
            ).fetchone()
            target.close()
            self.assertEqual(after_target, before_target)
            work = sqlite3.connect(work_db)
            self.assertEqual(
                work.execute(
                    "SELECT updated_at FROM grant_recipient_signature WHERE signature_hash='SIG_VALID'"
                ).fetchone()[0],
                "do-not-rewrite",
            )
            work.close()

    def test_apply_forces_rollback_journals_for_attached_commit_then_restores_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            for db_path in (target_db, work_db):
                conn = sqlite3.connect(db_path)
                self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
                conn.close()
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, apply=True, suffix="journals")
            original = gai._commit_migration_transaction
            observed = {}

            def observe_commit(conn):
                observed["main_mode"] = conn.execute("PRAGMA main.journal_mode").fetchone()[0].lower()
                observed["work_mode"] = conn.execute("PRAGMA grant_work.journal_mode").fetchone()[0].lower()
                observed["main_sync"] = conn.execute("PRAGMA main.synchronous").fetchone()[0]
                observed["work_sync"] = conn.execute("PRAGMA grant_work.synchronous").fetchone()[0]
                return original(conn)

            gai._commit_migration_transaction = observe_commit
            try:
                gai.cmd_migrate_reviewed_decisions(args)
            finally:
                gai._commit_migration_transaction = original

            self.assertEqual(observed["main_mode"], "delete")
            self.assertEqual(observed["work_mode"], "delete")
            self.assertGreaterEqual(observed["main_sync"], 2)
            self.assertGreaterEqual(observed["work_sync"], 2)
            for db_path in (target_db, work_db):
                conn = sqlite3.connect(db_path)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                conn.close()

    def test_readiness_accepts_exact_zero_candidate_and_adjudicated_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            add_signature(target_db, work_db, "SIG_ADJUDICATED_SELECT")
            conn = sqlite3.connect(work_db)
            conn.execute(
                "UPDATE grant_recipient_signature SET ai_queue_status='adjudicated' "
                "WHERE signature_hash='SIG_ADJUDICATED_SELECT'"
            )
            conn.commit()
            conn.close()
            add_signature_without_candidate(
                target_db,
                work_db,
                "SIG_NO_CANDIDATES",
                queue_status="no_candidates",
            )
            add_signature_without_candidate(
                target_db,
                work_db,
                "SIG_ADJUDICATED_NO_MATCH",
                queue_status="adjudicated",
            )
            target = sqlite3.connect(target_db)
            gai.create_decision_schema(target)
            placeholders = ",".join("?" for _ in gai.DECISION_COLUMNS)
            target.execute(
                f"INSERT INTO grant_recipient_ai_decision "
                f"({','.join(gai.DECISION_COLUMNS)}) VALUES ({placeholders})",
                decision_tuple("SIG_ADJUDICATED_SELECT"),
            )
            no_match_row = list(
                decision_tuple(
                    "SIG_ADJUDICATED_NO_MATCH",
                    decision="NO_MATCH",
                    candidate_id="",
                    selected_ein="",
                )
            )
            no_match_row[4] = ""
            target.execute(
                f"INSERT INTO grant_recipient_ai_decision "
                f"({','.join(gai.DECISION_COLUMNS)}) VALUES ({placeholders})",
                no_match_row,
            )
            target.commit()
            target.close()
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, suffix="valid-counts")

            gai.cmd_migrate_reviewed_decisions(args)

            with Path(args.audit_csv).open(newline="", encoding="utf-8-sig") as fh:
                audit_rows = list(csv.DictReader(fh))
            self.assertEqual(len(audit_rows), 1)
            self.assertEqual(audit_rows[0]["status"], "eligible")

    def test_readiness_rejects_stale_candidate_counts_and_queue_semantics(self):
        cases = (
            (
                "balanced_interruption",
                "UPDATE grant_recipient_signature SET candidate_count=0, "
                "ai_queue_status='no_candidates' WHERE signature_hash='SIG_VALID'",
                None,
                "no_candidates.*zero candidate rows",
            ),
            (
                "stored_count_mismatch",
                "UPDATE grant_recipient_signature SET candidate_count=2 "
                "WHERE signature_hash='SIG_VALID'",
                None,
                "stored candidate_count=2.*candidate table has 1",
            ),
            (
                "ready_without_candidate",
                "UPDATE grant_recipient_signature SET candidate_count=0 "
                "WHERE signature_hash='SIG_VALID'",
                "DELETE FROM grant_recipient_ai_candidate "
                "WHERE signature_hash='SIG_VALID'",
                "candidates_ready.*at least one candidate row",
            ),
            (
                "adjudicated_count_mismatch",
                "UPDATE grant_recipient_signature SET candidate_count=0, "
                "ai_queue_status='adjudicated' WHERE signature_hash='SIG_VALID'",
                None,
                "stored candidate_count=0.*candidate table has 1",
            ),
            (
                "stored_positive_without_candidate",
                "UPDATE grant_recipient_signature SET candidate_count=1, "
                "ai_queue_status='no_candidates' WHERE signature_hash='SIG_VALID'",
                "DELETE FROM grant_recipient_ai_candidate "
                "WHERE signature_hash='SIG_VALID'",
                "stored candidate_count=1.*candidate table has 0",
            ),
            (
                "null_count",
                "UPDATE grant_recipient_signature SET candidate_count=NULL "
                "WHERE signature_hash='SIG_VALID'",
                None,
                "stored candidate_count=None.*candidate table has 1",
            ),
            (
                "invalid_new_status",
                "UPDATE grant_recipient_signature SET ai_queue_status='new' "
                "WHERE signature_hash='SIG_VALID'",
                None,
                "candidate generation is incomplete.*queue status 'new'",
            ),
        )
        for case, signature_sql, candidate_sql, expected in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source_db = root / "source.db"
                target_db = root / "target.db"
                work_db = root / "work.db"
                create_source_db(source_db)
                create_empty_db(target_db)
                create_work_db(work_db)
                add_signature(target_db, work_db, "SIG_VALID")
                insert_source_decision(source_db, decision_tuple("SIG_VALID"))
                conn = sqlite3.connect(work_db)
                conn.execute(signature_sql)
                if candidate_sql:
                    conn.execute(candidate_sql)
                conn.commit()
                conn.close()
                self.configure(target_db, work_db)
                args = migration_args(
                    root,
                    source_db,
                    target_db,
                    work_db,
                    suffix=case,
                )

                with self.assertRaisesRegex(RuntimeError, expected):
                    gai.cmd_migrate_reviewed_decisions(args)
                self.assertFalse(Path(args.audit_csv).exists())
                self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_readiness_rejects_candidate_rows_without_a_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            conn = sqlite3.connect(work_db)
            conn.execute(
                "INSERT INTO grant_recipient_ai_candidate VALUES (?,?,?,?,?,?)",
                ("SIG_ORPHAN", "C1", 1, TARGET_EIN, "Orphan", 95),
            )
            conn.commit()
            conn.close()
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, suffix="orphan-candidate")

            with self.assertRaisesRegex(RuntimeError, "candidate row.*no signature row"):
                gai.cmd_migrate_reviewed_decisions(args)
            self.assertFalse(Path(args.audit_csv).exists())
            self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_readiness_rejects_blank_candidate_signature_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            self.configure(target_db, work_db)
            work = sqlite3.connect(work_db)
            work.execute(
                "INSERT INTO grant_recipient_ai_candidate VALUES (?,?,?,?,?,?)",
                ("", "C1", 1, TARGET_EIN, "Blank hash", 95),
            )
            work.commit()
            work.close()
            args = migration_args(root, source_db, target_db, work_db, suffix="blank-hash")

            with self.assertRaisesRegex(RuntimeError, "blank/malformed signature_hash"):
                gai.cmd_migrate_reviewed_decisions(args)
            self.assertFalse(Path(args.audit_csv).exists())
            self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_readiness_requires_candidate_count_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            conn = sqlite3.connect(work_db)
            conn.execute(
                "ALTER TABLE grant_recipient_signature DROP COLUMN candidate_count"
            )
            conn.commit()
            conn.close()
            self.configure(target_db, work_db)
            args = migration_args(root, source_db, target_db, work_db, suffix="missing-count")

            with self.assertRaisesRegex(
                RuntimeError,
                "missing required columns: candidate_count",
            ):
                gai.cmd_migrate_reviewed_decisions(args)
            self.assertFalse(Path(args.audit_csv).exists())
            self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_readiness_rejects_valid_looking_zero_row_without_balanced_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature_without_candidate(target_db, work_db, "SIG_ZERO")
            insert_source_decision(
                source_db,
                decision_tuple(
                    "SIG_ZERO",
                    decision="NO_MATCH",
                    candidate_id="",
                    selected_ein="",
                ),
            )
            self.configure(target_db, work_db)
            work = sqlite3.connect(work_db)
            work.execute(
                "UPDATE grant_recipient_signature "
                "SET candidate_generation_phase='fast', "
                "candidate_generation_run_id='RUN_FAST' "
                "WHERE signature_hash='SIG_ZERO'"
            )
            work.commit()
            work.close()
            args = migration_args(root, source_db, target_db, work_db, suffix="unbalanced-zero")

            with self.assertRaisesRegex(
                RuntimeError,
                "zero-candidate signatures require completed balanced coverage",
            ):
                gai.cmd_migrate_reviewed_decisions(args)
            self.assertFalse(Path(args.audit_csv).exists())
            self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_readiness_rejects_incomplete_or_stale_run_evidence(self):
        cases = (
            (
                "incomplete_balanced",
                "UPDATE grant_recipient_candidate_generation_run "
                "SET run_status='running', processed_count=0, completed_at=NULL "
                "WHERE run_id='RUN_BALANCED'",
                "Latest candidate-generation run is not a completed",
            ),
            (
                "stale_signature_population",
                "UPDATE grant_recipient_signature "
                "SET recipient_name_norm='CHANGED AFTER GENERATION' "
                "WHERE signature_hash='SIG_VALID'",
                "Current signature population does not match",
            ),
            (
                "forged_config",
                "UPDATE grant_recipient_candidate_generation_run "
                "SET config_fingerprint='CANDCFG_FORGED' "
                "WHERE run_id='RUN_BALANCED'",
                "stale/invalid config fingerprint",
            ),
            (
                "completed_count_mismatch",
                "UPDATE grant_recipient_candidate_generation_run "
                "SET processed_count=selected_count+1 "
                "WHERE run_id='RUN_BALANCED'",
                "did not process its exact selected scope",
            ),
            (
                "stale_per_signature_version",
                "UPDATE grant_recipient_signature "
                "SET candidate_generation_version='legacy' "
                "WHERE signature_hash='SIG_VALID'",
                "candidate-generation version is missing or stale",
            ),
        )
        for case, mutation, expected in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source_db = root / "source.db"
                target_db = root / "target.db"
                work_db = root / "work.db"
                create_source_db(source_db)
                create_empty_db(target_db)
                create_work_db(work_db)
                add_signature(target_db, work_db, "SIG_VALID")
                insert_source_decision(source_db, decision_tuple("SIG_VALID"))
                self.configure(target_db, work_db)
                work = sqlite3.connect(work_db)
                work.execute(mutation)
                work.commit()
                work.close()
                args = migration_args(root, source_db, target_db, work_db, suffix=case)

                with self.assertRaisesRegex(RuntimeError, expected):
                    gai.cmd_migrate_reviewed_decisions(args)
                self.assertFalse(Path(args.audit_csv).exists())
                self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_readiness_rejects_legacy_candidate_generation_without_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            insert_source_decision(source_db, decision_tuple("SIG_VALID"))
            self.configure(target_db, work_db, certify=False)
            args = migration_args(root, source_db, target_db, work_db, suffix="no-ledger")

            with self.assertRaisesRegex(RuntimeError, "no durable candidate-generation run ledger"):
                gai.cmd_migrate_reviewed_decisions(args)
            self.assertFalse(Path(args.audit_csv).exists())
            self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_readiness_requires_adjudicated_decision_integrity(self):
        cases = (
            "adjudicated_without_decision",
            "orphan_decision",
            "select_missing_current_candidate",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source_db = root / "source.db"
                target_db = root / "target.db"
                work_db = root / "work.db"
                create_source_db(source_db)
                create_empty_db(target_db)
                create_work_db(work_db)
                add_signature(target_db, work_db, "SIG_VALID")
                insert_source_decision(source_db, decision_tuple("SIG_VALID"))
                if case == "adjudicated_without_decision":
                    work = sqlite3.connect(work_db)
                    work.execute(
                        "UPDATE grant_recipient_signature "
                        "SET ai_queue_status='adjudicated'"
                    )
                    work.commit()
                    work.close()
                    expected = "adjudicated signature but no decision table"
                else:
                    target = sqlite3.connect(target_db)
                    gai.create_decision_schema(target)
                    placeholders = ",".join("?" for _ in gai.DECISION_COLUMNS)
                    target.execute(
                        f"INSERT INTO grant_recipient_ai_decision "
                        f"({','.join(gai.DECISION_COLUMNS)}) VALUES ({placeholders})",
                        decision_tuple(
                            "SIG_ORPHAN" if case == "orphan_decision" else "SIG_VALID",
                            candidate_id=(
                                "C_MISSING"
                                if case == "select_missing_current_candidate"
                                else "C1"
                            ),
                        ),
                    )
                    target.commit()
                    target.close()
                    if case == "select_missing_current_candidate":
                        work = sqlite3.connect(work_db)
                        work.execute(
                            "UPDATE grant_recipient_signature "
                            "SET ai_queue_status='adjudicated'"
                        )
                        work.commit()
                        work.close()
                        expected = "does not reference an exact current candidate_id/EIN row"
                    else:
                        expected = "has no matching signature row"
                self.configure(target_db, work_db)
                args = migration_args(root, source_db, target_db, work_db, suffix=case)

                with self.assertRaisesRegex(RuntimeError, expected):
                    gai.cmd_migrate_reviewed_decisions(args)
                self.assertFalse(Path(args.audit_csv).exists())
                self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_readiness_streaming_scans_use_signature_indexes_without_temp_sort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_empty_db(target_db)
            create_work_db(work_db)
            add_signature(target_db, work_db, "SIG_VALID")
            self.configure(target_db, work_db)
            conn = gai._connect_migration_target(
                str(target_db),
                str(work_db),
                readonly=True,
            )
            try:
                for sql in gai._candidate_consistency_scan_sql():
                    details = [
                        str(row[3]).upper()
                        for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
                    ]
                    self.assertFalse(
                        any("TEMP B-TREE" in detail for detail in details),
                        details,
                    )
                    self.assertTrue(
                        any("INDEX" in detail for detail in details),
                        details,
                    )
            finally:
                conn.close()

    def test_readiness_requires_complete_signature_grant_references(self):
        cases = ("missing_mapping", "missing_signature", "missing_grant")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source_db = root / "source.db"
                target_db = root / "target.db"
                work_db = root / "work.db"
                create_source_db(source_db)
                create_empty_db(target_db)
                create_work_db(work_db)
                add_signature(target_db, work_db, "SIG_VALID")
                insert_source_decision(source_db, decision_tuple("SIG_VALID"))
                if case == "missing_mapping":
                    conn = sqlite3.connect(work_db)
                    conn.execute("DELETE FROM grant_recipient_signature_grant")
                    conn.commit()
                    conn.close()
                    expected = "has no mapped rebuilt grant"
                elif case == "missing_signature":
                    target = sqlite3.connect(target_db)
                    target.execute("INSERT INTO grant_recipient_resolved(grant_id) VALUES (999)")
                    target.commit()
                    target.close()
                    conn = sqlite3.connect(work_db)
                    conn.execute("INSERT INTO grant_recipient_signature_grant VALUES ('SIG_GHOST',999)")
                    conn.commit()
                    conn.close()
                    expected = "has no signature row"
                else:
                    target = sqlite3.connect(target_db)
                    target.execute("DELETE FROM grant_recipient_resolved")
                    target.commit()
                    target.close()
                    expected = "absent from rebuilt resolution"
                self.configure(target_db, work_db)
                args = migration_args(root, source_db, target_db, work_db, suffix=case)
                with self.assertRaisesRegex(RuntimeError, expected):
                    gai.cmd_migrate_reviewed_decisions(args)
                self.assertFalse(Path(args.audit_csv).exists())
                self.assertFalse(Path(args.quarantine_jsonl).exists())

    def test_refuses_database_companion_output_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            target_db = root / "target.db"
            work_db = root / "work.db"
            create_source_db(source_db)
            create_empty_db(target_db)
            create_work_db(work_db)
            self.configure(target_db, work_db)
            protected_outputs = (
                str(source_db) + "-wal",
                str(target_db) + "-shm",
                str(work_db) + "-journal",
            )
            for index, output in enumerate(protected_outputs):
                with self.subTest(output=output):
                    args = migration_args(root, source_db, target_db, work_db, suffix=f"companion-{index}")
                    args.audit_csv = output
                    with self.assertRaisesRegex(RuntimeError, "SQLite companion"):
                        gai.cmd_migrate_reviewed_decisions(args)


if __name__ == "__main__":
    unittest.main()
