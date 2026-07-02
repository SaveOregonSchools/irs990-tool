import sqlite3
import unittest

from queries import ngo_grants_io, ngo_grants_out


TARGET_EIN = "472772048"
FILER_EIN = "111111111"


def build_fixture_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE canonical_by_ein_year (
          ein TEXT,
          tax_year INTEGER,
          filing_id TEXT,
          return_type TEXT,
          return_ts TEXT,
          period_end TEXT
        );

        CREATE TABLE returns (
          filing_id TEXT,
          ein TEXT,
          org_name TEXT,
          dba_name TEXT,
          city TEXT,
          state TEXT,
          zip TEXT
        );

        CREATE TABLE grants_compat_v1 (
          filing_id TEXT,
          recipient_ein TEXT,
          recipient_name TEXT,
          city TEXT,
          state TEXT,
          country TEXT,
          cash_amount NUMERIC,
          noncash_amount NUMERIC,
          purpose TEXT
        );

        CREATE TABLE grants (
          id INTEGER PRIMARY KEY,
          filing_id TEXT,
          us_state_abbreviation_cd TEXT,
          foreign_country_cd TEXT,
          us_address_line1_txt TEXT,
          us_address_line2_txt TEXT,
          foreign_address_line1_txt TEXT
        );

        CREATE TABLE grant_recipient_resolved (
          grant_id INTEGER PRIMARY KEY,
          filing_id TEXT NOT NULL,
          grantor_ein TEXT,
          grantor_name TEXT,
          tax_year INTEGER,
          return_type TEXT,
          recipient_reported_ein TEXT,
          recipient_reported_name TEXT,
          recipient_city TEXT,
          recipient_state TEXT,
          recipient_zip TEXT,
          cash_amount NUMERIC,
          noncash_amount NUMERIC,
          total_amount NUMERIC,
          purpose TEXT,
          resolved_ein TEXT,
          resolved_org_name TEXT,
          resolved_city TEXT,
          resolved_state TEXT,
          resolved_zip TEXT,
          resolved_filing_id TEXT,
          match_status TEXT,
          match_method TEXT,
          confidence NUMERIC,
          name_score NUMERIC,
          address_score NUMERIC,
          warning_flags TEXT,
          candidate_count INTEGER,
          processed_at TEXT
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

        CREATE TABLE grant_recipient_ai_decision (
          signature_hash TEXT PRIMARY KEY,
          decision TEXT,
          selected_candidate_id TEXT,
          selected_ein TEXT,
          selected_name TEXT,
          confidence NUMERIC,
          reason_codes_json TEXT,
          explanation TEXT,
          needs_human_review INTEGER,
          auto_accept INTEGER,
          validation_status TEXT,
          validation_error TEXT,
          model TEXT
        );

        CREATE VIEW grant_recipient_resolved_plus_ai_v1 AS
        SELECT * FROM grant_recipient_resolved;
        """
    )
    conn.execute(
        "INSERT INTO canonical_by_ein_year VALUES (?,?,?,?,?,?)",
        (FILER_EIN, 2023, "F1", "990", "2024-05-01", "2023-12-31"),
    )
    conn.execute(
        "INSERT INTO returns VALUES (?,?,?,?,?,?,?)",
        ("F1", FILER_EIN, "Grantmaker Foundation", "", "Austin", "TX", "78701"),
    )
    conn.executemany(
        "INSERT INTO grants_compat_v1 VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("F1", TARGET_EIN, "Target Org Reported", "Los Angeles", "CA", "US", 1000, 0, "Legacy reported grant"),
            ("F1", "999999999", "Ambiguous Reported Name", "Los Angeles", "CA", "US", 2000, 0, "Enhanced matched grant"),
        ],
    )
    conn.executemany(
        "INSERT INTO grants VALUES (?,?,?,?,?,?,?)",
        [
            (1, "F1", "CA", None, "100 Main St", "", None),
            (2, "F1", "CA", None, "200 Main St", "", None),
        ],
    )
    conn.executemany(
        """
        INSERT INTO grant_recipient_resolved VALUES (
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        [
            (
                1, "F1", FILER_EIN, "Grantmaker Foundation", 2023, "990",
                TARGET_EIN, "Target Org Reported", "Los Angeles", "CA", "90001",
                1000, 0, 1000, "Legacy reported grant",
                TARGET_EIN, "Target Org Resolved", "Los Angeles", "CA", "90001", "RF1",
                "reported_ein_match", "reported_ein", 0.99, 1.0, 1.0, "", 1, "now",
            ),
            (
                2, "F1", FILER_EIN, "Grantmaker Foundation", 2023, "990",
                "999999999", "Ambiguous Reported Name", "Los Angeles", "CA", "90002",
                2000, 0, 2000, "Enhanced matched grant",
                "", "", "", "", "", "",
                "unresolved", "none", 0.25, 0.4, 0.0, "needs_ai", 3, "now",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO grant_recipient_ai_applied VALUES (?,?,?,?,?,?,?,?)",
        (2, "sig-2", TARGET_EIN, "Target Org AI Resolved", 0.97, "SELECT_CANDIDATE", "external:test-model", "now"),
    )
    conn.execute(
        "INSERT INTO grant_recipient_ai_decision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "sig-2", "SELECT_CANDIDATE", "cand-1", TARGET_EIN, "Target Org AI Resolved",
            0.97, '["name_address_match"]', "High-confidence fixture match", 0, 1, "ok", "", "external:test-model",
        ),
    )
    conn.commit()
    return conn


class EnhancedGrantQueryTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture_db()
        self.orig_out_connect = ngo_grants_out.connect_ro
        self.orig_io_connect = ngo_grants_io.connect_ro
        ngo_grants_out.connect_ro = lambda: self.conn
        ngo_grants_io.connect_ro = lambda: self.conn

    def tearDown(self):
        ngo_grants_out.connect_ro = self.orig_out_connect
        ngo_grants_io.connect_ro = self.orig_io_connect
        self.conn.close()

    def test_out_enhanced_resolves_paid_recipients_and_adds_audit_headers(self):
        legacy_headers, legacy_rows = ngo_grants_out.run({"ein_list": FILER_EIN})
        self.assertEqual(legacy_headers, ngo_grants_out.BASE_HEADERS)
        self.assertEqual(len(legacy_rows), 2)
        self.assertIn("999999999", {row[10] for row in legacy_rows})

        headers, rows = ngo_grants_out.run({"ein_list": FILER_EIN, "use_resolved_grants": "on"})
        self.assertEqual(headers, ngo_grants_out.BASE_HEADERS + ngo_grants_out.ENHANCED_AUDIT_HEADERS)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row) == len(headers) for row in rows))
        self.assertEqual({row[10] for row in rows}, {TARGET_EIN})
        self.assertIn("ai_adjudicated", {row[headers.index("final_match_source")] for row in rows})
        self.assertEqual(ngo_grants_out.export_headers({"use_resolved_grants": "on"}), headers)

    def test_io_received_enhanced_matches_final_resolved_ein(self):
        legacy_headers, legacy_rows = ngo_grants_io.run(
            {"ein_list": TARGET_EIN, "mode": "received", "dedupe": "false"}
        )
        self.assertEqual(legacy_headers, ngo_grants_io.BASE_HEADERS)
        self.assertEqual(len(legacy_rows), 1)

        headers, rows = ngo_grants_io.run(
            {
                "ein_list": "47-2772048",
                "mode": "received",
                "use_resolved_grants": "on",
                "dedupe": "false",
            }
        )
        self.assertEqual(headers, ngo_grants_io.BASE_HEADERS + ngo_grants_io.ENHANCED_AUDIT_HEADERS)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row) == len(headers) for row in rows))
        self.assertEqual({row[11] for row in rows}, {TARGET_EIN})
        self.assertIn("Target Org AI Resolved", {row[12] for row in rows})
        self.assertEqual(ngo_grants_io.export_headers({"use_resolved_grants": "on"}), headers)

    def test_io_paid_enhanced_resolves_recipient_identity(self):
        headers, rows = ngo_grants_io.run(
            {
                "ein_list": FILER_EIN,
                "mode": "paid",
                "use_resolved_grants": "on",
                "dedupe": "false",
            }
        )
        self.assertEqual(headers, ngo_grants_io.BASE_HEADERS + ngo_grants_io.ENHANCED_AUDIT_HEADERS)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row[11] for row in rows}, {TARGET_EIN})
        self.assertIn("ai_adjudicated", {row[headers.index("final_match_source")] for row in rows})

    def test_paid_enhanced_sql_keeps_resolved_lookup_candidate_scoped(self):
        self.assertIn("candidate_grants AS", ngo_grants_io._SQL_PAID_ENHANCED)
        self.assertIn("JOIN candidate_grants cg ON cg.grant_id = rr.grant_id", ngo_grants_io._ENHANCED_PAID_GSRC_CTE_BODY)
        self.assertIn("JOIN gsrc            ON gsrc.grant_id = c.grant_id", ngo_grants_io._SQL_PAID_ENHANCED)
        self.assertNotIn("JOIN gsrc            ON gsrc.filing_id = c.filing_id", ngo_grants_io._SQL_PAID_ENHANCED)

        self.assertIn("candidate_grants AS", ngo_grants_out._SQL_ENHANCED)
        self.assertIn("JOIN candidate_grants cg ON cg.grant_id = rr.grant_id", ngo_grants_out._SQL_ENHANCED)
        self.assertIn("JOIN gsrc       ON gsrc.grant_id = c.grant_id", ngo_grants_out._SQL_ENHANCED)
        self.assertNotIn("JOIN gsrc       ON gsrc.filing_id = c.filing_id", ngo_grants_out._SQL_ENHANCED)


if __name__ == "__main__":
    unittest.main()
