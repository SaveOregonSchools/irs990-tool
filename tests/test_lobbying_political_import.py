import sqlite3
import tempfile
import unittest
from pathlib import Path

from import_lobbying_political_data import import_lobbying_data
from rebuild_irs990_slim_clean import extract_file


SCHEDULE_C_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Return returnVersion="2024v5.5">
  <ReturnHeader>
    <ReturnTypeCd>990</ReturnTypeCd>
    <TaxYr>2024</TaxYr>
    <TaxPeriodEndDt>2024-12-31</TaxPeriodEndDt>
    <Filer>
      <EIN>111111111</EIN>
      <BusinessName><BusinessNameLine1Txt>Schedule C Org</BusinessNameLine1Txt></BusinessName>
    </Filer>
  </ReturnHeader>
  <ReturnData>
    <IRS990>
      <LobbyingActivitiesInd>true</LobbyingActivitiesInd>
    </IRS990>
    <IRS990ScheduleC>
      <PoliticalExpendituresAmt>7812</PoliticalExpendituresAmt>
      <Expended527ActivitiesAmt>3210</Expended527ActivitiesAmt>
      <Form1120POLFiledInd>false</Form1120POLFiledInd>
      <OrganizationBelongsAffltGrpInd>X</OrganizationBelongsAffltGrpInd>
      <OtherExemptPurposeExpendGrp>
        <FilingOrganizationsTotalAmt>221966</FilingOrganizationsTotalAmt>
      </OtherExemptPurposeExpendGrp>
      <LobbyingNontaxableAmountGrp>
        <FilingOrganizationsTotalAmt>44393</FilingOrganizationsTotalAmt>
      </LobbyingNontaxableAmountGrp>
      <AvgLobbyingNontaxableAmountGrp>
        <CurrentYearMinus3Amt>42015</CurrentYearMinus3Amt>
        <CurrentYearMinus2Amt>42057</CurrentYearMinus2Amt>
        <CurrentYearMinus1Amt>43926</CurrentYearMinus1Amt>
        <CurrentYearAmt>44393</CurrentYearAmt>
        <TotalAmt>172391</TotalAmt>
      </AvgLobbyingNontaxableAmountGrp>
      <LobbyingCeilingAmt>258587</LobbyingCeilingAmt>
      <VolunteersInd>true</VolunteersInd>
      <PaidStaffOrManagementInd>true</PaidStaffOrManagementInd>
      <DirectContactLegislatorsInd>true</DirectContactLegislatorsInd>
      <DirectContactLegislatorsAmt>807</DirectContactLegislatorsAmt>
      <TotalLobbyingExpendituresAmt>807</TotalLobbyingExpendituresAmt>
      <NotDescribedSection501c3Ind>false</NotDescribedSection501c3Ind>
      <DuesAssessmentsAmt>142000</DuesAssessmentsAmt>
      <NonDeductibleLbbyngPltclCYAmt>6825</NonDeductibleLbbyngPltclCYAmt>
      <NonDeductibleLbbyngPltclTotAmt>6825</NonDeductibleLbbyngPltclTotAmt>
      <AggregateReportedDuesNtcAmt>6677</AggregateReportedDuesNtcAmt>
      <SubstantiallyAllDuesNondedInd>false</SubstantiallyAllDuesNondedInd>
      <SupplementalInformationDetail>
        <FormAndLineReferenceDesc>Part II-B, Line 1</FormAndLineReferenceDesc>
        <ExplanationTxt>Direct lobbying fixture explanation.</ExplanationTxt>
      </SupplementalInformationDetail>
    </IRS990ScheduleC>
  </ReturnData>
</Return>
"""


PF_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Return returnVersion="2024v5.5">
  <ReturnHeader>
    <ReturnTypeCd>990PF</ReturnTypeCd>
    <TaxYr>2024</TaxYr>
    <TaxPeriodEndDt>2024-12-31</TaxPeriodEndDt>
    <Filer>
      <EIN>222222222</EIN>
      <BusinessName><BusinessNameLine1Txt>PF Org</BusinessNameLine1Txt></BusinessName>
    </Filer>
  </ReturnHeader>
  <ReturnData>
    <IRS990PF>
      <StatementsRegardingActyGrp>
        <LegislativePoliticalActyInd>true</LegislativePoliticalActyInd>
        <MoreThan100SpentInd>true</MoreThan100SpentInd>
        <Form1120POLFiledInd>false</Form1120POLFiledInd>
      </StatementsRegardingActyGrp>
      <StatementsRegardingActy4720Grp>
        <InfluenceLegislationInd>true</InfluenceLegislationInd>
        <InfluenceElectionInd>false</InfluenceElectionInd>
      </StatementsRegardingActy4720Grp>
    </IRS990PF>
  </ReturnData>
</Return>
"""


class LobbyingPoliticalImportTests(unittest.TestCase):
    def test_extract_file_captures_expanded_schedule_c_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = Path(tmp) / "SCHC1_public.xml"
            xml_path.write_text(SCHEDULE_C_XML, encoding="utf-8")

            row = extract_file(str(xml_path))
            schedule_c = row["irs990_schedule_c_root"]

            self.assertEqual(schedule_c["political_expenditures_amt"], "7812")
            self.assertEqual(schedule_c["form1120_pol_filed_ind"], "0")
            self.assertEqual(schedule_c["organization_belongs_afflt_grp_ind"], "X")
            self.assertEqual(schedule_c["other_exempt_purpose_expend_amt"], 221966)
            self.assertEqual(schedule_c["lobbying_nontaxable_amt"], 44393)
            self.assertEqual(schedule_c["avg_lobbying_nontaxable_minus3_amt"], 42015)
            self.assertEqual(schedule_c["avg_lobbying_nontaxable_total_amt"], 172391)
            self.assertEqual(schedule_c["direct_contact_legislators_ind"], "X")
            self.assertEqual(schedule_c["not_described_section501c3_ind"], "0")
            self.assertEqual(schedule_c["non_deductible_lbbyng_pltcl_cy_amt"], "6825")

            supp = row["irs990_schedule_c_supplemental_info"]
            self.assertEqual(len(supp), 1)
            self.assertEqual(supp[0]["form_and_line_reference_desc"], "Part II-B, Line 1")

    def test_backfill_updates_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_dir = root / "xml"
            xml_dir.mkdir()
            (xml_dir / "SCHC1_public.xml").write_text(SCHEDULE_C_XML, encoding="utf-8")
            (xml_dir / "PF1_public.xml").write_text(PF_XML, encoding="utf-8")

            db_path = root / "fixture.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE returns (filing_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO returns VALUES (?)", [("SCHC1_public",), ("PF1_public",)])
            conn.commit()
            conn.close()

            totals = import_lobbying_data(
                db_path=db_path,
                xml_dir=xml_dir,
                workers=1,
                chunksize=1,
                commit_every=1,
            )

            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(totals["schedule_c"], 1)
                self.assertEqual(totals["schedule_c_supplemental"], 1)
                self.assertEqual(totals["pf_root"], 1)

                schc = conn.execute(
                    """
                    SELECT political_expenditures_amt, lobbying_nontaxable_amt,
                           direct_contact_legislators_amt, total_lobbying_expenditures_amt
                    FROM irs990_schedule_c_root
                    WHERE filing_id = 'SCHC1_public'
                    """
                ).fetchone()
                self.assertEqual(schc, (7812, 44393, 807, 807))

                supp_count = conn.execute(
                    "SELECT COUNT(*) FROM irs990_schedule_c_supplemental_info WHERE filing_id = 'SCHC1_public'"
                ).fetchone()[0]
                self.assertEqual(supp_count, 1)

                pf = conn.execute(
                    """
                    SELECT legislative_political_acty_ind, more_than100_spent_ind,
                           form1120_pol_filed_ind, influence_legislation_ind, influence_election_ind
                    FROM irs990_pf_root
                    WHERE filing_id = 'PF1_public'
                    """
                ).fetchone()
                self.assertEqual(pf, ("X", "X", "0", "X", "0"))
            finally:
                conn.close()

    def test_backfill_skips_duplicate_xml_object_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_dir = root / "xml"
            dup_a = xml_dir / "a"
            dup_b = xml_dir / "b"
            dup_a.mkdir(parents=True)
            dup_b.mkdir(parents=True)
            (dup_a / "DUP1_public.xml").write_text(SCHEDULE_C_XML, encoding="utf-8")
            (dup_b / "DUP1_public.xml").write_text(SCHEDULE_C_XML, encoding="utf-8")

            db_path = root / "fixture.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE returns (filing_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO returns VALUES (?)", ("DUP1_public",))
            conn.commit()
            conn.close()

            totals = import_lobbying_data(
                db_path=db_path,
                xml_dir=xml_dir,
                workers=1,
                chunksize=1,
                commit_every=1,
            )

            conn = sqlite3.connect(db_path)
            try:
                row_count = conn.execute("SELECT COUNT(*) FROM irs990_schedule_c_root").fetchone()[0]
                self.assertEqual(totals["files_seen"], 2)
                self.assertEqual(totals["files_selected"], 1)
                self.assertEqual(totals["files_processed"], 1)
                self.assertEqual(row_count, 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
