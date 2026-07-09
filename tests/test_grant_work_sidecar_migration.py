import argparse
import sqlite3
import tempfile
import unittest
from pathlib import Path

import migrate_grant_work_sidecar


def build_migration_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE org_identity (
          identity_id INTEGER PRIMARY KEY AUTOINCREMENT,
          identity_key TEXT NOT NULL UNIQUE,
          ein TEXT NOT NULL,
          source TEXT NOT NULL,
          display_name TEXT,
          name_norm TEXT NOT NULL
        );
        CREATE TABLE org_identity_token (
          identity_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          state TEXT,
          zip5 TEXT,
          PRIMARY KEY(identity_id, token)
        );
        CREATE VIRTUAL TABLE org_identity_fts USING fts5(
          display_name,
          name_norm,
          content='org_identity',
          content_rowid='identity_id'
        );
        CREATE TABLE grant_recipient_signature (
          signature_hash TEXT PRIMARY KEY,
          reported_ein TEXT,
          recipient_name TEXT,
          recipient_name_norm TEXT,
          grant_count INTEGER NOT NULL DEFAULT 0,
          total_amount NUMERIC NOT NULL DEFAULT 0,
          candidate_count INTEGER DEFAULT 0,
          ai_queue_status TEXT DEFAULT 'new'
        );
        CREATE TABLE grant_recipient_signature_grant (
          signature_hash TEXT NOT NULL,
          grant_id INTEGER NOT NULL,
          PRIMARY KEY(signature_hash, grant_id)
        );
        CREATE TABLE grant_recipient_ai_candidate (
          signature_hash TEXT NOT NULL,
          candidate_id TEXT NOT NULL,
          candidate_rank INTEGER NOT NULL,
          identity_id INTEGER,
          ein TEXT NOT NULL,
          candidate_name TEXT,
          PRIMARY KEY(signature_hash, candidate_id)
        );
        CREATE TABLE grant_recipient_ai_decision (
          signature_hash TEXT PRIMARY KEY,
          decision TEXT,
          selected_ein TEXT
        );
        CREATE TABLE grant_recipient_ai_applied (
          grant_id INTEGER PRIMARY KEY,
          signature_hash TEXT NOT NULL,
          selected_ein TEXT NOT NULL
        );
        CREATE TABLE grant_recipient_resolved (
          grant_id INTEGER PRIMARY KEY,
          resolved_ein TEXT
        );
        CREATE INDEX idx_org_identity_ein ON org_identity(ein);
        CREATE INDEX idx_org_token_token ON org_identity_token(token);
        CREATE INDEX idx_sig_queue ON grant_recipient_signature(ai_queue_status, total_amount DESC);
        CREATE INDEX idx_ai_cand_ein ON grant_recipient_ai_candidate(ein);
        """
    )
    conn.execute(
        "INSERT INTO org_identity(identity_key, ein, source, display_name, name_norm) VALUES (?,?,?,?,?)",
        ("id-1", "472772048", "fixture", "Learning Policy Institute", "LEARNING POLICY INSTITUTE"),
    )
    conn.execute("INSERT INTO org_identity_token VALUES (?,?,?,?)", (1, "LEARNING", "CA", "94301"))
    conn.execute("INSERT INTO org_identity_fts(org_identity_fts) VALUES('rebuild')")
    conn.execute(
        "INSERT INTO grant_recipient_signature VALUES (?,?,?,?,?,?,?,?)",
        ("sig-1", "", "Learning Policy Institute", "LEARNING POLICY INSTITUTE", 2, 1000, 1, "candidates_ready"),
    )
    conn.execute("INSERT INTO grant_recipient_signature_grant VALUES (?,?)", ("sig-1", 10))
    conn.execute(
        "INSERT INTO grant_recipient_ai_candidate VALUES (?,?,?,?,?,?)",
        ("sig-1", "C1", 1, 1, "472772048", "Learning Policy Institute"),
    )
    conn.execute("INSERT INTO grant_recipient_ai_decision VALUES (?,?,?)", ("sig-1", "SELECT_CANDIDATE", "472772048"))
    conn.execute("INSERT INTO grant_recipient_ai_applied VALUES (?,?,?)", (10, "sig-1", "472772048"))
    conn.execute("INSERT INTO grant_recipient_resolved VALUES (?,?)", (10, ""))
    conn.commit()
    conn.close()


def has_object(conn: sqlite3.Connection, schema: str, name: str) -> bool:
    return conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_schema WHERE name=? LIMIT 1",
        (name,),
    ).fetchone() is not None


class GrantWorkSidecarMigrationTests(unittest.TestCase):
    def test_migration_moves_working_tables_and_keeps_final_tables_in_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            main_db = Path(tmp) / "irs990.db"
            work_db = Path(tmp) / "grant_matching_work.db"
            build_migration_fixture(main_db)

            migrate_grant_work_sidecar.migrate(
                argparse.Namespace(
                    db=str(main_db),
                    work_db=str(work_db),
                    drop_main=True,
                    overwrite_sidecar=False,
                    dry_run=False,
                    yes=True,
                )
            )

            conn = sqlite3.connect(main_db)
            conn.execute(f"ATTACH DATABASE ? AS {migrate_grant_work_sidecar.WORK_SCHEMA}", (str(work_db),))
            try:
                for table in migrate_grant_work_sidecar.WORK_TABLES + [migrate_grant_work_sidecar.FTS_TABLE]:
                    self.assertFalse(has_object(conn, "main", table), table)
                    self.assertTrue(has_object(conn, migrate_grant_work_sidecar.WORK_SCHEMA, table), table)

                for table in ["grant_recipient_ai_decision", "grant_recipient_ai_applied", "grant_recipient_resolved"]:
                    self.assertTrue(has_object(conn, "main", table), table)

                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM grant_work.org_identity").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM grant_work.org_identity_fts").fetchone()[0],
                    1,
                )
                self.assertTrue(has_object(conn, migrate_grant_work_sidecar.WORK_SCHEMA, "idx_ai_cand_ein"))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
