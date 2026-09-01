import csv
import os
import sqlite3
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as app_module
import refresh_data_stats


def build_stats_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE returns (
          filing_id TEXT PRIMARY KEY,
          ein TEXT,
          return_type TEXT,
          tax_year INTEGER
        );

        CREATE TABLE grants (
          id INTEGER PRIMARY KEY,
          filing_id TEXT,
          recipient_ein TEXT,
          cash_grant_amt NUMERIC,
          non_cash_assistance_amt NUMERIC
        );

        CREATE TABLE grant_recipient_signature (
          signature_hash TEXT PRIMARY KEY,
          grant_count INTEGER,
          total_amount NUMERIC,
          ai_queue_status TEXT,
          candidate_count INTEGER,
          first_pass_avg_confidence NUMERIC,
          queued_reason TEXT
        );

        CREATE TABLE grant_recipient_ai_candidate (
          signature_hash TEXT,
          source TEXT,
          candidate_reason TEXT,
          candidate_score NUMERIC,
          ein TEXT
        );

        CREATE TABLE grant_recipient_ai_decision (
          signature_hash TEXT PRIMARY KEY,
          decision TEXT,
          validation_status TEXT,
          auto_accept INTEGER,
          needs_human_review INTEGER,
          confidence NUMERIC
        );

        CREATE TABLE grant_recipient_ai_applied (
          grant_id INTEGER PRIMARY KEY,
          signature_hash TEXT,
          ai_decision TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO returns VALUES (?,?,?,?)",
        [
            ("F1", "111111111", "990", 2023),
            ("F2", "222222222", "990PF", 2023),
            ("F3", "111111111", "990", 2022),
        ],
    )
    conn.executemany(
        "INSERT INTO grants VALUES (?,?,?,?,?)",
        [
            (1, "F1", "333333333", 100, 0),
            (2, "F1", "", 200, 0),
            (3, "F2", "444444444", 300, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO grant_recipient_signature VALUES (?,?,?,?,?,?,?)",
        [
            ("sig-match", 1, 100, "adjudicated", 1, 0.95, "fixture"),
            ("sig-review", 1, 200, "adjudicated", 2, 0.70, "fixture"),
            ("sig-pending", 1, 300, "candidates_ready", 1, 0.50, "fixture"),
        ],
    )
    conn.executemany(
        "INSERT INTO grant_recipient_ai_candidate VALUES (?,?,?,?,?)",
        [
            ("sig-match", "fixture", "fixture", 95, "333333333"),
            ("sig-review", "fixture", "fixture", 70, "444444444"),
            ("sig-pending", "fixture", "fixture", 60, "555555555"),
        ],
    )
    conn.executemany(
        "INSERT INTO grant_recipient_ai_decision VALUES (?,?,?,?,?,?)",
        [
            ("sig-match", "SELECT_CANDIDATE", "ok", 1, 0, 0.95),
            ("sig-review", "HUMAN_REVIEW", "ok", 0, 1, 0.70),
        ],
    )
    conn.execute("INSERT INTO grant_recipient_ai_applied VALUES (?,?,?)", (1, "sig-match", "SELECT_CANDIDATE"))
    conn.commit()
    conn.close()


class AppPagesAndStatsTests(unittest.TestCase):
    def setUp(self):
        self.original_registry = app_module.REGISTRY
        self.original_plugin_fingerprint = app_module.PLUGIN_FINGERPRINT
        self.original_plugin_fingerprint_func = app_module.plugin_fingerprint
        self.original_load_plugins = app_module.load_plugins
        self.original_db_path = app_module.DB_PATH
        self.original_connect_ro = app_module.connect_ro
        fake_query = SimpleNamespace(
            META={
                "key": "fixture_query",
                "name": "Fixture Query",
                "description": "Runs a fixture query for tests.",
            },
            render_fields=lambda form: "<input name='fixture' value='ok'>",
            run=lambda form: (["col"], [("value",)]),
            export_rows=lambda form: [("value",)],
        )
        fake_ask_query = SimpleNamespace(
            META={
                "key": "ask_database",
                "name": "Ask Database - Generate/Run SQL",
                "description": "Ask a plain-English database question.",
            },
            render_fields=lambda form: "<input name='question' value=''>",
            run=lambda form: (["col"], [("ask",)]),
            export_rows=lambda form: [("ask",)],
        )
        fake_pdf_query = SimpleNamespace(
            META={
                "key": "pdf_query",
                "name": "PDF Query",
                "description": "Runs a PDF-capable query for tests.",
            },
            HIDE_PREVIEW_LIMIT=True,
            HIDE_CSV_EXPORT=True,
            DISABLE_ROW_LIMIT=True,
            PDF_EXPORT=True,
            RUN_BUTTON_LABEL="Open Fixture",
            render_fields=lambda form: "<input name='fixture' value='ok'>",
            run=lambda form: (["col"], [("one",), ("two",)]),
            export_rows=lambda form: [("one",), ("two",)],
            render_results=lambda form, headers, rows: f"<div>custom rows:{len(rows)}</div>",
            render_pdf_export=lambda form: "<!doctype html><title>PDF fixture</title>",
        )
        fake_download_query = SimpleNamespace(
            META={
                "key": "download_query",
                "name": "Download Query",
                "description": "Tests conditional report downloads.",
            },
            HIDE_PREVIEW_LIMIT=True,
            HIDE_CSV_EXPORT=True,
            DISABLE_ROW_LIMIT=True,
            PDF_EXPORT=True,
            DOWNLOAD_FILINGS=True,
            EXPORTS_REQUIRE_RESULTS=True,
            RUN_BUTTON_LABEL="Open EIN",
            render_fields=lambda form: "<input name='ein' value='{}'>".format(form.get("ein", "")),
            run=lambda form: (["ein"], [(form.get("ein"),)] if form.get("ein") else []),
            export_rows=lambda form: [],
            render_results=lambda form, headers, rows: f"<div>report rows:{len(rows)}</div>",
            render_pdf_export=lambda form: "<!doctype html><title>PDF fixture</title>",
            filing_xml_paths=lambda form: [],
        )
        app_module.REGISTRY = {
            "ask_database": fake_ask_query,
            "fixture_query": fake_query,
            "pdf_query": fake_pdf_query,
            "download_query": fake_download_query,
        }
        app_module.PLUGIN_FINGERPRINT = app_module.plugin_fingerprint()
        app_module.app.config.update(TESTING=True)

    def tearDown(self):
        app_module.REGISTRY = self.original_registry
        app_module.PLUGIN_FINGERPRINT = self.original_plugin_fingerprint
        app_module.plugin_fingerprint = self.original_plugin_fingerprint_func
        app_module.load_plugins = self.original_load_plugins
        app_module.DB_PATH = self.original_db_path
        app_module.connect_ro = self.original_connect_ro

    def test_home_lists_stats_and_query_modules_without_loading_first_query(self):
        client = app_module.app.test_client()
        response = client.get("/")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Database Statistics", body)
        self.assertIn("IRS 990 - Most Popular", body)
        self.assertIn("IRS 990 - Other Modules", body)
        self.assertIn("Ask Database", body)
        self.assertIn('class="module-columns"', body)
        self.assertIn('class="module-group module-group-irs"', body)
        self.assertIn('class="module-group module-group-maintenance"', body)
        self.assertIn("Select a module from the list below", body)
        self.assertIn("Ask a plain-English question involving nonprofit tax data.", body)
        self.assertIn("Review statistics of what is in this IRS database.", body)
        self.assertIn("Fixture Query", body)
        self.assertIn("Runs a fixture query for tests.", body)
        self.assertIn("save-oregon-schools-logo.png", body)
        self.assertIn('href="https://www.saveoregonschools.com"', body)
        self.assertIn('href="https://github.com/SaveOregonSchools/irs990-tool"', body)
        self.assertIn('href="https://github.com/SaveOregonSchools/irs990-tool/blob/main/LICENSE"', body)
        self.assertIn('href="https://github.com/SaveOregonSchools/irs990-tool/blob/main/TRADEMARKS.md"', body)
        self.assertIn("Save Oregon Schools, LLC", body)
        self.assertNotIn("Select the module to open", body)
        self.assertNotIn("Choose a research module", body)
        self.assertNotIn("Preview row limit", body)

    def test_home_menu_matches_requested_groups_and_order(self):
        actual = {
            title: [entry[2] for entry in entries]
            for title, _group, _column, entries in app_module.HOME_MENU
        }
        self.assertEqual(
            actual,
            {
                "IRS 990 - Most Popular": [
                    "Nonprofit Deep Dive",
                    "Find EINs by Organization Name",
                    "Grants Paid/Received",
                    "Fraud & Risk Indicators",
                    "Contractors",
                    "Lobbying & Political Activity",
                    "Federal Lobbyist Registrations & Reports",
                    "Ask Database",
                    "Schedule R: Related Orgs",
                    "Find Filings by Person Name",
                ],
                "IRS 990 - Other Modules": [
                    "Core Data Lookup",
                    "Grants Received",
                    "Grants Paid",
                    "Filings by EINs",
                ],
                "Labor / OLMS": [
                    "Union Deep Dive",
                    "Filing Compliance / Timeliness",
                    "Grants / Contributions Paid",
                    "Vendors / Contractors / Payees",
                    "Grantee / Vendor Explorer",
                    "OLMS / IRS Match Audit",
                ],
                "Data Maintenance": [
                    "Database Statistics",
                    "Import New IRS Data",
                    "OLMS Import / Data Quality",
                ],
            },
        )
        placements = {
            title: (group, column)
            for title, group, column, _entries in app_module.HOME_MENU
        }
        self.assertEqual(placements["IRS 990 - Most Popular"], ("irs", "left"))
        self.assertEqual(placements["IRS 990 - Other Modules"], ("irs", "left"))
        self.assertEqual(placements["Labor / OLMS"], ("olms", "right"))
        self.assertEqual(placements["Data Maintenance"], ("maintenance", "right"))

    def test_toolbox_home_link_is_opt_in(self):
        client = app_module.app.test_client()

        with patch.dict(os.environ, {"TOOLBOX_HOME_URL": ""}):
            body = client.get("/").get_data(as_text=True)
        self.assertNotIn('class="toolbox-link"', body)

        with patch.dict(os.environ, {"TOOLBOX_HOME_URL": "/"}):
            body = client.get("/").get_data(as_text=True)
        self.assertIn('class="toolbox-link" href="/">All tools</a>', body)

    def test_query_page_has_home_link_and_runs_selected_module(self):
        client = app_module.app.test_client()
        response = client.get("/query/fixture_query")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('aria-label="Home"', body)
        self.assertIn("Preview row limit", body)
        self.assertIn('onchange="this.form.submit()"', body)
        self.assertIn('action="/query/fixture_query"', body)
        self.assertNotIn("loadBtn", body)
        self.assertNotIn(">Load<", body)
        self.assertNotIn("Refresh Queries", body)

        run_response = client.post(
            "/query/fixture_query",
            data={"qkey": "fixture_query", "_limit": "5"},
        )
        run_body = run_response.get_data(as_text=True)
        self.assertEqual(run_response.request.path, "/query/fixture_query")
        self.assertIn("value", run_body)

    def test_query_page_urls_honor_script_name_and_preserve_button_action(self):
        client = app_module.app.test_client()
        response = client.get(
            "/query/fixture_query",
            environ_overrides={"SCRIPT_NAME": "/irs990"},
        )
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/irs990/query/fixture_query"', body)
        self.assertIn('href="/irs990/"', body)
        self.assertIn("submittedAction.name = submitter.name", body)
        self.assertIn("submittedAction.value = submitter.value", body)

    def test_pdf_query_hides_generic_preview_and_csv_controls(self):
        client = app_module.app.test_client()
        response = client.get("/query/pdf_query")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Preview row limit", body)
        self.assertNotIn("Export CSV", body)
        self.assertIn("Export PDF", body)
        self.assertIn("Open Fixture", body)
        self.assertIn('formaction="/export_pdf"', body)

        run_response = client.post("/run", data={"qkey": "pdf_query", "_limit": "1"})
        run_body = run_response.get_data(as_text=True)
        self.assertIn("custom rows:2", run_body)

        pdf_response = client.post("/export_pdf", data={"qkey": "pdf_query"})
        self.assertEqual(pdf_response.status_code, 200)
        self.assertIn("PDF fixture", pdf_response.get_data(as_text=True))

    def test_report_exports_only_appear_after_a_report_is_opened(self):
        client = app_module.app.test_client()
        initial_body = client.get("/query/download_query").get_data(as_text=True)
        self.assertNotIn("Export PDF", initial_body)
        self.assertNotIn("Download Filings", initial_body)

        report_body = client.post(
            "/run", data={"qkey": "download_query", "ein": "111111111"}
        ).get_data(as_text=True)
        self.assertIn("Export PDF", report_body)
        self.assertIn("Download Filings", report_body)
        self.assertIn('formaction="/download_filings"', report_body)
        self.assertIn("zip the XML versions of all displayed tax filings", report_body)

    def test_download_filings_streams_flat_zip_and_removes_temp_file(self):
        module = app_module.REGISTRY["download_query"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first_dir = root / "first"
            second_dir = root / "second"
            first_dir.mkdir()
            second_dir.mkdir()
            first = first_dir / "filing.xml"
            second = second_dir / "filing.xml"
            first.write_text("<first/>", encoding="utf-8")
            second.write_text("<second/>", encoding="utf-8")
            module.filing_xml_paths = lambda form: [first, second]
            app_module.app.config["DOWNLOAD_TEMP_DIR"] = td

            response = app_module.app.test_client().post(
                "/download_filings",
                data={"qkey": "download_query", "ein": "11-1111111"},
                buffered=True,
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "application/zip")
            self.assertIn("nonprofit_deep_dive_111111111_filings_", response.headers["Content-Disposition"])
            with zipfile.ZipFile(BytesIO(response.data)) as archive:
                self.assertEqual(archive.namelist(), ["filing.xml", "filing_2.xml"])
                self.assertTrue(all("/" not in name and "\\" not in name for name in archive.namelist()))
                self.assertEqual(archive.read("filing.xml"), b"<first/>")
                self.assertEqual(archive.read("filing_2.xml"), b"<second/>")
            self.assertEqual(list(root.glob("irs990_filings_*.zip")), [])
            app_module.app.config.pop("DOWNLOAD_TEMP_DIR", None)

    def test_registry_auto_reloads_when_query_files_change(self):
        app_module.REGISTRY = {"old": object()}
        app_module.PLUGIN_FINGERPRINT = (("old.py", 1),)
        app_module.plugin_fingerprint = lambda: (("new.py", 2),)
        app_module.load_plugins = lambda: {"new": object()}

        app_module.ensure_registry()

        self.assertIn("new", app_module.REGISTRY)
        self.assertEqual(app_module.PLUGIN_FINGERPRINT, (("new.py", 2),))

    def test_stats_page_reads_cached_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fixture.db"
            build_stats_fixture(db_path)
            refresh_data_stats.refresh_stats(str(db_path), include_final_view=False)

            app_module.DB_PATH = db_path
            app_module.connect_ro = lambda: sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)

            client = app_module.app.test_client()
            response = client.get("/stats")
            body = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Cached statistics last refreshed", body)
            self.assertIn("2023", body)
            self.assertIn("enhanced_match", body)
            self.assertIn("human_review", body)
            self.assertIn("pending_ai_adjudication", body)

    def test_stats_refresh_reuses_one_collection_for_csv_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fixture.db"
            csv_path = Path(tmp) / "grant_match_stats.csv"
            build_stats_fixture(db_path)

            with patch.object(
                refresh_data_stats.grant_stats,
                "collect_stats",
                wraps=refresh_data_stats.grant_stats.collect_stats,
            ) as collect_stats:
                refresh_data_stats.refresh_stats(
                    str(db_path),
                    include_final_view=False,
                    grant_stats_csv_path=str(csv_path),
                )

            self.assertEqual(collect_stats.call_count, 1)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(
                any(
                    row["section"] == "raw_grants"
                    and row["metric"] == "total_grants"
                    and row["count"] == "3"
                    for row in rows
                )
            )


if __name__ == "__main__":
    unittest.main()
