import sqlite3
import unittest

from queries import ngo_core_data
from queries import nonprofit_deep_dive as mod


def _core_row(**overrides):
    values = {h: "" for h in ngo_core_data.HEADERS}
    values.update(
        {
            "ein": "111111111",
            "org_name": "Deep Dive Org",
            "dba_name": "",
            "tax_year": 2024,
            "return_type": "990",
            "period_end": "2024-12-31",
            "state": "OR",
            "tax_exempt_status": "501(c)(3)",
            "filing_id": "F1",
            "contributions_and_grants": 1000,
            "program_service_revenue": 200,
            "investment_income": 50,
            "membership_dues": 25,
            "government_grants": 300,
            "other_revenue": 10,
            "total_revenue": 1500,
            "grants_paid": 250,
            "salaries_comp_emp_benefits": 400,
            "professional_fundraising_fees": 5,
            "total_fundraising_expenses": 30,
            "other_expenses": 200,
            "total_expenses": 1000,
            "revenue_less_expenses": 500,
            "total_assets_eoy": 2000,
            "total_liabilities_eoy": 300,
            "net_assets_eoy": 1700,
            "lobbying_expense": 100,
        }
    )
    values.update(overrides)
    return tuple(values[h] for h in ngo_core_data.HEADERS)


def build_fixture_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE canonical_by_ein_year (
          ein TEXT,
          tax_year INTEGER,
          filing_id TEXT,
          return_type TEXT
        );

        CREATE TABLE officers (
          filing_id TEXT,
          person_name TEXT,
          title_txt TEXT,
          comp_from_org NUMERIC,
          comp_from_related NUMERIC,
          other_compensation NUMERIC
        );
        """
    )
    conn.execute("INSERT INTO canonical_by_ein_year VALUES (?,?,?,?)", ("111111111", 2024, "F1", "990"))
    conn.execute(
        "INSERT INTO officers VALUES (?,?,?,?,?,?)",
        ("F1", "Jane Leader", "CEO", 120000, 0, 5000),
    )
    conn.commit()
    return conn


class NonprofitDeepDiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture_db()
        self.orig_connect = mod.connect_ro
        self.orig_core_run = ngo_core_data.run
        self.orig_grants_run = mod.ngo_grants_in.run
        mod.connect_ro = lambda: self.conn
        ngo_core_data.run = lambda form: (ngo_core_data.HEADERS, [_core_row()])
        mod.ngo_grants_in.run = lambda form: (
            ["tax_year", "grantor_ein", "grantor_org_name", "total_amount"],
            [
                (2024, "222222222", "Grantor A", 1000),
                (2024, "333333333", "Grantor B", 500),
                (2024, "222222222", "Grantor A", 250),
                (2024, "444444444", "Grantor C", 700),
                (2024, "555555555", "Grantor D", 100),
                (2024, "666666666", "Grantor E", 900),
                (2024, "777777777", "Grantor F", 50),
            ],
        )

    def tearDown(self):
        mod.connect_ro = self.orig_connect
        ngo_core_data.run = self.orig_core_run
        mod.ngo_grants_in.run = self.orig_grants_run
        self.conn.close()

    def test_single_ein_report_rows_and_rendered_cards(self):
        headers, rows = mod.run({"ein": "11-1111111"})
        self.assertEqual(headers, mod.HEADERS)
        self.assertEqual(len(rows), 1)
        row = dict(zip(headers, rows[0]))
        self.assertEqual(row["org_name"], "Deep Dive Org")
        self.assertEqual(row["lobbying_pct_expenses"], 10.0)
        self.assertIn("Grantor A", row["top_grantors"])
        self.assertIn("Grantor E", row["top_grantors"])
        self.assertNotIn("Grantor F", row["top_grantors"])

        html = mod.render_results({"ein": "11-1111111"}, headers, rows)
        self.assertIn("Revenue vs Expenses", html)
        self.assertIn("Grants Paid vs Government Grants", html)
        self.assertIn("Lobbying Expenses", html)
        self.assertIn("Top Grantors", html)
        self.assertIn("Grantor A", html)
        self.assertIn("Jane Leader", html)

    def test_requires_exactly_one_ein(self):
        headers, rows = mod.run({"ein": "111111111 222222222"})
        self.assertEqual(rows, [])
        html = mod.render_results({"ein": "111111111 222222222"}, headers, rows)
        self.assertIn("Enter exactly one valid 9-digit EIN", html)


if __name__ == "__main__":
    unittest.main()
