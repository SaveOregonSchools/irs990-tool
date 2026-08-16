from pathlib import Path
import unittest


class LauncherConfigTests(unittest.TestCase):
    def test_launcher_uses_shared_database_configuration(self):
        launcher = (
            Path(__file__).resolve().parents[1] / "Launch IRS 990 Tool.ps1"
        ).read_text(encoding="utf-8-sig")

        self.assertIn("[switch]$NoBrowser", launcher)
        self.assertIn("from common import DB_PATH; print(DB_PATH)", launcher)
        self.assertNotIn('Join-Path $projectRoot "db\\irs990.db"', launcher)


if __name__ == "__main__":
    unittest.main()
