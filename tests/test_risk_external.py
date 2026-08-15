import copy
import json
import ssl
import unittest
import urllib.error
import urllib.parse

from queries import _risk_external as risk_external


class FakeResponse:
    def __init__(self, payload):
        if isinstance(payload, bytes):
            self.payload = payload
        else:
            self.payload = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, size=-1):
        return self.payload if size is None or size < 0 else self.payload[:size]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, handler=None):
        self.handler = handler or (lambda request: {})
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        result = self.handler(request)
        if isinstance(result, BaseException):
            raise result
        return FakeResponse(result)


def _path(request):
    return urllib.parse.urlsplit(request.full_url).path


class RiskExternalTests(unittest.TestCase):
    def setUp(self):
        risk_external._clear_cache()
        # Unit tests must not change behavior merely because a developer has
        # installed the optional production FAC sidecar at its default path.
        # Individual offline-merge tests replace this stub explicitly.
        self._original_offline_lookup = risk_external._lookup_offline_fac
        risk_external._lookup_offline_fac = lambda *args, **kwargs: {
            "status": "not_configured",
            "reason": "test_offline_fac_unavailable",
            "reports": [],
            "ueis": [],
        }

    def tearDown(self):
        risk_external._lookup_offline_fac = self._original_offline_lookup
        risk_external._clear_cache()

    def test_local_mode_makes_zero_calls(self):
        opener = FakeOpener()
        result = risk_external.fetch_external_checks(
            "12-3456789",
            "Local Charity",
            "OR",
            mode="local",
            environ={
                "FAC_API_KEY": "fac-secret",
                "SAM_API_KEY": "sam-secret",
                "FEC_API_KEY": "fec-secret",
            },
            opener=opener,
        )

        self.assertEqual(opener.calls, [])
        for source in ("fac", "usaspending", "sam", "fec", "lda"):
            self.assertEqual(result[source]["status"], "blocked")
            self.assertEqual(result["sources"][source]["status"], "blocked")

    def test_local_mode_uses_offline_fac_without_http(self):
        opener = FakeOpener()
        original = risk_external._lookup_offline_fac
        risk_external._lookup_offline_fac = lambda ein, **kwargs: {
            "status": "ok",
            "reports": [{
                "report_id": "historic:2012:fixture",
                "general": {"report_id": "historic:2012:fixture", "audit_year": 2012},
            }],
            "ueis": [],
            "source_as_of_date": "2026-08-14",
        }
        try:
            result = risk_external.fetch_external_checks(
                "12-3456789", "Local Charity", "OR",
                mode="local", environ={}, opener=opener,
            )
        finally:
            risk_external._lookup_offline_fac = original

        self.assertEqual(opener.calls, [])
        self.assertEqual(result["fac"]["status"], "ok")
        self.assertEqual(result["fac"]["reports"][0]["general"]["audit_year"], 2012)
        self.assertEqual(result["fec"]["status"], "blocked")

    def test_missing_keys_are_explicit_without_accidental_calls(self):
        opener = FakeOpener()
        result = risk_external.fetch_external_checks(
            "12-3456789",
            "",
            mode="live",
            environ={},
            opener=opener,
        )

        self.assertEqual(opener.calls, [])
        self.assertEqual(result["fac"]["status"], "not_configured")
        self.assertEqual(result["usaspending"]["status"], "blocked")
        self.assertEqual(result["sam"]["status"], "not_configured")
        self.assertEqual(result["fec"]["status"], "not_configured")
        self.assertEqual(result["lda"]["status"], "blocked")

    def test_fac_deduplicates_resubmission_and_normalizes_findings(self):
        uei = "ABCDEF123456"

        def handler(request):
            path = _path(request)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            if path == "/general" and "auditee_ein" in query:
                return [
                    {
                        "report_id": "2024-12-GSAFAC-0000000001",
                        "audit_year": "2024",
                        "fy_start_date": "2024-01-01",
                        "fy_end_date": "2024-12-31",
                        "fac_accepted_date": "2025-05-01",
                        "auditee_ein": "123456789",
                        "auditee_uei": uei,
                        "auditee_name": "Fixture Charity",
                        "is_internal_control_material_weakness_disclosed": "No",
                        "resubmission_version": 1,
                    },
                    {
                        "report_id": "2024-12-GSAFAC-0000000002",
                        "audit_year": "2024",
                        "fy_start_date": "2024-01-01",
                        "fy_end_date": "2024-12-31",
                        "fac_accepted_date": "2025-06-01",
                        "auditee_ein": "123456789",
                        "auditee_uei": uei,
                        "auditee_name": "Fixture Charity",
                        "is_internal_control_material_weakness_disclosed": "Yes",
                        "resubmission_version": 2,
                    },
                ]
            if path == "/additional_eins":
                return [
                    {
                        "report_id": "2024-12-GSAFAC-0000000002",
                        "additional_ein": "123456789",
                    }
                ]
            if path == "/findings":
                return [
                    {
                        "report_id": "2024-12-GSAFAC-0000000002",
                        "reference_number": "2024-001",
                        "is_material_weakness": "Y",
                        "is_questioned_costs": "true",
                        "is_repeat_finding": "GSA_MIGRATION",
                    }
                ]
            if path == "/findings_text":
                self.assertLessEqual(int(query["limit"][0]), 100)
                return [{
                    "report_id": "2024-12-GSAFAC-0000000002",
                    "finding_ref_number": "2024-001",
                    "finding_text": "Controls did not prevent an unsupported charge.",
                }]
            if path == "/corrective_action_plans":
                self.assertLessEqual(int(query["limit"][0]), 100)
                return [{
                    "report_id": "2024-12-GSAFAC-0000000002",
                    "finding_ref_number": "2024-001",
                    "planned_action": "Management will add a second-level review.",
                }]
            if path == "/federal_awards":
                return [
                    {
                        "report_id": "2024-12-GSAFAC-0000000002",
                        "federal_program_name": "Fixture Grant",
                        "amount_expended": 1250000,
                        "is_major": "1",
                    }
                ]
            if path == "/api/v2/recipient/":
                return {"results": [], "page_metadata": {}}
            raise AssertionError("unexpected request: " + request.full_url)

        opener = FakeOpener(handler)
        result = risk_external.fetch_external_checks(
            "12-3456789",
            "",
            mode="live",
            environ={"FAC_API_KEY": "fac-secret"},
            opener=opener,
        )

        self.assertEqual(result["fac"]["status"], "ok")
        self.assertEqual(result["fac"]["report_count"], 1)
        report = result["fac"]["reports"][0]
        self.assertEqual(report["report_id"], "2024-12-GSAFAC-0000000002")
        self.assertIs(report["general"]["is_internal_control_material_weakness_disclosed"], True)
        self.assertIs(report["findings"][0]["is_material_weakness"], True)
        self.assertIs(report["findings"][0]["is_questioned_costs"], True)
        self.assertIsNone(report["findings"][0]["is_repeat_finding"])
        self.assertIn("unsupported charge", report["findings_text"][0]["finding_text"])
        self.assertIn("second-level review", report["corrective_action_plans"][0]["planned_action"])
        self.assertIs(report["federal_awards"][0]["is_major"], True)
        self.assertEqual(result["fac"]["ueis"], [uei])

        fac_requests = [request for request, _ in opener.calls if request.full_url.startswith("https://api.fac.gov/")]
        self.assertTrue(fac_requests)
        self.assertEqual(fac_requests[0].get_header("X-api-key"), "fac-secret")
        self.assertNotIn("fac-secret", fac_requests[0].full_url)

    def test_offline_fac_history_merges_with_live_and_falls_back(self):
        live = {
            "status": "ok",
            "reports": [{
                "report_id": "R2",
                "general": {"report_id": "R2", "audit_year": 2025, "fy_end_date": "2025-12-31"},
                "findings": [{"reference_number": "live"}],
            }],
            "ueis": ["ABCDEF123456"],
        }
        offline = {
            "status": "ok",
            "reports": [
                {
                    "report_id": "R2",
                    "general": {"report_id": "R2", "audit_year": 2025, "fy_end_date": "2025-12-31"},
                    "findings": [{"reference_number": "stale"}],
                },
                {
                    "report_id": "historic:2010:1",
                    "general": {"report_id": "historic:2010:1", "audit_year": 2010},
                    "findings": [],
                },
            ],
            "ueis": [],
            "source_as_of_date": "2026-08-14",
        }

        combined = risk_external._combine_fac_results(live, offline)
        self.assertEqual(combined["source"], "live_and_offline_fac")
        self.assertEqual(len(combined["reports"]), 2)
        current = next(row for row in combined["reports"] if row["report_id"] == "R2")
        self.assertEqual(current["findings"][0]["reference_number"], "live")
        self.assertEqual(combined["offline_source_as_of_date"], "2026-08-14")

        fallback = risk_external._combine_fac_results(
            {"status": "error", "reports": [], "ueis": []}, offline
        )
        self.assertEqual(fallback["source"], "offline_fac_sidecar_fallback")
        self.assertFalse(fallback["uses_live"])

        partial_live = {
            "status": "ok",
            "reports": [{
                "report_id": "R2",
                "general": {"report_id": "R2", "audit_year": 2025, "auditee_name": "Fresh name"},
                "findings": [],
                "findings_status": "error",
                "federal_awards": [],
                "federal_awards_status": "not_requested",
                "findings_text": [],
                "findings_text_status": "not_requested",
                "corrective_action_plans": [],
                "corrective_action_plans_status": "not_requested",
            }],
            "ueis": [],
            "partial_errors": 1,
        }
        offline_with_details = copy.deepcopy(offline)
        offline_with_details["reports"][0].update({
            "federal_awards": [{"federal_program_name": "Offline award"}],
            "findings_text": [{"finding_text": "Offline narrative"}],
            "corrective_action_plans": [{"planned_action": "Offline CAP"}],
        })
        combined_partial = risk_external._combine_fac_results(partial_live, offline_with_details)
        current = next(row for row in combined_partial["reports"] if row["report_id"] == "R2")
        self.assertEqual(current["general"]["auditee_name"], "Fresh name")
        self.assertEqual(current["findings"][0]["reference_number"], "stale")
        self.assertEqual(current["federal_awards"][0]["federal_program_name"], "Offline award")
        self.assertEqual(current["findings_text"][0]["finding_text"], "Offline narrative")
        self.assertEqual(current["corrective_action_plans"][0]["planned_action"], "Offline CAP")
        self.assertTrue(current["partial_coverage"])

    def test_usaspending_keeps_only_exact_fac_uei(self):
        exact_uei = "ABCDEF123456"

        def handler(request):
            path = _path(request)
            if path == "/general":
                return [
                    {
                        "report_id": "2025-12-GSAFAC-0000000001",
                        "fy_start_date": "2025-01-01",
                        "fy_end_date": "2025-12-31",
                        "auditee_ein": "123456789",
                        "auditee_uei": exact_uei,
                    }
                ]
            if path == "/additional_eins":
                return []
            if path in {"/findings", "/federal_awards"}:
                return []
            if path == "/api/v2/recipient/":
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body["keyword"], exact_uei)
                self.assertEqual(body["award_type"], "grants")
                return {
                    "results": [
                        {
                            "id": "exact-P",
                            "name": "Exact Charity",
                            "uei": exact_uei,
                            "recipient_level": "P",
                            "amount": 123,
                        },
                        {
                            "id": "namesake-P",
                            "name": "Exact Charity",
                            "uei": "ZZZZZZ999999",
                            "recipient_level": "P",
                            "amount": 999999,
                        },
                    ],
                    "page_metadata": {},
                }
            raise AssertionError("unexpected request: " + request.full_url)

        result = risk_external.fetch_external_checks(
            "123456789",
            "",
            environ={"API_GOV_KEY": "fallback-fac-key"},
            opener=FakeOpener(handler),
        )

        self.assertEqual(result["usaspending"]["status"], "ok")
        self.assertEqual([row["id"] for row in result["usaspending"]["matches"]], ["exact-P"])
        self.assertIn("not_single_audit", result["usaspending"]["amount_basis"])

    def test_lda_uses_successor_host_and_expands_strong_client(self):
        def handler(request):
            self.assertEqual(urllib.parse.urlsplit(request.full_url).hostname, "lda.gov")
            if _path(request) == "/api/v1/clients/":
                return {"results": [{"id": 7, "client_id": "C7", "name": "Fixture Charity", "state": "OR"}]}
            if _path(request) == "/api/v1/filings/":
                return {"results": [{"filing_uuid": "F7", "filing_year": 2025, "expenses": "10000"}]}
            raise AssertionError("unexpected request: " + request.full_url)

        opener = FakeOpener(handler)
        result = risk_external.fetch_external_checks(
            "123456789",
            "Fixture Charity",
            "OR",
            environ={},
            opener=opener,
        )

        self.assertEqual(result["lda"]["status"], "ok")
        self.assertEqual(result["lda"]["clients"][0]["match_strength"], "exact")
        self.assertEqual(result["lda"]["clients"][0]["filings"][0]["filing_uuid"], "F7")

    def test_federal_tls_compatibility_keeps_verification_enabled(self):
        payload = risk_external._request_json(
            "https://api.open.fec.gov/v1/committees/",
            params={"q": "fixture", "api_key": "DEMO_KEY"},
            opener=FakeOpener(lambda _request: {"results": []}),
        )
        self.assertEqual(payload, {"results": []})
        context = risk_external._federal_tls_context("api.open.fec.gov")
        self.assertIsNotNone(context)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            self.assertFalse(context.verify_flags & strict_flag)

    def test_api_redirects_are_restricted_to_same_https_origin(self):
        origin = "https://api.fac.gov/general?limit=1"
        handler = risk_external._SameOriginRedirectHandler(origin)
        request = risk_external.urllib.request.Request(
            origin, headers={"X-Api-Key": "never-forward-this"}
        )
        with self.assertRaises(risk_external._ExternalRequestError) as caught:
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://attacker.example/collect"
            )
        self.assertEqual(caught.exception.code, "redirect_not_allowed")

        class RedirectedResponse(FakeResponse):
            def geturl(self):
                return "https://attacker.example/collect"

        class RedirectingInjectedOpener:
            def open(self, request, timeout=None):
                self.request = request
                return RedirectedResponse({"ok": True})

        injected = RedirectingInjectedOpener()
        with self.assertRaises(risk_external._ExternalRequestError) as caught:
            risk_external._request_json(
                "https://api.fac.gov/general",
                headers={"X-Api-Key": "never-forward-this"},
                opener=injected,
            )
        self.assertEqual(caught.exception.code, "redirect_not_allowed")
        self.assertEqual(injected.request.get_header("X-api-key"), "never-forward-this")

    def test_sam_keeps_exact_fac_uei_entities_and_official_exclusion_shape(self):
        uei = "ABCDEF123456"

        def handler(request):
            path = _path(request)
            if path == "/general":
                return [{
                    "report_id": "FAC-SAM-1",
                    "fy_start_date": "2024-01-01",
                    "fy_end_date": "2024-12-31",
                    "auditee_ein": "123456789",
                    "auditee_uei": uei,
                }]
            if path == "/additional_eins":
                return []
            if path in {"/findings", "/federal_awards"}:
                return []
            if path == "/api/v2/recipient/":
                return {"results": []}
            if path == "/entity-information/v4/entities":
                return {"entityData": [{"entityRegistration": {"ueiSAM": uei, "legalBusinessName": "Fixture Charity"}}]}
            if path == "/entity-information/v4/exclusions":
                return {"excludedEntity": [{
                    "exclusionIdentification": {"ueiSAM": uei, "entityName": "Fixture Charity"},
                    "exclusionDetails": {"exclusionType": "Prohibition/Restriction"},
                }]}
            raise AssertionError("unexpected request: " + request.full_url)

        result = risk_external.fetch_external_checks(
            "123456789",
            "",
            environ={"FAC_API_KEY": "fac-key", "SAM_API_KEY": "sam-key"},
            opener=FakeOpener(handler),
        )

        self.assertEqual(result["sam"]["status"], "ok")
        self.assertEqual(result["sam"]["entities"][0]["uei"], uei)
        self.assertEqual(result["sam"]["exclusions"][0]["exclusionIdentification"]["ueiSAM"], uei)

    def test_sam_default_queries_one_ordered_uei_and_reports_omissions(self):
        ueis = ["NEWEST123456", "OLDERX123456", "OLDEST123456"]
        secret = "quota-sensitive-sam-key"

        def handler(request):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            self.assertEqual(query["ueiSAM"], [ueis[0]])
            self.assertEqual(query["page"], ["0"])
            self.assertEqual(query["size"], ["10"])
            if _path(request) == "/entity-information/v4/entities":
                return {"totalRecords": 0, "entityData": []}
            if _path(request) == "/entity-information/v4/exclusions":
                return {"totalRecords": 0, "excludedEntity": []}
            raise AssertionError("unexpected request: " + request.full_url)

        opener = FakeOpener(handler)
        result = risk_external._fetch_sam(
            ueis,
            secret,
            opener=opener,
            timeout=1,
        )

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["queried_ueis"], [ueis[0]])
        self.assertEqual(result["omitted_ueis"], ueis[1:])
        self.assertEqual(result["coverage_status"], "partial")
        self.assertTrue(result["partial"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["quota"]["request_budget"], 3)
        self.assertEqual(result["quota"]["requests_used"], 2)
        self.assertEqual(result["quota"]["published_lowest_daily_limit"], 10)
        self.assertFalse(result["cache_policy"]["persistent"])
        self.assertIn("process restart", result["cache_policy"]["limitation"])
        self.assertEqual(len(opener.calls), 2)
        self.assertNotIn(secret, json.dumps(result))

    def test_sam_exclusion_pagination_is_bounded_and_marks_omitted_pages(self):
        uei = "ABCDEF123456"
        exclusion_pages = []

        def exclusion_row(index):
            return {
                "exclusionIdentification": {
                    "ueiSAM": uei,
                    "entityName": f"Fixture {index}",
                }
            }

        def handler(request):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            if _path(request) == "/entity-information/v4/entities":
                return {
                    "totalRecords": 1,
                    "entityData": [{"entityRegistration": {"ueiSAM": uei}}],
                }
            if _path(request) == "/entity-information/v4/exclusions":
                page = int(query["page"][0])
                exclusion_pages.append(page)
                self.assertEqual(query["size"], ["10"])
                return {
                    "totalRecords": 25,
                    "excludedEntity": [
                        exclusion_row(page * 10 + index) for index in range(10)
                    ],
                }
            raise AssertionError("unexpected request: " + request.full_url)

        result = risk_external._fetch_sam(
            [uei],
            "sam-key",
            opener=FakeOpener(handler),
            timeout=1,
            request_budget=3,
        )

        self.assertEqual(exclusion_pages, [0, 1])
        self.assertEqual(len(result["exclusions"]), 20)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage_status"], "partial")
        self.assertTrue(result["truncated"])
        page_meta = result["coverage"]["exclusion_queries"][0]
        self.assertEqual(page_meta["pages_fetched"], 2)
        self.assertEqual(page_meta["pages_omitted"], 1)
        self.assertEqual(page_meta["records_reported"], 25)
        self.assertIn("exclusion_pages_omitted", result["coverage"]["truncation_reasons"])
        self.assertEqual(result["quota"]["requests_used"], 3)

    def test_sam_exclusion_pagination_can_complete_within_configured_budget(self):
        uei = "ABCDEF123456"

        def row(index):
            return {"exclusionIdentification": {"ueiSAM": uei, "entityName": str(index)}}

        def handler(request):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            if _path(request) == "/entity-information/v4/entities":
                return {"totalRecords": 0, "entityData": []}
            if _path(request) == "/entity-information/v4/exclusions":
                page = int(query["page"][0])
                count = 5 if page == 2 else 10
                return {
                    "totalRecords": 25,
                    "excludedEntity": [row(page * 10 + index) for index in range(count)],
                }
            raise AssertionError("unexpected request: " + request.full_url)

        result = risk_external._fetch_sam(
            [uei],
            "sam-key",
            opener=FakeOpener(handler),
            timeout=1,
            request_budget=4,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["exclusions"]), 25)
        self.assertEqual(result["coverage_status"], "complete")
        self.assertFalse(result["partial"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["quota"]["requests_used"], 4)

    def test_sam_env_configuration_expands_only_primary_fac_ueis(self):
        newest = "NEWEST123456"
        older = "OLDERX123456"
        additional = "ADDTNL123456"

        def handler(request):
            path = _path(request)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            if path == "/general":
                return [
                    {
                        "report_id": "new",
                        "fy_end_date": "2025-12-31",
                        "auditee_ein": "123456789",
                        "auditee_uei": newest,
                    },
                    {
                        "report_id": "old",
                        "fy_end_date": "2024-12-31",
                        "auditee_ein": "123456789",
                        "auditee_uei": older,
                    },
                ]
            if path == "/additional_eins":
                return [{
                    "report_id": "additional",
                    "audit_year": "2025",
                    "auditee_uei": additional,
                    "additional_ein": "123456789",
                }]
            if path in {"/findings", "/federal_awards"}:
                return []
            if path == "/api/v2/recipient/":
                return {"results": [], "page_metadata": {}}
            if path == "/entity-information/v4/entities":
                return {"totalRecords": 0, "entityData": []}
            if path == "/entity-information/v4/exclusions":
                return {"totalRecords": 0, "excludedEntity": []}
            raise AssertionError("unexpected request: " + request.full_url)

        original_offline = risk_external._lookup_offline_fac
        risk_external._lookup_offline_fac = lambda *args, **kwargs: {
            "status": "not_configured",
            "reports": [],
            "ueis": [],
        }
        opener = FakeOpener(handler)
        try:
            result = risk_external.fetch_external_checks(
                "123456789",
                "",
                environ={
                    "FAC_API_KEY": "fac-key",
                    "SAM_API_KEY": "sam-key",
                    "SAM_MAX_UEIS": "2",
                    "SAM_REQUEST_BUDGET": "4",
                },
                opener=opener,
            )
        finally:
            risk_external._lookup_offline_fac = original_offline

        self.assertEqual(result["sam"]["queried_ueis"], [newest, older])
        self.assertNotIn(additional, result["sam"]["coverage"]["candidate_ueis"])
        self.assertEqual(result["sam"]["coverage_status"], "complete")
        sam_calls = [
            request
            for request, _timeout in opener.calls
            if _path(request).startswith("/entity-information/v4/")
        ]
        self.assertEqual(len(sam_calls), 4)

    def test_errors_are_graceful_and_do_not_leak_key_or_url(self):
        secret = "very-secret-fac-key"

        def handler(request):
            return urllib.error.URLError(
                "failed URL https://api.fac.gov/general?api_key=" + secret
            )

        result = risk_external.fetch_external_checks(
            "123456789",
            "",
            environ={"FAC_API_KEY": secret},
            opener=FakeOpener(handler),
        )

        self.assertEqual(result["fac"]["status"], "error")
        serialized = json.dumps(result)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("api.fac.gov/general", serialized)

    def test_cache_uses_configuration_fingerprint_and_returns_copies(self):
        def handler(request):
            if _path(request) in {"/general", "/additional_eins"}:
                return []
            raise AssertionError("unexpected request")

        opener = FakeOpener(handler)
        kwargs = {
            "ein": "123456789",
            "org_name": "",
            "environ": {"FAC_API_KEY": "one"},
            "opener": opener,
        }
        first = risk_external.fetch_external_checks(**kwargs)
        call_count = len(opener.calls)
        first["fac"]["status"] = "mutated"
        second = risk_external.fetch_external_checks(**kwargs)

        self.assertEqual(len(opener.calls), call_count)
        self.assertEqual(second["fac"]["status"], "no_match")

        risk_external.fetch_external_checks(
            "123456789",
            "",
            environ={"FAC_API_KEY": "two"},
            opener=opener,
        )
        self.assertGreater(len(opener.calls), call_count)


if __name__ == "__main__":
    unittest.main()
