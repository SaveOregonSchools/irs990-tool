import unittest
from unittest.mock import patch

import app as app_module
from queries import olms_counterparty_explorer as counterparty
from queries import olms_union_deep_dive as union
from queries import nonprofit_deep_dive as nonprofit


class QueryInternalLinkTests(unittest.TestCase):
    def test_union_candidate_and_ein_links_honor_script_name(self):
        search_report = {
            "search_query": "National Education",
            "search_results": [
                (545574, "National Education Association", "NEA", "Washington", "DC", "2025-12-31", "530115269")
            ],
        }
        detail_report = {
            "org": {
                "display_name": "National Education Association",
                "f_num": 545574,
                "candidate_ein": "530115269",
            },
            "trend_rows": [],
            "history": [],
            "missing": [],
            "grants": {},
            "vendors": {},
            "officers": {},
        }

        with app_module.app.test_request_context(
            "/query/olms_union_deep_dive",
            environ_overrides={"SCRIPT_NAME": "/irs990"},
        ):
            search_html = union._render_search(search_report)

        self.assertIn("/irs990/query/olms_union_deep_dive?f_num=545574", search_html)

        with app_module.app.test_request_context(
            "/query/olms_union_deep_dive",
            environ_overrides={"SCRIPT_NAME": "/irs990"},
        ), patch.object(union, "_build_report", return_value=detail_report):
            detail_html = union.render_results({}, union.HEADERS, [])

        self.assertIn("/irs990/query/nonprofit_deep_dive?ein=530115269", detail_html)

    def test_counterparty_candidate_link_honors_script_name(self):
        report = {
            "search_results": [
                ("cp/id", "Vendor", "Portland", "OR", "97201", "", "name", 2, 1000, 1, "2024", "2025")
            ]
        }
        with app_module.app.test_request_context(
            "/query/olms_counterparty_explorer",
            environ_overrides={"SCRIPT_NAME": "/irs990"},
        ), patch.object(counterparty, "_build_report", return_value=report):
            html = counterparty.render_results({}, counterparty.HEADERS, [])

        self.assertIn(
            "/irs990/query/olms_counterparty_explorer?counterparty_id=cp/id",
            html,
        )

    def test_nonprofit_candidate_form_honors_script_name(self):
        report = {
            "search_query": "Education",
            "search_results": [{
                "org_name": "Education Foundation",
                "ein": "123456789",
                "city": "Portland",
                "state": "OR",
                "tax_year": 2024,
                "return_type": "990",
            }],
        }
        with app_module.app.test_request_context(
            "/query/nonprofit_deep_dive",
            environ_overrides={"SCRIPT_NAME": "/irs990"},
        ):
            html = nonprofit._render_search_results(report)

        self.assertIn('action="/irs990/query/nonprofit_deep_dive"', html)

    def test_typed_union_name_overrides_existing_file_number(self):
        matches = [
            (545574, "National Education Association", "NEA", "Washington", "DC", "2025-12-31", "530115269"),
            (123456, "National Education Local", "NEA", "Salem", "OR", "2025-09-30", ""),
        ]
        form = {"f_num": "545574", "org_search": "National Education"}
        with patch.object(union, "_selected_fnum", return_value=545574), patch.object(
            union, "_search", return_value=matches
        ) as search:
            report = union._build_report(form)

        search.assert_called_once_with("National Education")
        self.assertEqual(report["search_results"], matches)
        self.assertEqual(report["rows"], [])


if __name__ == "__main__":
    unittest.main()
