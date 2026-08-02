import argparse
import sqlite3
import tempfile
import unittest
from pathlib import Path

import scan_xml_sources


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?><Return><ReturnHeader><ReturnTypeCd>990</ReturnTypeCd></ReturnHeader></Return>"""
DIFFERENT_XML = """<?xml version="1.0" encoding="UTF-8"?><Return><ReturnHeader><ReturnTypeCd>990EZ</ReturnTypeCd></ReturnHeader></Return>"""


def args_for(tmp: Path, xml_dir: Path, **overrides):
    defaults = {
        "xml_dir": str(xml_dir),
        "sidecar_db": str(tmp / "irs990_sources.db"),
        "main_db": None,
        "report_csv": None,
        "duplicates_csv": None,
        "conflict_groups_csv": None,
        "conflict_resolution_csv": None,
        "hash_mode": "candidates",
        "quarantine_duplicates": None,
        "quarantine_resolved_conflicts": None,
        "analyze_conflicts": False,
        "yes": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class XmlSourceScannerTests(unittest.TestCase):
    def test_scan_marks_exact_duplicates_and_object_id_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            xml_dir = root / "xml"
            (xml_dir / "a").mkdir(parents=True)
            (xml_dir / "b").mkdir()
            (xml_dir / "c").mkdir()

            (xml_dir / "a" / "202331099349100118_public.xml").write_text(SAMPLE_XML, encoding="utf-8")
            (xml_dir / "b" / "202331099349100118_public.xml").write_text(SAMPLE_XML, encoding="utf-8")
            (xml_dir / "c" / "202331099349100118.xml").write_text(DIFFERENT_XML, encoding="utf-8")
            (xml_dir / "solo.xml").write_text(SAMPLE_XML, encoding="utf-8")

            scan_xml_sources.run(args_for(root, xml_dir))

            conn = sqlite3.connect(root / "irs990_sources.db")
            try:
                statuses = dict(
                    conn.execute(
                        "SELECT duplicate_status, COUNT(*) FROM source_files GROUP BY duplicate_status"
                    ).fetchall()
                )
                self.assertEqual(statuses["exact_duplicate"], 1)
                self.assertEqual(statuses["primary_duplicate_group"], 1)
                self.assertEqual(statuses["object_id_conflict"], 1)
                self.assertEqual(statuses["unique"], 1)
            finally:
                conn.close()

    def test_scan_imports_loaded_filings_for_audit_view(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            xml_dir = root / "xml"
            xml_dir.mkdir()
            xml_file = xml_dir / "LOADME_public.xml"
            xml_file.write_text(SAMPLE_XML, encoding="utf-8")

            main_db = root / "irs990.db"
            conn = sqlite3.connect(main_db)
            conn.execute(
                """
                CREATE TABLE returns (
                  filing_id TEXT PRIMARY KEY,
                  source_file TEXT,
                  ein TEXT,
                  return_type TEXT,
                  tax_year INTEGER,
                  return_ts TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO returns VALUES (?,?,?,?,?,?)",
                ("LOADME_public", str(xml_file), "123456789", "990", 2023, "2024-01-01T00:00:00"),
            )
            conn.commit()
            conn.close()

            scan_xml_sources.run(args_for(root, xml_dir, main_db=str(main_db)))

            conn = sqlite3.connect(root / "irs990_sources.db")
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT * FROM v_source_file_audit").fetchone()
                self.assertEqual(row["loaded_by_exact_filing_id"], 1)
                self.assertEqual(row["loaded_by_object_id"], 1)
                self.assertEqual(row["loaded_filing_id"], "LOADME_public")
                self.assertEqual(row["ein"], "123456789")
            finally:
                conn.close()

    def test_quarantine_requires_confirmation_and_moves_only_exact_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            xml_dir = root / "xml"
            (xml_dir / "a").mkdir(parents=True)
            (xml_dir / "b").mkdir()
            keep = xml_dir / "a" / "DUP_public.xml"
            duplicate = xml_dir / "b" / "DUP_public.xml"
            keep.write_text(SAMPLE_XML, encoding="utf-8")
            duplicate.write_text(SAMPLE_XML, encoding="utf-8")

            quarantine = root / "quarantine"
            scan_xml_sources.run(
                args_for(
                    root,
                    xml_dir,
                    quarantine_duplicates=str(quarantine),
                    yes=True,
                )
            )

            self.assertTrue(keep.exists())
            self.assertFalse(duplicate.exists())
            self.assertTrue((quarantine / "b" / "DUP_public.xml").exists())

    def test_conflict_analysis_identifies_canonical_equivalent_xml(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            xml_dir = root / "xml"
            (xml_dir / "a").mkdir(parents=True)
            (xml_dir / "b").mkdir()
            compact = xml_dir / "a" / "SAME_public.xml"
            pretty = xml_dir / "b" / "SAME_public.xml"
            compact.write_text(
                '<Return><ReturnHeader><ReturnTypeCd code="x">990</ReturnTypeCd></ReturnHeader></Return>',
                encoding="utf-8",
            )
            pretty.write_text(
                """
                <Return>
                  <ReturnHeader>
                    <ReturnTypeCd code="x">
                      990
                    </ReturnTypeCd>
                  </ReturnHeader>
                </Return>
                """,
                encoding="utf-8",
            )

            conflict_csv = root / "conflicts.csv"
            summary = scan_xml_sources.run(
                args_for(root, xml_dir, analyze_conflicts=True, conflict_groups_csv=str(conflict_csv))
            )

            self.assertEqual(summary["conflict_equivalent_groups"], 1)
            self.assertEqual(summary["conflict_different_groups"], 0)
            csv_text = conflict_csv.read_text(encoding="utf-8")
            self.assertIn("canonical_equivalent", csv_text)

    def test_conflict_resolution_uses_loaded_relative_source_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            xml_dir = root / "xml"
            old_root = root / "old_xml"
            keep = xml_dir / "download990xml_2020_1" / "CONFLICT_public.xml"
            move = xml_dir / "2020_TEOS_XML_CT1" / "CONFLICT_public.xml"
            keep.parent.mkdir(parents=True)
            move.parent.mkdir(parents=True)
            keep.write_text(SAMPLE_XML, encoding="utf-8")
            move.write_text(DIFFERENT_XML, encoding="utf-8")

            main_db = root / "irs990.db"
            conn = sqlite3.connect(main_db)
            conn.execute(
                """
                CREATE TABLE returns (
                  filing_id TEXT PRIMARY KEY,
                  source_file TEXT,
                  ein TEXT,
                  return_type TEXT,
                  tax_year INTEGER,
                  return_ts TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO returns VALUES (?,?,?,?,?,?)",
                (
                    "CONFLICT_public",
                    str(old_root / "download990xml_2020_1" / "CONFLICT_public.xml"),
                    "123456789",
                    "990",
                    2020,
                    "2020-01-01T00:00:00",
                ),
            )
            conn.commit()
            conn.close()

            quarantine = root / "conflict_quarantine"
            resolution_csv = root / "resolution.csv"
            scan_xml_sources.run(
                args_for(
                    root,
                    xml_dir,
                    main_db=str(main_db),
                    conflict_resolution_csv=str(resolution_csv),
                    quarantine_resolved_conflicts=str(quarantine),
                    yes=True,
                )
            )

            self.assertTrue(keep.exists())
            self.assertFalse(move.exists())
            self.assertTrue((quarantine / "2020_TEOS_XML_CT1" / "CONFLICT_public.xml").exists())
            text = resolution_csv.read_text(encoding="utf-8")
            self.assertIn("loaded_relative_path", text)
            self.assertIn("quarantine_conflict", text)


if __name__ == "__main__":
    unittest.main()
