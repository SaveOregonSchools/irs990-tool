from pathlib import Path

from rebuild_irs990_slim_clean import extract_file


GRANT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Return returnVersion="2024v5.5">
  <ReturnHeader>
    <ReturnTypeCd>990</ReturnTypeCd>
    <TaxYr>2024</TaxYr>
    <TaxPeriodEndDt>2024-12-31</TaxPeriodEndDt>
    <Filer>
      <EIN>111111111</EIN>
      <BusinessName>
        <BusinessNameLine1Txt>Grant Fixture Org</BusinessNameLine1Txt>
      </BusinessName>
    </Filer>
  </ReturnHeader>
  <ReturnData>
    <IRS990 />
    <IRS990ScheduleI>
      <RecipientTable />
      <RecipientTable>
        <CashGrantAmt>0</CashGrantAmt>
      </RecipientTable>
      <RecipientTable>
        <RecipientBusinessName>
          <BusinessNameLine1Txt>Zero Cash Recipient</BusinessNameLine1Txt>
        </RecipientBusinessName>
        <CashGrantAmt>0</CashGrantAmt>
        <NonCashAssistanceAmt>250</NonCashAssistanceAmt>
      </RecipientTable>
      <RecipientTable>
        <CashGrantAmt>0</CashGrantAmt>
        <PurposeOfGrantTxt>Zero-dollar award record</PurposeOfGrantTxt>
      </RecipientTable>
      <RecipientTable>
        <RecipientBusinessName>
          <BusinessNameLine1Txt>Amount Not Reported</BusinessNameLine1Txt>
        </RecipientBusinessName>
        <PurposeOfGrantTxt>Amount intentionally absent</PurposeOfGrantTxt>
      </RecipientTable>
    </IRS990ScheduleI>
  </ReturnData>
</Return>
"""


def test_grant_extraction_rejects_empty_and_zero_only_recipient_placeholders(
    tmp_path: Path,
) -> None:
    xml_path = tmp_path / "GRANT1_public.xml"
    xml_path.write_text(GRANT_XML, encoding="utf-8")

    grants = extract_file(str(xml_path))["grants"]

    assert len(grants) == 3
    by_name = {row["business_name_line1_txt"]: row for row in grants}

    named_zero = by_name["Zero Cash Recipient"]
    assert named_zero["cash_grant_amt"] == 0
    assert named_zero["non_cash_assistance_amt"] == 250

    purpose_zero = next(
        row for row in grants if row["purpose_of_grant_txt"] == "Zero-dollar award record"
    )
    assert purpose_zero["cash_grant_amt"] == 0

    missing_amount = by_name["Amount Not Reported"]
    assert missing_amount["cash_grant_amt"] is None
    assert missing_amount["non_cash_assistance_amt"] is None
