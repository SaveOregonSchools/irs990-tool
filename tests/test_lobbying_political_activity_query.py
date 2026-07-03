import sqlite3
import unittest

from queries import lobbying_political_activity as mod


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
          filing_id TEXT,
          org_name TEXT,
          dba_name TEXT,
          state TEXT
        );

        CREATE TABLE irs990_root (
          filing_id TEXT,
          political_campaign_acty_ind TEXT,
          lobbying_activities_ind TEXT,
          organization501c3_ind TEXT,
          organization501c_ind TEXT,
          attr_organization501c_type_txt TEXT
        );

        CREATE TABLE irs990_ez_root (
          filing_id TEXT,
          political_campaign_acty_ind TEXT,
          lobbying_activities_ind TEXT,
          organization501c3_ind TEXT,
          organization501c_ind TEXT,
          attr_organization501c_type_txt TEXT
        );

        CREATE TABLE irs990_pf_root (
          filing_id TEXT,
          organization501c3_exempt_pfind TEXT,
          organization4947a1_trtd_pfind TEXT,
          legislative_political_acty_ind TEXT,
          more_than100_spent_ind TEXT,
          form1120_pol_filed_ind TEXT,
          influence_legislation_ind TEXT,
          influence_election_ind TEXT
        );

        CREATE TABLE irs990_schedule_c_root (
          filing_id TEXT,
          political_expenditures_amt NUMERIC,
          expended527_activities_amt NUMERIC,
          total_exempt_function_expend_amt NUMERIC,
          form1120_pol_filed_ind TEXT,
          total_lobbying_expenditures_amt NUMERIC,
          total_direct_lobbying_amt NUMERIC,
          total_grassroots_lobbying_amt NUMERIC,
          lobbying_nontaxable_amt NUMERIC,
          grassroots_nontaxable_amt NUMERIC,
          lobbying_ceiling_amt NUMERIC,
          grassroots_ceiling_amt NUMERIC,
          lobbying_excess_amt NUMERIC,
          lobbying_grassroots_excess_amt NUMERIC,
          volunteers_ind TEXT,
          paid_staff_or_management_ind TEXT,
          media_advertisements_ind TEXT,
          media_advertisements_amt NUMERIC,
          mailings_members_ind TEXT,
          mailings_members_amt NUMERIC,
          publications_or_broadcast_ind TEXT,
          publications_or_broadcast_amt NUMERIC,
          grants_other_organizations_ind TEXT,
          grants_other_organizations_amt NUMERIC,
          direct_contact_legislators_ind TEXT,
          direct_contact_legislators_amt NUMERIC,
          rallies_demonstrations_ind TEXT,
          rallies_demonstrations_amt NUMERIC,
          other_activities_ind TEXT,
          other_activities_amt NUMERIC,
          not_described_section501c3_ind TEXT,
          substantially_all_dues_nonded_ind TEXT,
          dues_assessments_amt NUMERIC,
          non_deductible_lbbyng_pltcl_cy_amt NUMERIC,
          non_deductible_lbbyng_pltcl_tot_amt NUMERIC,
          aggregate_reported_dues_ntc_amt NUMERIC,
          carried_over_amt NUMERIC
        );

        CREATE TABLE irs990_schedule_c_supplemental_info (
          id INTEGER PRIMARY KEY,
          filing_id TEXT,
          form_and_line_reference_desc TEXT,
          explanation_txt TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO canonical_by_ein_year VALUES (?,?,?,?)",
        ("111111111", 2024, "F1", "990"),
    )
    conn.execute("INSERT INTO returns VALUES (?,?,?,?)", ("F1", "Advocacy Org", "", "OR"))
    conn.execute("INSERT INTO irs990_root VALUES (?,?,?,?,?,?)", ("F1", "0", "X", "X", "", ""))
    conn.execute(
        """
        INSERT INTO irs990_schedule_c_root VALUES (
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            "F1",
            500,
            100,
            100,
            "X",
            2500,
            1700,
            800,
            2000,
            500,
            10000,
            2500,
            0,
            0,
            "0",
            "X",
            "0",
            0,
            "0",
            0,
            "0",
            0,
            "0",
            0,
            "X",
            1700,
            "0",
            0,
            "0",
            0,
            "0",
            "X",
            12000,
            500,
            500,
            500,
            0,
        ),
    )
    conn.execute(
        "INSERT INTO irs990_schedule_c_supplemental_info VALUES (?,?,?,?)",
        (1, "F1", "Part II-B", "Direct contact with legislators."),
    )

    conn.execute(
        "INSERT INTO canonical_by_ein_year VALUES (?,?,?,?)",
        ("222222222", 2024, "F2", "990PF"),
    )
    conn.execute("INSERT INTO returns VALUES (?,?,?,?)", ("F2", "PF Org", "", "OR"))
    conn.execute("INSERT INTO irs990_pf_root VALUES (?,?,?,?,?,?,?,?)", ("F2", "X", "", "X", "X", "0", "X", "0"))
    conn.commit()
    return conn


class LobbyingPoliticalActivityQueryTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture_db()
        self.orig_connect = mod.connect_ro
        mod.connect_ro = lambda: self.conn

    def tearDown(self):
        mod.connect_ro = self.orig_connect
        self.conn.close()

    def test_requires_ein_state_or_return_all_by_default(self):
        headers, rows = mod.run({})
        self.assertEqual(headers, mod.HEADERS)
        self.assertEqual(rows, [])

    def test_ein_lookup_returns_activity_fields_and_supplemental_text(self):
        headers, rows = mod.run({"ein_list": "11-1111111"})
        self.assertEqual(len(rows), 1)
        row = dict(zip(headers, rows[0]))

        self.assertEqual(row["ein"], "111111111")
        self.assertEqual(row["org_name"], "Advocacy Org")
        self.assertEqual(row["tax_year"], 2024)
        self.assertEqual(row["return_type"], "990")
        self.assertEqual(row["tax_exempt_status"], "501(c)(3)")
        self.assertIn("political", row["activity_summary"])
        self.assertIn("lobbying", row["activity_summary"])
        self.assertIn("dues/proxy-tax", row["activity_summary"])
        self.assertEqual(row["total_lobbying_expenditures_amt"], 2500)
        self.assertEqual(row["direct_contact_legislators_amt"], 1700)
        self.assertEqual(row["schedule_c_supplemental_count"], 1)
        self.assertIn("Direct contact", row["schedule_c_explanations"])

    def test_state_filter_can_find_pf_flags(self):
        headers, rows = mod.run({"state": "OR", "activity_mode": "pf_flags"})
        self.assertEqual(len(rows), 1)
        row = dict(zip(headers, rows[0]))
        self.assertEqual(row["ein"], "222222222")
        self.assertEqual(row["return_type"], "990PF")
        self.assertIn("990-PF flags", row["activity_summary"])

    def test_min_amount_filters_amount_fields(self):
        headers, rows = mod.run({"state": "OR", "activity_mode": "any", "min_amount": "10000"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(zip(headers, rows[0]))["ein"], "111111111")


if __name__ == "__main__":
    unittest.main()
