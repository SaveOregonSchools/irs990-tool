import csv
import hashlib
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

import fac_bulk
from build_fac_db import main as fac_cli_main


def write_csv(path: Path, headers, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


class FacBulkTests(unittest.TestCase):
    def make_current_fixture(self, root: Path) -> Path:
        current = root / "current"
        report_id = "2024-12-GSAFAC-0000000001"
        write_csv(
            current / "general.csv",
            [
                "report_id", "audit_year", "fy_start_date", "fy_end_date",
                "fac_accepted_date", "auditee_ein", "auditee_uei", "auditee_name",
                "entity_type", "audit_type", "total_amount_expended",
                "is_going_concern_included",
                "is_internal_control_material_weakness_disclosed",
                "is_internal_control_deficiency_disclosed",
                "is_material_noncompliance_disclosed", "is_low_risk_auditee",
                "auditor_firm_name", "is_public", "resubmission_version",
            ],
            [{
                "report_id": report_id,
                "audit_year": "2024",
                "fy_start_date": "2024-01-01",
                "fy_end_date": "2024-12-31",
                "fac_accepted_date": "2025-04-01",
                "auditee_ein": "12-3456789",
                "auditee_uei": "ABCDEF123456",
                "auditee_name": "Fixture Charity",
                "entity_type": "non-profit",
                "audit_type": "single-audit",
                "total_amount_expended": "1,250,000",
                "is_going_concern_included": "No",
                "is_internal_control_material_weakness_disclosed": "Yes",
                "is_internal_control_deficiency_disclosed": "Yes",
                "is_material_noncompliance_disclosed": "No",
                "is_low_risk_auditee": "No",
                "auditor_firm_name": "Fixture CPAs",
                "is_public": "True",
                "resubmission_version": "2",
            }],
        )
        write_csv(
            current / "federal_awards.csv",
            [
                "report_id", "audit_year", "award_reference", "federal_agency_prefix",
                "federal_award_extension", "federal_program_name", "amount_expended",
                "is_direct", "is_major", "audit_report_type", "findings_count",
            ],
            [{
                "report_id": report_id, "audit_year": "2024",
                "award_reference": "AWARD-0001", "federal_agency_prefix": "93",
                "federal_award_extension": "778", "federal_program_name": "Medical Assistance",
                "amount_expended": "900000", "is_direct": "Y", "is_major": "Y",
                "audit_report_type": "Q", "findings_count": "1",
            }],
        )
        write_csv(
            current / "findings.csv",
            [
                "report_id", "audit_year", "award_reference", "reference_number",
                "type_requirement", "is_modified_opinion", "is_material_weakness",
                "is_significant_deficiency", "is_questioned_costs", "is_repeat_finding",
            ],
            [{
                "report_id": report_id, "audit_year": "2024",
                "award_reference": "AWARD-0001", "reference_number": "2024-001",
                "type_requirement": "B", "is_modified_opinion": "Y",
                "is_material_weakness": "Y", "is_significant_deficiency": "Y",
                "is_questioned_costs": "Y", "is_repeat_finding": "N",
            }],
        )
        write_csv(
            current / "findings_text.csv",
            ["report_id", "audit_year", "finding_ref_number", "finding_text", "contains_chart_or_table"],
            [{
                "report_id": report_id, "audit_year": "2024",
                "finding_ref_number": "2024-001",
                "finding_text": "Unsupported charge was identified.",
                "contains_chart_or_table": "N",
            }],
        )
        write_csv(
            current / "corrective_action_plans.csv",
            ["report_id", "audit_year", "finding_ref_number", "planned_action", "contains_chart_or_table"],
            [{
                "report_id": report_id, "audit_year": "2024",
                "finding_ref_number": "2024-001",
                "planned_action": "Management will add a second-level review.",
                "contains_chart_or_table": "N",
            }],
        )
        write_csv(
            current / "additional_eins.csv",
            ["report_id", "audit_year", "additional_ein"],
            [{"report_id": report_id, "audit_year": "2024", "additional_ein": "98-7654321"}],
        )
        write_csv(
            current / "additional_ueis.csv",
            ["report_id", "audit_year", "additional_uei"],
            [{"report_id": report_id, "audit_year": "2024", "additional_uei": "ZYXWVU654321"}],
        )
        return current

    def make_historic_fixture(self, root: Path) -> Path:
        source_dir = root / "historic_source" / "2015"
        write_csv(
            source_dir / "ELECAUDITHEADER.csv",
            [
                "AUDITYEAR", "DBKEY", "EIN", "AUDITEENAME", "FYSTARTDATE",
                "FYENDDATE", "AUDITTYPE", "TOTFEDEXPEND", "GOINGCONCERN",
                "MATERIALWEAKNESS", "SIGNIFICANTDEFICIENCY", "MATERIALNONCOMPLIANCE",
                "LOWRISK", "CPAFIRMNAME",
            ],
            [{
                "AUDITYEAR": "2015", "DBKEY": "77", "EIN": "111223333",
                "AUDITEENAME": "Historic Charity", "FYSTARTDATE": "01/01/2015",
                "FYENDDATE": "12/31/2015", "AUDITTYPE": "S",
                "TOTFEDEXPEND": "$800,000", "GOINGCONCERN": "N",
                "MATERIALWEAKNESS": "Y", "SIGNIFICANTDEFICIENCY": "Y",
                "MATERIALNONCOMPLIANCE": "N", "LOWRISK": "N",
                "CPAFIRMNAME": "Old CPAs",
            }],
        )
        write_csv(
            source_dir / "ELECAUDITS.csv",
            [
                "AUDITYEAR", "DBKEY", "ELECAUDITSID", "CFDA",
                "FEDERALPROGRAMNAME", "AMOUNT", "DIRECT", "MAJORPROGRAM",
                "TYPEREPORT_MP", "FINDINGSCOUNT",
            ],
            [{
                "AUDITYEAR": "2015", "DBKEY": "77", "ELECAUDITSID": "5",
                "CFDA": "84.010", "FEDERALPROGRAMNAME": "Title I",
                "AMOUNT": "700000", "DIRECT": "N", "MAJORPROGRAM": "Y",
                "TYPEREPORT_MP": "U", "FINDINGSCOUNT": "1",
            }],
        )
        write_csv(
            source_dir / "ELECAUDITFINDINGS.csv",
            [
                "AUDITYEAR", "DBKEY", "ELECAUDITSID", "FINDINGREFNUMS",
                "TYPEREQUIREMENT", "MODIFIEDOPINION", "MATERIALWEAKNESS",
                "SIGNIFICANTDEFICIENCY", "QCOSTS", "REPEATFINDING",
            ],
            [{
                "AUDITYEAR": "2015", "DBKEY": "77", "ELECAUDITSID": "5",
                "FINDINGREFNUMS": "2015-001", "TYPEREQUIREMENT": "M",
                "MODIFIEDOPINION": "N", "MATERIALWEAKNESS": "Y",
                "SIGNIFICANTDEFICIENCY": "Y", "QCOSTS": "Y", "REPEATFINDING": "Y",
            }],
        )
        write_csv(
            source_dir / "ELECEINS.csv",
            ["AUDITYEAR", "DBKEY", "EIN"],
            [{"AUDITYEAR": "2015", "DBKEY": "77", "EIN": "444556666"}],
        )
        archive_path = root / "census-2015.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for csv_path in source_dir.glob("*.csv"):
                archive.write(csv_path, f"2015/{csv_path.name}")
        sha1 = hashlib.sha1(archive_path.read_bytes()).hexdigest()
        archive_path.with_suffix(".sha1").write_text(
            f"{sha1}  census-2015.zip\n", encoding="utf-8"
        )
        return archive_path

    def test_builds_current_and_historic_data_with_read_only_lookup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = self.make_current_fixture(root)
            historic = self.make_historic_fixture(root)
            db_path = root / "fac.db"

            summary = fac_bulk.build_fac_database(
                [current, historic],
                db_path,
                source_as_of_date="2026-08-14",
            )

            self.assertEqual(summary["coverage"]["current"]["report_count"], 1)
            self.assertEqual(summary["coverage"]["historic"]["report_count"], 1)
            self.assertEqual(summary["coverage"]["fac_findings_text"], 1)
            self.assertEqual(summary["coverage"]["fac_corrective_action_plans"], 1)

            conn = fac_bulk.connect_fac_readonly(db_path)
            try:
                metadata = dict(conn.execute("SELECT key, value FROM fac_metadata"))
                self.assertEqual(metadata["build_status"], "complete")
                self.assertEqual(metadata["source_as_of_date"], "2026-08-14")
                verified = conn.execute(
                    "SELECT MAX(official_sha1_verified) FROM fac_source_files WHERE source_era='historic'"
                ).fetchone()[0]
                self.assertEqual(verified, 1)
                indexes = {
                    row[1]
                    for row in conn.execute("PRAGMA index_list('fac_reports')")
                }
                self.assertIn("idx_fac_reports_ein_year", indexes)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM fac_reports")
            finally:
                conn.close()

            current_result = fac_bulk.lookup_fac_by_ein("12-3456789", db_path)
            self.assertEqual(current_result["status"], "ok")
            self.assertEqual(current_result["source"], "offline_fac_sidecar")
            report = current_result["reports"][0]
            self.assertEqual(report["ein_match"], "primary_ein")
            self.assertEqual(report["general"]["total_amount_expended"], 1_250_000)
            self.assertIs(report["general"]["is_internal_control_material_weakness_disclosed"], True)
            self.assertEqual(report["federal_awards"][0]["federal_program_name"], "Medical Assistance")
            self.assertIn("Unsupported charge", report["findings_text"][0]["finding_text"])
            self.assertIn("second-level review", report["corrective_action_plans"][0]["planned_action"])
            self.assertEqual(current_result["ueis"], ["ABCDEF123456"])

            additional_result = fac_bulk.lookup_fac_by_ein("987654321", db_path)
            self.assertEqual(additional_result["reports"][0]["ein_match"], "additional_ein")
            self.assertEqual(additional_result["ueis"], [])

            historic_result = fac_bulk.lookup_fac_by_ein("111223333", db_path)
            historic_report = historic_result["reports"][0]
            self.assertEqual(historic_report["report_id"], "historic:2015:77")
            self.assertEqual(historic_report["general"]["fy_end_date"], "2015-12-31")
            self.assertEqual(historic_report["findings_text"], [])
            self.assertEqual(historic_report["corrective_action_plans"], [])

    def test_interrupted_build_keeps_final_and_resumes_staging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = self.make_current_fixture(root)
            db_path = root / "fac.db"
            old = sqlite3.connect(db_path)
            old.execute("CREATE TABLE sentinel(value TEXT)")
            old.execute("INSERT INTO sentinel VALUES ('old')")
            old.commit()
            old.close()

            original_import = fac_bulk._import_candidate
            calls = 0

            def fail_on_second(conn, fingerprint, source_as_of_date):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated interruption")
                return original_import(conn, fingerprint, source_as_of_date)

            with mock.patch.object(fac_bulk, "_import_candidate", side_effect=fail_on_second):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    fac_bulk.build_fac_database(
                        [current], db_path, source_as_of_date="2026-08-14", replace=True
                    )

            old = sqlite3.connect(db_path)
            try:
                self.assertEqual(old.execute("SELECT value FROM sentinel").fetchone()[0], "old")
            finally:
                old.close()
            staging = root / "fac.building.db"
            self.assertTrue(staging.exists())

            summary = fac_bulk.build_fac_database(
                [current], db_path, source_as_of_date="2026-08-14", replace=True
            )
            self.assertTrue(summary["resumed"])
            self.assertGreaterEqual(summary["sources_resumed_from_staging"], 1)
            self.assertFalse(staging.exists())
            result = fac_bulk.lookup_fac_by_ein("123456789", db_path)
            self.assertEqual(result["status"], "ok")

    def test_partial_large_file_resumes_after_last_row_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "current"
            rows = [
                {
                    "report_id": f"2024-12-GSAFAC-{number:010d}",
                    "audit_year": "2024",
                    "auditee_ein": f"{number:09d}",
                    "auditee_name": f"Fixture {number}",
                }
                for number in range(1, 5)
            ]
            write_csv(
                source / "general.csv",
                ["report_id", "audit_year", "auditee_ein", "auditee_name"],
                rows,
            )
            db_path = root / "fac.db"
            original_insert = fac_bulk._insert_row
            calls = 0

            def fail_on_third(conn, row, fingerprint):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise RuntimeError("mid-file interruption")
                return original_insert(conn, row, fingerprint)

            with mock.patch.object(fac_bulk, "_IMPORT_CHECKPOINT_ROWS", 1), mock.patch.object(
                fac_bulk, "_insert_row", side_effect=fail_on_third
            ):
                with self.assertRaisesRegex(RuntimeError, "mid-file interruption"):
                    fac_bulk.build_fac_database(
                        [source], db_path, source_as_of_date="2026-08-14"
                    )

            staging = root / "fac.building.db"
            progress_conn = sqlite3.connect(staging)
            try:
                progress = progress_conn.execute(
                    "SELECT last_source_row_number, accepted_count FROM fac_import_progress"
                ).fetchone()
                self.assertEqual(progress, (3, 2))
            finally:
                progress_conn.close()

            with mock.patch.object(fac_bulk, "_IMPORT_CHECKPOINT_ROWS", 1):
                summary = fac_bulk.build_fac_database(
                    [source], db_path, source_as_of_date="2026-08-14"
                )
            self.assertTrue(summary["resumed"])
            self.assertEqual(summary["sources_resumed_from_staging"], 1)
            conn = fac_bulk.connect_fac_readonly(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM fac_reports").fetchone()[0], 4)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM fac_import_progress").fetchone()[0], 0)
            finally:
                conn.close()

    def test_bad_adjacent_historic_sha1_is_rejected_before_database_build(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = self.make_historic_fixture(root)
            archive.with_suffix(".sha1").write_text("0" * 40, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "SHA1 mismatch"):
                fac_bulk.build_fac_database([archive], root / "fac.db")
            self.assertFalse((root / "fac.db").exists())

    def test_cli_lists_official_urls_without_inputs(self):
        with mock.patch("builtins.print") as printer:
            self.assertEqual(fac_cli_main(["--print-download-urls"]), 0)
        output = printer.call_args.args[0]
        self.assertIn("general.csv", output)
        self.assertIn("census-1998-2015.zip", output)

    def test_missing_sidecar_is_explicitly_not_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = fac_bulk.lookup_fac_by_ein(
                "123456789", Path(temp_dir) / "missing.db"
            )
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["reason"], "missing_sidecar")

    def test_invalid_sidecar_isolated_as_bounded_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "wrong.db"
            sqlite3.connect(db_path).close()
            result = fac_bulk.lookup_fac_by_ein("123456789", db_path)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "invalid_sidecar")
        self.assertNotIn(str(db_path), repr(result))

    def test_builder_refuses_main_database_as_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            main_db = Path(temp_dir) / "irs990.db"
            with closing(sqlite3.connect(main_db)) as conn:
                conn.execute("CREATE TABLE returns(filing_id TEXT PRIMARY KEY)")
                conn.commit()
            with mock.patch.dict(
                fac_bulk.os.environ, {"IRS_DB_PATH": str(main_db)}, clear=False
            ):
                with self.assertRaisesRegex(RuntimeError, "separate"):
                    fac_bulk.build_fac_database([], main_db, replace=True)
            with closing(sqlite3.connect(main_db)) as conn:
                self.assertIsNotNone(
                    conn.execute("SELECT 1 FROM sqlite_master WHERE name='returns'").fetchone()
                )


if __name__ == "__main__":
    unittest.main()
