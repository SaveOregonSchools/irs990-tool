BEGIN TRANSACTION;
CREATE TABLE IF NOT EXISTS canonical_by_ein_year (
  ein TEXT NOT NULL,
  tax_year INTEGER NOT NULL,
  filing_id TEXT NOT NULL,
  return_type TEXT,
  return_ts TEXT,
  amended_return_ind TEXT,
  period_end TEXT,
  PRIMARY KEY (ein, tax_year)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS core_hot (
  filing_id TEXT PRIMARY KEY,
  total_revenue NUMERIC,
  total_expenses NUMERIC,
  net_assets_boy NUMERIC,
  net_assets_eoy NUMERIC,
  contributions NUMERIC,
  program_service_revenue NUMERIC,
  membership_dues NUMERIC,
  investment_income NUMERIC,
  government_grants NUMERIC,
  grants_paid NUMERIC,
  lobbying_expense NUMERIC,
  employees_count INTEGER,
  volunteers_count INTEGER,
  mission_desc TEXT
);
CREATE TABLE IF NOT EXISTS former_key_people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_name TEXT,
  title_txt TEXT,
  comp_from_org NUMERIC,
  comp_from_related NUMERIC,
  other_compensation NUMERIC
);
CREATE TABLE IF NOT EXISTS grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  filer_ein TEXT,
  filer_name TEXT,
  recipient_ein TEXT,
  business_name_line1_txt TEXT,
  business_name_line2_txt TEXT,
  us_address_line1_txt TEXT,
  us_address_line2_txt TEXT,
  us_city_nm TEXT,
  us_state_abbreviation_cd TEXT,
  us_zip_cd TEXT,
  foreign_address_line1_txt TEXT,
  foreign_city_nm TEXT,
  foreign_province_or_state_nm TEXT,
  foreign_postal_cd TEXT,
  foreign_country_cd TEXT,
  ircsection_desc TEXT,
  cash_grant_amt NUMERIC,
  non_cash_assistance_amt NUMERIC,
  non_cash_assistance_desc TEXT,
  valuation_method_used_desc TEXT,
  purpose_of_grant_txt TEXT
);
CREATE TABLE IF NOT EXISTS highest_comp_employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_name TEXT,
  title_txt TEXT,
  avg_hours_week NUMERIC,
  comp_from_org NUMERIC,
  comp_from_related NUMERIC,
  other_compensation NUMERIC
);
CREATE TABLE IF NOT EXISTS irs990_books_in_care_of_detail (
  filing_id TEXT PRIMARY KEY,
  address_line1_txt TEXT,
  city_nm TEXT,
  state_abbreviation_cd TEXT,
  zipcd TEXT,
  phone_num TEXT,
  person_nm TEXT
);
CREATE TABLE IF NOT EXISTS irs990_contractor_compensation_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  compensation_amt NUMERIC,
  address_line1_txt TEXT,
  address_line2_txt TEXT,
  city_nm TEXT,
  country_cd TEXT,
  foreign_postal_cd TEXT,
  province_or_state_nm TEXT,
  usaddress_address_line1_txt TEXT,
  usaddress_address_line2_txt TEXT,
  usaddress_city_nm TEXT,
  state_abbreviation_cd TEXT,
  zipcd TEXT,
  business_name_line1_txt TEXT,
  business_name_line2_txt TEXT,
  person_nm TEXT,
  services_desc TEXT
);
CREATE TABLE IF NOT EXISTS irs990_ez_books_in_care_of_detail (
  filing_id TEXT PRIMARY KEY,
  address_line1_txt TEXT,
  city_nm TEXT,
  state_abbreviation_cd TEXT,
  zipcd TEXT,
  phone_num TEXT,
  person_nm TEXT
);
CREATE TABLE IF NOT EXISTS irs990_ez_form990_total_assets_grp (
  filing_id TEXT PRIMARY KEY,
  boyamt NUMERIC,
  eoyamt NUMERIC
);
CREATE TABLE IF NOT EXISTS irs990_ez_officer_director_trustee_empl_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  title_txt TEXT,
  average_hrs_per_wk_devoted_to_pos_rt TEXT,
  compensation_amt NUMERIC,
  employee_benefit_program_amt NUMERIC,
  expense_account_other_allwnc_amt NUMERIC
);
CREATE TABLE IF NOT EXISTS irs990_ez_root (
  filing_id TEXT PRIMARY KEY,
  net_assets_or_fund_balances_boyamt NUMERIC,
  net_assets_or_fund_balances_eoyamt NUMERIC,
  program_service_revenue_amt NUMERIC,
  membership_dues_amt NUMERIC,
  investment_income_amt NUMERIC,
  grants_and_similar_amounts_paid_amt NUMERIC,
  primary_exempt_purpose_txt TEXT,
  address_change_ind TEXT,
  name_change_ind TEXT,
  initial_return_ind TEXT,
  final_return_ind TEXT,
  amended_return_ind TEXT,
  application_pending_ind TEXT,
  website_address_txt TEXT,
  organization501c3_ind TEXT,
  organization501c_ind TEXT,
  attr_organization501c_type_txt TEXT,
  organization4947a1_not_pfind TEXT,
  type_of_organization_corp_ind TEXT,
  type_of_organization_trust_ind TEXT,
  type_of_organization_assoc_ind TEXT,
  type_of_organization_other_ind TEXT,
  type_of_organization_other_desc TEXT,
  other_revenue_total_amt NUMERIC,
  salaries_other_comp_empl_bnft_amt NUMERIC,
  other_expenses_total_amt NUMERIC,
  excess_or_deficit_for_year_amt NUMERIC,
  political_campaign_acty_ind TEXT,
  lobbying_activities_ind TEXT
);
CREATE TABLE IF NOT EXISTS irs990_ez_sum_of_total_liabilities_grp (
  filing_id TEXT PRIMARY KEY,
  boyamt NUMERIC,
  eoyamt NUMERIC
);
CREATE TABLE IF NOT EXISTS irs990_pf_analysis_of_revenue_and_expenses (
  filing_id TEXT PRIMARY KEY,
  other_income_rev_and_expnss_amt NUMERIC,
  oth_empl_slrs_wgs_rev_and_expnss_amt NUMERIC,
  pension_empl_bnft_rev_and_expnss_amt NUMERIC,
  other_expenses_rev_and_expnss_amt NUMERIC,
  excess_revenue_over_expenses_amt NUMERIC,
  interest_on_savings_temp_cash_investments_rev_and_expnss_amt NUMERIC,
  dividends_interest_securities_rev_and_expnss_amt NUMERIC,
  net_investment_income_amt NUMERIC,
  contri_paid_rev_and_expnss_amt NUMERIC
);
CREATE TABLE IF NOT EXISTS irs990_pf_form990_pfbalance_sheets_grp (
  filing_id TEXT PRIMARY KEY,
  total_assets_boyamt NUMERIC,
  total_assets_eoyamt NUMERIC,
  total_liabilities_boyamt NUMERIC,
  total_liabilities_eoyamt NUMERIC
);
CREATE TABLE IF NOT EXISTS irs990_pf_officer_dir_trst_key_empl_info_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  title_txt TEXT,
  average_hrs_per_wk_devoted_to_pos_rt TEXT,
  compensation_amt NUMERIC,
  employee_benefits_amt NUMERIC,
  expense_account_amt NUMERIC
);
CREATE TABLE IF NOT EXISTS irs990_pf_root (
  filing_id TEXT PRIMARY KEY,
  address_change_ind TEXT,
  name_change_ind TEXT,
  initial_return_ind TEXT,
  final_return_ind TEXT,
  amended_return_ind TEXT,
  application_pending_ind TEXT,
  organization501c3_exempt_pfind TEXT,
  organization4947a1_trtd_pfind TEXT,
  website_address_txt TEXT,
  legislative_political_acty_ind TEXT,
  total_grant_or_contri_pd_dur_yr_amt NUMERIC,
  contri_paid_rev_and_expnss_amt NUMERIC,
  mission_desc_txt TEXT
);
CREATE TABLE IF NOT EXISTS irs990_root (
  filing_id TEXT PRIMARY KEY,
  net_assets_or_fund_balances_boyamt NUMERIC,
  net_assets_or_fund_balances_eoyamt NUMERIC,
  total_program_service_revenue_amt NUMERIC,
  cyprogram_service_revenue_amt NUMERIC,
  membership_dues_amt NUMERIC,
  cyinvestment_income_amt NUMERIC,
  cygrants_and_similar_paid_amt NUMERIC,
  total_employee_cnt TEXT,
  employee_cnt TEXT,
  total_volunteers_cnt TEXT,
  mission_desc TEXT,
  activity_or_mission_desc TEXT,
  address_change_ind TEXT,
  name_change_ind TEXT,
  initial_return_ind TEXT,
  final_return_ind TEXT,
  amended_return_ind TEXT,
  application_pending_ind TEXT,
  website_address_txt TEXT,
  formation_yr INTEGER,
  legal_domicile_state_cd TEXT,
  organization501c3_ind TEXT,
  organization501c_ind TEXT,
  attr_organization501c_type_txt TEXT,
  organization4947a1_not_pfind TEXT,
  type_of_organization_corp_ind TEXT,
  type_of_organization_trust_ind TEXT,
  type_of_organization_assoc_ind TEXT,
  type_of_organization_other_ind TEXT,
  other_organization_dsc TEXT,
  cyother_revenue_amt NUMERIC,
  cysalaries_comp_emp_bnft_paid_amt NUMERIC,
  cytotal_prof_fndrsng_expns_amt NUMERIC,
  cytotal_fundraising_expense_amt NUMERIC,
  cyother_expenses_amt NUMERIC,
  cyrevenues_less_expenses_amt NUMERIC,
  total_assets_boyamt NUMERIC,
  total_assets_eoyamt NUMERIC,
  total_liabilities_boyamt NUMERIC,
  total_liabilities_eoyamt NUMERIC,
  political_campaign_acty_ind TEXT,
  lobbying_activities_ind TEXT
);
CREATE TABLE IF NOT EXISTS irs990_schedule_c_root (
  filing_id TEXT PRIMARY KEY,
  total_lobbying_expenditures_amt NUMERIC,
  substantially_all_dues_nonded_ind TEXT
);
CREATE TABLE IF NOT EXISTS irs990_schedule_j_rltd_org_officer_trst_key_empl_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  title_txt TEXT,
  base_compensation_filing_org_amt NUMERIC,
  bonus_filing_organization_amount NUMERIC,
  total_compensation_filing_org_amt NUMERIC,
  total_compensation_rltd_orgs_amt NUMERIC
);
CREATE TABLE IF NOT EXISTS irs990_schedule_l_bus_tr_involve_interested_prsn_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  relationship_description_txt TEXT,
  transaction_amt NUMERIC,
  transaction_desc TEXT
);
CREATE TABLE IF NOT EXISTS irs990_schedule_l_disqualified_person_ex_bnft_tr_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  rln_disqualified_person_org_txt TEXT,
  transaction_corrected_ind TEXT,
  transaction_desc TEXT
);
CREATE TABLE IF NOT EXISTS irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  relationship_with_org_txt TEXT,
  cash_grant_amt NUMERIC,
  type_of_assistance_txt TEXT,
  assistance_purpose_txt TEXT
);
CREATE TABLE IF NOT EXISTS irs990_schedule_l_loans_btwn_org_interested_prsn_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  relationship_with_org_txt TEXT,
  original_principal_amt NUMERIC,
  balance_due_amt NUMERIC,
  loan_purpose_txt TEXT
);
CREATE TABLE IF NOT EXISTS irs990_schedule_r_id_disregarded_entities_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  disregarded_entity_name_business_name_line1_txt TEXT,
  disregarded_entity_name_business_name_line2_txt TEXT,
  primary_activities_txt TEXT
);
CREATE TABLE IF NOT EXISTS irs990_schedule_r_id_related_org_txbl_corp_tr_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS irs990_schedule_r_id_related_org_txbl_partnership_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS irs990_schedule_r_id_related_tax_exempt_org_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS irs990_schedule_r_transactions_related_org_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  business_name_line1_txt TEXT,
  business_name_line2_txt TEXT,
  involved_amt NUMERIC,
  transaction_type_txt TEXT,
  method_of_amount_determination_txt TEXT
);
CREATE TABLE IF NOT EXISTS irs990_schedule_r_unrelated_org_txbl_partnership_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS officers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_name TEXT,
  title_txt TEXT,
  avg_hours_week NUMERIC,
  comp_from_org NUMERIC,
  comp_from_related NUMERIC,
  other_compensation NUMERIC,
  is_officer TEXT,
  is_director TEXT,
  is_key_employee TEXT,
  is_former TEXT
);
CREATE TABLE IF NOT EXISTS return_header_all (
  filing_id TEXT PRIMARY KEY,
  person_nm TEXT,
  preparer_person_nm TEXT,
  person_title_txt TEXT,
  signature_dt TEXT,
  preparer_firm_name_business_name_line1_txt TEXT,
  ptin TEXT,
  preparation_dt TEXT,
  tax_period_begin_dt TEXT,
  tax_period_end_dt TEXT
);
CREATE TABLE IF NOT EXISTS returns (
  filing_id TEXT PRIMARY KEY,
  source_file TEXT,
  ein TEXT,
  return_type TEXT,
  tax_year INTEGER,
  period_end TEXT,
  schema_version TEXT,
  return_ts TEXT,
  amended_return_ind TEXT,
  org_name TEXT,
  dba_name TEXT,
  in_care_of_name TEXT,
  us_address_line1 TEXT,
  us_address_line2 TEXT,
  city TEXT,
  state TEXT,
  zip TEXT,
  foreign_address_line1 TEXT,
  foreign_city TEXT,
  foreign_province TEXT,
  foreign_country TEXT,
  foreign_postal_code TEXT,
  website TEXT
);
CREATE VIEW grants_compat_v1 AS
SELECT
  filing_id,
  recipient_ein,
  TRIM(COALESCE(business_name_line1_txt,'') ||
       CASE WHEN business_name_line2_txt IS NOT NULL AND business_name_line2_txt<>'' THEN ' '||business_name_line2_txt ELSE '' END) AS recipient_name,
  COALESCE(us_city_nm, foreign_city_nm) AS city,
  COALESCE(us_state_abbreviation_cd, foreign_province_or_state_nm) AS state,
  CASE WHEN us_state_abbreviation_cd IS NOT NULL AND us_state_abbreviation_cd <> '' THEN 'US' ELSE foreign_country_cd END AS country,
  cash_grant_amt AS cash_amount,
  non_cash_assistance_amt AS noncash_amount,
  purpose_of_grant_txt AS purpose
FROM grants;
CREATE VIEW sched_r_related_orgs_expanded AS
SELECT
  'Related Tax-Exempt Org' AS relationship_category,
  r.filing_id,
  r.ein AS related_ein,
  COALESCE(r.business_name_line1_txt, r.disregarded_entity_name_business_name_line1_txt) AS related_name_line1,
  COALESCE(r.business_name_line2_txt, r.disregarded_entity_name_business_name_line2_txt) AS related_name_line2,
  r.exempt_code_section_txt,
  r.public_charity_status_txt,
  r.controlled_organization_ind,
  r.direct_controlling_nacd,
  r.primary_activities_txt,
  r.address_line1_txt,
  r.address_line2_txt,
  r.city_nm,
  r.state_abbreviation_cd,
  r.legal_domicile_state_cd,
  r.country_cd,
  r.foreign_postal_cd,
  CAST(NULL AS NUMERIC) AS ownership_pct,
  CAST(NULL AS NUMERIC) AS share_of_total_income_amt,
  CAST(NULL AS NUMERIC) AS share_of_eoyassets_amt,
  CAST(NULL AS NUMERIC) AS ubicode_vamt,
  CAST(NULL AS NUMERIC) AS involved_amt,
  CAST(NULL AS TEXT) AS transaction_type_txt,
  CAST(NULL AS TEXT) AS method_of_amount_determination_txt,
  'irs990_schedule_r_id_related_tax_exempt_org_grp' AS table_source
FROM irs990_schedule_r_id_related_tax_exempt_org_grp r
UNION ALL
SELECT
  'Related Taxable Corporation/Trust',
  r.filing_id,
  r.ein,
  COALESCE(r.related_organization_name_business_name_line1_txt, r.business_name_line1_txt),
  COALESCE(r.related_organization_name_business_name_line2_txt, r.business_name_line2_txt),
  CAST(NULL AS TEXT),
  CAST(NULL AS TEXT),
  r.controlled_organization_ind,
  r.direct_controlling_nacd,
  r.primary_activities_txt,
  r.address_line1_txt,
  r.address_line2_txt,
  r.city_nm,
  r.state_abbreviation_cd,
  r.legal_domicile_state_cd,
  CAST(NULL AS TEXT),
  r.foreign_postal_cd,
  r.ownership_pct,
  r.share_of_total_income_amt,
  r.share_of_eoyassets_amt,
  CAST(NULL AS NUMERIC),
  CAST(NULL AS NUMERIC),
  CAST(NULL AS TEXT),
  CAST(NULL AS TEXT),
  'irs990_schedule_r_id_related_org_txbl_corp_tr_grp'
FROM irs990_schedule_r_id_related_org_txbl_corp_tr_grp r
UNION ALL
SELECT
  'Related Taxable Partnership',
  r.filing_id,
  r.ein,
  COALESCE(r.related_organization_name_business_name_line1_txt, r.business_name_line1_txt),
  COALESCE(r.related_organization_name_business_name_line2_txt, r.business_name_line2_txt),
  CAST(NULL AS TEXT),
  CAST(NULL AS TEXT),
  r.controlled_organization_ind,
  r.direct_controlling_nacd,
  r.primary_activities_txt,
  r.address_line1_txt,
  r.address_line2_txt,
  r.city_nm,
  r.state_abbreviation_cd,
  r.legal_domicile_state_cd,
  CAST(NULL AS TEXT),
  r.foreign_postal_cd,
  r.ownership_pct,
  r.share_of_total_income_amt,
  r.share_of_eoyassets_amt,
  r.ubicode_vamt,
  CAST(NULL AS NUMERIC),
  CAST(NULL AS TEXT),
  CAST(NULL AS TEXT),
  'irs990_schedule_r_id_related_org_txbl_partnership_grp'
FROM irs990_schedule_r_id_related_org_txbl_partnership_grp r
UNION ALL
SELECT
  'Disregarded Entity',
  r.filing_id,
  CAST(NULL AS TEXT),
  r.disregarded_entity_name_business_name_line1_txt,
  r.disregarded_entity_name_business_name_line2_txt,
  CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT),
  r.primary_activities_txt,
  CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT),
  CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT),
  CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC),
  CAST(NULL AS TEXT), CAST(NULL AS TEXT),
  'irs990_schedule_r_id_disregarded_entities_grp'
FROM irs990_schedule_r_id_disregarded_entities_grp r
UNION ALL
SELECT
  'Transactions with Related Org',
  r.filing_id,
  CAST(NULL AS TEXT),
  r.business_name_line1_txt,
  r.business_name_line2_txt,
  CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT),
  CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT),
  CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC), CAST(NULL AS NUMERIC),
  r.involved_amt, r.transaction_type_txt, r.method_of_amount_determination_txt,
  'irs990_schedule_r_transactions_related_org_grp'
FROM irs990_schedule_r_transactions_related_org_grp r
UNION ALL
SELECT
  'Unrelated Taxable Partnership',
  r.filing_id,
  r.ein,
  r.business_name_line1_txt,
  CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT),
  r.general_or_managing_partner_ind,
  CAST(NULL AS TEXT),
  r.primary_activities_txt,
  r.address_line1_txt,
  r.address_line2_txt,
  r.city_nm,
  r.state_abbreviation_cd,
  r.legal_domicile_state_cd,
  CAST(NULL AS TEXT), CAST(NULL AS TEXT),
  r.ownership_pct,
  r.share_of_total_income_amt,
  r.share_of_eoyassets_amt,
  r.ubicode_vamt,
  CAST(NULL AS NUMERIC), CAST(NULL AS TEXT), CAST(NULL AS TEXT),
  'irs990_schedule_r_unrelated_org_txbl_partnership_grp'
FROM irs990_schedule_r_unrelated_org_txbl_partnership_grp r;
CREATE VIEW vw_contractors AS
SELECT
  c.id,
  c.filing_id,
  COALESCE(NULLIF(TRIM(c.business_name_line1_txt), ''), NULLIF(TRIM(c.person_nm), '')) AS contractor_name,
  c.business_name_line1_txt,
  c.business_name_line2_txt,
  c.person_nm,
  c.services_desc,
  c.compensation_amt,
  COALESCE(NULLIF(TRIM(c.usaddress_address_line1_txt), ''), NULLIF(TRIM(c.address_line1_txt), '')) AS address1,
  COALESCE(NULLIF(TRIM(c.usaddress_address_line2_txt), ''), NULLIF(TRIM(c.address_line2_txt), '')) AS address2,
  COALESCE(NULLIF(TRIM(c.usaddress_city_nm), ''), NULLIF(TRIM(c.city_nm), '')) AS city,
  COALESCE(NULLIF(TRIM(c.state_abbreviation_cd), ''), NULLIF(TRIM(c.province_or_state_nm), '')) AS region,
  COALESCE(NULLIF(TRIM(c.zipcd), ''), NULLIF(TRIM(c.foreign_postal_cd), '')) AS postal_code,
  CASE
    WHEN NULLIF(TRIM(c.country_cd), '') IS NOT NULL THEN TRIM(c.country_cd)
    WHEN NULLIF(TRIM(c.state_abbreviation_cd), '') IS NOT NULL
      OR NULLIF(TRIM(c.usaddress_address_line1_txt), '') IS NOT NULL THEN 'US'
    ELSE NULL
  END AS country,
  CASE
    WHEN NULLIF(TRIM(c.state_abbreviation_cd), '') IS NOT NULL
      OR NULLIF(TRIM(c.usaddress_address_line1_txt), '') IS NOT NULL THEN 1
    ELSE 0
  END AS is_us_address
FROM irs990_contractor_compensation_grp c;
CREATE INDEX idx_canonical_taxyear_filing ON canonical_by_ein_year(tax_year, filing_id);
CREATE INDEX idx_cby_filing_id ON canonical_by_ein_year(filing_id);
CREATE INDEX idx_contractor_filing_id ON irs990_contractor_compensation_grp(filing_id);
CREATE INDEX idx_contractor_orgname ON irs990_contractor_compensation_grp(business_name_line1_txt);
CREATE INDEX idx_contractor_person ON irs990_contractor_compensation_grp(person_nm);
CREATE INDEX idx_contractor_province ON irs990_contractor_compensation_grp(province_or_state_nm);
CREATE INDEX idx_contractor_us_state ON irs990_contractor_compensation_grp(state_abbreviation_cd);
CREATE INDEX idx_former_filing ON former_key_people(filing_id);
CREATE INDEX idx_grants_filing_id ON grants(filing_id);
CREATE INDEX idx_grants_filing_recipient ON grants(filing_id, recipient_ein);
CREATE INDEX idx_grants_recipient_ein ON grants(recipient_ein);
CREATE INDEX idx_grants_recipient_filing ON grants(recipient_ein, filing_id);
CREATE INDEX idx_grants_state_norm ON grants(COALESCE(us_state_abbreviation_cd, foreign_province_or_state_nm));
CREATE INDEX idx_highcomp_filing ON highest_comp_employees(filing_id);
CREATE INDEX idx_officers_filing ON officers(filing_id);
CREATE INDEX idx_returns_ein ON returns(ein);
CREATE INDEX idx_returns_ein_year ON returns(ein, tax_year);
CREATE INDEX idx_returns_filing_id ON returns(filing_id);
CREATE INDEX idx_returns_state_year ON returns(state, tax_year);
CREATE INDEX idx_returns_tax_year ON returns(tax_year);
CREATE INDEX idx_returns_type_year ON returns(return_type, tax_year);
COMMIT;
