import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from queries import federal_lobbying as mod


FILING_ONE = {
    "filing_uuid": "11111111-1111-4111-8111-111111111111",
    "filing_type": "Q1",
    "filing_type_display": "1st Quarter - Report",
    "filing_year": 2023,
    "income": "10000.00",
    "expenses": None,
    "dt_posted": "2023-04-20T09:43:00-04:00",
    "registrant": {"name": "THE INSTITUTE FOR EDUCATIONAL LEADERSHIP"},
    "client": {"name": "INSTITUTE FOR EDUCATIONAL LEADERSHIP"},
}

FILING_TWO = {
    "filing_uuid": "22222222-2222-4222-8222-222222222222",
    "filing_type": "RA",
    "filing_type_display": "Registration - Amendment",
    "filing_year": 2024,
    "income": None,
    "expenses": "40000.00",
    "dt_posted": "2024-06-01T14:30:00-04:00",
    "registrant": {"name": "THE INSTITUTE FOR EDUCATIONAL LEADERSHIP"},
    "client": {"name": "INSTITUTE FOR EDUCATIONAL LEADERSHIP"},
}

FALSE_POSITIVE_FILING = {
    **FILING_TWO,
    "filing_uuid": "33333333-3333-4333-8333-333333333333",
    "client": {"name": "OTHER EDUCATIONAL LEADERSHIP GROUP"},
}


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _size=-1):
        return self.body


class FederalLobbyingTests(unittest.TestCase):
    def setUp(self):
        with mod._CACHE_LOCK:
            mod._CACHE.clear()

    def tearDown(self):
        with mod._CACHE_LOCK:
            mod._CACHE.clear()

    def test_requires_exactly_one_valid_ein(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            mod.run({"ein": "123"})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            mod.run({"ein": "11-1111111 22-2222222"})
        with self.assertRaisesRegex(ValueError, "either"):
            mod.run({})

    def test_ein_takes_precedence_when_both_search_fields_are_present(self):
        with patch.object(
            mod, "_resolve_name_candidates", return_value=["Resolved EIN Name"]
        ):
            with patch.object(mod, "_find_matches", return_value=[]) as find:
                headers, rows = mod.run(
                    {
                        "ein": "11-1111111",
                        "organization_name": "Ignored Organization Name",
                    }
                )
        self.assertEqual(headers, mod.MATCH_HEADERS)
        self.assertEqual(rows, [])
        find.assert_called_once_with(["Resolved EIN Name"])

        fields = mod.render_fields(
            {"ein": "11-1111111", "organization_name": "Ignored Name"}
        )
        self.assertIn("search will use the EIN", fields)
        self.assertIn("organization-name field", fields)

    def test_organization_name_can_start_match_search_without_an_ein(self):
        match = mod.MatchName("EXAMPLE ORGANIZATION", "client", 123)
        with patch.object(
            mod,
            "_find_matches",
            return_value=[("Client", match, "Oregon", "EXAMPLE REGISTRANT")],
        ) as find:
            headers, rows = mod.run({"organization_name": "Example Organization"})
        self.assertEqual(headers, mod.MATCH_HEADERS)
        self.assertEqual(str(rows[0][1]), "EXAMPLE ORGANIZATION")
        find.assert_called_once_with(["Example Organization"])

    def test_name_normalization_handles_the_ampersand_and_legal_suffixes(self):
        self.assertTrue(mod._names_equivalent("The Example & Company, LLC", "EXAMPLE AND COMPANY"))
        self.assertTrue(mod._names_equivalent("Example L.L.C.", "THE EXAMPLE"))
        self.assertFalse(mod._names_equivalent("Example Education", "Example Health"))

    def test_resolves_current_and_historical_irs_names_in_priority_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fixture.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE canonical_by_ein_year (
                  ein TEXT, tax_year INTEGER, filing_id TEXT
                );
                CREATE TABLE returns (
                  filing_id TEXT, org_name TEXT, dba_name TEXT
                );
                INSERT INTO canonical_by_ein_year VALUES
                  ('111111111', 2024, 'F2'),
                  ('111111111', 2023, 'F1');
                INSERT INTO returns VALUES
                  ('F2', 'Example & Company, LLC', 'Example Learning'),
                  ('F1', 'Example and Company', 'Former Example Name');
                """
            )
            conn.close()

            with patch.object(mod, "connect_ro", side_effect=lambda: sqlite3.connect(db_path)):
                names = mod._resolve_name_candidates("111111111")

        self.assertEqual(
            names,
            [
                "Example & Company, LLC Example Learning",
                "Example & Company, LLC",
                "Example Learning",
                "Example and Company Former Example Name",
                "Former Example Name",
            ],
        )

    def test_collection_follows_only_valid_lda_pagination(self):
        first_url = (
            "https://lda.gov/api/v1/clients/?client_name=Example&page_size=25"
        )
        second_url = (
            "https://lda.gov/api/v1/clients/?client_name=Example&page=2&page_size=25"
        )
        payloads = {
            first_url: {"results": [{"name": "EXAMPLE"}], "next": second_url},
            second_url: {"results": [{"name": "THE EXAMPLE"}], "next": None},
        }
        with patch.object(mod, "_api_get_json", side_effect=lambda url: payloads[url]):
            rows = mod._fetch_collection(
                mod._CLIENTS_URL, {"client_name": "Example"}
            )
        self.assertEqual([row["name"] for row in rows], ["EXAMPLE", "THE EXAMPLE"])

        with patch.object(
            mod,
            "_api_get_json",
            return_value={
                "results": [],
                "next": "https://attacker.example/api/v1/clients/?page=2",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "unsafe pagination"):
                mod._fetch_collection(mod._CLIENTS_URL, {"client_name": "Example"})

    def test_api_request_uses_token_and_reports_rate_limit(self):
        payload = {"results": [], "next": None}
        with patch.dict(mod.os.environ, {"LDA_API_TOKEN": "fixture-token"}, clear=False):
            with patch.object(
                mod,
                "_open_api_request",
                return_value=FakeResponse(payload),
            ) as open_request:
                result = mod._api_get_json(
                    "https://lda.gov/api/v1/clients/?client_name=Example"
                )
        self.assertEqual(result, payload)
        request = open_request.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Token fixture-token")

        error = urllib.error.HTTPError(
            "https://lda.gov/api/v1/clients/",
            429,
            "Too Many Requests",
            {},
            None,
        )
        error.read = lambda _size=-1: b'{"detail":"Expected available in 30 seconds."}'
        with patch.object(mod, "_open_api_request", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "LDA_API_TOKEN"):
                mod._api_get_json(
                    "https://lda.gov/api/v1/clients/?client_name=Example"
                )

    def test_two_step_run_lists_matches_then_loads_only_selected_client(self):
        calls = []

        def fetch(endpoint, params, **kwargs):
            calls.append((endpoint, dict(params), dict(kwargs)))
            if endpoint == mod._REGISTRANTS_URL:
                return [
                    {
                        "id": 310995,
                        "name": "THE INSTITUTE FOR EDUCATIONAL LEADERSHIP",
                        "state_display": "District of Columbia",
                    }
                ]
            if endpoint == mod._CLIENTS_URL:
                return [
                    {
                        "id": 183738,
                        "name": "INSTITUTE FOR EDUCATIONAL LEADERSHIP",
                        "state_display": "District of Columbia",
                        "registrant": {
                            "name": "THE INSTITUTE FOR EDUCATIONAL LEADERSHIP"
                        },
                    }
                ]
            self.assertEqual(params["client_id"], 183738)
            return [FILING_ONE, FILING_TWO, FALSE_POSITIVE_FILING]

        with patch.object(
            mod,
            "_resolve_name_candidates",
            return_value=["Institute for Educational Leadership"],
        ):
            with patch.object(mod, "_fetch_collection", side_effect=fetch):
                match_headers, matches = mod.run({"ein": "11-1111111"})
                headers, rows = mod.run(
                    {
                        "action": "load_filings",
                        "selected_role": "client",
                        "selected_id": "183738",
                        "selected_name": "INSTITUTE FOR EDUCATIONAL LEADERSHIP",
                    }
                )

        self.assertEqual(match_headers, mod.MATCH_HEADERS)
        self.assertEqual([row[0] for row in matches], ["Registrant", "Client"])
        self.assertEqual(matches[1][1].entity_id, 183738)
        self.assertEqual(
            matches[1][3], "THE INSTITUTE FOR EDUCATIONAL LEADERSHIP"
        )
        self.assertEqual(headers, mod.HEADERS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][2], "Registration - Amendment")
        self.assertEqual(rows[0][3], "40000.00")
        self.assertEqual(rows[1][3], "10000.00")
        self.assertEqual(
            rows[0][0].filing_url,
            "https://lda.gov/filings/public/filing/"
            "22222222-2222-4222-8222-222222222222/print/",
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][2]["max_results"], mod._MAX_MATCHES_PER_ROLE)

    def test_export_reuses_short_lived_result_cache(self):
        form = {
            "action": "load_filings",
            "selected_role": "client",
            "selected_id": "183738",
            "selected_name": "INSTITUTE FOR EDUCATIONAL LEADERSHIP",
        }
        with patch.object(
            mod,
            "_filings_for_match",
            return_value=[mod._filing_row(FILING_ONE)],
        ) as find:
            first = mod.run(form)[1]
            exported = list(mod.export_rows(form))
        self.assertEqual(exported, first)
        self.assertEqual(mod.export_headers(form), mod.HEADERS)
        find.assert_called_once_with(
            "client", 183738, "INSTITUTE FOR EDUCATIONAL LEADERSHIP"
        )

    def test_match_results_render_selectable_official_names(self):
        name = mod.MatchName("INSTITUTE FOR EDUCATIONAL LEADERSHIP", "client", 183738)
        rendered = mod.render_results(
            {"ein": "52-1198450"},
            mod.MATCH_HEADERS,
            [
                (
                    "Client",
                    name,
                    "District of Columbia",
                    "THE INSTITUTE FOR EDUCATIONAL LEADERSHIP",
                )
            ],
        )
        self.assertIn("Possible LDA.gov matches", rendered)
        self.assertIn('name="selected_id" value="183738"', rendered)
        self.assertIn('name="selected_role" value="client"', rendered)
        self.assertIn('name="ein" value="52-1198450"', rendered)
        self.assertIn('class="lda-match-button"', rendered)
        self.assertIn("THE INSTITUTE FOR EDUCATIONAL LEADERSHIP", rendered)

    def test_query_page_exposes_ein_or_name_and_renders_first_step(self):
        import app

        original_registry = app.REGISTRY
        original_fingerprint = app.PLUGIN_FINGERPRINT
        try:
            app.REGISTRY = {mod.META["key"]: mod}
            app.PLUGIN_FINGERPRINT = app.plugin_fingerprint()
            app.app.config.update(TESTING=True)
            match = mod.MatchName("EXAMPLE ORGANIZATION", "client", 123)
            with patch.object(
                mod,
                "_find_matches",
                return_value=[("Client", match, "Oregon", "EXAMPLE REGISTRANT")],
            ):
                client = app.app.test_client()
                get_body = client.get("/query/federal_lobbying").get_data(as_text=True)
                post_body = client.post(
                    "/query/federal_lobbying",
                    data={"organization_name": "Example Organization"},
                ).get_data(as_text=True)
        finally:
            app.REGISTRY = original_registry
            app.PLUGIN_FINGERPRINT = original_fingerprint

        self.assertIn('name="ein"', get_body)
        self.assertIn('name="organization_name"', get_body)
        self.assertIn("Find LDA.gov Matches", get_body)
        self.assertNotIn("Export CSV (full result)", get_body)
        self.assertIn("Possible LDA.gov matches", post_body)
        self.assertIn("EXAMPLE ORGANIZATION", post_body)

    def test_render_results_has_six_sort_controls_and_safe_filing_link(self):
        registrant = mod.FilingRegistrant(
            "<Example & Registrant>",
            "https://lda.gov/filings/public/filing/"
            "11111111-1111-4111-8111-111111111111/print/",
        )
        rows = [
            (
                registrant,
                "Client <One>",
                "1st Quarter - Report",
                "1234.5",
                2024,
                "2024-04-20T09:43:00-04:00",
            )
        ]
        rendered = mod.render_results(
            {
                "action": "load_filings",
                "selected_role": "client",
                "selected_id": "183738",
                "selected_name": "Client <One>",
            },
            mod.HEADERS,
            rows,
        )
        self.assertEqual(rendered.count('class="lda-sort-button"'), 6)
        self.assertIn("$1,234.50", rendered)
        self.assertIn("04/20/2024 @ 09:43 AM", rendered)
        self.assertIn('target="_blank" rel="noopener noreferrer"', rendered)
        self.assertIn("&lt;Example &amp; Registrant&gt;", rendered)
        self.assertIn("Client &lt;One&gt;", rendered)
        self.assertIn("aria-sort=\"descending\"", rendered)
        self.assertIn("Export CSV (full result)", rendered)
        self.assertIn("Selected <b>Client</b>", rendered)

        unsafe = mod.FilingRegistrant("Unsafe", "javascript:alert(1)")
        unsafe_rendered = mod.render_results(
            {}, mod.HEADERS, [(unsafe, "Client", "Registration", "", 2024, "")]
        )
        self.assertNotIn("javascript:", unsafe_rendered)

    def test_home_menu_places_module_immediately_after_local_lobbying(self):
        import app

        popular = next(entries for title, _, _, entries in app.HOME_MENU if title == "IRS 990 - Most Popular")
        labels = [entry[2] for entry in popular]
        local_index = labels.index("Lobbying & Political Activity")
        self.assertEqual(
            labels[local_index + 1], "Federal Lobbyist Registrations & Reports"
        )


if __name__ == "__main__":
    unittest.main()
