import argparse
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import grant_ai_assist_v1 as gai


TARGET_EIN = "131548339"


def legacy_decision_row(
    signature_hash,
    *,
    decision="HUMAN_REVIEW",
    model="rule:reported_ein_no_ai_review",
    reported_ein=TARGET_EIN,
    recipient_name="Recipient Filing Name",
    selected_candidate_id="",
    selected_ein=TARGET_EIN,
    selected_name="Current Identity Name",
    auto_accept=0,
    output_candidate_id="",
    candidate_set_hash=None,
):
    validity = gai.reported_ein_validity_reason(reported_ein)
    if model == "rule:invalid_reported_ein_no_ai":
        reason_codes = ["reported_ein_invalid", validity, "ollama_skipped"]
        shortcut_reason = validity
        explanation = (
            f"The filing-supplied recipient EIN value '{reported_ein}' is not a usable EIN "
            f"({validity}). It was not kept as a recipient EIN, and Ollama was skipped by policy."
        )
    else:
        reason_codes = [
            "reported_ein_present",
            "reported_ein_known_but_name_disagrees",
            "ollama_skipped_nonconflicting_reported_ein",
        ]
        shortcut_reason = "recipient_name_disagrees_with_reported_ein_identity"
        explanation = (
            f"Reported recipient EIN {TARGET_EIN} is known in org_identity as 'Current Identity Name', "
            "but the recipient name has weak agreement with that identity. Ollama was skipped because "
            "there was no strong first-pass contradiction flag; this should be reviewed manually."
        )
    input_obj = {
        "task": "reported_ein_triage_no_ollama",
        "rules": [
            "The filing supplied a recipient EIN.",
            "Non-conflicting reported EINs should not be sent to Ollama for second-guessing.",
            "Only reported-EIN cases with strong contradiction signals should proceed to AI adjudication.",
        ],
        "grant_recipient_signature": {
            "signature_hash": signature_hash,
            "reported_ein": gai.digits9(reported_ein),
            "recipient_name": recipient_name,
            "street": "1 Main St",
            "city": "New York",
            "state": "NY",
            "zip5": "10001",
            "grant_count": 2,
            "total_amount": 750000.0,
            "first_pass_statuses_json": "{}",
            "first_pass_warning_flags": "",
        },
        "reported_ein_triage": {
            "shortcut_reason": shortcut_reason,
            **({"identity_source": "returns_org_name"} if model == "rule:reported_ein_no_ai_review" else {}),
            **({"reported_ein_raw": reported_ein} if model == "rule:invalid_reported_ein_no_ai" else {}),
        },
    }
    needs_review = decision == "HUMAN_REVIEW"
    confidence = 0.0 if needs_review else 1.0
    confidence_label = "none" if needs_review else "high"
    output_obj = {
        "decision": decision,
        "candidate_id": output_candidate_id,
        "confidence": confidence,
        "confidence_label": confidence_label,
        "reason_codes": reason_codes,
        "explanation": explanation,
        "needs_human_review": needs_review,
    }
    input_json = json.dumps(input_obj, ensure_ascii=False, sort_keys=True)
    output_json = json.dumps(output_obj, ensure_ascii=False, sort_keys=True)
    return (
        signature_hash,
        decision,
        selected_candidate_id,
        selected_ein,
        selected_name,
        confidence,
        confidence_label,
        json.dumps(reason_codes, ensure_ascii=False, sort_keys=True),
        explanation,
        1 if needs_review else 0,
        auto_accept,
        "ok",
        "",
        model,
        json.dumps({"rule": "reported_ein_triage"}, sort_keys=True),
        gai.stable_hash([input_json], "PROMPT_"),
        candidate_set_hash or gai.stable_hash(["[]"], "CANDS_"),
        input_json,
        output_json,
        "2026-08-15 01:02:03",
    )


def unknown_identity_review_row(signature_hash, reported_ein, recipient_name):
    row = list(
        legacy_decision_row(
            signature_hash,
            reported_ein=reported_ein,
            recipient_name=recipient_name,
            selected_ein=reported_ein,
            selected_name=recipient_name,
        )
    )
    reason_codes = [
        "reported_ein_present",
        "reported_ein_not_in_org_identity",
        "recipient_name_present",
        "ollama_skipped",
    ]
    explanation = (
        f"The filing supplied recipient EIN {reported_ein}, but org_identity has no name for it. "
        "Ollama was skipped because there was no strong reported-EIN contradiction; this should be reviewed manually."
    )
    input_obj = json.loads(row[17])
    input_obj["reported_ein_triage"] = {"shortcut_reason": "reported_ein_not_in_org_identity"}
    input_json = json.dumps(input_obj, ensure_ascii=False, sort_keys=True)
    output_obj = json.loads(row[18])
    output_obj["reason_codes"] = reason_codes
    output_obj["explanation"] = explanation
    row[7] = json.dumps(reason_codes, ensure_ascii=False, sort_keys=True)
    row[8] = explanation
    row[15] = gai.stable_hash([input_json], "PROMPT_")
    row[17] = input_json
    row[18] = json.dumps(output_obj, ensure_ascii=False, sort_keys=True)
    return tuple(row)


def selection_decision_row(signature_hash, decision, candidate_id, ein, name):
    input_json = json.dumps(
        {"grant_recipient_signature": {"signature_hash": signature_hash}},
        ensure_ascii=False,
        sort_keys=True,
    )
    output_json = json.dumps(
        {
            "decision": decision,
            "candidate_id": candidate_id,
            "confidence": 0.99,
            "confidence_label": "high",
            "reason_codes": ["reviewed_selection"],
            "explanation": "Reviewed current selection.",
            "needs_human_review": False,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        signature_hash,
        decision,
        candidate_id,
        ein,
        name,
        0.99,
        "high",
        json.dumps(["reviewed_selection"]),
        "Reviewed current selection.",
        0,
        1,
        "ok",
        "",
        "external:reviewed",
        "{}",
        gai.stable_hash([input_json], "PROMPT_"),
        gai.stable_hash(["[]"], "CANDS_"),
        input_json,
        output_json,
        "2026-08-15 01:02:03",
    )


class RuleNonselectionIdentityRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.main_path = root / "main.db"
        self.work_path = root / "work.db"
        self.audit_path = root / "repair.csv"
        self.saved_globals = {
            "SIG_TABLE": gai.SIG_TABLE,
            "CAND_TABLE": gai.CAND_TABLE,
            "ORG_IDENTITY_TABLE": gai.ORG_IDENTITY_TABLE,
            "GRANT_WORK_DB_PATH": gai.GRANT_WORK_DB_PATH,
            "GRANT_WORK_SIDECAR_ENABLED": gai.GRANT_WORK_SIDECAR_ENABLED,
        }
        gai.SIG_TABLE = "grant_work.grant_recipient_signature"
        gai.CAND_TABLE = "grant_work.grant_recipient_ai_candidate"
        gai.ORG_IDENTITY_TABLE = "grant_work.org_identity"
        gai.GRANT_WORK_DB_PATH = str(self.work_path)
        gai.GRANT_WORK_SIDECAR_ENABLED = True

        main = sqlite3.connect(self.main_path)
        main.row_factory = sqlite3.Row
        gai._ensure_decision_schema_in_transaction(main)
        main.execute(
            "CREATE TABLE grant_recipient_ai_applied (signature_hash TEXT PRIMARY KEY, selected_ein TEXT)"
        )
        main.commit()
        main.close()

        work = sqlite3.connect(self.work_path)
        work.executescript(
            """
            CREATE TABLE grant_recipient_signature (
              signature_hash TEXT PRIMARY KEY,
              reported_ein TEXT,
              recipient_name TEXT,
              street TEXT,
              city TEXT,
              state TEXT,
              zip5 TEXT,
              grant_count INTEGER,
              total_amount NUMERIC,
              first_pass_statuses_json TEXT,
              first_pass_warning_flags TEXT,
              ai_queue_status TEXT
            );
            CREATE TABLE grant_recipient_ai_candidate (
              signature_hash TEXT NOT NULL,
              candidate_id TEXT NOT NULL,
              ein TEXT,
              candidate_rank INTEGER,
              candidate_score NUMERIC,
              PRIMARY KEY (signature_hash, candidate_id)
            );
            CREATE TABLE org_identity (
              identity_id INTEGER PRIMARY KEY,
              ein TEXT,
              display_name TEXT,
              source TEXT,
              source_rank INTEGER,
              tax_year INTEGER
            );
            INSERT INTO org_identity VALUES
              (1, '131548339', 'Current Identity Name', 'returns_org_name', 10, 2024);
            """
        )
        work.commit()
        work.close()

    def tearDown(self):
        for name, value in self.saved_globals.items():
            setattr(gai, name, value)
        self.temp.cleanup()

    def add_signature(self, signature_hash, reported_ein=TARGET_EIN, recipient_name="Recipient Filing Name"):
        conn = sqlite3.connect(self.work_path)
        conn.execute(
            "INSERT INTO grant_recipient_signature VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                signature_hash,
                reported_ein,
                recipient_name,
                "1 Main St",
                "New York",
                "NY",
                "10001",
                2,
                750000,
                "{}",
                "",
                "adjudicated",
            ),
        )
        conn.commit()
        conn.close()

    def add_decision(self, row):
        conn = sqlite3.connect(self.main_path)
        placeholders = ",".join("?" for _ in gai.DECISION_COLUMNS)
        conn.execute(
            f"INSERT INTO {gai.DECISION_TABLE} ({','.join(gai.DECISION_COLUMNS)}) VALUES ({placeholders})",
            row,
        )
        conn.commit()
        conn.close()

    def add_candidate(self, signature_hash, candidate_id, ein, rank, score):
        conn = sqlite3.connect(self.work_path)
        conn.execute(
            "INSERT INTO grant_recipient_ai_candidate VALUES (?,?,?,?,?)",
            (signature_hash, candidate_id, ein, rank, score),
        )
        conn.commit()
        conn.close()

    def args(self, *, apply=False, audit_path=None):
        return argparse.Namespace(
            db=str(self.main_path),
            work_db=str(self.work_path),
            audit_csv=str(audit_path or self.audit_path),
            overwrite_audit=False,
            apply=apply,
        )

    def fetch_decision(self, signature_hash):
        conn = sqlite3.connect(self.main_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM {gai.DECISION_TABLE} WHERE signature_hash=?", (signature_hash,)
        ).fetchone()
        conn.close()
        return dict(row)

    def assessment_reasons(self):
        conn = gai._connect_migration_target(str(self.main_path), str(self.work_path), readonly=True)
        try:
            return {
                row["signature_hash"]: gai._rule_nonselection_repair_assessment(conn, row)
                for row in gai._iter_rule_nonselection_identity_repair_rows(conn)
            }
        finally:
            conn.close()

    def test_apply_clears_only_verified_rule_leaks_and_preserves_full_provenance(self):
        self.add_signature("SIG_REVIEW")
        self.add_signature("SIG_INVALID", "000000000")
        review = legacy_decision_row("SIG_REVIEW")
        invalid = legacy_decision_row(
            "SIG_INVALID",
            decision="NO_MATCH",
            model="rule:invalid_reported_ein_no_ai",
            reported_ein="000000000",
            selected_ein="",
            selected_name="Recipient Filing Name",
        )
        self.add_decision(review)
        self.add_decision(invalid)
        self.add_signature("SIG_MANUAL")
        manual = list(legacy_decision_row("SIG_MANUAL"))
        manual[2:5] = ["", "", ""]
        manual[13] = "external:reviewed"
        self.add_decision(tuple(manual))
        manual_before = self.fetch_decision("SIG_MANUAL")
        self.add_signature("SIG_SELECT")
        self.add_signature("SIG_KEEP")
        self.add_candidate("SIG_SELECT", "C_SELECT", TARGET_EIN, 1, 200)
        self.add_decision(
            selection_decision_row(
                "SIG_SELECT", "SELECT_CANDIDATE", "C_SELECT", TARGET_EIN, "Current Identity Name"
            )
        )
        self.add_decision(
            selection_decision_row(
                "SIG_KEEP", "KEEP_REPORTED_EIN", "REPORTED_EIN", TARGET_EIN, "Recipient Filing Name"
            )
        )
        selection_before = {key: self.fetch_decision(key) for key in ("SIG_SELECT", "SIG_KEEP")}
        before = {key: self.fetch_decision(key) for key in ("SIG_REVIEW", "SIG_INVALID")}

        gai.cmd_repair_rule_nonselect_identities(self.args(apply=True))

        for signature_hash in before:
            after = self.fetch_decision(signature_hash)
            self.assertEqual(
                (after["selected_candidate_id"], after["selected_ein"], after["selected_name"]),
                ("", "", ""),
            )
            for column in gai.DECISION_COLUMNS:
                if column not in {"selected_candidate_id", "selected_ein", "selected_name"}:
                    self.assertEqual(after[column], before[signature_hash][column], column)
        self.assertEqual(self.fetch_decision("SIG_MANUAL"), manual_before)
        for signature_hash, expected in selection_before.items():
            self.assertEqual(self.fetch_decision(signature_hash), expected)

        with self.audit_path.open(encoding="utf-8-sig", newline="") as fh:
            audit = list(csv.DictReader(fh))
        self.assertEqual(len(audit), 2)
        self.assertEqual({row["repair_status"] for row in audit}, {"eligible_known_legacy_bug"})
        by_sig = {row["signature_hash"]: row for row in audit}
        self.assertEqual(by_sig["SIG_REVIEW"]["selected_ein"], TARGET_EIN)
        self.assertEqual(by_sig["SIG_INVALID"]["selected_name"], "Recipient Filing Name")

        second_audit = self.audit_path.with_name("repair-second-run.csv")
        gai.cmd_repair_rule_nonselect_identities(self.args(audit_path=second_audit))
        with second_audit.open(encoding="utf-8-sig", newline="") as fh:
            self.assertEqual(list(csv.DictReader(fh)), [])

    def test_candidate_hash_must_equal_an_exact_ordered_current_prefix(self):
        self.add_signature("SIG_PREFIX")
        candidates = [
            {"candidate_id": "C1", "ein": TARGET_EIN, "candidate_score": 150},
            {"candidate_id": "C2", "ein": "222222223", "candidate_score": 120},
        ]
        self.add_candidate("SIG_PREFIX", "C1", TARGET_EIN, 1, 150.0)
        self.add_candidate("SIG_PREFIX", "C2", "222222223", 2, 120.0)
        self.add_decision(
            legacy_decision_row(
                "SIG_PREFIX",
                candidate_set_hash=gai.candidate_set_fingerprint(candidates[:1]),
            )
        )

        gai.cmd_repair_rule_nonselect_identities(self.args(apply=True))
        self.assertEqual(self.fetch_decision("SIG_PREFIX")["selected_ein"], "")

    def test_unknown_identity_review_branch_is_verified_exactly(self):
        unknown_ein = "472772048"
        self.add_signature("SIG_UNKNOWN", unknown_ein, "Unknown Filing Recipient")
        self.add_decision(
            unknown_identity_review_row("SIG_UNKNOWN", unknown_ein, "Unknown Filing Recipient")
        )

        gai.cmd_repair_rule_nonselect_identities(self.args(apply=True))
        repaired = self.fetch_decision("SIG_UNKNOWN")
        self.assertEqual(
            (repaired["selected_candidate_id"], repaired["selected_ein"], repaired["selected_name"]),
            ("", "", ""),
        )

    def test_candidate_hash_that_is_not_a_current_prefix_is_refused(self):
        self.add_signature("SIG_HASH")
        self.add_candidate("SIG_HASH", "C1", TARGET_EIN, 1, 150.0)
        self.add_decision(
            legacy_decision_row(
                "SIG_HASH",
                candidate_set_hash=gai.stable_hash(["forged"], "CANDS_"),
            )
        )

        with self.assertRaisesRegex(RuntimeError, "did not match the verified legacy producer shape"):
            gai.cmd_repair_rule_nonselect_identities(self.args(apply=True))
        with self.audit_path.open(encoding="utf-8-sig", newline="") as fh:
            row = next(csv.DictReader(fh))
        self.assertIn("candidate_set_hash_not_current_ordered_prefix", row["repair_reasons"])
        self.assertEqual(self.fetch_decision("SIG_HASH")["selected_ein"], TARGET_EIN)

    def test_empty_candidate_hash_is_refused_when_current_candidates_exist(self):
        self.add_signature("SIG_EMPTY_HASH")
        self.add_candidate("SIG_EMPTY_HASH", "C1", TARGET_EIN, 1, 150)
        self.add_decision(legacy_decision_row("SIG_EMPTY_HASH"))

        with self.assertRaisesRegex(RuntimeError, "did not match the verified legacy producer shape"):
            gai.cmd_repair_rule_nonselect_identities(self.args(apply=True))
        with self.audit_path.open(encoding="utf-8-sig", newline="") as fh:
            row = next(csv.DictReader(fh))
        self.assertIn("candidate_set_hash_not_current_ordered_prefix", row["repair_reasons"])

    def test_default_dry_run_audits_without_mutating(self):
        self.add_signature("SIG_REVIEW")
        self.add_decision(legacy_decision_row("SIG_REVIEW"))
        before = self.fetch_decision("SIG_REVIEW")

        gai.cmd_repair_rule_nonselect_identities(self.args())

        self.assertEqual(self.fetch_decision("SIG_REVIEW"), before)
        with self.audit_path.open(encoding="utf-8-sig", newline="") as fh:
            row = next(csv.DictReader(fh))
        self.assertEqual(row["target_action"], "clear_selected_identity")
        self.assertEqual(row["selected_ein"], TARGET_EIN)

    def test_apply_refuses_unexpected_nonrule_or_forged_rule_shape(self):
        self.add_signature("SIG_RULE")
        self.add_signature("SIG_MANUAL")
        forged = list(legacy_decision_row("SIG_RULE"))
        forged[2] = "REPORTED_EIN"
        self.add_decision(tuple(forged))
        manual = list(legacy_decision_row("SIG_MANUAL"))
        manual[13] = "external:reviewed"
        self.add_decision(tuple(manual))

        with self.assertRaisesRegex(RuntimeError, "did not match the verified legacy producer shape"):
            gai.cmd_repair_rule_nonselect_identities(self.args(apply=True))

        self.assertEqual(self.fetch_decision("SIG_RULE")["selected_ein"], TARGET_EIN)
        self.assertEqual(self.fetch_decision("SIG_MANUAL")["selected_ein"], TARGET_EIN)
        with self.audit_path.open(encoding="utf-8-sig", newline="") as fh:
            audit = {row["signature_hash"]: row for row in csv.DictReader(fh)}
        self.assertIn("selected_candidate_id_not_exact_blank", audit["SIG_RULE"]["repair_reasons"])
        self.assertIn("unexpected_model_or_decision", audit["SIG_MANUAL"]["repair_reasons"])

    def test_assessment_rejects_malformed_boolean_output_stale_queue_and_applied_row(self):
        for signature_hash in ("SIG_BOOL", "SIG_OUTPUT", "SIG_QUEUE", "SIG_APPLIED"):
            self.add_signature(signature_hash)

        bad_bool = list(legacy_decision_row("SIG_BOOL"))
        bad_bool[10] = "garbage"
        self.add_decision(tuple(bad_bool))

        bad_output = list(legacy_decision_row("SIG_OUTPUT"))
        output_obj = json.loads(bad_output[18])
        output_obj["needs_human_review"] = "false"
        bad_output[18] = json.dumps(output_obj, ensure_ascii=False, sort_keys=True)
        self.add_decision(tuple(bad_output))

        self.add_decision(legacy_decision_row("SIG_QUEUE"))
        work = sqlite3.connect(self.work_path)
        work.execute(
            "UPDATE grant_recipient_signature SET ai_queue_status='no_candidates' WHERE signature_hash='SIG_QUEUE'"
        )
        work.commit()
        work.close()

        self.add_decision(legacy_decision_row("SIG_APPLIED"))
        main = sqlite3.connect(self.main_path)
        main.execute("INSERT INTO grant_recipient_ai_applied VALUES ('SIG_APPLIED', ?)", (TARGET_EIN,))
        main.commit()
        main.close()

        reasons = self.assessment_reasons()
        self.assertIn("auto_accept_not_canonical_false", reasons["SIG_BOOL"])
        self.assertIn("output_review_flag_mismatch", reasons["SIG_OUTPUT"])
        self.assertIn("current_signature_not_adjudicated", reasons["SIG_QUEUE"])
        self.assertIn("unexpected_applied_row", reasons["SIG_APPLIED"])

    def test_insert_boundary_rejects_nonselection_identity_and_auto_accept(self):
        self.add_signature("SIG_GUARD")
        conn = gai._connect_migration_target(str(self.main_path), str(self.work_path), readonly=False)
        try:
            identity_row = legacy_decision_row("SIG_GUARD")
            with self.assertRaisesRegex(RuntimeError, "selected identity fields must be blank"):
                gai.insert_decision(conn, identity_row)

            auto_row = list(identity_row)
            auto_row[2:5] = ["", "", ""]
            auto_row[10] = 1
            with self.assertRaisesRegex(RuntimeError, "auto_accept must be false"):
                gai.insert_decision(conn, tuple(auto_row))
            self.assertIsNone(
                conn.execute(
                    f"SELECT 1 FROM {gai.DECISION_TABLE} WHERE signature_hash='SIG_GUARD'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_audit_path_cannot_be_a_database_or_companion(self):
        protected = [
            self.main_path,
            Path(str(self.main_path) + "-wal"),
            self.work_path,
            Path(str(self.work_path) + "-journal"),
        ]
        for audit_path in protected:
            with self.subTest(audit_path=audit_path):
                with self.assertRaisesRegex(RuntimeError, "SQLite companion path"):
                    gai.cmd_repair_rule_nonselect_identities(self.args(audit_path=audit_path))


if __name__ == "__main__":
    unittest.main()
