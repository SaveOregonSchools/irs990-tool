import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            "us_address_line1": "1 Main St",
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


def build_fixture_db(path=":memory:"):
    conn = sqlite3.connect(path)
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

        CREATE TABLE irs990_contractor_compensation_grp (
          filing_id TEXT,
          business_name_line1_txt TEXT,
          person_nm TEXT
        );

        CREATE TABLE sched_r_related_orgs_expanded (
          filing_id TEXT,
          relationship_category TEXT,
          related_ein TEXT,
          related_name_line1 TEXT,
          related_name_line2 TEXT,
          controlled_organization_ind TEXT,
          involved_amt NUMERIC,
          transaction_type_txt TEXT
        );

        CREATE TABLE grant_recipient_resolved (
          grant_id INTEGER PRIMARY KEY,
          filing_id TEXT,
          grantor_ein TEXT,
          grantor_name TEXT,
          tax_year INTEGER,
          recipient_reported_ein TEXT,
          recipient_reported_name TEXT,
          total_amount NUMERIC,
          resolved_ein TEXT,
          resolved_org_name TEXT,
          match_status TEXT,
          confidence NUMERIC,
          warning_flags TEXT
        );

        CREATE TABLE org_identity (
          identity_id INTEGER PRIMARY KEY,
          identity_key TEXT,
          ein TEXT,
          source TEXT,
          source_detail TEXT,
          source_rank INTEGER,
          display_name TEXT,
          street TEXT,
          street_norm TEXT,
          city TEXT,
          state TEXT,
          zip5 TEXT,
          subsection TEXT,
          foundation TEXT,
          deductibility TEXT,
          ntee_cd TEXT,
          status TEXT,
          tax_period TEXT,
          asset_amt NUMERIC,
          income_amt NUMERIC,
          revenue_amt NUMERIC,
          extra_json TEXT
        );

        CREATE TABLE irs990_schedule_c_root (
          filing_id TEXT,
          political_expenditures_amt NUMERIC,
          expended527_activities_amt NUMERIC,
          lobbying_excess_amt NUMERIC,
          lobbying_grassroots_excess_amt NUMERIC,
          grants_other_organizations_amt NUMERIC,
          non_deductible_lbbyng_pltcl_cy_amt NUMERIC,
          form1120_pol_filed_ind TEXT
        );

        CREATE TABLE irs990_schedule_l_disqualified_person_ex_bnft_tr_grp (
          filing_id TEXT,
          person_nm TEXT,
          transaction_corrected_ind TEXT
        );
        CREATE TABLE irs990_schedule_l_bus_tr_involve_interested_prsn_grp (
          filing_id TEXT,
          person_nm TEXT,
          transaction_amt NUMERIC
        );
        CREATE TABLE irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp (
          filing_id TEXT,
          person_nm TEXT,
          cash_grant_amt NUMERIC
        );
        CREATE TABLE irs990_schedule_l_loans_btwn_org_interested_prsn_grp (
          filing_id TEXT,
          person_nm TEXT,
          balance_due_amt NUMERIC
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
            ("F2", "Jane Director", "Trustee", 2, 0, 0, 0),
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
    conn.executemany(
        "INSERT INTO irs990_contractor_compensation_grp VALUES (?,?,?)",
        [("F1", "Major Vendor", ""), ("F2", "Major Vendor", "")],
    )
    conn.execute(
        "INSERT INTO sched_r_related_orgs_expanded VALUES (?,?,?,?,?,?,?,?)",
        ("F1", "Related Tax-Exempt Org", "222222222", "Risky Foundation", "", "X", 0, ""),
    )
    conn.executemany(
        "INSERT INTO grant_recipient_resolved VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "F1", "111111111", "Risky Org", 2024, "222222222", "Risky Foundation", 200000, "222222222", "Risky Foundation", "resolved", 0.99, ""),
            (2, "F2", "222222222", "Risky Foundation", 2024, "111111111", "Risky Org", 150000, "111111111", "Risky Org", "resolved", 0.99, ""),
            (3, "F1", "111111111", "Risky Org", 2024, "", "Unknown", 50000, "", "", "unresolved", 0.20, "reported_ein_blank"),
        ],
    )
    conn.executemany(
        "INSERT INTO org_identity VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "bmf:111", "111111111", "bmf_name", "eo1.csv", 20, "RISKY ORG", "1 Main St", "1 MAIN ST", "Portland", "OR", "97201", "03", "15", "1", "P20", "01", "202412", 200, 1000, 1000, '{"affiliation":"3","filing_req_cd":"01"}'),
            (2, "return:111", "111111111", "returns_org_name", "returns.org_name", 10, "Risky Org", "1 Main St", "1 MAIN ST", "Portland", "OR", "97201", "", "", "", "", "", "", None, None, None, "{}"),
            (3, "return:222", "222222222", "returns_org_name", "returns.org_name", 10, "Risky Foundation", "1 Main St", "1 MAIN ST", "Portland", "OR", "97201", "", "", "", "", "", "", None, None, None, "{}"),
        ],
    )
    conn.execute(
        "INSERT INTO irs990_schedule_c_root VALUES (?,?,?,?,?,?,?,?)",
        ("F1", 250, 100, 50, 0, 75, 20, "X"),
    )
    conn.execute(
        "INSERT INTO irs990_schedule_l_disqualified_person_ex_bnft_tr_grp VALUES (?,?,?)",
        ("F1", "Inside Person", ""),
    )
    conn.execute(
        "INSERT INTO irs990_schedule_l_bus_tr_involve_interested_prsn_grp VALUES (?,?,?)",
        ("F1", "Inside Vendor", 1000),
    )
    conn.execute(
        "INSERT INTO irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp VALUES (?,?,?)",
        ("F1", "Inside Grantee", 750),
    )
    conn.execute(
        "INSERT INTO irs990_schedule_l_loans_btwn_org_interested_prsn_grp VALUES (?,?,?)",
        ("F1", "Inside Borrower", 2000),
    )
    conn.commit()
    return conn


class FraudRiskDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "fixture.db")
        self.conn = build_fixture_db(self.db_path)
        self.orig_connect = mod.connect_ro
        self.orig_deep_connect = deep.connect_ro
        self.orig_core_run = ngo_core_data.run
        self.orig_external = mod.fetch_external_checks
        self.orig_lookup_irs = mod.lookup_irs_status
        self.orig_lookup_names = mod.lookup_name_candidates
        self.orig_network_available = mod.risk_network_available
        self.orig_network_for_ein = mod.network_for_ein
        self.orig_network_path = mod.risk_network_path
        self.open_connections = []

        def open_fixture():
            conn = sqlite3.connect(self.db_path)
            self.open_connections.append(conn)
            return conn

        mod.connect_ro = open_fixture
        deep.connect_ro = open_fixture
        mod.fetch_external_checks = lambda *args, **kwargs: {
            "fetched_at": "",
            "fac": {"status": "not_configured", "reports": [], "ueis": []},
            "usaspending": {"status": "blocked", "reason": "local_mode"},
            "sam": {"status": "blocked", "reason": "local_mode"},
            "fec": {"status": "blocked", "reason": "local_mode"},
            "lda": {"status": "blocked", "reason": "local_mode"},
        }
        mod.lookup_irs_status = lambda *args, **kwargs: {
            "available": False, "results": [], "coverage": [], "error": "fixture has no screening sidecar"
        }
        mod.lookup_name_candidates = lambda *args, **kwargs: {
            "available": False, "results": [], "coverage": [], "error": "fixture has no screening sidecar"
        }
        mod.risk_network_available = lambda *args, **kwargs: False
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
        mod.fetch_external_checks = self.orig_external
        mod.lookup_irs_status = self.orig_lookup_irs
        mod.lookup_name_candidates = self.orig_lookup_names
        mod.risk_network_available = self.orig_network_available
        mod.network_for_ein = self.orig_network_for_ein
        mod.risk_network_path = self.orig_network_path
        for conn in self.open_connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self.conn.close()
        self.temp_dir.cleanup()

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
        self.assertIn("Review Priority Score", html)
        self.assertIn("not a statistically validated model", html)
        self.assertIn("Operating deficit", html)
        self.assertIn("Political campaign activity flag", html)
        self.assertIn("Most grant dollars lack recipient EINs", html)
        self.assertIn("Schedule C political or section 527 expenditures", html)
        self.assertIn("Schedule L excess-benefit transaction", html)
        self.assertIn("IRS EO BMF Status Snapshot", html)
        self.assertIn("Unconditional exemption", html)
        self.assertIn("Relationship Network", html)
        self.assertIn("Reciprocal grant relationship", html)
        self.assertIn("Shared officer", html)
        self.assertIn("Shared address", html)
        self.assertIn("Screening result, not a fraud probability or determination", html)
        self.assertIn("Data Coverage &amp; Remaining Work", html)
        self.assertIn("Build public screening snapshots", html)
        self.assertIn("Print / Save PDF", mod.render_pdf_export({"ein": "11-1111111"}))

    def test_score_groups_repeated_signals_and_excludes_data_quality(self):
        signal = mod._indicator("Medium", "Financial", "Repeated issue", 2024, "e", "w", "n")
        repeated = [dict(signal, tax_year=year) for year in range(2018, 2025)]
        score = mod._risk_score(repeated)
        self.assertLess(score, 40)
        with_quality = repeated + [
            mod._indicator("High", "Data Quality", "Missing rows", 2024, "e", "w", "n")
        ]
        self.assertEqual(mod._risk_score(with_quality), score)
        with_external_leads = repeated + [
            mod._indicator("High", "External Lead", "Candidate only", 2024, "e", "w", "n"),
            mod._indicator("High", "External Coverage", "Coverage only", 2024, "e", "w", "n"),
        ]
        self.assertEqual(mod._risk_score(with_external_leads), score)
        with_disclosures = repeated + [
            mod._indicator("High", "Disclosure Context", "Lawful disclosure", 2024, "e", "w", "n")
        ]
        self.assertEqual(mod._risk_score(with_disclosures), score)

    def test_private_foundation_grantmaking_and_zero_staff_are_not_risk_signals(self):
        row = {
            "tax_year": 2024,
            "return_type": "990-PF",
            "total_expenses": 1_000_000,
            "grants_paid": 900_000,
            "employees_count": 0,
        }
        titles = {item["title"] for item in mod._financial_indicators([row])}
        self.assertNotIn("Grants dominate expenses", titles)
        self.assertNotIn("High expenses with zero employees", titles)

        bmf_private_foundation = {
            "matched": True,
            "subsection": "03",
            "foundation": "04",
        }
        titles = {
            item["title"]
            for item in mod._financial_indicators(
                [dict(row, return_type="")], bmf_private_foundation
            )
        }
        self.assertNotIn("Grants dominate expenses", titles)
        self.assertNotIn("High expenses with zero employees", titles)

        historical_public_charity = {
            item["title"]
            for item in mod._financial_indicators(
                [dict(row, return_type="990")], bmf_private_foundation
            )
        }
        self.assertIn("Grants dominate expenses", historical_public_charity)
        self.assertIn("High expenses with zero employees", historical_public_charity)

        public_charity_titles = {
            item["title"]
            for item in mod._financial_indicators(
                [dict(row, return_type="990")],
                {"matched": True, "subsection": "03", "foundation": "15"},
            )
        }
        self.assertIn("Grants dominate expenses", public_charity_titles)
        self.assertIn("High expenses with zero employees", public_charity_titles)

    def test_missing_employee_count_is_not_treated_as_reported_zero(self):
        base = {
            "tax_year": 2024,
            "return_type": "990",
            "total_expenses": 600_000,
            "grants_paid": 0,
        }
        for missing in (None, "", "   ", "not reported", float("nan")):
            with self.subTest(missing=missing):
                titles = {
                    item["title"]
                    for item in mod._financial_indicators(
                        [dict(base, employees_count=missing)]
                    )
                }
                self.assertNotIn("High expenses with zero employees", titles)

        for reported_zero in (0, 0.0, "0"):
            with self.subTest(reported_zero=reported_zero):
                titles = {
                    item["title"]
                    for item in mod._financial_indicators(
                        [dict(base, employees_count=reported_zero)]
                    )
                }
                self.assertIn("High expenses with zero employees", titles)

    def test_political_activity_uses_current_bmf_subsection_context(self):
        row = {
            "tax_year": 2024,
            "return_type": "990",
            "political_campaign_activity_ind": "Yes",
        }

        def signal(bmf):
            return next(
                item
                for item in mod._financial_indicators([row], bmf)
                if item["title"] == "Political campaign activity flag"
            )

        c3 = signal({"matched": True, "subsection": "03"})
        other = signal({"matched": True, "subsection": "04"})
        unknown = signal({"available": False, "matched": False})
        self.assertEqual(c3["severity"], "High")
        self.assertIn("prohibited", c3["why"])
        self.assertEqual(other["severity"], "Low")
        self.assertIn("can be lawful", other["why"])
        self.assertEqual(unknown["severity"], "Medium")
        self.assertIn("unresolved", unknown["why"])

        row["tax_exempt_status"] = "501(c)(4)"
        historical_other = signal({"matched": True, "subsection": "03"})
        self.assertEqual(historical_other["severity"], "Low")
        self.assertIn("filing-year Form 990", historical_other["why"])

    def test_schedule_c_political_spending_uses_subsection_and_materiality(self):
        def political_signal(expenses, bmf, tax_exempt_status=""):
            indicators = mod._schedule_c_indicators(
                self.conn,
                [{
                    "filing_id": "F1",
                    "tax_year": 2024,
                    "total_expenses": expenses,
                    "tax_exempt_status": tax_exempt_status,
                }],
                bmf,
            )
            return next(
                item
                for item in indicators
                if item["title"] == "Schedule C political or section 527 expenditures"
            )

        c3 = political_signal(100_000, {"matched": True, "subsection": "03"})
        other_immaterial = political_signal(
            100_000, {"matched": True, "subsection": "04"}
        )
        other_material = political_signal(
            5_000, {"matched": True, "subsection": "04"}
        )
        unknown_immaterial = political_signal(
            100_000, {"available": False, "matched": False}
        )
        self.assertEqual(c3["severity"], "High")
        self.assertIn("501(c)(3)", c3["why"])
        self.assertEqual(other_immaterial["severity"], "Low")
        self.assertIn("did not meet", other_immaterial["evidence"])
        self.assertEqual(other_material["severity"], "Medium")
        self.assertIn("met the dashboard materiality screen", other_material["evidence"])
        self.assertEqual(unknown_immaterial["severity"], "Low")
        self.assertIn("subsection is unavailable", unknown_immaterial["why"])
        historical_other = political_signal(
            100_000, {"matched": True, "subsection": "03"}, "501(c)(4)"
        )
        self.assertEqual(historical_other["severity"], "Low")
        self.assertIn("filing-year Form 990", historical_other["why"])

    def test_trend_detects_large_decrease_but_excludes_short_tax_period(self):
        full_years = [
            {
                "tax_year": 2024,
                "period_start": "2024-01-01",
                "period_end": "2024-12-31",
                "total_revenue": 250,
                "total_expenses": 250,
                "grants_paid": 250,
            },
            {
                "tax_year": 2023,
                "period_start": "2023-01-01",
                "period_end": "2023-12-31",
                "total_revenue": 1000,
                "total_expenses": 1000,
                "grants_paid": 1000,
            },
        ]
        titles = {item["title"] for item in mod._financial_indicators(full_years)}
        self.assertIn("Large year-over-year revenue change", titles)
        self.assertIn("Large year-over-year expenses change", titles)
        self.assertIn("Large year-over-year grants paid change", titles)

        short_years = [dict(full_years[0], period_start="2024-10-01"), full_years[1]]
        indicators = mod._financial_indicators(short_years)
        short_titles = {item["title"] for item in indicators}
        self.assertIn("Short tax period excluded from annual trend scoring", short_titles)
        self.assertNotIn("Large year-over-year revenue change", short_titles)

    def test_local_analysis_passes_bmf_context_to_financial_and_schedule_c_rules(self):
        local = mod._build_local_analysis(mod._core_years("111111111"))
        contextual = {
            item["title"]: item
            for item in local["indicators"]
            if item["title"] in {
                "Political campaign activity flag",
                "Schedule C political or section 527 expenditures",
            }
        }
        self.assertEqual(contextual["Political campaign activity flag"]["severity"], "High")
        self.assertEqual(
            contextual["Schedule C political or section 527 expenditures"]["severity"],
            "High",
        )

    def test_live_external_audit_details_are_scored_rendered_and_escaped(self):
        mod.fetch_external_checks = lambda *args, **kwargs: {
            "fetched_at": "2026-08-14T12:00:00Z",
            "fac": {
                "status": "ok",
                "reports": [{
                    "report_id": "FAC-1",
                    "ein_match": "primary_ein",
                    "general": {
                        "report_id": "FAC-1",
                        "audit_year": "2025",
                        "fy_start_date": "2024-10-01",
                        "fy_end_date": "2025-09-30",
                        "audit_type": "Single audit",
                        "total_amount_expended": 1_250_000,
                        "is_low_risk_auditee": False,
                        "is_internal_control_material_weakness_disclosed": True,
                    },
                    "findings": [{
                        "reference_number": "2025-001",
                        "type_requirement": "Allowable costs",
                        "is_material_weakness": True,
                        "is_questioned_costs": True,
                    }],
                    "findings_text": [{
                        "finding_ref_number": "2025-001",
                        "finding_text": "Unsupported <script>alert(1)</script> charge",
                    }],
                    "corrective_action_plans": [{
                        "finding_ref_number": "2025-001",
                        "planned_action": "Add second-level review",
                    }],
                    "federal_awards": [{
                        "federal_agency_prefix": "93",
                        "federal_award_extension": "778",
                        "federal_program_name": "Medical Assistance Program",
                        "amount_expended": 1_100_000,
                        "is_major": True,
                        "audit_report_type": "Qualified",
                        "findings_count": 1,
                    }],
                }],
                "ueis": ["ABCDEF123456"],
            },
            "usaspending": {"status": "ok", "matches": [{"name": "Risky Org", "uei": "ABCDEF123456", "recipient_level": "P", "amount": 99}]},
            "sam": {"status": "ok", "entities": [{}], "exclusions": [{"ueiSAM": "ABCDEF123456"}], "queried_ueis": ["ABCDEF123456"]},
            "fec": {"status": "ok", "candidates": [{"name": "Risky Org PAC", "committee_id": "C001", "state": "OR"}]},
            "lda": {"status": "ok", "clients": [{"name": "Risky Org", "state": "OR", "match_strength": "exact", "filings": [{"filing_year": 2025}]}]},
        }

        headers, rows = mod.run({"ein": "111111111", "external_mode": "live"})
        html = mod.render_results({"ein": "111111111", "external_mode": "live"}, headers, rows)
        self.assertIn("Federal Audit &amp; Public-Record Checks", html)
        self.assertIn("Federal Single Audit issues reported", html)
        self.assertIn("$1,000,000 threshold", html)
        self.assertIn("Medical Assistance Program", html)
        self.assertIn("Add second-level review", html)
        self.assertIn("Unsupported &lt;script&gt;alert(1)&lt;/script&gt; charge", html)
        self.assertNotIn("Unsupported <script>", html)
        self.assertIn("SAM exclusion record returned", html)
        self.assertIn("candidate-only", html.casefold())

    def test_fac_absence_with_large_990_grants_is_unscored_coverage_only(self):
        ngo_core_data.run = lambda form: (
            ngo_core_data.HEADERS,
            [_core_row(period_start="2024-01-01", government_grants=800_000)],
        )
        mod.fetch_external_checks = lambda *args, **kwargs: {
            "fetched_at": "2026-08-14T12:00:00Z",
            "fac": {"status": "no_match", "reports": [], "ueis": []},
            "usaspending": {"status": "blocked", "reason": "requires_fac_uei"},
            "sam": {"status": "blocked", "reason": "requires_fac_uei"},
            "fec": {"status": "no_match", "candidates": []},
            "lda": {"status": "no_match", "clients": []},
        }
        report = mod._build_report({"ein": "111111111", "external_mode": "live"})
        coverage = [item for item in report["indicators"] if item["category"] == "External Coverage"]
        self.assertEqual(len(coverage), 1)
        self.assertIn("not the same measure", coverage[0]["why"])
        scored_only = [item for item in report["indicators"] if item["category"] != "External Coverage"]
        self.assertEqual(report["risk_score"], mod._risk_score(scored_only))
        self.assertEqual(mod._single_audit_threshold("2024-09-30"), 750_000)
        self.assertEqual(mod._single_audit_threshold("2024-10-01"), 1_000_000)

    def test_sam_partial_no_match_discloses_omitted_coverage(self):
        sam = {
            "status": "no_match",
            "coverage_status": "partial",
            "partial": True,
            "truncated": True,
            "queried_ueis": ["NEWEST123456"],
            "omitted_ueis": ["OLDERX123456"],
            "coverage": {
                "status": "partial",
                "partial": True,
                "queried_ueis": ["NEWEST123456"],
                "omitted_ueis": ["OLDERX123456"],
                "truncation_reasons": ["uei_limit", "exclusion_pages_omitted"],
                "exclusion_queries": [{
                    "uei": "NEWEST123456",
                    "pages_omitted": 2,
                }],
            },
            "quota": {"requests_used": 3, "request_budget": 3},
        }

        cards = mod._external_source_cards({"sam": sam})
        details = mod._external_candidate_sections({"sam": sam})

        self.assertIn("Partial SAM check; no match within the queried coverage", cards)
        self.assertNotIn("Checked; no exact or vetted match", cards)
        self.assertIn("Partial SAM coverage", details)
        self.assertIn("Queried UEIs: NEWEST123456", details)
        self.assertIn("omitted UEIs: OLDERX123456", details)
        self.assertIn("exclusion pages omitted: NEWEST123456: 2", details)
        self.assertIn("requests: 3 of 3 budgeted", details)
        self.assertIn("this was not a complete SAM check", details)

    def test_sam_partial_match_discloses_page_and_request_limits(self):
        sam = {
            "status": "ok",
            "entities": [{
                "uei": "ABCDEF123456",
                "entity_registration": {"legalBusinessName": "Fixture Charity"},
            }],
            "exclusions": [],
            "queried_ueis": ["ABCDEF123456"],
            "coverage": {
                "status": "partial",
                "truncated": True,
                "queried_ueis": ["ABCDEF123456"],
                "omitted_ueis": [],
                "truncation_reasons": ["exclusion_pages_omitted"],
                "exclusion_queries": [{
                    "uei": "ABCDEF123456",
                    "pages_omitted": "unknown",
                }],
            },
            "quota": {"requests_used": 2, "request_budget": 3},
        }

        cards = mod._external_source_cards({"sam": sam})
        details = mod._external_candidate_sections({"sam": sam})

        self.assertIn("partial coverage", cards)
        self.assertIn("Partial SAM coverage", details)
        self.assertIn("omitted UEIs: none", details)
        self.assertIn("exclusion pages omitted: ABCDEF123456: unknown", details)
        self.assertIn("requests: 2 of 3 budgeted", details)
        self.assertNotIn("not a complete SAM check", details)

    def test_sam_failed_partial_request_remains_unavailable(self):
        cards = mod._external_source_cards({
            "sam": {
                "status": "error",
                "error": "request_failed",
                "partial": True,
                "coverage_status": "partial",
            },
        })

        self.assertIn("source-error", cards)
        self.assertIn("Unavailable", cards)
        self.assertNotIn("source-partial", cards)

    def test_untrusted_grants_unrelated_partnerships_and_reversed_time_do_not_score(self):
        self.conn.execute("UPDATE grant_recipient_resolved SET tax_year = 2019 WHERE grant_id = 2")
        self.conn.execute(
            "INSERT INTO grant_recipient_resolved VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (4, "F1", "111111111", "Risky Org", 2024, "555555555", "Untrusted Recipient", 900000, "555555555", "Untrusted Recipient", "unresolved", 0.2, "conflict"),
        )
        self.conn.execute(
            "INSERT INTO sched_r_related_orgs_expanded VALUES (?,?,?,?,?,?,?,?)",
            ("F1", "Unrelated Taxable Partnership", "444444444", "Unrelated Partnership", "", "", 0, ""),
        )
        self.conn.commit()

        years = mod._core_years("111111111")
        local = mod._build_local_analysis(years)
        connections = {item.get("ein"): item for item in local["network"]["connections"]}
        self.assertNotIn("555555555", connections)
        self.assertIn("Schedule R (unrelated partnership)", connections["444444444"]["relationships"])
        self.assertNotIn("Schedule R", connections["444444444"]["relationships"])
        titles = {item["title"] for item in local["indicators"]}
        self.assertNotIn("Reciprocal grant relationship", titles)
        self.assertNotIn("Chronologically plausible two-step grant return", titles)
        reversed_paths = [p for p in local["network"]["paths"] if p.get("returns_to_subject")]
        self.assertTrue(reversed_paths)
        self.assertFalse(reversed_paths[0]["chronology_supported"])

    def test_inflated_grant_detail_is_unscored_and_excluded_from_outgoing_network(self):
        self.conn.execute(
            "UPDATE grants_compat_v1 SET cash_amount = cash_amount * 10 WHERE filing_id = 'F1'"
        )
        self.conn.execute(
            """INSERT INTO grants_compat_v1
               SELECT filing_id, recipient_ein, recipient_name, cash_amount, noncash_amount
               FROM grants_compat_v1 WHERE filing_id = 'F1'"""
        )
        self.conn.commit()
        years = [{
            "ein": "111111111",
            "filing_id": "F1",
            "tax_year": 2024,
            "grants_paid": 40_000,
        }]

        indicators = mod._grant_indicators(self.conn, years)
        mismatch = next(
            item for item in indicators
            if item["title"] == "Grant detail does not reconcile to return total"
        )
        self.assertEqual(mismatch["category"], "Data Quality")
        self.assertIn("excluded from scored outgoing network", mismatch["evidence"])

        network = mod._build_network(self.conn, "111111111", years)
        self.assertEqual(network["metrics"]["grant_years_excluded_data_quality"], 1)
        self.assertEqual(network["metrics"]["grants_paid"], 0)
        paid = [
            item for item in network["connections"]
            if "Grant paid" in item.get("relationships", set())
        ]
        self.assertFalse(paid)
        titles = {item["title"] for item in mod._network_indicators(network, 2024)}
        self.assertNotIn("Reciprocal grant relationship", titles)
        self.assertNotIn("Chronologically plausible two-step grant return", titles)

    def test_discontiguous_grant_years_use_only_actual_near_time_amounts(self):
        self.conn.execute("DELETE FROM grant_recipient_resolved")
        self.conn.executemany(
            "INSERT INTO grant_recipient_resolved VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (10, "F1", "111111111", "Risky Org", 2000, "222222222", "Risky Foundation", 80_000, "222222222", "Risky Foundation", "resolved", 0.99, ""),
                (11, "F1", "111111111", "Risky Org", 2024, "222222222", "Risky Foundation", 120_000, "222222222", "Risky Foundation", "resolved", 0.99, ""),
                (12, "F2", "222222222", "Risky Foundation", 2012, "111111111", "Risky Org", 900_000, "111111111", "Risky Org", "resolved", 0.99, ""),
            ],
        )

        years = mod._core_years("111111111")
        network = mod._build_network(self.conn, "111111111", years)
        connection = next(item for item in network["connections"] if item.get("ein") == "222222222")
        self.assertEqual(
            connection["year_amounts_by_type"]["Grant paid"],
            {2000: 80_000.0, 2024: 120_000.0},
        )
        self.assertEqual(
            connection["year_amounts_by_type"]["Grant received"],
            {2012: 900_000.0},
        )
        titles = {item["title"] for item in mod._network_indicators(network, 2024)}
        self.assertNotIn("Reciprocal grant relationship", titles)
        self.assertNotIn("Chronologically plausible two-step grant return", titles)
        return_path = next(path for path in network["paths"] if path.get("returns_to_subject"))
        self.assertFalse(return_path["chronology_supported"])

        self.conn.execute(
            "INSERT INTO grant_recipient_resolved VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (13, "F2", "222222222", "Risky Foundation", 2025, "111111111", "Risky Org", 110_000, "111111111", "Risky Org", "resolved", 0.99, ""),
        )
        network = mod._build_network(self.conn, "111111111", years)
        indicators = mod._network_indicators(network, 2024)
        reciprocal = next(item for item in indicators if item["title"] == "Reciprocal grant relationship")
        self.assertIn("paid $120,000 in 2024", reciprocal["evidence"])
        self.assertIn("received $110,000 in 2025", reciprocal["evidence"])
        self.assertNotIn("$1,010,000", reciprocal["evidence"])
        return_path = next(path for path in network["paths"] if path.get("returns_to_subject"))
        self.assertTrue(return_path["chronology_supported"])
        self.assertEqual(return_path["qualifying_first_years"], [2024])
        self.assertEqual(return_path["qualifying_second_years"], [2025])
        self.assertEqual(return_path["amount"], 110_000.0)
        self.assertEqual(return_path["rows"], 1)

    def test_reviewed_enhanced_grant_layer_drives_dashboard_network(self):
        self.conn.executescript(
            """
            CREATE VIEW grant_recipient_resolved_plus_ai_v1 AS
            SELECT rr.*,
              CASE WHEN grant_id=3 THEN '333333333' ELSE resolved_ein END AS final_resolved_ein,
              CASE WHEN grant_id=3 THEN 'Reviewed Recipient' ELSE resolved_org_name END AS final_resolved_org_name,
              CASE WHEN grant_id=3 THEN 0.99 ELSE confidence END AS final_confidence,
              CASE WHEN grant_id=3 THEN 'ai_assisted' ELSE 'deterministic' END AS final_match_source
            FROM grant_recipient_resolved rr;
            """
        )
        self.conn.commit()

        years = mod._core_years("111111111")
        local = mod._build_local_analysis(years)
        connection = next(
            item for item in local["network"]["connections"]
            if item.get("ein") == "333333333"
        )
        self.assertIn("Grant paid", connection["relationships"])
        self.assertIn("Reviewed Recipient", connection["name"])
        unresolved = [
            item for item in local["indicators"]
            if item["title"] == "Most grant dollars remain unresolved to a recipient EIN"
        ]
        self.assertFalse(unresolved)

    def test_incoming_reviewed_grants_use_indexable_applied_and_deterministic_branches(self):
        self.conn.executescript(
            """
            CREATE TABLE grant_recipient_ai_applied (
              grant_id INTEGER PRIMARY KEY,
              selected_ein TEXT,
              ai_confidence NUMERIC,
              model TEXT
            );
            CREATE INDEX idx_test_applied_ein ON grant_recipient_ai_applied(selected_ein);
            CREATE INDEX idx_test_resolved_ein ON grant_recipient_resolved(resolved_ein);
            UPDATE grant_recipient_resolved
              SET resolved_ein='999999999', resolved_org_name='Other Recipient'
              WHERE grant_id=2;
            INSERT INTO grant_recipient_ai_applied VALUES
              (2, '111111111', 0.99, 'reviewed:model');
            INSERT INTO grant_recipient_resolved VALUES
              (14, 'F2', '222222222', 'Risky Foundation', 2024,
               '111111111', 'Risky Org', 700000, '111111111', 'Risky Org',
               'resolved', 0.99, '');
            INSERT INTO grant_recipient_ai_applied VALUES
              (14, '111111111', 0.99, 'rule:reported_ein_from_filing_unverified');
            """
        )
        self.conn.commit()

        source = mod._indexed_incoming_grant_rows_sql(self.conn, "111111111")
        self.assertIsNotNone(source)
        sql, params = source
        self.assertNotIn("final_resolved_ein", sql)
        self.assertIn("grant_recipient_ai_applied", sql)
        rows = list(self.conn.execute(sql, params))
        self.assertEqual([(row[0], row[3]) for row in rows], [("222222222", 150000)])
        plan = " ".join(
            _row[3] for _row in self.conn.execute("EXPLAIN QUERY PLAN " + sql, params)
        )
        self.assertIn("idx_test_applied_ein", plan)
        self.assertIn("idx_test_resolved_ein", plan)

        years = mod._core_years("111111111")
        network = mod._build_network(self.conn, "111111111", years)
        other = next(item for item in network["connections"] if item.get("ein") == "222222222")
        self.assertEqual(other["amount_by_type"]["Grant received"], 150000)

    def test_name_only_schedule_r_does_not_score_contractor_overlap(self):
        connections = {}
        mod._add_connection(
            connections,
            subject_ein="111111111",
            name="Same Name LLC",
            relationship="Contractor",
            evidence="Payment",
            amount=100000,
        )
        mod._add_connection(
            connections,
            subject_ein="111111111",
            name="Same Name LLC",
            relationship="Schedule R",
            evidence="Name-only related organization row",
            scored_relationship=False,
        )
        item = next(iter(connections.values()))
        self.assertEqual(item["relationships"], {"Contractor", "Schedule R"})
        self.assertEqual(item["scored_relationships"], {"Contractor"})
        titles = {
            indicator["title"]
            for indicator in mod._network_indicators(
                {"connections": [item], "paths": []}, 2024
            )
        }
        self.assertNotIn("Financial flow overlaps an identity or control link", titles)

    def test_governance_xml_extracts_controls_and_scores_direct_disclosures(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <Return xmlns="http://www.irs.gov/efile">
          <ReturnData><IRS990>
            <VotingMembersGoverningBodyCnt>5</VotingMembersGoverningBodyCnt>
            <VotingMembersIndependentCnt>1</VotingMembersIndependentCnt>
            <FamilyOrBusinessRlnInd>true</FamilyOrBusinessRlnInd>
            <MaterialDiversionOrMisuseInd>true</MaterialDiversionOrMisuseInd>
            <MinutesOfGoverningBodyInd>false</MinutesOfGoverningBodyInd>
            <ConflictOfInterestPolicyInd>false</ConflictOfInterestPolicyInd>
            <WhistleblowerPolicyInd>false</WhistleblowerPolicyInd>
            <DocumentRetentionPolicyInd>true</DocumentRetentionPolicyInd>
            <Form990ProvidedToGvrnBodyInd>false</Form990ProvidedToGvrnBodyInd>
            <FSAuditedInd>false</FSAuditedInd>
            <FederalGrantAuditRequiredInd>true</FederalGrantAuditRequiredInd>
            <FederalGrantAuditPerformedInd>false</FederalGrantAuditPerformedInd>
          </IRS990></ReturnData>
        </Return>"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fixture.xml"
            path.write_text(xml, encoding="utf-8")
            values = mod._extract_governance_xml(path)

        self.assertEqual(values["voting_members"], 5)
        self.assertEqual(values["independent_members"], 1)
        self.assertIs(values["material_diversion"], True)
        self.assertIs(values["federal_grant_audit_performed"], False)
        governance = {"available": True, "records": [{"tax_year": 2024, **values}]}
        indicators = mod._governance_indicators(
            governance,
            [dict(zip(ngo_core_data.HEADERS, _core_row(total_revenue=6_000_000)))],
        )
        titles = {item["title"] for item in indicators}
        self.assertIn("Material diversion or misuse reported", titles)
        self.assertIn("Required federal grant audit reported as not performed", titles)
        self.assertIn("Independent members are a minority", titles)
        self.assertIn("Governance policies reported absent", titles)

    def test_governance_xml_loads_available_filings_and_reports_partial_coverage(self):
        years = [
            {"tax_year": 2024, "filing_id": "F1_public"},
            {"tax_year": 2023, "filing_id": "F2_public"},
            {"tax_year": 2022, "filing_id": "F3_public"},
            {"tax_year": 2021, "filing_id": "F4_public"},
            {"tax_year": 2020, "filing_id": "F5_public"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xml_dir = root / "xml"
            xml_dir.mkdir()
            (xml_dir / "F1_public.xml").write_text(
                "<Return><ReturnData><IRS990>"
                "<VotingMembersGoverningBodyCnt>7</VotingMembersGoverningBodyCnt>"
                "<ConflictOfInterestPolicyInd>true</ConflictOfInterestPolicyInd>"
                "</IRS990></ReturnData></Return>",
                encoding="utf-8",
            )
            (xml_dir / "F3_public.xml").write_text(
                "<Return><ReturnData><IRS990><FSAuditedInd>true</FSAuditedInd>"
                "</IRS990></ReturnData></Return>",
                encoding="utf-8",
            )
            (xml_dir / "F4_public.xml").write_text("<Return><broken>", encoding="utf-8")
            (xml_dir / "F5_public.xml").write_text(
                "<Return><ReturnData><IRS990 /></ReturnData></Return>", encoding="utf-8"
            )

            inventory_path = root / "sources.db"
            inventory = sqlite3.connect(inventory_path)
            inventory.executescript(
                """
                CREATE TABLE source_files (
                  filing_id TEXT,
                  object_id TEXT,
                  relative_path TEXT,
                  source_file TEXT,
                  duplicate_status TEXT,
                  keep_source_file TEXT,
                  quarantine_status TEXT
                );
                CREATE INDEX idx_source_object ON source_files(object_id);
                """
            )
            inventory.executemany(
                "INSERT INTO source_files VALUES (?,?,?,?,?,?,?)",
                [
                    ("F1_public", "F1", "xml/F1_public.xml", "xml/F1_public.xml", "unique", None, None),
                    ("F3_public", "F3", "xml/F3_public.xml", "xml/F3_public.xml", "object_id_conflict", None, "quarantined"),
                    ("F4_public", "F4", "xml/F4_public.xml", "xml/F4_public.xml", "unique", None, None),
                    ("F5_public", "F5", "xml/F5_public.xml", "xml/F5_public.xml", "unique", None, None),
                ],
            )
            inventory.commit()
            inventory.close()

            with (
                patch.object(deep, "SOURCE_INVENTORY_DB_PATH", inventory_path),
                patch.dict("os.environ", {"IRS_XML_ROOT": str(root)}),
                patch.object(deep, "filing_xml_paths", side_effect=AssertionError("all-or-nothing resolver used")),
            ):
                governance = mod._load_governance_xml("111111111", years)

            inventory = sqlite3.connect(inventory_path)
            try:
                self.assertEqual(inventory.execute("SELECT COUNT(*) FROM source_files").fetchone()[0], 4)
            finally:
                inventory.close()

        self.assertTrue(governance["available"])
        self.assertEqual(governance["requested"], 5)
        self.assertEqual(governance["missing"], 1)
        self.assertEqual(governance["quarantined"], 1)
        self.assertEqual(governance["parse_errors"], 1)
        self.assertEqual(governance["empty"], 1)
        self.assertEqual(governance["coverage"]["loaded"], 1)
        self.assertEqual([row["filing_id"] for row in governance["records"]], ["F1_public"])
        self.assertEqual(governance["records"][0]["voting_members"], 7)

        coverage_indicators = [
            item for item in mod._governance_indicators(governance, years)
            if item["title"] == "Governance XML coverage is incomplete"
        ]
        self.assertEqual(len(coverage_indicators), 1)
        self.assertEqual(coverage_indicators[0]["category"], "Data Quality")
        self.assertIn("1 missing, 1 quarantined, 1 parse error", coverage_indicators[0]["evidence"])

        panel = mod._governance_panel(governance)
        self.assertIn("XML coverage:", panel)
        self.assertIn("1 of 5 requested canonical filing(s)", panel)
        self.assertIn("1 missing source", panel)
        self.assertIn("1 quarantined source", panel)
        self.assertIn("1 parse error", panel)
        self.assertIn("1 filing with no supported fields", panel)

    def test_governance_xml_missing_inventory_reports_every_requested_filing(self):
        years = [
            {"tax_year": 2024, "filing_id": "F1_public"},
            {"tax_year": 2023, "filing_id": "F2_public"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_inventory = Path(temp_dir) / "missing.db"
            with patch.object(deep, "SOURCE_INVENTORY_DB_PATH", missing_inventory):
                governance = mod._load_governance_xml("111111111", years)

        self.assertFalse(governance["available"])
        self.assertEqual(governance["reason"], "xml_inventory_unavailable")
        self.assertEqual(governance["requested"], 2)
        self.assertEqual(governance["missing"], 2)
        indicators = mod._governance_indicators(governance, years)
        self.assertEqual(
            [item["title"] for item in indicators],
            ["Governance XML coverage is incomplete"],
        )
        self.assertEqual(indicators[0]["severity"], "Medium")

    def test_public_screening_exact_ein_and_candidate_leads_are_rendered_safely(self):
        coverage = [{
            "dataset_key": "irs_pub78",
            "title": "Publication 78 eligible organizations",
            "source_date": "2026-08-01",
            "retrieved_at": "2026-08-14T12:00:00Z",
            "record_count": 100,
        }]
        mod.lookup_irs_status = lambda *args, **kwargs: {
            "available": True,
            "coverage": coverage,
            "results": [
                {
                    "dataset_key": "irs_pub78",
                    "primary_name": "Risky Org",
                    "status": "eligible_for_deductible_contributions",
                    "deductibility_code": "1",
                },
                {
                    "dataset_key": "irs_auto_revocation",
                    "primary_name": "Risky Org",
                    "status": "automatically_revoked",
                    "status_date": "2025-01-15",
                    "reinstatement_date": "",
                },
            ],
        }

        def name_lookup(name, **kwargs):
            result = {
                "available": True,
                "coverage": [{
                    "dataset_key": "ofac_sdn",
                    "title": "OFAC SDN",
                    "source_date": "2026-08-14",
                    "retrieved_at": "2026-08-14T12:00:00Z",
                    "record_count": 10,
                }],
                "query": {"name": name},
                "results": [],
            }
            if name == "Risky Org":
                result["results"] = [{
                    "dataset_key": "ofac_sdn",
                    "matched_name": "Risky Org <script>",
                    "primary_name": "Risky Org",
                    "match_evidence": {"kind": "exact_normalized_primary_name"},
                    "location_evidence": {"kind": "exact"},
                    "status": "active",
                    "verification_required": "manual OFAC identity verification",
                }]
            return result

        mod.lookup_name_candidates = name_lookup
        report = mod._build_report({"ein": "111111111", "external_mode": "local"})
        titles = {item["title"] for item in report["indicators"]}
        self.assertIn("IRS automatic-revocation status requires review", titles)
        self.assertIn("OFAC or HHS exclusion name candidates require identity verification", titles)
        html = mod._render_report(report)
        self.assertIn("IRS Eligibility, Revocation &amp; Sanctions Screening", html)
        self.assertIn("Automatic revocation; no reinstatement date", html)
        self.assertIn("Candidate-only OFAC/HHS results", html)
        self.assertIn("Risky Org &lt;script&gt;", html)
        self.assertNotIn("Risky Org <script>", html)

    def test_partial_screening_does_not_infer_pub78_absence(self):
        screening = {
            "irs": {
                "coverage": [{"dataset_key": "irs_auto_revocation"}],
                "results": [{
                    "dataset_key": "irs_auto_revocation",
                    "status": "automatically_revoked",
                    "status_date": "2024-01-01",
                    "reinstatement_date": "",
                }],
            },
            "organization": {"coverage": [], "results": []},
            "people": [],
        }
        indicators = mod._screening_indicators(
            screening, [{"tax_year": 2024}], {"available": False, "matched": False}
        )
        self.assertEqual(len(indicators), 1)
        self.assertEqual(indicators[0]["category"], "External Coverage")
        self.assertIn("current Pub. 78 coverage unavailable", indicators[0]["title"])
        self.assertNotIn("absent from the loaded Pub. 78", indicators[0]["evidence"])
        self.assertEqual(mod._risk_score(indicators), 0)

    def test_indexed_network_sidecar_is_loaded_and_rendered_with_provenance(self):
        mod.risk_network_available = lambda *args, **kwargs: True
        mod.risk_network_path = lambda *args, **kwargs: Path(self.temp_dir.name) / "risk_network.db"
        mod.network_for_ein = lambda *args, **kwargs: {
            "ein": "111111111",
            "build": {"build_status": "complete", "completed_at": "2026-08-14T12:00:00Z"},
            "sources": [{"source_name": "grants", "available": 1}],
            "coverage": {"covered": True, "covered_tax_years": [2023, 2024], "build_scope": "full"},
            "outgoing": [{
                "source_ein": "111111111", "target_ein": "222222222", "target_name": "Risky Foundation",
                "edge_type": "grant_paid", "tax_year": 2024, "amount": 200000,
                "confidence": 0.99, "provenance_table": "grant_recipient_resolved", "provenance_row_id": "1",
            }],
            "incoming": [],
            "shared_neighbors": [],
        }
        report = mod._build_report({"ein": "111111111", "external_mode": "local"})
        indexed = report["network"]["indexed"]
        self.assertTrue(indexed["available"])
        html = mod._render_report(report)
        self.assertIn("Indexed network evidence", html)
        self.assertIn("grant_recipient_resolved", html)
        self.assertIn("Indexed relationship network active", html)

    def test_cache_action_isolated_and_pdf_expands_paths(self):
        analyze = mod._cache_key({"ein": "111111111", "external_mode": "local"})
        search = mod._cache_key({"ein": "111111111", "org_search": "Risky", "_action": "search_org", "external_mode": "local"})
        self.assertNotEqual(analyze, search)
        pdf = mod.render_pdf_export({"ein": "111111111", "external_mode": "local"})
        self.assertIn('class="network-paths" open', pdf)
        self.assertIn("min-width:0 !important", pdf)

    def test_name_search_returns_selectable_matches(self):
        headers, rows = mod.run({"org_search": "Risky"})
        self.assertEqual(rows, [])
        html = mod.render_results({"org_search": "Risky"}, headers, rows)
        self.assertIn("Organization Matches", html)
        self.assertIn('action="/query/fraud_risk_dashboard"', html)
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
