import sqlite3
import tempfile
import unittest
from pathlib import Path

import common


class GrantWorkSidecarTests(unittest.TestCase):
    def test_attach_uses_configured_file_and_keeps_it_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main_path = root / "irs990.db"
            sidecar_path = root / "grant_matching_work.db"
            sqlite3.connect(main_path).close()
            sidecar = sqlite3.connect(sidecar_path)
            sidecar.execute("CREATE TABLE org_identity (ein TEXT, display_name TEXT)")
            sidecar.execute("INSERT INTO org_identity VALUES (?, ?)", ("123456789", "Fixture Charity"))
            sidecar.commit()
            sidecar.close()

            original_main = common.DB_PATH
            original_sidecar = common.GRANT_WORK_DB_PATH
            common.DB_PATH = main_path.resolve()
            common.GRANT_WORK_DB_PATH = sidecar_path.resolve()
            conn = sqlite3.connect(
                f"file:{main_path.as_posix()}?mode=ro&immutable=1", uri=True
            )
            try:
                self.assertTrue(common.attach_grant_work_ro(conn))
                row = conn.execute(
                    "SELECT ein, display_name FROM grant_work.org_identity"
                ).fetchone()
                self.assertEqual(row, ("123456789", "Fixture Charity"))
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute(
                        "INSERT INTO grant_work.org_identity VALUES (?, ?)",
                        ("987654321", "Should Fail"),
                    )
            finally:
                conn.close()
                common.DB_PATH = original_main
                common.GRANT_WORK_DB_PATH = original_sidecar


if __name__ == "__main__":
    unittest.main()
