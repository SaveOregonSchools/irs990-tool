import sqlite3
import unittest

from queries import ngo_related_orgs_sched_r as mod


def build_fixture_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE canonical_by_ein_year (
          ein TEXT,
          tax_year INTEGER,
          filing_id TEXT
        );

        CREATE TABLE returns (
          filing_id TEXT,
          state TEXT
        );

        CREATE TABLE irs990_schedule_r_id_related_tax_exempt_org_grp (
          filing_id TEXT,
          ein TEXT,
          business_name_line1_txt TEXT,
          business_name_line2_txt TEXT,
          disregarded_entity_name_business_name_line1_txt TEXT,
          disregarded_entity_name_business_name_line2_txt TEXT,
          exempt_code_section_txt TEXT,
          public_charity_status_txt TEXT,
          controlled_organization_ind TEXT,
          direct_controlling_nacd TEXT,
          primary_activities_txt TEXT,
          address_line1_txt TEXT,
          address_line2_txt TEXT,
          city_nm TEXT,
          state_abbreviation_cd TEXT,
          legal_domicile_state_cd TEXT,
          country_cd TEXT,
          foreign_postal_cd TEXT
        );

        CREATE TABLE irs990_schedule_r_id_related_org_txbl_corp_tr_grp (
          filing_id TEXT,
          ein TEXT,
          related_organization_name_business_name_line1_txt TEXT,
          related_organization_name_business_name_line2_txt TEXT,
          business_name_line1_txt TEXT,
          business_name_line2_txt TEXT,
          controlled_organization_ind TEXT,
          direct_controlling_nacd TEXT,
          primary_activities_txt TEXT,
          address_line1_txt TEXT,
          address_line2_txt TEXT,
          city_nm TEXT,
          state_abbreviation_cd TEXT,
          legal_domicile_state_cd TEXT,
          foreign_postal_cd TEXT,
          ownership_pct NUMERIC,
          share_of_total_income_amt NUMERIC,
          share_of_eoyassets_amt NUMERIC
        );

        CREATE TABLE irs990_schedule_r_id_related_org_txbl_partnership_grp (
          filing_id TEXT,
          ein TEXT,
          related_organization_name_business_name_line1_txt TEXT,
          related_organization_name_business_name_line2_txt TEXT,
          business_name_line1_txt TEXT,
          business_name_line2_txt TEXT,
          controlled_organization_ind TEXT,
          direct_controlling_nacd TEXT,
          primary_activities_txt TEXT,
          address_line1_txt TEXT,
          address_line2_txt TEXT,
          city_nm TEXT,
          state_abbreviation_cd TEXT,
          legal_domicile_state_cd TEXT,
          foreign_postal_cd TEXT,
          ownership_pct NUMERIC,
          share_of_total_income_amt NUMERIC,
          share_of_eoyassets_amt NUMERIC,
          ubicode_vamt NUMERIC
        );

        CREATE TABLE irs990_schedule_r_id_disregarded_entities_grp (
          filing_id TEXT,
          disregarded_entity_name_business_name_line1_txt TEXT,
          disregarded_entity_name_business_name_line2_txt TEXT,
          primary_activities_txt TEXT
        );

        CREATE TABLE irs990_schedule_r_transactions_related_org_grp (
          filing_id TEXT,
          business_name_line1_txt TEXT,
          business_name_line2_txt TEXT,
          involved_amt NUMERIC,
          transaction_type_txt TEXT,
          method_of_amount_determination_txt TEXT
        );

        CREATE TABLE irs990_schedule_r_unrelated_org_txbl_partnership_grp (
          filing_id TEXT,
          ein TEXT,
          business_name_line1_txt TEXT,
          general_or_managing_partner_ind TEXT,
          primary_activities_txt TEXT,
          address_line1_txt TEXT,
          address_line2_txt TEXT,
          city_nm TEXT,
          state_abbreviation_cd TEXT,
          legal_domicile_state_cd TEXT,
          ownership_pct NUMERIC,
          share_of_total_income_amt NUMERIC,
          share_of_eoyassets_amt NUMERIC,
          ubicode_vamt NUMERIC
        );
        """
    )
    conn.execute("INSERT INTO canonical_by_ein_year VALUES (?,?,?)", ("111111111", 2024, "F1"))
    conn.execute("INSERT INTO returns VALUES (?,?)", ("F1", "OR"))
    conn.execute(
        "INSERT INTO irs990_schedule_r_id_related_tax_exempt_org_grp VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "F1", "222222222", "Related Charity", "", "", "", "501(c)(3)",
            "9", "X", "", "Education", "1 Main", "", "Portland", "OR", "OR", "US", "",
        ),
    )
    conn.execute(
        "INSERT INTO irs990_schedule_r_unrelated_org_txbl_partnership_grp VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("F1", "333333333", "Unrelated LP", "", "Investing", "2 Main", "", "Portland", "OR", "OR", 25, 0, 0, 0),
    )
    conn.commit()
    return conn


class RelatedOrgsScheduleRTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture_db()
        self.orig_connect = mod.connect_ro
        mod.connect_ro = lambda: self.conn

    def tearDown(self):
        mod.connect_ro = self.orig_connect
        self.conn.close()

    def test_excludes_unrelated_partnership_by_default(self):
        headers, rows = mod.run({"ein_list": "111111111"})
        self.assertEqual(headers, mod.HEADERS)
        self.assertEqual(len(rows), 1)
        row = dict(zip(headers, rows[0]))
        self.assertEqual(row["related_ein"], "222222222")
        self.assertEqual(row["relationship_category"], "Related Tax-Exempt Org")

    def test_can_include_unrelated_partnership(self):
        headers, rows = mod.run({"ein_list": "111111111", "include_unrelated": "on"})
        categories = {dict(zip(headers, row))["relationship_category"] for row in rows}
        self.assertEqual(categories, {"Related Tax-Exempt Org", "Unrelated Taxable Partnership"})


if __name__ == "__main__":
    unittest.main()
