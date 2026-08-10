import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

import olms


class OlmsMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.irs_path = self.root / "irs.db"
        irs = sqlite3.connect(self.irs_path)
        irs.execute(
            "CREATE TABLE returns(ein TEXT,org_name TEXT,dba_name TEXT,city TEXT,state TEXT,zip TEXT)"
        )
        irs.executemany(
            "INSERT INTO returns VALUES (?,?,?,?,?,?)",
            [
                ("111111111", "Teachers Local 1", None, "Portland", "OR", "97201"),
                ("222222222", "Education Staff Guild", None, "Salem", "OR", "99999"),
                ("333333333", "Ambiguous Association", None, "Eugene", "OR", "97401"),
                ("444444444", "Ambiguous Association", None, "Eugene", "OR", "97401"),
                ("555555555", "Completely Different Name", None, "Bend", "OR", "97701"),
            ],
        )
        irs.commit()
        irs.close()
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE organizations(
              f_num INTEGER PRIMARY KEY,display_name TEXT,union_name TEXT,unit_name TEXT,
              city TEXT,state TEXT,zip TEXT
            )
            """
        )
        self.conn.executemany(
            "INSERT INTO organizations VALUES (?,?,?,?,?,?,?)",
            [
                (1, "Teachers Local 1", "Teachers", "Local 1", "Portland", "OR", "97201"),
                (2, "Education Staff Guild", "Education Staff Guild", None, "Salem", "OR", "97301"),
                (3, "Ambiguous Association", "Ambiguous Association", None, "Eugene", "OR", "97401"),
                (4, "Fuzzy Teachers Group", "Fuzzy Teachers Group", None, "Bend", "OR", "97701"),
            ],
        )
        self.conn.execute("CREATE TABLE counterparties(counterparty_id TEXT PRIMARY KEY)")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_exact_location_matches_ambiguity_and_fuzzy_name_only(self):
        olms.build_irs_matches(self.conn, self.irs_path, match_counterparties=False)
        accepted = dict(self.conn.execute(
            "SELECT f_num,candidate_ein FROM irs_matches WHERE match_status='MATCHED_HIGH_CONFIDENCE'"
        ))
        self.assertEqual(accepted[1], "111111111")
        self.assertEqual(accepted[2], "222222222")
        self.assertNotIn(3, accepted)
        self.assertTrue(all(
            row[0] == "CANDIDATE_REVIEW"
            for row in self.conn.execute("SELECT match_status FROM irs_matches WHERE f_num=3")
        ))
        self.assertEqual(
            self.conn.execute("SELECT match_status FROM irs_matches WHERE f_num=4").fetchone()[0],
            "UNMATCHED",
        )

    def test_manual_accept_reject_and_unmatch_override_automation(self):
        overrides = self.root / "overrides.csv"
        with overrides.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["f_num", "ein", "action", "note"])
            writer.writerow([1, "111111111", "reject", "wrong legal entity"])
            writer.writerow([2, "", "unmatch", "known separate entity"])
            writer.writerow([4, "555555555", "accept", "manually verified"])
        olms.build_irs_matches(self.conn, self.irs_path, overrides, match_counterparties=False)
        self.assertEqual(
            self.conn.execute("SELECT match_status FROM irs_matches WHERE f_num=1 AND candidate_ein='111111111'").fetchone()[0],
            "REJECTED_MANUAL",
        )
        self.assertEqual(
            self.conn.execute("SELECT match_status FROM irs_matches WHERE f_num=2 ORDER BY match_id DESC LIMIT 1").fetchone()[0],
            "UNMATCHED",
        )
        self.assertEqual(
            self.conn.execute("SELECT match_status FROM irs_matches WHERE f_num=4 ORDER BY match_id DESC LIMIT 1").fetchone()[0],
            "MATCHED_MANUAL",
        )


if __name__ == "__main__":
    unittest.main()
