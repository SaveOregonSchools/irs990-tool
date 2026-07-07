import sqlite3
import unittest

from queries import fraud_risk_dashboard as mod
from queries import ngo_core_data
from queries import nonprofit_deep_dive as deep


def _core_row(**overrides):
    values = {h: "" for h in ngo_core_data.HEADERS}
    values.update(
        {
            "ein": "111111111",
            "org_name": "Risky Org",
            "tax_year": 2024,
            "return_type": "990",
            "period_end": "2024-12-31",
            "city": "Portland",
            "state": "OR",
            "tax_exempt_status": "501(c)(3)",
            "filing_id": "F1",
            "employees_count": 0,
            "total_revenue": 1000,
            "total_expenses": 5000,
            "revenue_less_expenses": -4000,
            "total_assets_eoy": 200,
            "total_liabilities_eoy": 800,
            "net_assets_eoy": -600,
            "grants_paid": 4000,
            "lobbying_expense": 500,
            "political_campaign_activity_ind": "Yes",
            "lobbying_activities_ind": "Yes",
            "dues_assessments_ind": "Yes",
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

        CREATE TABLE returns (
          filing_id TEXT PRIMARY KEY,
          ein TEXT,
          org_name TEXT,
          city TEXT,
          state TEXT
        );
        CREATE INDEX idx_returns_org_name_nocase ON returns(org_name COLLATE NOCASE);

        CREATE TABLE officers (
          filing_id TEXT,
          person_name TEXT,
          title_txt TEXT,
          avg_hours_week NUMERIC,
          comp_from_org NUMERIC,
          comp_from_related NUMERIC,
          other_compensation NUMERIC
        );

        CREATE TABLE grants_compat_v1 (
          filing_id TEXT,
          recipient_ein TEXT,
          recipient_name TEXT,
          cash_amount NUMERIC,
          noncash_amount NUMERIC
        );

        CREATE TABLE vw_contractors (
          filing_id TEXT,
          contractor_name TEXT,
          compensation_amt NUMERIC
        );

        CREATE TABLE sched_r_related_orgs_expanded (
          filing_id TEXT,
          controlled_organization_ind TEXT,
          involved_amt NUMERIC
        );
        """
    )
    conn.execute("INSERT INTO canonical_by_ein_year VALUES (?,?,?,?)", ("111111111", 2024, "F1", "990"))
    conn.execute("INSERT INTO canonical_by_ein_year VALUES (?,?,?,?)", ("111111111", 2023, "F0", "990"))
    conn.execute("INSERT INTO canonical_by_ein_year VALUES (?,?,?,?)", ("222222222", 2024, "F2", "990"))
    conn.executemany(
        "INSERT INTO returns VALUES (?,?,?,?,?)",
        [
            ("F1", "111111111", "Risky Org", "Portland", "OR"),
            ("F0", "111111111", "Risky Org", "Portland", "OR"),
            ("F2", "222222222", "Risky Foundation", "Salem", "OR"),
        ],
    )
    conn.executemany(
        "INSERT INTO officers VALUES (?,?,?,?,?,?,?)",
        [
            ("F1", "Jane Director", "CEO", 40, 1800, 200, 0),
            ("F1", "Jane Director", "CEO", 40, 1800, 200, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO grants_compat_v1 VALUES (?,?,?,?,?)",
        [
            ("F1", "", "Unknown Recipient", 2500, 0),
            ("F1", "333333333", "Known Recipient", 1500, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO vw_contractors VALUES (?,?,?)",
        [
            ("F1", "Major Vendor", 2200),
            ("F1", "Small Vendor", 100),
        ],
    )
    conn.execute("INSERT INTO sched_r_related_orgs_expanded VALUES (?,?,?)", ("F1", "X", 2000))
    conn.commit()
    return conn


class FraudRiskDashboardTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture_db()
        self.orig_connect = mod.connect_ro
        self.orig_deep_connect = deep.connect_ro
        self.orig_core_run = ngo_core_data.run
        mod.connect_ro = lambda: self.conn
        deep.connect_ro = lambda: self.conn
        ngo_core_data.run = lambda form: (
            ngo_core_data.HEADERS,
            [
                _core_row(),
                _core_row(
                    tax_year=2023,
                    filing_id="F0",
                    total_revenue=100,
                    total_expenses=100,
                    revenue_less_expenses=0,
                    total_assets_eoy=1000,
                    total_liabilities_eoy=100,
                    net_assets_eoy=900,
                    grants_paid=0,
                    lobbying_expense=0,
                    political_campaign_activity_ind="",
                    lobbying_activities_ind="",
                    dues_assessments_ind="",
                ),
            ],
        )

    def tearDown(self):
        mod.connect_ro = self.orig_connect
        deep.connect_ro = self.orig_deep_connect
        ngo_core_data.run = self.orig_core_run
        self.conn.close()

    def test_risk_dashboard_builds_explainable_indicators(self):
        headers, rows = mod.run({"ein": "11-1111111"})
        self.assertEqual(headers, mod.HEADERS)
        self.assertEqual(len(rows), 1)
        row = dict(zip(headers, rows[0]))
        self.assertEqual(row["ein"], "111111111")
        self.assertGreater(row["risk_score"], 0)
        self.assertGreater(row["high_indicators"], 0)

        html = mod.render_results({"ein": "11-1111111"}, headers, rows)
        self.assertIn("Fraud", mod.META["name"])
        self.assertIn("Risk Score", html)
        self.assertIn("Operating deficit", html)
        self.assertIn("Political campaign activity flag", html)
        self.assertIn("Most grant dollars lack recipient EINs", html)
        self.assertIn("Ways To Improve This Dashboard", html)
        self.assertIn("FEC and state campaign-finance APIs", html)
        self.assertIn("Print / Save PDF", mod.render_pdf_export({"ein": "11-1111111"}))

    def test_name_search_returns_selectable_matches(self):
        headers, rows = mod.run({"org_search": "Risky"})
        self.assertEqual(rows, [])
        html = mod.render_results({"org_search": "Risky"}, headers, rows)
        self.assertIn("Organization Matches", html)
        self.assertIn("Risky Org", html)
        self.assertIn("Analyze</button>", html)
        self.assertIn('name="qkey" value="fraud_risk_dashboard"', html)

    def test_requires_single_ein_or_name_search(self):
        headers, rows = mod.run({"ein": "111111111 222222222"})
        self.assertEqual(rows, [])
        html = mod.render_results({"ein": "111111111 222222222"}, headers, rows)
        self.assertIn("Enter exactly one valid 9-digit EIN", html)


if __name__ == "__main__":
    unittest.main()
