import sqlite3
import unittest

from queries import people_lookup as mod


def build_fixture_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE returns (
          filing_id TEXT PRIMARY KEY,
          ein TEXT,
          org_name TEXT,
          tax_year INTEGER,
          state TEXT,
          in_care_of_name TEXT,
          us_address_line1 TEXT,
          city TEXT,
          zip TEXT
        );

        CREATE TABLE officers (
          id INTEGER PRIMARY KEY,
          filing_id TEXT,
          person_name TEXT,
          title_txt TEXT,
          avg_hours_week NUMERIC,
          comp_from_org NUMERIC,
          comp_from_related NUMERIC,
          other_compensation NUMERIC
        );

        CREATE INDEX idx_officers_filing ON officers(filing_id);
        CREATE INDEX idx_officers_person_upper ON officers(UPPER(person_name));
        """
    )
    conn.execute(
        "INSERT INTO returns VALUES (?,?,?,?,?,?,?,?,?)",
        ("F1", "111111111", "Oregon Org", 2024, "OR", "", "1 Main", "Portland", "97201"),
    )
    conn.execute(
        "INSERT INTO returns VALUES (?,?,?,?,?,?,?,?,?)",
        ("F2", "222222222", "California Org", 2024, "CA", "Jane Doe", "2 Main", "Oakland", "94612"),
    )
    conn.execute(
        "INSERT INTO officers VALUES (?,?,?,?,?,?,?,?)",
        (1, "F1", "Jane Doe", "President", 10, 100, 0, 0),
    )
    conn.execute(
        "INSERT INTO officers VALUES (?,?,?,?,?,?,?,?)",
        (2, "F2", "Janet Doe", "Treasurer", 5, 50, 0, 0),
    )
    conn.commit()
    return conn


class PeopleLookupTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture_db()
        self.orig_connect = mod.connect_ro
        mod.connect_ro = lambda: self.conn

    def tearDown(self):
        mod.connect_ro = self.orig_connect
        self.conn.close()

    def test_filtered_exact_lookup_returns_matching_officer(self):
        headers, rows = mod.run(
            {
                "person_name": "Jane Doe",
                "fuzzy_match": "false",
                "state": "OR",
                "min_year": "2024",
                "max_year": "2024",
            }
        )

        self.assertEqual(headers, mod.HEADERS)
        self.assertEqual(len(rows), 1)
        row = dict(zip(headers, rows[0]))
        self.assertEqual(row["filing_id"], "F1")
        self.assertEqual(row["found_in"], "Officer/Director/Trustee")

    def test_in_care_of_path_still_uses_return_filters(self):
        headers, rows = mod.run(
            {
                "person_name": "Jane",
                "fuzzy_match": "on",
                "state": "CA",
                "min_year": "2024",
                "max_year": "2024",
            }
        )

        self.assertEqual(headers, mod.HEADERS)
        self.assertTrue(any(row[0] == "F2" and row[4] == "Returns (In Care Of Name)" for row in rows))


if __name__ == "__main__":
    unittest.main()
