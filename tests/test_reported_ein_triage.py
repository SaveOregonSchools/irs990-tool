import json
import sqlite3
import unittest

import grant_ai_assist_v1 as gai


TARGET_EIN = "472772048"


def build_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    gai.create_identity_schema(conn, create_fts=False)
    identity = gai.make_identity_row(
        ein=TARGET_EIN,
        source="returns_org_name",
        source_detail="returns.org_name",
        source_rank=10,
        legal_name="Learning Policy Institute",
        street="1530 Page Mill Road No 200",
        city="PALO ALTO",
        state="CA",
        zip_value="94304",
        filing_id="F1",
        tax_year=2015,
    )
    gai.insert_identity_batch(conn, [identity], build_tokens=False)
    conn.executescript(
        """
        CREATE TABLE sig_fixture (
          signature_hash TEXT,
          reported_ein TEXT,
          recipient_name TEXT,
          recipient_name_norm TEXT,
          street TEXT,
          street_norm TEXT,
          city TEXT,
          state TEXT,
          zip5 TEXT,
          country TEXT,
          grant_count INTEGER,
          total_amount NUMERIC,
          sample_purpose TEXT,
          sample_grantor_ein TEXT,
          sample_grantor_name TEXT,
          first_pass_statuses_json TEXT,
          first_pass_methods_json TEXT,
          first_pass_warning_flags TEXT,
          first_pass_min_confidence NUMERIC,
          first_pass_avg_confidence NUMERIC,
          first_pass_max_confidence NUMERIC,
          queued_reason TEXT,
          candidate_count INTEGER,
          ai_queue_status TEXT
        );

        CREATE TABLE candidate_fixture (
          candidate_id TEXT,
          candidate_rank INTEGER,
          ein TEXT,
          candidate_name TEXT,
          source TEXT,
          source_rank INTEGER,
          street TEXT,
          city TEXT,
          state TEXT,
          zip5 TEXT,
          name_score NUMERIC,
          address_score NUMERIC,
          zip_match INTEGER,
          city_state_match INTEGER,
          state_match INTEGER,
          exact_name INTEGER,
          exact_address INTEGER,
          reported_ein_match INTEGER,
          candidate_score NUMERIC,
          candidate_reason TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO sig_fixture VALUES (
          'SIG_FORMER_NAME', ?, 'Institute for Education Policy',
          'INSTITUTE FOR EDUCATION POLICY', '1530 Page Mill Road Suite 200',
          '1530 PAGE MILL RD STE 200', 'PALO ALTO', 'CA', '94304', 'US',
          3, 4755000, 'General support', '943147856', 'Sandler Foundation',
          '{"unresolved": 3}', '{}', 'reported_ein_name_disagrees',
          0, 0, 0, 'ai_second_pass_target', 1, 'candidates_ready'
        )
        """,
        (TARGET_EIN,),
    )
    conn.commit()
    return conn


def insert_candidate(conn, *, address_score=0.898, zip_match=1, city_state_match=1, exact_address=0):
    conn.execute("DELETE FROM candidate_fixture")
    conn.execute(
        """
        INSERT INTO candidate_fixture VALUES (
          'C1', 1, ?, 'Learning Policy Institute', 'returns_org_name', 10,
          '1530 Page Mill Road No 200', 'PALO ALTO', 'CA', '94304',
          0.3273, ?, ?, ?, 1, 0, ?, 1, 130.1195,
          'reported_ein_candidate;zip_match;city_state_match;seen_in_990_returns'
        )
        """,
        (TARGET_EIN, address_score, zip_match, city_state_match, exact_address),
    )
    conn.commit()


def fixture_rows(conn):
    sig = conn.execute("SELECT * FROM sig_fixture").fetchone()
    candidates = conn.execute("SELECT * FROM candidate_fixture ORDER BY candidate_rank").fetchall()
    return sig, candidates


class ReportedEinTriageTests(unittest.TestCase):
    def test_new_list_style_recipient_patterns_are_nonadjudicable(self):
        cases = {
            "VARIOUS-AVAILABLE UPON REQUEST": "available_upon_request_placeholder",
            "NETWORK OF 685 AGENCIES": "network_of_recipients_placeholder",
            "NETWORK OF OVER 700 AGENCIES": "network_of_recipients_placeholder",
            "200-300 DIFFERENT ORGANIZATIONS RECEIVE ASSISTANCE MONTHLY FROM CARITAS": "different_recipients_placeholder",
            "GRANTS MADE TO VARIOUS IRC 501(C)(3) ENTITIES": "various_recipients_placeholder",
            "DESIGNATIONS TO OTHER UNITED WAYS AND AGENCIES PAID DIRECTLY BY THIRD-PARTY": "designations_to_other_recipients_placeholder",
            "85 partner charity organizations": "partner_charity_organizations_placeholder",
        }
        for name, reason in cases.items():
            with self.subTest(name=name):
                self.assertEqual(gai.recipient_name_nonadjudicable_reason(name), reason)

    def test_new_list_style_patterns_do_not_catch_specific_names(self):
        names = [
            "THE MULTIPLE SCLEROSIS CENTER OF ATLANTA",
            "CHARTER SCHOOLS DEVELOPMENT CORPORATION",
            "NC Citizens for Protecting our Schools",
            "KAMEHAMEHA SCHOOLS",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(gai.recipient_name_nonadjudicable_reason(name), "")

    def test_reported_ein_triage_parks_new_list_style_rows_when_configured(self):
        conn = build_conn()
        try:
            conn.execute(
                """
                UPDATE sig_fixture
                SET recipient_name='NETWORK OF 685 AGENCIES',
                    recipient_name_norm='NETWORK OF 685 AGENCIES'
                """
            )
            sig, candidates = fixture_rows(conn)

            row, reason = gai.reported_ein_triage_decision_row(
                conn,
                sig,
                candidates,
                placeholder_action="human_review",
            )

            self.assertEqual(reason, "nonadjudicable_recipient_network_of_recipients_placeholder")
            self.assertEqual(row[1], "HUMAN_REVIEW")
            self.assertEqual(row[10], 0)
            self.assertEqual(row[11], "ok")
            self.assertIn("recipient_name_nonadjudicable", json.loads(row[7]))
        finally:
            conn.close()

    def test_keeps_known_reported_ein_when_address_location_matches_despite_low_name_score(self):
        conn = build_conn()
        try:
            insert_candidate(conn)
            sig, candidates = fixture_rows(conn)

            row, reason = gai.reported_ein_triage_decision_row(conn, sig, candidates)

            self.assertEqual(reason, "reported_ein_known_name_disagrees_address_location_kept_no_ai")
            self.assertEqual(row[1], "KEEP_REPORTED_EIN")
            self.assertEqual(row[2], "C1")
            self.assertEqual(row[3], TARGET_EIN)
            self.assertEqual(row[10], 1)
            self.assertEqual(row[13], "rule:reported_ein_address_location")
            self.assertIn("reported_ein_address_location_match", json.loads(row[7]))
        finally:
            conn.close()

    def test_low_name_score_reported_ein_still_needs_review_without_address_support(self):
        conn = build_conn()
        try:
            insert_candidate(conn, address_score=0.2, zip_match=1, city_state_match=1)
            sig, candidates = fixture_rows(conn)

            row, reason = gai.reported_ein_triage_decision_row(conn, sig, candidates)

            self.assertEqual(reason, "reported_ein_known_name_disagrees_human_review_no_ai")
            self.assertEqual(row[1], "HUMAN_REVIEW")
            self.assertEqual(row[3], TARGET_EIN)
            self.assertEqual(row[10], 0)
            self.assertEqual(row[13], "rule:reported_ein_no_ai_review")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
