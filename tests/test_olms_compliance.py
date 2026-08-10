import sqlite3
import unittest

import olms


class OlmsComplianceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE filings (
              f_num INTEGER, pd_covered_from TEXT, pd_covered_to TEXT, yr_covered INTEGER,
              rpt_id INTEGER, amendment INTEGER, receive_date TEXT, form_type TEXT,
              hardship TEXT, terminate TEXT, term_date TEXT
            );
            CREATE TABLE organizations (
              f_num INTEGER PRIMARY KEY, terminated INTEGER, termination_date TEXT
            );
            """
        )

    def tearDown(self):
        self.conn.close()

    def _filing(self, f_num, rpt_id, end, received, amendment=0, hardship="F", terminate="F"):
        year = int(end[:4])
        self.conn.execute(
            "INSERT INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f_num, f"{year-1}-07-01", end, year, rpt_id, amendment, received, "LM-3", hardship, terminate, None),
        )

    def test_due_date_on_time_one_day_late_amendment_and_hardship(self):
        self._filing(1, 11, "2025-06-30", "2025-09-28")
        self._filing(2, 21, "2025-06-30", "2025-09-29")
        self._filing(3, 31, "2025-06-30", "2025-09-28")
        self._filing(3, 32, "2025-06-30", "2026-01-15", amendment=1)
        self._filing(4, 41, "2025-06-30", "2025-10-15", hardship="T")
        olms.build_filing_periods(self.conn)
        rows = {
            row[0]: row[1:]
            for row in self.conn.execute(
                "SELECT f_num,filing_status,days_late,initial_receive_date,latest_receive_date,amendment_count,due_date FROM filing_periods"
            )
        }
        self.assertEqual(rows[1][0], "FILED_ON_TIME")
        self.assertEqual(rows[1][5], "2025-09-28")
        self.assertEqual((rows[2][0], rows[2][1]), ("FILED_LATE", 1))
        self.assertEqual(rows[3][0], "FILED_ON_TIME")
        self.assertEqual(rows[3][2], "2025-09-28")
        self.assertEqual(rows[3][3], "2026-01-15")
        self.assertEqual(rows[3][4], 1)
        self.assertEqual(rows[4][0], "HARDSHIP_REVIEW")

    def test_missing_fye_change_termination_and_historical_gap(self):
        # Stable two-year history -> current missing expected.
        self._filing(10, 101, "2023-06-30", "2023-09-01")
        self._filing(10, 102, "2024-06-30", "2024-09-01")
        # Fiscal-year change -> review, no automatic current missing.
        self._filing(20, 201, "2023-06-30", "2023-09-01")
        self._filing(20, 202, "2024-12-31", "2025-02-01")
        # Terminated -> no expected next annual report.
        self._filing(30, 301, "2023-06-30", "2023-09-01")
        self._filing(30, 302, "2024-06-30", "2024-09-01", terminate="T")
        # Historical gap surrounded by reports.
        self._filing(40, 401, "2022-06-30", "2022-09-01")
        self._filing(40, 402, "2024-06-30", "2024-09-01")
        self.conn.executemany(
            "INSERT INTO organizations VALUES (?,?,?)",
            [(10, 0, None), (20, 0, None), (30, 1, "2024-06-30"), (40, 0, None)],
        )
        olms.build_filing_periods(self.conn)
        olms.build_compliance_results(self.conn, "2026-01-31")
        statuses = list(self.conn.execute(
            "SELECT f_num,status,result_kind,period_end FROM compliance_results WHERE result_kind<>'OBSERVED'"
        ))
        self.assertIn((10, "POTENTIAL_MISSING_FILING", "CURRENT_EXPECTATION", "2025-06-30"), statuses)
        self.assertIn((20, "FYE_CHANGED_REVIEW", "EXPECTATION", "2024-12-31"), statuses)
        self.assertTrue(any(row[0] == 30 and row[1] == "TERMINATED" for row in statuses))
        self.assertIn((40, "POTENTIAL_MISSING_FILING", "HISTORICAL_GAP", "2023-06-30"), statuses)


if __name__ == "__main__":
    unittest.main()
