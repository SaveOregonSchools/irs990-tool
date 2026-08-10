import sqlite3
import unittest
from io import StringIO

import olms


class OlmsParserTests(unittest.TestCase):
    def setUp(self):
        self.disbursement_columns = (
            olms.ColumnSpec("OID", "INTEGER", False, 0),
            olms.ColumnSpec("DISBURSEMENT_TYPE", "INTEGER", False, 1),
            olms.ColumnSpec("PURPOSE", "VARCHAR", True, 2),
            olms.ColumnSpec("DATE", "DATE", True, 3),
            olms.ColumnSpec("AMOUNT", "BIGINT", True, 4),
            olms.ColumnSpec("PAYER_PAYEE_ID", "INTEGER", True, 5),
            olms.ColumnSpec("RPT_ID", "INTEGER", True, 6),
        )

    def test_valid_pipe_record(self):
        result = olms.parse_record("1|503|Community grant|2025-06-30|100|9|88", self.disbursement_columns)
        self.assertTrue(result.ok)
        self.assertEqual(result.values[4], 100)

    def test_literal_pipe_inside_purpose_is_repaired(self):
        result = olms.parse_record("1|503|Youth|education grant|2025-06-30|100|9|88", self.disbursement_columns)
        self.assertTrue(result.ok)
        self.assertEqual(result.repair_type, "LITERAL_PIPE")
        self.assertEqual(result.values[2], "Youth|education grant")

    def test_literal_pipe_inside_name_and_address_can_be_repaired_when_unique(self):
        for field_name in ("NAME", "STREET_ADDRESS"):
            columns = (
                olms.ColumnSpec("OID", "INTEGER", False, 0),
                olms.ColumnSpec(field_name, "VARCHAR", True, 1),
                olms.ColumnSpec("RPT_ID", "INTEGER", False, 2),
            )
            result = olms.parse_record("1|Alpha|Beta|99", columns)
            self.assertTrue(result.ok)
            self.assertEqual(result.values[1], "Alpha|Beta")

    def test_embedded_newline_is_repaired_as_space(self):
        handle = StringIO("1|504|Security Services\n|2025-01-01|31222|7|99\n")
        records = list(olms.iter_logical_records(handle, self.disbursement_columns))
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].result.ok)
        self.assertEqual(records[0].result.repair_type, "EMBEDDED_NEWLINE")
        self.assertEqual(records[0].result.values[2], "Security Services")

    def test_invalid_integer_date_too_few_and_not_null_are_quarantined(self):
        invalid_integer = olms.parse_record("X|503|Grant|2025-06-30|100|9|88", self.disbursement_columns)
        invalid_date = olms.parse_record("1|503|Grant|06/30/2025|100|9|88", self.disbursement_columns)
        too_few = olms.parse_record("1|503|Grant", self.disbursement_columns)
        not_null = olms.parse_record("|503|Grant|2025-06-30|100|9|88", self.disbursement_columns)
        for result in (invalid_integer, invalid_date, too_few, not_null):
            self.assertFalse(result.ok)

    def test_ambiguous_literal_pipe_in_name_address_area_is_not_guessed(self):
        columns = (
            olms.ColumnSpec("PAYER_PAYEE_ID", "INTEGER", False, 0),
            olms.ColumnSpec("PAYER_PAYEE_TYPE", "INTEGER", False, 1),
            olms.ColumnSpec("RCPT_DISB_TYPE", "INTEGER", False, 2),
            olms.ColumnSpec("NAME", "VARCHAR", True, 3),
            olms.ColumnSpec("PO_BOX", "VARCHAR", True, 4),
            olms.ColumnSpec("STREET", "VARCHAR", True, 5),
            olms.ColumnSpec("CITY", "VARCHAR", True, 6),
            olms.ColumnSpec("STATE", "CHAR", True, 7),
            olms.ColumnSpec("ZIP", "VARCHAR", True, 8),
            olms.ColumnSpec("TYPE_OR_CLASS", "VARCHAR", True, 9),
            olms.ColumnSpec("ITEMIZED", "INTEGER", True, 10),
            olms.ColumnSpec("NON_ITEMIZED", "INTEGER", True, 11),
            olms.ColumnSpec("TOTAL", "INTEGER", True, 12),
            olms.ColumnSpec("RPT_ID", "INTEGER", True, 13),
        )
        raw = "3636026|1002|503|ART DISPLAY COMPANY, INC||401 HAMPTON PARK BLVD|CAPITOL HEIGHTS|MD|20743|SIGNS|DISPLAYS FABRICATION||7808|7808|938069"
        result = olms.parse_record(raw, columns)
        self.assertFalse(result.ok)
        self.assertIn("equally plausible repairs", result.error)


class OlmsAuditTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        olms.create_audit_schema(self.conn)
        self.conn.execute(
            "INSERT INTO import_runs(started_at,input_directory,requested_years,mode,status) VALUES ('x','x','2025','REBUILD','RUNNING')"
        )

    def tearDown(self):
        self.conn.close()

    def _register_table(self, name, columns):
        olms.ensure_source_table(self.conn, name, columns)
        for column in columns:
            self.conn.execute(
                "INSERT INTO olms_schema_versions VALUES (1,2025,?,?,?,?,?,?,?,?)",
                (name, name, f"{name}.txt", "hash", column.position, column.name, column.dol_type, int(column.nullable)),
            )

    def test_identical_and_conflicting_duplicates_are_audited(self):
        cols = (olms.ColumnSpec("RPT_ID", "INTEGER", False, 0), olms.ColumnSpec("NAME", "VARCHAR", True, 1))
        self._register_table("filings", cols)
        self.conn.executemany(
            "INSERT INTO filings(rpt_id,name,_source_year,_source_file,_source_row,_import_run_id,_raw_hash) VALUES (?,?,?,?,?,?,?)",
            [
                (1, "Same", 2024, "a", 2, 1, "same"),
                (1, "Same", 2025, "b", 2, 1, "same"),
                (2, "First", 2024, "a", 3, 1, "first"),
                (2, "Second", 2025, "b", 3, 1, "second"),
            ],
        )
        identical, conflicting = olms.deduplicate_sources(self.conn, 1)
        self.assertEqual((identical, conflicting), (1, 2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0], 1)
        kinds = dict(self.conn.execute("SELECT conflict_type,occurrence_count FROM import_duplicate_conflicts"))
        self.assertEqual(kinds["IDENTICAL_DUPLICATE"], 2)
        self.assertEqual(kinds["CONFLICTING_DUPLICATE"], 2)

    def test_orphan_rpt_id_and_schema_superset_column_are_recorded(self):
        filing_cols = (olms.ColumnSpec("RPT_ID", "INTEGER", False, 0),)
        detail_cols = (
            olms.ColumnSpec("OID", "INTEGER", False, 0),
            olms.ColumnSpec("RPT_ID", "INTEGER", True, 1),
        )
        self._register_table("filings", filing_cols)
        self._register_table("membership", detail_cols)
        olms.ensure_source_table(
            self.conn,
            "membership",
            detail_cols + (olms.ColumnSpec("NEW_2027_FIELD", "VARCHAR", True, 2),),
        )
        self.conn.execute(
            "INSERT INTO filings(rpt_id,_source_year,_source_file,_source_row,_import_run_id,_raw_hash) VALUES (1,2025,'f',2,1,'a')"
        )
        self.conn.executemany(
            "INSERT INTO membership(oid,rpt_id,_source_year,_source_file,_source_row,_import_run_id,_raw_hash) VALUES (?,?,?,?,?,?,?)",
            [(1, 1, 2025, "m", 2, 1, "a"), (2, 999, 2025, "m", 3, 1, "b")],
        )
        self.assertEqual(olms.record_orphans(self.conn, 1), 1)
        self.assertEqual(self.conn.execute("SELECT rpt_id FROM import_orphans").fetchone()[0], 999)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(membership)")}
        self.assertIn("new_2027_field", columns)


if __name__ == "__main__":
    unittest.main()
