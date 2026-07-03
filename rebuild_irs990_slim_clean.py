#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Slim rebuild script for the IRS 990 database.
Builds only the tables, views, and indexes needed by the current live query modules.

Patched version:
- Fixes returns-header extraction to use precise filer/header paths, matching the
  original master rebuild behavior for org_name, dba_name, in_care_of_name,
  address, website, return_ts, amended flag, etc.
- Fixes issues with missing data for 990PFs due to different XML file structure
- Adds safe append/incremental-load mode that skips XML filings already present in returns
- Adds preflight mode for XML compatibility checks; filename-year vs TaxYr warnings removed
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET


def local(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


def norm_text(x: Optional[str]) -> Optional[str]:
    if x is None:
        return None
    x = str(x).strip()
    return x or None


def norm_num(x: Optional[str]) -> Optional[float]:
    x = norm_text(x)
    if x is None:
        return None
    x = x.replace(',', '')
    try:
        return float(x)
    except Exception:
        return None


def norm_int(x: Optional[str]) -> Optional[int]:
    n = norm_num(x)
    return None if n is None else int(n)


def first_present(*vals):
    """Return the first value that is not None/blank. Keeps numeric zero values."""
    for v in vals:
        if v not in (None, ''):
            return v
    return None


def object_id_from_filing_id(filing_id: str) -> str:
    """Normalize common IRS public XML filename stems to the underlying object id.

    Current filings use the filename stem as filing_id, e.g.
    202331099349100118_public. The object id is the stable prefix before
    _public/_private, so this also catches copies renamed without that suffix.
    """
    s = (filing_id or '').strip()
    low = s.lower()
    for suffix in ('_public', '_private'):
        if low.endswith(suffix):
            return s[:-len(suffix)]
    return s


def snake_to_camel(name: str) -> str:
    return ''.join(p.title() for p in name.split('_'))


def col_candidates(col: str) -> List[str]:
    out = [snake_to_camel(col)]
    specials = {
        'business_name_line1_txt': ['BusinessNameLine1Txt'],
        'business_name_line2_txt': ['BusinessNameLine2Txt'],
        'address_line1_txt': ['AddressLine1Txt'],
        'address_line2_txt': ['AddressLine2Txt'],
        'city_nm': ['CityNm'],
        'state_abbreviation_cd': ['StateAbbreviationCd'],
        'zipcd': ['ZIPCd', 'ZipCd'],
        'phone_num': ['PhoneNum'],
        'person_nm': ['PersonNm'],
        'title_txt': ['TitleTxt'],
        'country_cd': ['CountryCd'],
        'foreign_postal_cd': ['ForeignPostalCd'],
        'province_or_state_nm': ['ProvinceOrStateNm'],
        'recipient_ein': ['RecipientEIN', 'EIN'],
        'ein': ['EIN'],
        'tax_period_begin_dt': ['TaxPeriodBeginDt'],
        'tax_period_end_dt': ['TaxPeriodEndDt'],
        'ptin': ['PTIN'],
        'website_address_txt': ['WebsiteAddressTxt'],
        'formation_yr': ['FormationYr'],
        'legal_domicile_state_cd': ['LegalDomicileStateCd'],
        'attr_organization501c_type_txt': ['Organization501cTypeTxt', 'AttrOrganization501cTypeTxt'],
        'type_of_organization_other_desc': ['TypeOfOrganizationOtherDesc'],
        'other_organization_dsc': ['OtherOrganizationDsc'],
        'boyamt': ['BOYAmt', 'BoyAmt'],
        'eoyamt': ['EOYAmt', 'EoyAmt'],
        'ubicode_vamt': ['UBICodeVAmt', 'UBicodeVAmt', 'UbicodeVAmt'],
        'direct_controlling_nacd': ['DirectControllingNACd', 'DirectControllingNAcd'],
        'legislative_political_acty_ind': ['LegislativePoliticalActyInd'],
        'interest_on_savings_temp_cash_investments_rev_and_expnss_amt': ['InterestOnSavingsTempCashInvstRevAndExpnssAmt'],
        'dividends_interest_securities_rev_and_expnss_amt': ['DividendsInterestSecuritiesRevAndExpnssAmt'],
        'net_investment_income_amt': ['NetInvestmentIncomeAmt'],
        'total_grant_or_contri_pd_dur_yr_amt': ['TotalGrantOrContriPdDurYrAmt'],
        'contri_paid_rev_and_expnss_amt': ['ContriPaidRevAndExpnssAmt'],
        'mission_desc_txt': ['RestrictionsOnAwardsTxt'],
        'employee_benefits_amt': ['EmployeeBenefitsAmt', 'EmployeeBenefitProgramAmt'],
        'expense_account_amt': ['ExpenseAccountAmt', 'ExpenseAccountOtherAllwncAmt'],
        'form1120_pol_filed_ind': ['Form1120POLFiledInd'],
        'non_deductible_lbbyng_pltcl_cy_amt': ['NonDeductibleLbbyngPltclCYAmt', 'NonDeductibleLbbyngPltclCYAmt'],
        'non_deductible_lbbyng_pltcl_tot_amt': ['NonDeductibleLbbyngPltclTotAmt'],
        'aggregate_reported_dues_ntc_amt': ['AggregateReportedDuesNtcAmt'],
        'not_described_section501c3_ind': ['NotDescribedSection501c3Ind'],
        'more_than100_spent_ind': ['MoreThan100SpentInd'],
    }
    out.extend(specials.get(col, []))
    seen, final = set(), []
    for v in out:
        k = v.lower()
        if k not in seen:
            seen.add(k)
            final.append(v)
    return final


def descendants_text_first(node: ET.Element, candidates: Sequence[str]) -> Optional[str]:
    cands = {c.lower() for c in candidates}
    for sub in node.iter():
        if local(sub.tag).lower() in cands:
            t = norm_text(sub.text)
            if t is not None:
                return t
    return None


def descendants_first_by_col(node: ET.Element, col: str) -> Optional[str]:
    return descendants_text_first(node, col_candidates(col))


def descendants_num_by_col(node: Optional[ET.Element], col: str) -> Optional[float]:
    if node is None:
        return None
    return norm_num(descendants_first_by_col(node, col))


def find_first(root: ET.Element, tag_candidates: Sequence[str]) -> Optional[ET.Element]:
    cands = {c.lower() for c in tag_candidates}
    for sub in root.iter():
        if local(sub.tag).lower() in cands:
            return sub
    return None


def find_groups(root: ET.Element, tag_candidates: Sequence[str]) -> List[ET.Element]:
    cands = {c.lower() for c in tag_candidates}
    return [sub for sub in root.iter() if local(sub.tag).lower() in cands]


def find_nodes_path_local(root: ET.Element, abs_path: str) -> List[ET.Element]:
    parts = [p for p in abs_path.strip("/").split("/") if p]
    if not parts:
        return []
    curr = [root]
    for idx, seg in enumerate(parts):
        nxt: List[ET.Element] = []
        for node in curr:
            if idx == 0 and local(node.tag) == seg:
                nxt.append(node)
                continue
            if seg == "*":
                nxt.extend(list(node))
                continue
            for ch in list(node):
                if local(ch.tag) == seg:
                    nxt.append(ch)
        curr = nxt
        if not curr:
            break
    return curr


def rel_find_nodes(node: ET.Element, rel_path: str) -> List[ET.Element]:
    parts = [p for p in rel_path.strip("/").split("/") if p]
    if not parts:
        return [node]
    curr = [node]
    for seg in parts:
        nxt: List[ET.Element] = []
        for n in curr:
            if seg == "*":
                nxt.extend(list(n))
            else:
                for ch in list(n):
                    if local(ch.tag) == seg:
                        nxt.append(ch)
        curr = nxt
        if not curr:
            break
    return curr


def first_text_paths(root: ET.Element, candidates: Sequence[str]) -> Optional[str]:
    for xp in candidates:
        nodes = find_nodes_path_local(root, xp)
        if nodes:
            txt = nodes[0].text
            if txt and txt.strip():
                return txt.strip()
    return None


def rel_first_text(node: ET.Element, candidates: Sequence[str]) -> Optional[str]:
    for rel in candidates:
        nodes = rel_find_nodes(node, rel)
        if nodes:
            txt = nodes[0].text
            if txt and txt.strip():
                return txt.strip()
    return None


def rel_first_num(node: ET.Element, candidates: Sequence[str]) -> Optional[float]:
    txt = rel_first_text(node, candidates)
    if txt is None:
        return None
    s = txt.replace(',', '').replace('$', '').strip()
    try:
        return float(s)
    except Exception:
        return None


def truthy_x01(text: Optional[str]) -> Optional[str]:
    t = norm_text(text)
    if t is None:
        return None
    u = t.upper()
    if u in {'X', '1', 'TRUE', 'T', 'YES', 'Y'}:
        return 'X'
    if u in {'0', 'FALSE', 'F', 'NO', 'N'}:
        return '0'
    return t


def normalize_bool01(token: Optional[str]) -> Optional[str]:
    if token is None:
        return None
    s = ''.join(ch for ch in token.strip().lower() if ch.isalnum())
    if s in {'1', 'true', 't', 'yes', 'y', 'x'}:
        return 'X'
    if s in {'0', 'false', 'f', 'no', 'n'}:
        return '0'
    if s.isdigit():
        try:
            return 'X' if int(s) != 0 else '0'
        except Exception:
            return None
    return token


def one_of(root: ET.Element, names: Sequence[str]) -> Optional[str]:
    return descendants_text_first(root, names)


def form_nodes(root: ET.Element) -> Dict[str, Optional[ET.Element]]:
    return {
        '990': find_first(root, ['IRS990']),
        '990EZ': find_first(root, ['IRS990EZ']),
        '990PF': find_first(root, ['IRS990PF']),
        'SCHC': find_first(root, ['IRS990ScheduleC']),
    }


HEADER_PATHS = {
    "return_type": ["/Return/ReturnHeader/ReturnTypeCd"],
    "tax_year": ["/Return/ReturnHeader/TaxYr", "/Return/ReturnHeader/TaxYear"],
    "period_end": ["/Return/ReturnHeader/TaxPeriodEndDt"],
    "ein": ["/Return/ReturnHeader/Filer/EIN"],
    "org_name": [
        "/Return/ReturnHeader/Filer/BusinessName/BusinessNameLine1Txt",
        "/Return/ReturnHeader/Filer/Name/BusinessNameLine1Txt",
    ],
    "dba_name": [
        "/Return/ReturnHeader/Filer/DoingBusinessAsNm",
        "/Return/ReturnHeader/Filer/DBANm",
        "/Return/ReturnHeader/Filer/BusinessName/BusinessNameLine2Txt",
        "/Return/ReturnHeader/Filer/Name/BusinessNameLine2Txt",
    ],
    "incareof": [
        "/Return/ReturnHeader/Filer/InCareOfNm",
        "/Return/ReturnHeader/Filer/USAddress/InCareOfNm",
        "/Return/ReturnHeader/Filer/ForeignAddress/InCareOfNm",
    ],
    "us1": ["/Return/ReturnHeader/Filer/USAddress/AddressLine1Txt"],
    "us2": ["/Return/ReturnHeader/Filer/USAddress/AddressLine2Txt"],
    "city": ["/Return/ReturnHeader/Filer/USAddress/CityNm"],
    "state": ["/Return/ReturnHeader/Filer/USAddress/StateAbbreviationCd"],
    "zip": ["/Return/ReturnHeader/Filer/USAddress/ZIPCd", "/Return/ReturnHeader/Filer/USAddress/ZipCd"],
    "f1": ["/Return/ReturnHeader/Filer/ForeignAddress/AddressLine1Txt"],
    "fcity": ["/Return/ReturnHeader/Filer/ForeignAddress/CityNm"],
    "fprov": ["/Return/ReturnHeader/Filer/ForeignAddress/ProvinceOrStateNm"],
    "fcountry": ["/Return/ReturnHeader/Filer/ForeignAddress/CountryCd"],
    "fpostal": ["/Return/ReturnHeader/Filer/ForeignAddress/ForeignPostalCd"],
    "website": [
        "/Return/ReturnHeader/WebsiteAddressTxt",
        "/Return/ReturnHeader/Filer/WebsiteAddressTxt",
        "/Return/ReturnData/IRS990PF/StatementsRegardingActyGrp/WebsiteAddressTxt",
    ],
    "return_ts": ["/Return/ReturnHeader/ReturnTs", "/Return/ReturnHeader/ReturnTimestamp", "/Return/ReturnHeader/ReceivedDt"],
    "amended": [
        "/Return/ReturnHeader/AmendedReturnInd",
        "/Return/ReturnData/IRS990/AmendedReturnInd",
        "/Return/ReturnData/IRS990EZ/AmendedReturnInd",
        "/Return/ReturnData/IRS990PF/AmendedReturnInd",
        "/Return/ReturnData/IRS990T/AmendedReturnInd",
    ],
}


DDL = [
"""
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
""",
"""
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
""",
"""
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
""",
"""
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
""",
"""
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
""",
"""
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
  more_than100_spent_ind TEXT,
  form1120_pol_filed_ind TEXT,
  influence_legislation_ind TEXT,
  influence_election_ind TEXT,
  total_grant_or_contri_pd_dur_yr_amt NUMERIC,
  mission_desc_txt TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_schedule_c_root (
  filing_id TEXT PRIMARY KEY,
  political_expenditures_amt NUMERIC,
  volunteer_hours_cnt NUMERIC,
  expended527_activities_amt NUMERIC,
  total_exempt_function_expend_amt NUMERIC,
  form1120_pol_filed_ind TEXT,
  total_grassroots_lobbying_amt NUMERIC,
  total_direct_lobbying_amt NUMERIC,
  total_lobbying_expend_grp_amt NUMERIC,
  other_exempt_purpose_expend_amt NUMERIC,
  total_exempt_purpose_expenditures_amt NUMERIC,
  lobbying_nontaxable_amt NUMERIC,
  grassroots_nontaxable_amt NUMERIC,
  lobbying_grassroots_excess_amt NUMERIC,
  lobbying_excess_amt NUMERIC,
  avg_lobbying_nontaxable_minus3_amt NUMERIC,
  avg_lobbying_nontaxable_minus2_amt NUMERIC,
  avg_lobbying_nontaxable_minus1_amt NUMERIC,
  avg_lobbying_nontaxable_current_amt NUMERIC,
  avg_lobbying_nontaxable_total_amt NUMERIC,
  lobbying_ceiling_amt NUMERIC,
  avg_grassroots_nontaxable_minus3_amt NUMERIC,
  avg_grassroots_nontaxable_minus2_amt NUMERIC,
  avg_grassroots_nontaxable_minus1_amt NUMERIC,
  avg_grassroots_nontaxable_current_amt NUMERIC,
  avg_grassroots_nontaxable_total_amt NUMERIC,
  grassroots_ceiling_amt NUMERIC,
  organization_belongs_afflt_grp_ind TEXT,
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
  total_lobbying_expenditures_amt NUMERIC,
  not_described_section501c3_ind TEXT,
  dues_assessments_amt NUMERIC,
  non_deductible_lbbyng_pltcl_cy_amt NUMERIC,
  non_deductible_lbbyng_pltcl_tot_amt NUMERIC,
  aggregate_reported_dues_ntc_amt NUMERIC,
  carried_over_amt NUMERIC,
  substantially_all_dues_nonded_ind TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_schedule_c_supplemental_info (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  form_and_line_reference_desc TEXT,
  explanation_txt TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_ez_form990_total_assets_grp (
  filing_id TEXT PRIMARY KEY,
  boyamt NUMERIC,
  eoyamt NUMERIC
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_ez_sum_of_total_liabilities_grp (
  filing_id TEXT PRIMARY KEY,
  boyamt NUMERIC,
  eoyamt NUMERIC
);
""",
"""
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
""",
"""
CREATE TABLE IF NOT EXISTS irs990_pf_form990_pfbalance_sheets_grp (
  filing_id TEXT PRIMARY KEY,
  total_assets_boyamt NUMERIC,
  total_assets_eoyamt NUMERIC,
  total_liabilities_boyamt NUMERIC,
  total_liabilities_eoyamt NUMERIC
);
""",
"""
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
""",
"""
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
""",
"""
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
""",
"""
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
""",
"""
CREATE TABLE IF NOT EXISTS former_key_people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_name TEXT,
  title_txt TEXT,
  comp_from_org NUMERIC,
  comp_from_related NUMERIC,
  other_compensation NUMERIC
);
""",
"""
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
""",
"""
CREATE TABLE IF NOT EXISTS irs990_books_in_care_of_detail (
  filing_id TEXT PRIMARY KEY,
  address_line1_txt TEXT,
  city_nm TEXT,
  state_abbreviation_cd TEXT,
  zipcd TEXT,
  phone_num TEXT,
  person_nm TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_ez_books_in_care_of_detail (
  filing_id TEXT PRIMARY KEY,
  address_line1_txt TEXT,
  city_nm TEXT,
  state_abbreviation_cd TEXT,
  zipcd TEXT,
  phone_num TEXT,
  person_nm TEXT
);
""",
"""
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
""",
"""
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
""",
"""
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
""",
"""
CREATE TABLE IF NOT EXISTS irs990_schedule_l_bus_tr_involve_interested_prsn_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  relationship_description_txt TEXT,
  transaction_amt NUMERIC,
  transaction_desc TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_schedule_l_disqualified_person_ex_bnft_tr_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  rln_disqualified_person_org_txt TEXT,
  transaction_corrected_ind TEXT,
  transaction_desc TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  relationship_with_org_txt TEXT,
  cash_grant_amt NUMERIC,
  type_of_assistance_txt TEXT,
  assistance_purpose_txt TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_schedule_l_loans_btwn_org_interested_prsn_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  person_nm TEXT,
  relationship_with_org_txt TEXT,
  original_principal_amt NUMERIC,
  balance_due_amt NUMERIC,
  loan_purpose_txt TEXT
);
""",
"""
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
""",
"""
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
""",
"""
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
""",
"""
CREATE TABLE IF NOT EXISTS irs990_schedule_r_id_disregarded_entities_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  disregarded_entity_name_business_name_line1_txt TEXT,
  disregarded_entity_name_business_name_line2_txt TEXT,
  primary_activities_txt TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_schedule_r_transactions_related_org_grp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id TEXT NOT NULL,
  business_name_line1_txt TEXT,
  business_name_line2_txt TEXT,
  involved_amt NUMERIC,
  transaction_type_txt TEXT,
  method_of_amount_determination_txt TEXT
);
""",
"""
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
""",
]


VIEWS = [
"""
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
""",
"""
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
""",
"""
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
""",
]


INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_returns_filing_id ON returns(filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_returns_ein ON returns(ein);',
    'CREATE INDEX IF NOT EXISTS idx_returns_ein_year ON returns(ein, tax_year);',
    'CREATE INDEX IF NOT EXISTS idx_returns_state_year ON returns(state, tax_year);',
    'CREATE INDEX IF NOT EXISTS idx_returns_tax_year ON returns(tax_year);',
    'CREATE INDEX IF NOT EXISTS idx_returns_type_year ON returns(return_type, tax_year);',
    'CREATE INDEX IF NOT EXISTS idx_cby_filing_id ON canonical_by_ein_year(filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_canonical_taxyear_filing ON canonical_by_ein_year(tax_year, filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_schedule_c_root_filing ON irs990_schedule_c_root(filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_schedule_c_supp_filing ON irs990_schedule_c_supplemental_info(filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_grants_filing_id ON grants(filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_grants_recipient_ein ON grants(recipient_ein);',
    'CREATE INDEX IF NOT EXISTS idx_grants_filing_recipient ON grants(filing_id, recipient_ein);',
    'CREATE INDEX IF NOT EXISTS idx_grants_recipient_filing ON grants(recipient_ein, filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_grants_state_norm ON grants(COALESCE(us_state_abbreviation_cd, foreign_province_or_state_nm));',
    'CREATE INDEX IF NOT EXISTS idx_contractor_filing_id ON irs990_contractor_compensation_grp(filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_contractor_orgname ON irs990_contractor_compensation_grp(business_name_line1_txt);',
    'CREATE INDEX IF NOT EXISTS idx_contractor_person ON irs990_contractor_compensation_grp(person_nm);',
    'CREATE INDEX IF NOT EXISTS idx_contractor_us_state ON irs990_contractor_compensation_grp(state_abbreviation_cd);',
    'CREATE INDEX IF NOT EXISTS idx_contractor_province ON irs990_contractor_compensation_grp(province_or_state_nm);',
    'CREATE INDEX IF NOT EXISTS idx_officers_filing ON officers(filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_highcomp_filing ON highest_comp_employees(filing_id);',
    'CREATE INDEX IF NOT EXISTS idx_former_filing ON former_key_people(filing_id);',
]


IRS990_COLS = ['net_assets_or_fund_balances_boyamt','net_assets_or_fund_balances_eoyamt','total_program_service_revenue_amt','cyprogram_service_revenue_amt','membership_dues_amt','cyinvestment_income_amt','cygrants_and_similar_paid_amt','total_employee_cnt','employee_cnt','total_volunteers_cnt','mission_desc','activity_or_mission_desc','address_change_ind','name_change_ind','initial_return_ind','final_return_ind','amended_return_ind','application_pending_ind','website_address_txt','formation_yr','legal_domicile_state_cd','organization501c3_ind','organization501c_ind','attr_organization501c_type_txt','organization4947a1_not_pfind','type_of_organization_corp_ind','type_of_organization_trust_ind','type_of_organization_assoc_ind','type_of_organization_other_ind','other_organization_dsc','cyother_revenue_amt','cysalaries_comp_emp_bnft_paid_amt','cytotal_prof_fndrsng_expns_amt','cytotal_fundraising_expense_amt','cyother_expenses_amt','cyrevenues_less_expenses_amt','total_assets_boyamt','total_assets_eoyamt','total_liabilities_boyamt','total_liabilities_eoyamt','political_campaign_acty_ind','lobbying_activities_ind']
IRS990EZ_COLS = ['net_assets_or_fund_balances_boyamt','net_assets_or_fund_balances_eoyamt','program_service_revenue_amt','membership_dues_amt','investment_income_amt','grants_and_similar_amounts_paid_amt','primary_exempt_purpose_txt','address_change_ind','name_change_ind','initial_return_ind','final_return_ind','amended_return_ind','application_pending_ind','website_address_txt','organization501c3_ind','organization501c_ind','attr_organization501c_type_txt','organization4947a1_not_pfind','type_of_organization_corp_ind','type_of_organization_trust_ind','type_of_organization_assoc_ind','type_of_organization_other_ind','type_of_organization_other_desc','other_revenue_total_amt','salaries_other_comp_empl_bnft_amt','other_expenses_total_amt','excess_or_deficit_for_year_amt','political_campaign_acty_ind','lobbying_activities_ind']
IRS990PF_COLS = ['address_change_ind','name_change_ind','initial_return_ind','final_return_ind','amended_return_ind','application_pending_ind','organization501c3_exempt_pfind','organization4947a1_trtd_pfind','website_address_txt','legislative_political_acty_ind','more_than100_spent_ind','form1120_pol_filed_ind','influence_legislation_ind','influence_election_ind','total_grant_or_contri_pd_dur_yr_amt','mission_desc_txt']
SCHEDC_COLS = [
    'political_expenditures_amt',
    'volunteer_hours_cnt',
    'expended527_activities_amt',
    'total_exempt_function_expend_amt',
    'form1120_pol_filed_ind',
    'total_grassroots_lobbying_amt',
    'total_direct_lobbying_amt',
    'total_lobbying_expend_grp_amt',
    'other_exempt_purpose_expend_amt',
    'total_exempt_purpose_expenditures_amt',
    'lobbying_nontaxable_amt',
    'grassroots_nontaxable_amt',
    'lobbying_grassroots_excess_amt',
    'lobbying_excess_amt',
    'avg_lobbying_nontaxable_minus3_amt',
    'avg_lobbying_nontaxable_minus2_amt',
    'avg_lobbying_nontaxable_minus1_amt',
    'avg_lobbying_nontaxable_current_amt',
    'avg_lobbying_nontaxable_total_amt',
    'lobbying_ceiling_amt',
    'avg_grassroots_nontaxable_minus3_amt',
    'avg_grassroots_nontaxable_minus2_amt',
    'avg_grassroots_nontaxable_minus1_amt',
    'avg_grassroots_nontaxable_current_amt',
    'avg_grassroots_nontaxable_total_amt',
    'grassroots_ceiling_amt',
    'organization_belongs_afflt_grp_ind',
    'volunteers_ind',
    'paid_staff_or_management_ind',
    'media_advertisements_ind',
    'media_advertisements_amt',
    'mailings_members_ind',
    'mailings_members_amt',
    'publications_or_broadcast_ind',
    'publications_or_broadcast_amt',
    'grants_other_organizations_ind',
    'grants_other_organizations_amt',
    'direct_contact_legislators_ind',
    'direct_contact_legislators_amt',
    'rallies_demonstrations_ind',
    'rallies_demonstrations_amt',
    'other_activities_ind',
    'other_activities_amt',
    'total_lobbying_expenditures_amt',
    'not_described_section501c3_ind',
    'dues_assessments_amt',
    'non_deductible_lbbyng_pltcl_cy_amt',
    'non_deductible_lbbyng_pltcl_tot_amt',
    'aggregate_reported_dues_ntc_amt',
    'carried_over_amt',
    'substantially_all_dues_nonded_ind',
]
EZ_TA_COLS = ['boyamt','eoyamt']
EZ_TL_COLS = ['boyamt','eoyamt']
PF_ANA_COLS = ['other_income_rev_and_expnss_amt','oth_empl_slrs_wgs_rev_and_expnss_amt','pension_empl_bnft_rev_and_expnss_amt','other_expenses_rev_and_expnss_amt','excess_revenue_over_expenses_amt','interest_on_savings_temp_cash_investments_rev_and_expnss_amt','dividends_interest_securities_rev_and_expnss_amt','net_investment_income_amt','contri_paid_rev_and_expnss_amt']
PF_BS_COLS = ['total_assets_boyamt','total_assets_eoyamt','total_liabilities_boyamt','total_liabilities_eoyamt']


GROUP_TAGS = {
    'grants': ['RecipientTable', 'GrantsAndSimilarAmountsPaidGrp', 'GrantOrContributionPdDurYrGrp'],
    'contractors': [
        'ContractorCompensationGrp',
        'IndependentContractorGrp',
        'IndCntrctRcpntGrp',
        'CompensationOfHghstPdCntrctGrp',  # 990-PF highest-paid contractors
    ],
    'part_vii_a': ['Form990PartVIISectionAGrp'],
    'ez_officer': ['OfficerDirectorTrusteeEmplGrp', 'OfficerDirectorTrusteeEmployeeGrp'],
    'sched_j': ['RltdOrgOfficerTrstKeyEmplGrp'],
    'pf_officer': ['OfficerDirTrstKeyEmplGrp'],
    'pf_high_employee': ['CompensationHighestPaidEmplGrp'],
    'sched_l_bus': ['BusTrInvolveInterestedPrsnGrp'],
    'sched_l_ebt': ['DisqualifiedPersonExBnftTrGrp'],
    'sched_l_grant': ['GrntAsstBnftInterestedPrsnGrp'],
    'sched_l_loan': ['LoansBtwnOrgInterestedPrsnGrp'],
    'r_texempt': ['IdRelatedTaxExemptOrgGrp'],
    'r_corptr': ['IdRelatedOrgTxblCorpTrGrp'],
    'r_part': ['IdRelatedOrgTxblPartnershipGrp'],
    'r_disreg': ['IdDisregardedEntitiesGrp'],
    'r_trans': ['TransactionsRelatedOrgGrp'],
    'r_unrel': ['UnrelatedOrgTxblPartnershipGrp'],
}


def db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=OFF;')
    conn.execute('PRAGMA temp_store=MEMORY;')
    conn.execute('PRAGMA cache_size=-200000;')
    conn.execute('PRAGMA mmap_size=268435456;')
    conn.execute('PRAGMA foreign_keys=OFF;')
    return conn


def exec_all(conn: sqlite3.Connection, statements: Sequence[str], label: str) -> None:
    total = len(statements)
    for i, stmt in enumerate(statements, 1):
        conn.executescript(stmt)
        if i % 20 == 0 or i == total:
            print(f'[{label}] {i:,}/{total:,}')


def generic_singleton_extract(node: Optional[ET.Element], cols: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if node is None:
        return out
    for c in cols:
        out[c] = descendants_first_by_col(node, c)
    return out


def child_num(node: Optional[ET.Element], parent_tag: str, child_tag: str) -> Optional[float]:
    if node is None:
        return None
    parent = find_first(node, [parent_tag])
    if parent is None:
        return None
    return rel_first_num(parent, [child_tag])


def child_text(node: Optional[ET.Element], parent_tag: str, child_tag: str) -> Optional[str]:
    if node is None:
        return None
    parent = find_first(node, [parent_tag])
    if parent is None:
        return None
    return rel_first_text(parent, [child_tag])


def extract_schedule_c(node: Optional[ET.Element]) -> Dict[str, Any]:
    out = {c: None for c in SCHEDC_COLS}
    if node is None:
        return out

    # Generic direct descendants cover most Part I, Part II-B, and proxy-tax fields.
    for c in SCHEDC_COLS:
        out[c] = descendants_first_by_col(node, c)

    # Several Schedule C Part II-A groups reuse child names such as
    # FilingOrganizationsTotalAmt; map by parent group so the meaning is stable.
    grouped_amounts = {
        'total_grassroots_lobbying_amt': ('TotalGrassrootsLobbyingGrp', 'FilingOrganizationsTotalAmt'),
        'total_direct_lobbying_amt': ('TotalDirectLobbyingGrp', 'FilingOrganizationsTotalAmt'),
        'total_lobbying_expend_grp_amt': ('TotalLobbyingExpendGrp', 'FilingOrganizationsTotalAmt'),
        'other_exempt_purpose_expend_amt': ('OtherExemptPurposeExpendGrp', 'FilingOrganizationsTotalAmt'),
        'total_exempt_purpose_expenditures_amt': ('TotalExemptPurposeExpendGrp', 'FilingOrganizationsTotalAmt'),
        'lobbying_nontaxable_amt': ('LobbyingNontaxableAmountGrp', 'FilingOrganizationsTotalAmt'),
        'grassroots_nontaxable_amt': ('GrassrootsNontaxableGrp', 'FilingOrganizationsTotalAmt'),
        'lobbying_grassroots_excess_amt': ('TotLbbyngGrassrootMnsNonTxGrp', 'FilingOrganizationsTotalAmt'),
        'lobbying_excess_amt': ('TotLbbyExpendMnsLbbyngNonTxGrp', 'FilingOrganizationsTotalAmt'),
        'avg_lobbying_nontaxable_minus3_amt': ('AvgLobbyingNontaxableAmountGrp', 'CurrentYearMinus3Amt'),
        'avg_lobbying_nontaxable_minus2_amt': ('AvgLobbyingNontaxableAmountGrp', 'CurrentYearMinus2Amt'),
        'avg_lobbying_nontaxable_minus1_amt': ('AvgLobbyingNontaxableAmountGrp', 'CurrentYearMinus1Amt'),
        'avg_lobbying_nontaxable_current_amt': ('AvgLobbyingNontaxableAmountGrp', 'CurrentYearAmt'),
        'avg_lobbying_nontaxable_total_amt': ('AvgLobbyingNontaxableAmountGrp', 'TotalAmt'),
        'avg_grassroots_nontaxable_minus3_amt': ('AvgGrassrootsNontaxableGrp', 'CurrentYearMinus3Amt'),
        'avg_grassroots_nontaxable_minus2_amt': ('AvgGrassrootsNontaxableGrp', 'CurrentYearMinus2Amt'),
        'avg_grassroots_nontaxable_minus1_amt': ('AvgGrassrootsNontaxableGrp', 'CurrentYearMinus1Amt'),
        'avg_grassroots_nontaxable_current_amt': ('AvgGrassrootsNontaxableGrp', 'CurrentYearAmt'),
        'avg_grassroots_nontaxable_total_amt': ('AvgGrassrootsNontaxableGrp', 'TotalAmt'),
    }
    for col, (parent, child) in grouped_amounts.items():
        out[col] = child_num(node, parent, child)

    return out


def extract_schedule_c_supplemental(node: Optional[ET.Element], filing_id: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if node is None:
        return rows
    for detail in find_groups(node, ['SupplementalInformationDetail']):
        row = {
            'filing_id': filing_id,
            'form_and_line_reference_desc': descendants_text_first(detail, ['FormAndLineReferenceDesc']),
            'explanation_txt': descendants_text_first(detail, ['ExplanationTxt']),
        }
        if row['form_and_line_reference_desc'] or row['explanation_txt']:
            rows.append(row)
    return rows


def generic_group_rows(root: ET.Element, tag_key: str, cols: Sequence[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for g in find_groups(root, GROUP_TAGS[tag_key]):
        row = {c: descendants_first_by_col(g, c) for c in cols}
        if any(v not in (None, '') for v in row.values()):
            rows.append(row)
    return rows


def header_extract(root: ET.Element, p: Path) -> Optional[Dict[str, Any]]:
    rtype = first_text_paths(root, HEADER_PATHS["return_type"])
    tax_year = norm_int(first_text_paths(root, HEADER_PATHS["tax_year"]))
    ein = first_text_paths(root, HEADER_PATHS["ein"])
    if not rtype or not tax_year or not ein:
        return None

    schema_version = next((v for k, v in root.attrib.items() if local(k).lower().endswith('version')), None)

    return {
        'filing_id': p.stem,
        'source_file': str(p),
        'ein': ein,
        'return_type': rtype,
        'tax_year': tax_year,
        'period_end': first_text_paths(root, HEADER_PATHS["period_end"]),
        'schema_version': schema_version,
        'return_ts': first_text_paths(root, HEADER_PATHS["return_ts"]),
        'amended_return_ind': normalize_bool01(first_text_paths(root, HEADER_PATHS["amended"])),
        'org_name': first_text_paths(root, HEADER_PATHS["org_name"]),
        'dba_name': first_text_paths(root, HEADER_PATHS["dba_name"]),
        'in_care_of_name': first_text_paths(root, HEADER_PATHS["incareof"]),
        'us_address_line1': first_text_paths(root, HEADER_PATHS["us1"]),
        'us_address_line2': first_text_paths(root, HEADER_PATHS["us2"]),
        'city': first_text_paths(root, HEADER_PATHS["city"]),
        'state': first_text_paths(root, HEADER_PATHS["state"]),
        'zip': first_text_paths(root, HEADER_PATHS["zip"]),
        'foreign_address_line1': first_text_paths(root, HEADER_PATHS["f1"]),
        'foreign_city': first_text_paths(root, HEADER_PATHS["fcity"]),
        'foreign_province': first_text_paths(root, HEADER_PATHS["fprov"]),
        'foreign_country': first_text_paths(root, HEADER_PATHS["fcountry"]),
        'foreign_postal_code': first_text_paths(root, HEADER_PATHS["fpostal"]),
        'website': first_text_paths(root, HEADER_PATHS["website"]),
    }


def extract_file(file_path: str) -> Dict[str, Any]:
    p = Path(file_path)
    try:
        root = ET.parse(str(p)).getroot()
    except Exception as e:
        return {'error': f'parse_error:{p}: {e}'}
    hdr = header_extract(root, p)
    if hdr is None:
        return {'error': f'missing_required_header:{p}'}

    filing_id = hdr['filing_id']
    fns = form_nodes(root)
    rtype = hdr['return_type']

    irs990 = generic_singleton_extract(fns['990'], IRS990_COLS)
    irs990ez = generic_singleton_extract(fns['990EZ'], IRS990EZ_COLS)
    irs990pf = generic_singleton_extract(fns['990PF'], IRS990PF_COLS)
    schc = extract_schedule_c(fns['SCHC'])
    schc_supplemental = extract_schedule_c_supplemental(fns['SCHC'], filing_id)

    for d in (irs990, irs990ez, irs990pf, schc):
        for k, v in list(d.items()):
            if '_ind' in k or k.endswith('_ind'):
                d[k] = truthy_x01(v)

    ez_ta = generic_singleton_extract(fns['990EZ'], EZ_TA_COLS)
    ez_tl = generic_singleton_extract(fns['990EZ'], EZ_TL_COLS)
    pf_ana = generic_singleton_extract(fns['990PF'], PF_ANA_COLS)
    pf_bs = generic_singleton_extract(fns['990PF'], PF_BS_COLS)

    core_hot = {
        'filing_id': filing_id,
        'total_revenue': None,
        'total_expenses': None,
        'net_assets_boy': None,
        'net_assets_eoy': None,
        'contributions': None,
        'program_service_revenue': None,
        'membership_dues': None,
        'investment_income': None,
        'government_grants': None,
        'grants_paid': None,
        'lobbying_expense': None,
        'employees_count': None,
        'volunteers_count': None,
        'mission_desc': None
    }

    if rtype.startswith('990PF'):
        core_hot['total_revenue'] = descendants_num_by_col(fns['990PF'], 'total_rev_and_expnss_amt')
        core_hot['total_expenses'] = descendants_num_by_col(fns['990PF'], 'total_expenses_rev_and_expnss_amt')
    elif rtype.startswith('990EZ'):
        core_hot['total_revenue'] = descendants_num_by_col(fns['990EZ'], 'total_revenue_amt')
        core_hot['total_expenses'] = descendants_num_by_col(fns['990EZ'], 'total_expenses_amt')
        core_hot['net_assets_boy'] = norm_num(irs990ez.get('net_assets_or_fund_balances_boyamt'))
        core_hot['net_assets_eoy'] = norm_num(irs990ez.get('net_assets_or_fund_balances_eoyamt'))
        core_hot['program_service_revenue'] = norm_num(irs990ez.get('program_service_revenue_amt'))
        core_hot['membership_dues'] = norm_num(irs990ez.get('membership_dues_amt'))
        core_hot['investment_income'] = norm_num(irs990ez.get('investment_income_amt'))
        core_hot['grants_paid'] = norm_num(irs990ez.get('grants_and_similar_amounts_paid_amt'))
        core_hot['mission_desc'] = irs990ez.get('primary_exempt_purpose_txt')
    elif rtype.startswith('990'):
        core_hot['total_revenue'] = descendants_num_by_col(fns['990'], 'cytotal_revenue_amt') or descendants_num_by_col(fns['990'], 'total_revenue_amt')
        core_hot['total_expenses'] = descendants_num_by_col(fns['990'], 'cytotal_expenses_amt') or descendants_num_by_col(fns['990'], 'total_functional_expenses_amt')
        core_hot['net_assets_boy'] = norm_num(irs990.get('net_assets_or_fund_balances_boyamt'))
        core_hot['net_assets_eoy'] = norm_num(irs990.get('net_assets_or_fund_balances_eoyamt'))
        core_hot['contributions'] = descendants_num_by_col(fns['990'], 'cycontributions_grants_amt') or descendants_num_by_col(fns['990'], 'contributions_gifts_grants_etc_amt')
        core_hot['program_service_revenue'] = norm_num(irs990.get('total_program_service_revenue_amt')) or norm_num(irs990.get('cyprogram_service_revenue_amt'))
        core_hot['membership_dues'] = norm_num(irs990.get('membership_dues_amt'))
        core_hot['investment_income'] = norm_num(irs990.get('cyinvestment_income_amt'))
        core_hot['government_grants'] = descendants_num_by_col(fns['990'], 'government_grants_amt')
        core_hot['grants_paid'] = norm_num(irs990.get('cygrants_and_similar_paid_amt'))
        core_hot['lobbying_expense'] = descendants_num_by_col(fns['SCHC'], 'total_lobbying_expenditures_amt')
        core_hot['employees_count'] = norm_int(irs990.get('total_employee_cnt') or irs990.get('employee_cnt'))
        core_hot['volunteers_count'] = norm_int(irs990.get('total_volunteers_cnt'))
        core_hot['mission_desc'] = irs990.get('mission_desc') or irs990.get('activity_or_mission_desc')

    rha = {
        'filing_id': filing_id,
        'person_nm': one_of(root, ['PersonNm']),
        'preparer_person_nm': one_of(root, ['PreparerPersonNm']),
        'person_title_txt': one_of(root, ['PersonTitleTxt']),
        'signature_dt': one_of(root, ['SignatureDt']),
        'preparer_firm_name_business_name_line1_txt': one_of(root, ['PreparerFirmNameBusinessNameLine1Txt']),
        'ptin': one_of(root, ['PTIN']),
        'preparation_dt': one_of(root, ['PreparationDt']),
        'tax_period_begin_dt': one_of(root, ['TaxPeriodBeginDt']),
        'tax_period_end_dt': one_of(root, ['TaxPeriodEndDt'])
    }

    books_990 = {
        'filing_id': filing_id,
        'address_line1_txt': one_of(fns['990'] if fns['990'] is not None else root, ['AddressLine1Txt']),
        'city_nm': one_of(fns['990'] if fns['990'] is not None else root, ['CityNm']),
        'state_abbreviation_cd': one_of(fns['990'] if fns['990'] is not None else root, ['StateAbbreviationCd']),
        'zipcd': one_of(fns['990'] if fns['990'] is not None else root, ['ZIPCd', 'ZipCd']),
        'phone_num': one_of(fns['990'] if fns['990'] is not None else root, ['PhoneNum']),
        'person_nm': one_of(fns['990'] if fns['990'] is not None else root, ['IndividualWithBooksNm', 'PersonNm'])
    }

    books_ez = {
        'filing_id': filing_id,
        'address_line1_txt': one_of(fns['990EZ'] if fns['990EZ'] is not None else root, ['AddressLine1Txt']),
        'city_nm': one_of(fns['990EZ'] if fns['990EZ'] is not None else root, ['CityNm']),
        'state_abbreviation_cd': one_of(fns['990EZ'] if fns['990EZ'] is not None else root, ['StateAbbreviationCd']),
        'zipcd': one_of(fns['990EZ'] if fns['990EZ'] is not None else root, ['ZIPCd', 'ZipCd']),
        'phone_num': one_of(fns['990EZ'] if fns['990EZ'] is not None else root, ['PhoneNum']),
        'person_nm': one_of(fns['990EZ'] if fns['990EZ'] is not None else root, ['IndividualWithBooksNm', 'PersonNm'])
    }

    grants = []
    for g in find_groups(root, GROUP_TAGS['grants']):
        row = {
            'filing_id': filing_id,
            'filer_ein': hdr['ein'],
            'filer_name': hdr['org_name'],
            'recipient_ein': descendants_first_by_col(g, 'recipient_ein'),
            'business_name_line1_txt': (
                descendants_first_by_col(g, 'business_name_line1_txt')
                or rel_first_text(g, ['RecipientBusinessName/BusinessNameLine1Txt'])
            ),
            'business_name_line2_txt': (
                descendants_first_by_col(g, 'business_name_line2_txt')
                or rel_first_text(g, ['RecipientBusinessName/BusinessNameLine2Txt'])
            ),
            'us_address_line1_txt': descendants_text_first(g, ['AddressLine1Txt']),
            'us_address_line2_txt': descendants_text_first(g, ['AddressLine2Txt']),
            'us_city_nm': descendants_text_first(g, ['CityNm']),
            'us_state_abbreviation_cd': descendants_text_first(g, ['StateAbbreviationCd']),
            'us_zip_cd': descendants_text_first(g, ['ZIPCd', 'ZipCd']),
            'foreign_address_line1_txt': descendants_text_first(g, ['ForeignAddressLine1Txt']),
            'foreign_city_nm': descendants_text_first(g, ['ForeignCityNm']),
            'foreign_province_or_state_nm': descendants_text_first(g, ['ProvinceOrStateNm']),
            'foreign_postal_cd': descendants_text_first(g, ['ForeignPostalCd']),
            'foreign_country_cd': descendants_text_first(g, ['CountryCd']),
            'ircsection_desc': descendants_text_first(g, ['IRCSectionDesc']),
            'cash_grant_amt': first_present(
                descendants_num_by_col(g, 'cash_grant_amt'),
                norm_num(descendants_text_first(g, ['Amt']))
            ),
            'non_cash_assistance_amt': descendants_num_by_col(g, 'non_cash_assistance_amt'),
            'non_cash_assistance_desc': descendants_text_first(g, ['NonCashAssistanceDesc']),
            'valuation_method_used_desc': descendants_text_first(g, ['ValuationMethodUsedDesc']),
            'purpose_of_grant_txt': descendants_text_first(g, ['PurposeOfGrantTxt', 'GrantOrContributionPurposeTxt']),
        }
        if any(v not in (None, '') for k, v in row.items() if k not in {'filing_id', 'filer_ein', 'filer_name'}):
            grants.append(row)

    contractors = []
    for c in find_groups(root, GROUP_TAGS['contractors']):
        row = {
            'filing_id': filing_id,
            'compensation_amt': descendants_num_by_col(c, 'compensation_amt') or descendants_num_by_col(c, 'amount'),
            'address_line1_txt': descendants_text_first(c, ['AddressLine1Txt']),
            'address_line2_txt': descendants_text_first(c, ['AddressLine2Txt']),
            'city_nm': descendants_text_first(c, ['CityNm']),
            'country_cd': descendants_text_first(c, ['CountryCd']),
            'foreign_postal_cd': descendants_text_first(c, ['ForeignPostalCd']),
            'province_or_state_nm': descendants_text_first(c, ['ProvinceOrStateNm']),
            'usaddress_address_line1_txt': descendants_text_first(c, ['AddressLine1Txt']),
            'usaddress_address_line2_txt': descendants_text_first(c, ['AddressLine2Txt']),
            'usaddress_city_nm': descendants_text_first(c, ['CityNm']),
            'state_abbreviation_cd': descendants_text_first(c, ['StateAbbreviationCd']),
            'zipcd': descendants_text_first(c, ['ZIPCd', 'ZipCd']),
            'business_name_line1_txt': descendants_first_by_col(c, 'business_name_line1_txt'),
            'business_name_line2_txt': descendants_first_by_col(c, 'business_name_line2_txt'),
            'person_nm': descendants_first_by_col(c, 'person_nm'),
            'services_desc': descendants_text_first(c, ['ServicesDesc', 'ServiceTypeTxt', 'DescriptionTxt', 'ServicesDescriptionTxt']),
        }
        if any(v not in (None, '') for k, v in row.items() if k != 'filing_id'):
            contractors.append(row)

    officers_rows, high_rows, former_rows = [], [], []
    for g in find_groups(root, GROUP_TAGS['part_vii_a']):
        base = {
            'filing_id': filing_id,
            'person_name': descendants_text_first(g, ['PersonNm']),
            'title_txt': descendants_text_first(g, ['TitleTxt']),
            'avg_hours_week': descendants_num_by_col(g, 'average_hours_per_week_rt') or descendants_num_by_col(g, 'average_hours_per_week_rltd_org_rt'),
            'comp_from_org': descendants_num_by_col(g, 'reportable_comp_from_org_amt'),
            'comp_from_related': descendants_num_by_col(g, 'reportable_comp_from_rltd_org_amt'),
            'other_compensation': descendants_num_by_col(g, 'other_compensation_amt'),
            'is_officer': truthy_x01(descendants_text_first(g, ['OfficerInd'])),
            'is_director': truthy_x01(descendants_text_first(g, ['IndividualTrusteeOrDirectorInd', 'InstitutionalTrusteeInd'])),
            'is_key_employee': truthy_x01(descendants_text_first(g, ['KeyEmployeeInd'])),
            'is_former': truthy_x01(descendants_text_first(g, ['FormerOfcrDirectorTrusteeInd'])),
        }
        if base['person_name']:
            officers_rows.append(base)
            if truthy_x01(descendants_text_first(g, ['HighestCompensatedEmployeeInd'])) == 'X':
                high_rows.append({k: base[k] for k in ['filing_id', 'person_name', 'title_txt', 'avg_hours_week', 'comp_from_org', 'comp_from_related', 'other_compensation']})
            if base['is_former'] == 'X' or base['is_key_employee'] == 'X':
                former_rows.append({k: base[k] for k in ['filing_id', 'person_name', 'title_txt', 'comp_from_org', 'comp_from_related', 'other_compensation']})

    def groups(tagkey, cols):
        rows = generic_group_rows(root, tagkey, cols)
        for r in rows:
            r['filing_id'] = filing_id
        return rows

    ez_officer_rows = groups('ez_officer', ['person_nm', 'title_txt', 'average_hrs_per_wk_devoted_to_pos_rt', 'compensation_amt', 'employee_benefit_program_amt', 'expense_account_other_allwnc_amt'])
    schedj_rows = groups('sched_j', ['person_nm', 'title_txt', 'base_compensation_filing_org_amt', 'bonus_filing_organization_amount', 'total_compensation_filing_org_amt', 'total_compensation_rltd_orgs_amt'])
    pf_officer_rows = groups('pf_officer', ['person_nm', 'title_txt', 'average_hrs_per_wk_devoted_to_pos_rt', 'compensation_amt', 'employee_benefits_amt', 'expense_account_amt'])
    schedl_bus = groups('sched_l_bus', ['person_nm', 'relationship_description_txt', 'transaction_amt', 'transaction_desc'])
    schedl_ebt = groups('sched_l_ebt', ['person_nm', 'rln_disqualified_person_org_txt', 'transaction_corrected_ind', 'transaction_desc'])
    schedl_grant = groups('sched_l_grant', ['person_nm', 'relationship_with_org_txt', 'cash_grant_amt', 'type_of_assistance_txt', 'assistance_purpose_txt'])
    schedl_loan = groups('sched_l_loan', ['person_nm', 'relationship_with_org_txt', 'original_principal_amt', 'balance_due_amt', 'loan_purpose_txt'])
    r_texempt = groups('r_texempt', ['ein', 'business_name_line1_txt', 'business_name_line2_txt', 'disregarded_entity_name_business_name_line1_txt', 'disregarded_entity_name_business_name_line2_txt', 'exempt_code_section_txt', 'public_charity_status_txt', 'controlled_organization_ind', 'direct_controlling_nacd', 'primary_activities_txt', 'address_line1_txt', 'address_line2_txt', 'city_nm', 'state_abbreviation_cd', 'legal_domicile_state_cd', 'country_cd', 'foreign_postal_cd'])
    r_corptr = groups('r_corptr', ['ein', 'related_organization_name_business_name_line1_txt', 'related_organization_name_business_name_line2_txt', 'business_name_line1_txt', 'business_name_line2_txt', 'controlled_organization_ind', 'direct_controlling_nacd', 'primary_activities_txt', 'address_line1_txt', 'address_line2_txt', 'city_nm', 'state_abbreviation_cd', 'legal_domicile_state_cd', 'foreign_postal_cd', 'ownership_pct', 'share_of_total_income_amt', 'share_of_eoyassets_amt'])
    r_part = groups('r_part', ['ein', 'related_organization_name_business_name_line1_txt', 'related_organization_name_business_name_line2_txt', 'business_name_line1_txt', 'business_name_line2_txt', 'controlled_organization_ind', 'direct_controlling_nacd', 'primary_activities_txt', 'address_line1_txt', 'address_line2_txt', 'city_nm', 'state_abbreviation_cd', 'legal_domicile_state_cd', 'foreign_postal_cd', 'ownership_pct', 'share_of_total_income_amt', 'share_of_eoyassets_amt', 'ubicode_vamt'])
    r_disreg = groups('r_disreg', ['disregarded_entity_name_business_name_line1_txt', 'disregarded_entity_name_business_name_line2_txt', 'primary_activities_txt'])
    r_trans = groups('r_trans', ['business_name_line1_txt', 'business_name_line2_txt', 'involved_amt', 'transaction_type_txt', 'method_of_amount_determination_txt'])
    r_unrel = groups('r_unrel', ['ein', 'business_name_line1_txt', 'general_or_managing_partner_ind', 'primary_activities_txt', 'address_line1_txt', 'address_line2_txt', 'city_nm', 'state_abbreviation_cd', 'legal_domicile_state_cd', 'ownership_pct', 'share_of_total_income_amt', 'share_of_eoyassets_amt', 'ubicode_vamt'])

    return {
        'header': hdr,
        'core_hot': core_hot,
        'irs990_root': irs990,
        'irs990_ez_root': irs990ez,
        'irs990_pf_root': irs990pf,
        'irs990_schedule_c_root': schc,
        'irs990_schedule_c_supplemental_info': schc_supplemental,
        'irs990_ez_form990_total_assets_grp': ez_ta,
        'irs990_ez_sum_of_total_liabilities_grp': ez_tl,
        'irs990_pf_analysis_of_revenue_and_expenses': pf_ana,
        'irs990_pf_form990_pfbalance_sheets_grp': pf_bs,
        'return_header_all': rha,
        'irs990_books_in_care_of_detail': books_990,
        'irs990_ez_books_in_care_of_detail': books_ez,
        'grants': grants,
        'irs990_contractor_compensation_grp': contractors,
        'officers': officers_rows,
        'highest_comp_employees': high_rows,
        'former_key_people': former_rows,
        'irs990_ez_officer_director_trustee_empl_grp': ez_officer_rows,
        'irs990_schedule_j_rltd_org_officer_trst_key_empl_grp': schedj_rows,
        'irs990_pf_officer_dir_trst_key_empl_info_grp': pf_officer_rows,
        'irs990_schedule_l_bus_tr_involve_interested_prsn_grp': schedl_bus,
        'irs990_schedule_l_disqualified_person_ex_bnft_tr_grp': schedl_ebt,
        'irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp': schedl_grant,
        'irs990_schedule_l_loans_btwn_org_interested_prsn_grp': schedl_loan,
        'irs990_schedule_r_id_related_tax_exempt_org_grp': r_texempt,
        'irs990_schedule_r_id_related_org_txbl_corp_tr_grp': r_corptr,
        'irs990_schedule_r_id_related_org_txbl_partnership_grp': r_part,
        'irs990_schedule_r_id_disregarded_entities_grp': r_disreg,
        'irs990_schedule_r_transactions_related_org_grp': r_trans,
        'irs990_schedule_r_unrelated_org_txbl_partnership_grp': r_unrel,
    }


def iter_xml_files(root: Path) -> Iterable[str]:
    for base, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith('.xml'):
                yield str(Path(base, fn))


def build_schema(conn: sqlite3.Connection) -> None:
    exec_all(conn, DDL, 'tables')
    conn.commit()


SCHEMA_MIGRATIONS = {
    'irs990_pf_root': [
        ('website_address_txt', 'TEXT'),
        ('legislative_political_acty_ind', 'TEXT'),
        ('more_than100_spent_ind', 'TEXT'),
        ('form1120_pol_filed_ind', 'TEXT'),
        ('influence_legislation_ind', 'TEXT'),
        ('influence_election_ind', 'TEXT'),
        ('total_grant_or_contri_pd_dur_yr_amt', 'NUMERIC'),
        ('mission_desc_txt', 'TEXT'),
    ],
    'irs990_schedule_c_root': [
        ('political_expenditures_amt', 'NUMERIC'),
        ('volunteer_hours_cnt', 'NUMERIC'),
        ('expended527_activities_amt', 'NUMERIC'),
        ('total_exempt_function_expend_amt', 'NUMERIC'),
        ('form1120_pol_filed_ind', 'TEXT'),
        ('total_grassroots_lobbying_amt', 'NUMERIC'),
        ('total_direct_lobbying_amt', 'NUMERIC'),
        ('total_lobbying_expend_grp_amt', 'NUMERIC'),
        ('other_exempt_purpose_expend_amt', 'NUMERIC'),
        ('total_exempt_purpose_expenditures_amt', 'NUMERIC'),
        ('lobbying_nontaxable_amt', 'NUMERIC'),
        ('grassroots_nontaxable_amt', 'NUMERIC'),
        ('lobbying_grassroots_excess_amt', 'NUMERIC'),
        ('lobbying_excess_amt', 'NUMERIC'),
        ('avg_lobbying_nontaxable_minus3_amt', 'NUMERIC'),
        ('avg_lobbying_nontaxable_minus2_amt', 'NUMERIC'),
        ('avg_lobbying_nontaxable_minus1_amt', 'NUMERIC'),
        ('avg_lobbying_nontaxable_current_amt', 'NUMERIC'),
        ('avg_lobbying_nontaxable_total_amt', 'NUMERIC'),
        ('lobbying_ceiling_amt', 'NUMERIC'),
        ('avg_grassroots_nontaxable_minus3_amt', 'NUMERIC'),
        ('avg_grassroots_nontaxable_minus2_amt', 'NUMERIC'),
        ('avg_grassroots_nontaxable_minus1_amt', 'NUMERIC'),
        ('avg_grassroots_nontaxable_current_amt', 'NUMERIC'),
        ('avg_grassroots_nontaxable_total_amt', 'NUMERIC'),
        ('grassroots_ceiling_amt', 'NUMERIC'),
        ('organization_belongs_afflt_grp_ind', 'TEXT'),
        ('volunteers_ind', 'TEXT'),
        ('paid_staff_or_management_ind', 'TEXT'),
        ('media_advertisements_ind', 'TEXT'),
        ('media_advertisements_amt', 'NUMERIC'),
        ('mailings_members_ind', 'TEXT'),
        ('mailings_members_amt', 'NUMERIC'),
        ('publications_or_broadcast_ind', 'TEXT'),
        ('publications_or_broadcast_amt', 'NUMERIC'),
        ('grants_other_organizations_ind', 'TEXT'),
        ('grants_other_organizations_amt', 'NUMERIC'),
        ('direct_contact_legislators_ind', 'TEXT'),
        ('direct_contact_legislators_amt', 'NUMERIC'),
        ('rallies_demonstrations_ind', 'TEXT'),
        ('rallies_demonstrations_amt', 'NUMERIC'),
        ('other_activities_ind', 'TEXT'),
        ('other_activities_amt', 'NUMERIC'),
        ('total_lobbying_expenditures_amt', 'NUMERIC'),
        ('not_described_section501c3_ind', 'TEXT'),
        ('dues_assessments_amt', 'NUMERIC'),
        ('non_deductible_lbbyng_pltcl_cy_amt', 'NUMERIC'),
        ('non_deductible_lbbyng_pltcl_tot_amt', 'NUMERIC'),
        ('aggregate_reported_dues_ntc_amt', 'NUMERIC'),
        ('carried_over_amt', 'NUMERIC'),
        ('substantially_all_dues_nonded_ind', 'TEXT'),
    ],
    'irs990_pf_analysis_of_revenue_and_expenses': [
        ('interest_on_savings_temp_cash_investments_rev_and_expnss_amt', 'NUMERIC'),
        ('dividends_interest_securities_rev_and_expnss_amt', 'NUMERIC'),
        ('net_investment_income_amt', 'NUMERIC'),
        ('contri_paid_rev_and_expnss_amt', 'NUMERIC'),
    ],
}


def ensure_schema_columns(conn: sqlite3.Connection) -> None:
    """Add newly introduced optional columns when appending to an older DB."""
    for table, columns in SCHEMA_MIGRATIONS.items():
        existing = {r[1].lower() for r in conn.execute(f"PRAGMA table_info('{table}')")}
        for col, coltype in columns:
            if col.lower() not in existing:
                print(f"[schema] adding missing column: {table}.{col}")
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.commit()


def existing_filing_keys(conn: sqlite3.Connection) -> Tuple[set, set]:
    filing_ids = set()
    object_ids = set()
    try:
        for (filing_id,) in conn.execute("SELECT filing_id FROM returns"):
            if filing_id:
                filing_ids.add(filing_id)
                object_ids.add(object_id_from_filing_id(filing_id))
    except sqlite3.OperationalError:
        pass
    return filing_ids, object_ids


def select_xml_files(xml_dir: Path, append_only: bool, conn: sqlite3.Connection) -> Tuple[List[str], Dict[str, int]]:
    existing_ids, existing_object_ids = existing_filing_keys(conn) if append_only else (set(), set())
    selected: List[str] = []
    seen_input_object_ids = set()
    stats = {
        'total': 0,
        'selected': 0,
        'skipped_existing': 0,
        'skipped_duplicate_input': 0,
    }
    for fp in iter_xml_files(xml_dir):
        stats['total'] += 1
        stem = Path(fp).stem
        object_id = object_id_from_filing_id(stem)
        if append_only and (stem in existing_ids or object_id in existing_object_ids):
            stats['skipped_existing'] += 1
            continue
        if object_id in seen_input_object_ids:
            stats['skipped_duplicate_input'] += 1
            continue
        seen_input_object_ids.add(object_id)
        selected.append(fp)
        stats['selected'] += 1
    return selected, stats


def build_views_indexes(conn: sqlite3.Connection) -> None:
    for v in ['grants_compat_v1', 'vw_contractors', 'sched_r_related_orgs_expanded']:
        conn.execute(f'DROP VIEW IF EXISTS {v}')
    exec_all(conn, VIEWS, 'views')
    exec_all(conn, INDEXES, 'indexes')
    conn.commit()


def rebuild_canonical(conn: sqlite3.Connection) -> None:
    conn.execute('DELETE FROM canonical_by_ein_year')
    conn.executescript("""
        INSERT OR REPLACE INTO canonical_by_ein_year (ein, tax_year, filing_id, return_type, return_ts, amended_return_ind, period_end)
        SELECT ein, tax_year, filing_id, return_type, return_ts, amended_return_ind, period_end
        FROM (
          SELECT r.ein, r.tax_year, r.filing_id, r.return_type, r.return_ts, r.amended_return_ind, r.period_end,
                 ROW_NUMBER() OVER (
                   PARTITION BY r.ein, r.tax_year
                   ORDER BY (r.return_ts IS NOT NULL) DESC, r.return_ts DESC, IFNULL(r.amended_return_ind, '0') DESC, r.filing_id DESC
                 ) AS rn
          FROM returns r
          WHERE r.tax_year IS NOT NULL AND r.return_type IN ('990','990EZ','990PF')
        ) t
        WHERE rn = 1;
    """)
    conn.commit()


def load_data(conn: sqlite3.Connection, xml_dir: Path, workers: int, chunksize: int, commit_every: int, append_only: bool = False) -> None:
    err_log = xml_dir.parent / 'rebuild_irs990_slim_errors.log'
    processed = 0

    def ins(sql, vals):
        conn.execute(sql, vals)

    files_iter, file_stats = select_xml_files(xml_dir, append_only, conn)
    print(
        f"[load] XML files found: {file_stats['total']:,}; "
        f"selected: {file_stats['selected']:,}; "
        f"skipped existing: {file_stats['skipped_existing']:,}; "
        f"skipped duplicate input: {file_stats['skipped_duplicate_input']:,}"
    )
    if not files_iter:
        print('[load] no new XML files to load')
        return

    def handle(row):
        nonlocal processed
        if 'error' in row:
            with open(err_log, 'a', encoding='utf-8') as ef:
                ef.write(row['error'] + '\n')
            return

        h = row['header']
        ins("""INSERT OR REPLACE INTO returns (
            filing_id, source_file, ein, return_type, tax_year, period_end, schema_version, return_ts, amended_return_ind,
            org_name, dba_name, in_care_of_name, us_address_line1, us_address_line2, city, state, zip,
            foreign_address_line1, foreign_city, foreign_province, foreign_country, foreign_postal_code, website
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
            h['filing_id'], h['source_file'], h['ein'], h['return_type'], h['tax_year'], h['period_end'], h['schema_version'],
            h['return_ts'], h['amended_return_ind'], h['org_name'], h['dba_name'], h['in_care_of_name'],
            h['us_address_line1'], h['us_address_line2'], h['city'], h['state'], h['zip'], h['foreign_address_line1'],
            h['foreign_city'], h['foreign_province'], h['foreign_country'], h['foreign_postal_code'], h['website']
        ])

        ch = row['core_hot']
        ins("""INSERT OR REPLACE INTO core_hot (
            filing_id,total_revenue,total_expenses,net_assets_boy,net_assets_eoy,contributions,program_service_revenue,membership_dues,investment_income,government_grants,grants_paid,lobbying_expense,employees_count,volunteers_count,mission_desc
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [ch[k] for k in ['filing_id','total_revenue','total_expenses','net_assets_boy','net_assets_eoy','contributions','program_service_revenue','membership_dues','investment_income','government_grants','grants_paid','lobbying_expense','employees_count','volunteers_count','mission_desc']])

        def ins_singleton(table, cols):
            r = row[table]
            vals = [h['filing_id']] + [r.get(c) for c in cols]
            placeholders = ','.join('?' for _ in vals)
            conn.execute(f"INSERT OR REPLACE INTO {table} (filing_id,{','.join(cols)}) VALUES ({placeholders})", vals)

        ins_singleton('irs990_root', IRS990_COLS)
        ins_singleton('irs990_ez_root', IRS990EZ_COLS)
        ins_singleton('irs990_pf_root', IRS990PF_COLS)
        ins_singleton('irs990_schedule_c_root', SCHEDC_COLS)
        conn.execute("DELETE FROM irs990_schedule_c_supplemental_info WHERE filing_id = ?", [h['filing_id']])
        for r in row['irs990_schedule_c_supplemental_info']:
            ins("""INSERT INTO irs990_schedule_c_supplemental_info (
                filing_id,form_and_line_reference_desc,explanation_txt
            ) VALUES (?,?,?)""", [r.get(k) for k in ['filing_id','form_and_line_reference_desc','explanation_txt']])
        ins_singleton('irs990_ez_form990_total_assets_grp', EZ_TA_COLS)
        ins_singleton('irs990_ez_sum_of_total_liabilities_grp', EZ_TL_COLS)
        ins_singleton('irs990_pf_analysis_of_revenue_and_expenses', PF_ANA_COLS)
        ins_singleton('irs990_pf_form990_pfbalance_sheets_grp', PF_BS_COLS)
        ins_singleton('return_header_all', ['person_nm','preparer_person_nm','person_title_txt','signature_dt','preparer_firm_name_business_name_line1_txt','ptin','preparation_dt','tax_period_begin_dt','tax_period_end_dt'])
        ins_singleton('irs990_books_in_care_of_detail', ['address_line1_txt','city_nm','state_abbreviation_cd','zipcd','phone_num','person_nm'])
        ins_singleton('irs990_ez_books_in_care_of_detail', ['address_line1_txt','city_nm','state_abbreviation_cd','zipcd','phone_num','person_nm'])

        for r in row['grants']:
            ins("""INSERT INTO grants (
                filing_id,filer_ein,filer_name,recipient_ein,business_name_line1_txt,business_name_line2_txt,us_address_line1_txt,us_address_line2_txt,us_city_nm,us_state_abbreviation_cd,us_zip_cd,foreign_address_line1_txt,foreign_city_nm,foreign_province_or_state_nm,foreign_postal_cd,foreign_country_cd,ircsection_desc,cash_grant_amt,non_cash_assistance_amt,non_cash_assistance_desc,valuation_method_used_desc,purpose_of_grant_txt
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [r.get(k) for k in ['filing_id','filer_ein','filer_name','recipient_ein','business_name_line1_txt','business_name_line2_txt','us_address_line1_txt','us_address_line2_txt','us_city_nm','us_state_abbreviation_cd','us_zip_cd','foreign_address_line1_txt','foreign_city_nm','foreign_province_or_state_nm','foreign_postal_cd','foreign_country_cd','ircsection_desc','cash_grant_amt','non_cash_assistance_amt','non_cash_assistance_desc','valuation_method_used_desc','purpose_of_grant_txt']])

        for r in row['irs990_contractor_compensation_grp']:
            ins("""INSERT INTO irs990_contractor_compensation_grp (
                filing_id,compensation_amt,address_line1_txt,address_line2_txt,city_nm,country_cd,foreign_postal_cd,province_or_state_nm,usaddress_address_line1_txt,usaddress_address_line2_txt,usaddress_city_nm,state_abbreviation_cd,zipcd,business_name_line1_txt,business_name_line2_txt,person_nm,services_desc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [r.get(k) for k in ['filing_id','compensation_amt','address_line1_txt','address_line2_txt','city_nm','country_cd','foreign_postal_cd','province_or_state_nm','usaddress_address_line1_txt','usaddress_address_line2_txt','usaddress_city_nm','state_abbreviation_cd','zipcd','business_name_line1_txt','business_name_line2_txt','person_nm','services_desc']])

        for t, cols in [
            ('officers', ['filing_id','person_name','title_txt','avg_hours_week','comp_from_org','comp_from_related','other_compensation','is_officer','is_director','is_key_employee','is_former']),
            ('highest_comp_employees', ['filing_id','person_name','title_txt','avg_hours_week','comp_from_org','comp_from_related','other_compensation']),
            ('former_key_people', ['filing_id','person_name','title_txt','comp_from_org','comp_from_related','other_compensation']),
            ('irs990_ez_officer_director_trustee_empl_grp', ['filing_id','person_nm','title_txt','average_hrs_per_wk_devoted_to_pos_rt','compensation_amt','employee_benefit_program_amt','expense_account_other_allwnc_amt']),
            ('irs990_schedule_j_rltd_org_officer_trst_key_empl_grp', ['filing_id','person_nm','title_txt','base_compensation_filing_org_amt','bonus_filing_organization_amount','total_compensation_filing_org_amt','total_compensation_rltd_orgs_amt']),
            ('irs990_pf_officer_dir_trst_key_empl_info_grp', ['filing_id','person_nm','title_txt','average_hrs_per_wk_devoted_to_pos_rt','compensation_amt','employee_benefits_amt','expense_account_amt']),
            ('irs990_schedule_l_bus_tr_involve_interested_prsn_grp', ['filing_id','person_nm','relationship_description_txt','transaction_amt','transaction_desc']),
            ('irs990_schedule_l_disqualified_person_ex_bnft_tr_grp', ['filing_id','person_nm','rln_disqualified_person_org_txt','transaction_corrected_ind','transaction_desc']),
            ('irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp', ['filing_id','person_nm','relationship_with_org_txt','cash_grant_amt','type_of_assistance_txt','assistance_purpose_txt']),
            ('irs990_schedule_l_loans_btwn_org_interested_prsn_grp', ['filing_id','person_nm','relationship_with_org_txt','original_principal_amt','balance_due_amt','loan_purpose_txt']),
            ('irs990_schedule_r_id_related_tax_exempt_org_grp', ['filing_id','ein','business_name_line1_txt','business_name_line2_txt','disregarded_entity_name_business_name_line1_txt','disregarded_entity_name_business_name_line2_txt','exempt_code_section_txt','public_charity_status_txt','controlled_organization_ind','direct_controlling_nacd','primary_activities_txt','address_line1_txt','address_line2_txt','city_nm','state_abbreviation_cd','legal_domicile_state_cd','country_cd','foreign_postal_cd']),
            ('irs990_schedule_r_id_related_org_txbl_corp_tr_grp', ['filing_id','ein','related_organization_name_business_name_line1_txt','related_organization_name_business_name_line2_txt','business_name_line1_txt','business_name_line2_txt','controlled_organization_ind','direct_controlling_nacd','primary_activities_txt','address_line1_txt','address_line2_txt','city_nm','state_abbreviation_cd','legal_domicile_state_cd','foreign_postal_cd','ownership_pct','share_of_total_income_amt','share_of_eoyassets_amt']),
            ('irs990_schedule_r_id_related_org_txbl_partnership_grp', ['filing_id','ein','related_organization_name_business_name_line1_txt','related_organization_name_business_name_line2_txt','business_name_line1_txt','business_name_line2_txt','controlled_organization_ind','direct_controlling_nacd','primary_activities_txt','address_line1_txt','address_line2_txt','city_nm','state_abbreviation_cd','legal_domicile_state_cd','foreign_postal_cd','ownership_pct','share_of_total_income_amt','share_of_eoyassets_amt','ubicode_vamt']),
            ('irs990_schedule_r_id_disregarded_entities_grp', ['filing_id','disregarded_entity_name_business_name_line1_txt','disregarded_entity_name_business_name_line2_txt','primary_activities_txt']),
            ('irs990_schedule_r_transactions_related_org_grp', ['filing_id','business_name_line1_txt','business_name_line2_txt','involved_amt','transaction_type_txt','method_of_amount_determination_txt']),
            ('irs990_schedule_r_unrelated_org_txbl_partnership_grp', ['filing_id','ein','business_name_line1_txt','general_or_managing_partner_ind','primary_activities_txt','address_line1_txt','address_line2_txt','city_nm','state_abbreviation_cd','legal_domicile_state_cd','ownership_pct','share_of_total_income_amt','share_of_eoyassets_amt','ubicode_vamt']),
        ]:
            for r in row[t]:
                placeholders = ','.join('?' for _ in cols)
                conn.execute(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({placeholders})", [r.get(k) for k in cols])

        processed += 1
        if processed % commit_every == 0:
            conn.commit()
            print(f'[load] {processed:,} files')

    if workers <= 1:
        for fp in files_iter:
            handle(extract_file(fp))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for row in ex.map(extract_file, files_iter, chunksize=chunksize):
                handle(row)

    conn.commit()
    print(f'[load] done: {processed:,} files')


# ---------------------------------------------------------------------------
# XML preflight / compatibility scanner
# ---------------------------------------------------------------------------

PREFLIGHT_SUPPORTED_RETURN_TYPES = {'990', '990EZ', '990PF'}

# This is intentionally a warning inventory, not a hard allow/deny gate.
# The actual compatibility check is whether extract_file() can parse and extract
# the fields this slim database depends on.
PREFLIGHT_KNOWN_GOOD_COMBOS = {
    # Observed in 2017 bulk-download files / older return tax years.
    ('990', '2014v6.0'),
    ('990EZ', '2014v6.0'),
    ('990PF', '2014v6.0'),

    ('990', '2015v3.0'),
    ('990EZ', '2015v3.0'),
    ('990PF', '2015v3.0'),

    ('990', '2016v3.0'),
    ('990EZ', '2016v3.0'),
    ('990PF', '2016v3.0'),

    # Observed in 2018 bulk-download preflight samples for prior-year filings.
    ('990', '2016v3.1'),
    ('990EZ', '2016v3.1'),
    ('990PF', '2016v3.1'),

    # Observed in 2018 bulk-download files / 2017 return tax year.
    # These are warning-suppression inventory entries, not hard allow/deny rules.
    ('990', '2017v2.0'),
    ('990EZ', '2017v2.0'),
    ('990PF', '2017v2.0'),

    ('990', '2017v2.2'),
    ('990EZ', '2017v2.2'),
    ('990PF', '2017v2.2'),

    ('990', '2017v2.3'),
    ('990EZ', '2017v2.3'),
    ('990PF', '2017v2.3'),
}


def schema_version_from_root(root: ET.Element) -> Optional[str]:
    return next((v for k, v in root.attrib.items() if local(k).lower().endswith('version')), None)


def preflight_add_caveat(row: Dict[str, Any], code: str, message: str, severity: str = 'warning') -> None:
    row.setdefault('caveats', []).append({
        'code': code,
        'severity': severity,
        'message': message,
    })


def any_truthy_descendant(root: ET.Element, tag_names: Sequence[str]) -> bool:
    for tag in tag_names:
        if truthy_x01(descendants_text_first(root, [tag])) == 'X':
            return True
    return False


def any_positive_descendant(root: ET.Element, tag_names: Sequence[str]) -> bool:
    for tag in tag_names:
        val = norm_num(descendants_text_first(root, [tag]))
        if val is not None and val > 0:
            return True
    return False


def recognized_main_forms(root: ET.Element) -> List[str]:
    fns = form_nodes(root)
    return [name for name in ('990', '990EZ', '990PF') if fns.get(name) is not None]


def count_nonblank_values(d: Dict[str, Any], exclude: Sequence[str] = ('filing_id',)) -> int:
    exclude_set = set(exclude)
    return sum(1 for k, v in d.items() if k not in exclude_set and v not in (None, ''))


def preflight_file(file_path: str) -> Dict[str, Any]:
    """Inspect one XML file using the same parser/extractor used by the loader.

    This function intentionally calls extract_file() so preflight results reflect
    what the real load path would do, without writing anything to SQLite.
    """
    p = Path(file_path)
    row: Dict[str, Any] = {
        'source_file': str(p),
        'filing_id': p.stem,
        'status': 'ok',
        'return_type': None,
        'schema_version': None,
        'tax_year': None,
        'period_end': None,
        'ein': None,
        'recognized_forms': [],
        'core_present_fields': 0,
        'grant_rows': 0,
        'contractor_rows': 0,
        'officer_rows': 0,
        'ez_officer_rows': 0,
        'pf_officer_rows': 0,
        'schedule_l_rows': 0,
        'schedule_r_rows': 0,
        'caveats': [],
    }

    try:
        root = ET.parse(str(p)).getroot()
    except Exception as e:
        row['status'] = 'parse_error'
        preflight_add_caveat(row, 'parse_error', f'XML parse failed: {e}', 'error')
        return row

    row['return_type'] = first_text_paths(root, HEADER_PATHS['return_type'])
    row['schema_version'] = schema_version_from_root(root)
    row['tax_year'] = norm_int(first_text_paths(root, HEADER_PATHS['tax_year']))
    row['period_end'] = first_text_paths(root, HEADER_PATHS['period_end'])
    row['ein'] = first_text_paths(root, HEADER_PATHS['ein'])
    row['recognized_forms'] = recognized_main_forms(root)

    missing_header = []
    if not row['return_type']:
        missing_header.append('ReturnTypeCd')
    if not row['tax_year']:
        missing_header.append('TaxYr/TaxYear')
    if not row['ein']:
        missing_header.append('Filer/EIN')
    if missing_header:
        row['status'] = 'missing_required_header'
        preflight_add_caveat(
            row,
            'missing_required_header',
            'Missing required header field(s): ' + ', '.join(missing_header),
            'error',
        )
        return row

    if not row['schema_version']:
        preflight_add_caveat(row, 'schema_version_missing', 'Root returnVersion/schema version is missing.')

    rtype = str(row['return_type'] or '')
    schema = row['schema_version']

    if rtype not in PREFLIGHT_SUPPORTED_RETURN_TYPES:
        preflight_add_caveat(
            row,
            'unsupported_return_type',
            f'ReturnTypeCd={rtype!r} is not one of the slim-loader supported forms: 990, 990EZ, 990PF.',
            'error',
        )

    if not row['recognized_forms']:
        preflight_add_caveat(
            row,
            'no_recognized_main_form',
            'No IRS990, IRS990EZ, or IRS990PF form node was found under ReturnData.',
            'error',
        )
    elif rtype in PREFLIGHT_SUPPORTED_RETURN_TYPES and rtype not in row['recognized_forms']:
        preflight_add_caveat(
            row,
            'return_type_form_mismatch',
            f'ReturnTypeCd={rtype!r}, but recognized form nodes are {row["recognized_forms"]!r}.',
        )

    if schema and rtype in PREFLIGHT_SUPPORTED_RETURN_TYPES and (rtype, schema) not in PREFLIGHT_KNOWN_GOOD_COMBOS:
        preflight_add_caveat(
            row,
            'unknown_form_version_combo',
            f'{rtype} / {schema} is not in the known-good combo inventory. Extraction may still be fine; review coverage.',
        )

    # Confirm whether the actual extractor succeeds.
    extracted = extract_file(str(p))
    if 'error' in extracted:
        row['status'] = 'extractor_error'
        preflight_add_caveat(row, 'extractor_error', extracted['error'], 'error')
        return row

    row['core_present_fields'] = count_nonblank_values(extracted.get('core_hot', {}))
    row['grant_rows'] = len(extracted.get('grants', []) or [])
    row['contractor_rows'] = len(extracted.get('irs990_contractor_compensation_grp', []) or [])
    row['officer_rows'] = len(extracted.get('officers', []) or [])
    row['ez_officer_rows'] = len(extracted.get('irs990_ez_officer_director_trustee_empl_grp', []) or [])
    row['pf_officer_rows'] = len(extracted.get('irs990_pf_officer_dir_trst_key_empl_info_grp', []) or [])
    row['schedule_l_rows'] = sum(len(extracted.get(k, []) or []) for k in [
        'irs990_schedule_l_bus_tr_involve_interested_prsn_grp',
        'irs990_schedule_l_disqualified_person_ex_bnft_tr_grp',
        'irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp',
        'irs990_schedule_l_loans_btwn_org_interested_prsn_grp',
    ])
    row['schedule_r_rows'] = sum(len(extracted.get(k, []) or []) for k in [
        'irs990_schedule_r_id_related_tax_exempt_org_grp',
        'irs990_schedule_r_id_related_org_txbl_corp_tr_grp',
        'irs990_schedule_r_id_related_org_txbl_partnership_grp',
        'irs990_schedule_r_id_disregarded_entities_grp',
        'irs990_schedule_r_transactions_related_org_grp',
        'irs990_schedule_r_unrelated_org_txbl_partnership_grp',
    ])

    if row['core_present_fields'] == 0:
        preflight_add_caveat(
            row,
            'all_core_hot_fields_blank',
            'The file parsed, but no nonblank core_hot fields were extracted.',
        )

    # Caveat detector for the specific older-header issue we found in 2017/2018 samples.
    filer_level_ic = first_text_paths(root, ['/Return/ReturnHeader/Filer/InCareOfNm'])
    extracted_ic = (extracted.get('header') or {}).get('in_care_of_name')
    if filer_level_ic and not extracted_ic:
        preflight_add_caveat(
            row,
            'filer_incareof_unmapped',
            'Filer/InCareOfNm exists, but the current header extraction did not capture in_care_of_name.',
        )

    org_grant_indicator = any_truthy_descendant(root, [
        'GrantsToOrganizationsInd',
        'MoreThan5000KToOrgInd',
    ])
    individual_grant_indicator = any_truthy_descendant(root, [
        'GrantsToIndividualsInd',
        'MoreThan5000KToIndividualsInd',
    ])
    grant_amount_signal = any_positive_descendant(root, [
        'ContriPaidRevAndExpnssAmt',
        'ContriPaidDsbrsChrtblAmt',
        'CYGrantsAndSimilarPaidAmt',
        'GrantsAndSimilarAmountsPaidAmt',
        'GrantAmt',
        'GrantsAndAllocationsAmt',
    ])

    # For this research workflow, warnings should be organization-grant focused.
    # A filing that explicitly reports individual grants but not organization
    # grants should not be treated as a missing org-recipient-detail problem.
    org_relevant_grant_signal = org_grant_indicator or (
        grant_amount_signal and not individual_grant_indicator
    )

    if org_relevant_grant_signal and row['grant_rows'] == 0:
        preflight_add_caveat(
            row,
            'grant_signal_without_detail_rows',
            'The XML has organization-grant indicators or ambiguous grant amounts, but no detailed organization grant recipient rows were extracted. This may be legitimate for below-threshold detail, but should be spot-checked.',
            'warning',
        )

    pf_contrib_paid_signal = any_positive_descendant(root, [
        'ContriPaidRevAndExpnssAmt',
        'ContriPaidDsbrsChrtblAmt',
    ])
    pf_individual_only_signal = individual_grant_indicator and not org_grant_indicator

    if (
        rtype == '990PF'
        and row['grant_rows'] == 0
        and pf_contrib_paid_signal
        and not pf_individual_only_signal
    ):
        preflight_add_caveat(
            row,
            'pf_contributions_paid_without_detail_rows',
            '990PF reports contributions paid that are not clearly individual-only, but no GrantOrContributionPdDurYrGrp detail rows were extracted.',
            'warning',
        )

    return row


def preflight_csv_row(row: Dict[str, Any]) -> Dict[str, Any]:
    caveats = row.get('caveats') or []
    return {
        'source_file': row.get('source_file'),
        'filing_id': row.get('filing_id'),
        'status': row.get('status'),
        'return_type': row.get('return_type'),
        'schema_version': row.get('schema_version'),
        'tax_year': row.get('tax_year'),
        'period_end': row.get('period_end'),
        'ein': row.get('ein'),
        'recognized_forms': '|'.join(row.get('recognized_forms') or []),
        'core_present_fields': row.get('core_present_fields'),
        'grant_rows': row.get('grant_rows'),
        'contractor_rows': row.get('contractor_rows'),
        'officer_rows': row.get('officer_rows'),
        'ez_officer_rows': row.get('ez_officer_rows'),
        'pf_officer_rows': row.get('pf_officer_rows'),
        'schedule_l_rows': row.get('schedule_l_rows'),
        'schedule_r_rows': row.get('schedule_r_rows'),
        'caveat_codes': '|'.join(c.get('code', '') for c in caveats),
        'caveat_messages': ' || '.join(c.get('message', '') for c in caveats),
    }


def run_preflight(
    xml_dir: Path,
    workers: int,
    chunksize: int,
    max_files: int = 0,
    report_path: Optional[Path] = None,
    csv_path: Optional[Path] = None,
    sample_limit: int = 25,
) -> int:
    import csv
    import json
    from collections import Counter

    files = sorted(iter_xml_files(xml_dir))
    found_count = len(files)
    if max_files and max_files > 0:
        files = files[:max_files]

    print(f'[preflight] XML files found: {found_count:,}; scanning: {len(files):,}')
    if not files:
        print('[preflight] no XML files found')
        return 0

    counts = Counter()
    by_return_type = Counter()
    by_schema_version = Counter()
    by_combo = Counter()
    by_tax_year = Counter()
    caveat_counts = Counter()
    caveat_examples: Dict[str, List[Dict[str, Any]]] = {}
    status_counts = Counter()
    extraction_totals = Counter()

    fieldnames = list(preflight_csv_row({'caveats': []}).keys())
    csv_handle = None
    writer = None
    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_handle = open(csv_path, 'w', newline='', encoding='utf-8')
        writer = csv.DictWriter(csv_handle, fieldnames=fieldnames)
        writer.writeheader()

    try:
        if workers <= 1:
            iterator = map(preflight_file, files)
        else:
            executor = ProcessPoolExecutor(max_workers=workers)
            iterator = executor.map(preflight_file, files, chunksize=chunksize)

        try:
            for idx, row in enumerate(iterator, 1):
                counts['files_scanned'] += 1
                status_counts[row.get('status') or 'unknown'] += 1

                rtype = row.get('return_type') or '<missing>'
                schema = row.get('schema_version') or '<missing>'
                tax_year = row.get('tax_year') or '<missing>'
                by_return_type[str(rtype)] += 1
                by_schema_version[str(schema)] += 1
                by_combo[f'{rtype}|{schema}'] += 1
                by_tax_year[str(tax_year)] += 1

                extraction_totals['grant_rows'] += int(row.get('grant_rows') or 0)
                extraction_totals['contractor_rows'] += int(row.get('contractor_rows') or 0)
                extraction_totals['officer_rows'] += int(row.get('officer_rows') or 0)
                extraction_totals['ez_officer_rows'] += int(row.get('ez_officer_rows') or 0)
                extraction_totals['pf_officer_rows'] += int(row.get('pf_officer_rows') or 0)
                extraction_totals['schedule_l_rows'] += int(row.get('schedule_l_rows') or 0)
                extraction_totals['schedule_r_rows'] += int(row.get('schedule_r_rows') or 0)

                if row.get('grant_rows'):
                    extraction_totals['files_with_grants'] += 1
                if row.get('contractor_rows'):
                    extraction_totals['files_with_contractors'] += 1
                if row.get('officer_rows') or row.get('ez_officer_rows') or row.get('pf_officer_rows'):
                    extraction_totals['files_with_people'] += 1

                for caveat in row.get('caveats') or []:
                    code = caveat.get('code') or 'unknown_caveat'
                    caveat_counts[code] += 1
                    examples = caveat_examples.setdefault(code, [])
                    if len(examples) < sample_limit:
                        examples.append({
                            'source_file': row.get('source_file'),
                            'return_type': row.get('return_type'),
                            'schema_version': row.get('schema_version'),
                            'tax_year': row.get('tax_year'),
                            'severity': caveat.get('severity'),
                            'message': caveat.get('message'),
                        })

                if writer:
                    writer.writerow(preflight_csv_row(row))

                if idx % 1000 == 0:
                    print(f'[preflight] scanned {idx:,}/{len(files):,}')
        finally:
            if workers > 1:
                executor.shutdown(wait=True)
    finally:
        if csv_handle:
            csv_handle.close()

    report = {
        'xml_dir': str(xml_dir),
        'files_found': found_count,
        'files_scanned': counts['files_scanned'],
        'status_counts': dict(status_counts),
        'by_return_type': dict(by_return_type),
        'by_schema_version': dict(by_schema_version),
        'by_return_type_schema_version': dict(by_combo),
        'by_tax_year': dict(sorted(by_tax_year.items())),
        'extraction_totals': dict(extraction_totals),
        'caveat_counts': dict(caveat_counts),
        'caveat_examples': caveat_examples,
    }

    print('[preflight] complete')
    print(f"[preflight] status: {dict(status_counts)}")
    print(f"[preflight] return types: {dict(by_return_type)}")
    print(f"[preflight] schema versions: {dict(by_schema_version)}")
    print(f"[preflight] extraction totals: {dict(extraction_totals)}")

    if caveat_counts:
        print('[preflight] caveats:')
        for code, n in caveat_counts.most_common():
            print(f'  - {code}: {n:,}')
    else:
        print('[preflight] caveats: none')

    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, sort_keys=True)
        print(f'[preflight] wrote JSON summary: {report_path}')

    if csv_path:
        print(f'[preflight] wrote CSV file-level report: {csv_path}')

    error_count = sum(status_counts[s] for s in ('parse_error', 'missing_required_header', 'extractor_error'))
    if error_count:
        print(f'[preflight] completed with {error_count:,} file-level error(s); review report before append')
        return 1
    return 0

def validate(conn: sqlite3.Connection) -> None:
    for label, sql in [
        ('returns', 'SELECT COUNT(*) FROM returns'),
        ('canonical_by_ein_year', 'SELECT COUNT(*) FROM canonical_by_ein_year'),
        ('grants', 'SELECT COUNT(*) FROM grants'),
        ('contractors', 'SELECT COUNT(*) FROM irs990_contractor_compensation_grp'),
        ('officers', 'SELECT COUNT(*) FROM officers'),
    ]:
        try:
            n = conn.execute(sql).fetchone()[0]
            print(f'[validate] {label}: {n:,}')
        except Exception as e:
            print(f'[validate] {label}: ERROR: {e}')


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description='Slim rebuild for current IRS 990 query modules')
    ap.add_argument('--db', required=False,
                    help='SQLite database to create/rebuild/append. Required unless --preflight is used.')
    ap.add_argument('--xml-dir', required=True)
    ap.add_argument('--preflight', action='store_true',
                    help='Scan XML files recursively and report parser/extraction compatibility without writing to SQLite.')
    ap.add_argument('--preflight-report', default=None,
                    help='Optional JSON summary report path for --preflight.')
    ap.add_argument('--preflight-csv', default=None,
                    help='Optional CSV file-level report path for --preflight.')
    ap.add_argument('--preflight-max-files', type=int, default=0,
                    help='Optional cap on XML files to scan in --preflight mode. 0 means scan all files.')
    ap.add_argument('--preflight-sample-limit', type=int, default=25,
                    help='Maximum example files retained per caveat in the JSON report.')
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument('--chunksize', type=int, default=25)
    ap.add_argument('--commit-every', type=int, default=1000)
    ap.add_argument('--append', action='store_true',
                    help='Append only XML filings not already present in the database. Implies --keep-db.')
    ap.add_argument('--keep-db', action='store_true',
                    help='Keep the existing DB file and skip filings already present. Alias for safe append behavior.')
    ap.add_argument('--vacuum', action='store_true')
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    xml_dir = Path(args.xml_dir)

    if not xml_dir.exists():
        print(f'ERROR: xml dir not found: {xml_dir}', file=sys.stderr)
        return 2

    if args.preflight:
        return run_preflight(
            xml_dir=xml_dir,
            workers=args.workers,
            chunksize=args.chunksize,
            max_files=args.preflight_max_files,
            report_path=Path(args.preflight_report) if args.preflight_report else None,
            csv_path=Path(args.preflight_csv) if args.preflight_csv else None,
            sample_limit=args.preflight_sample_limit,
        )

    if not args.db:
        print('ERROR: --db is required unless --preflight is used', file=sys.stderr)
        return 2

    db_path = Path(args.db)
    append_mode = bool(args.append or args.keep_db)

    if db_path.exists() and not append_mode:
        print(f'[init] removing existing DB: {db_path}')
        db_path.unlink()
    elif db_path.exists() and append_mode:
        print(f'[init] append mode: preserving existing DB: {db_path}')
    elif append_mode:
        print(f'[init] append mode requested, but DB does not exist yet; creating new DB: {db_path}')

    conn = db_connect(db_path)
    try:
        print('[schema] creating/updating slim schema...')
        build_schema(conn)
        ensure_schema_columns(conn)

        print('[load] loading XML into slim schema...')
        load_data(conn, xml_dir, args.workers, args.chunksize, args.commit_every, append_only=append_mode)

        print('[canon] rebuilding canonical_by_ein_year...')
        rebuild_canonical(conn)

        print('[schema] creating views + indexes...')
        build_views_indexes(conn)

        print('[opt] ANALYZE / optimize...')
        conn.execute('ANALYZE;')
        conn.execute('PRAGMA optimize;')
        conn.commit()

        if args.vacuum:
            print('[opt] VACUUM...')
            conn.execute('VACUUM;')

        validate(conn)
    finally:
        conn.close()

    print('[done] slim rebuild complete')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
