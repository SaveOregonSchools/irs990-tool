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
  total_grant_or_contri_pd_dur_yr_amt NUMERIC,
  mission_desc_txt TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS irs990_schedule_c_root (
  filing_id TEXT PRIMARY KEY,
  total_lobbying_expenditures_amt NUMERIC,
  substantially_all_dues_nonded_ind TEXT
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
IRS990PF_COLS = ['address_change_ind','name_change_ind','initial_return_ind','final_return_ind','amended_return_ind','application_pending_ind','organization501c3_exempt_pfind','organization4947a1_trtd_pfind','website_address_txt','legislative_political_acty_ind','total_grant_or_contri_pd_dur_yr_amt','mission_desc_txt']
SCHEDC_COLS = ['total_lobbying_expenditures_amt','substantially_all_dues_nonded_ind']
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
    schc = generic_singleton_extract(fns['SCHC'], SCHEDC_COLS)

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
        'address_line1_txt': one_of(fns['990'] or root, ['AddressLine1Txt']),
        'city_nm': one_of(fns['990'] or root, ['CityNm']),
        'state_abbreviation_cd': one_of(fns['990'] or root, ['StateAbbreviationCd']),
        'zipcd': one_of(fns['990'] or root, ['ZIPCd', 'ZipCd']),
        'phone_num': one_of(fns['990'] or root, ['PhoneNum']),
        'person_nm': one_of(fns['990'] or root, ['IndividualWithBooksNm', 'PersonNm'])
    }

    books_ez = {
        'filing_id': filing_id,
        'address_line1_txt': one_of(fns['990EZ'] or root, ['AddressLine1Txt']),
        'city_nm': one_of(fns['990EZ'] or root, ['CityNm']),
        'state_abbreviation_cd': one_of(fns['990EZ'] or root, ['StateAbbreviationCd']),
        'zipcd': one_of(fns['990EZ'] or root, ['ZIPCd', 'ZipCd']),
        'phone_num': one_of(fns['990EZ'] or root, ['PhoneNum']),
        'person_nm': one_of(fns['990EZ'] or root, ['IndividualWithBooksNm', 'PersonNm'])
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
        ('total_grant_or_contri_pd_dur_yr_amt', 'NUMERIC'),
        ('mission_desc_txt', 'TEXT'),
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
    ap.add_argument('--db', required=True)
    ap.add_argument('--xml-dir', required=True)
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
    db_path = Path(args.db)
    xml_dir = Path(args.xml_dir)

    if not xml_dir.exists():
        print(f'ERROR: xml dir not found: {xml_dir}', file=sys.stderr)
        return 2

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