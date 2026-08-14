import sqlite3
from pathlib import Path

import rebuild_irs990_slim_clean as rebuild


def test_select_xml_files_combines_roots_and_deduplicates_across_them(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "111_public.xml").write_text("<Return />", encoding="utf-8")
    (second / "111_private.xml").write_text("<Return />", encoding="utf-8")
    (second / "222_public.xml").write_text("<Return />", encoding="utf-8")
    (second / "333_public.xml").write_text("<Return />", encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE returns (filing_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO returns VALUES ('333_public')")

    selected, stats = rebuild.select_xml_files((first, second), True, conn)

    assert {Path(path).stem for path in selected} == {"111_public", "222_public"}
    assert stats == {
        "total": 4,
        "selected": 2,
        "skipped_existing": 1,
        "skipped_duplicate_input": 1,
    }


def test_parser_accepts_repeated_xml_directories(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    args = rebuild.parse_args(
        [
            "--db", str(tmp_path / "fixture.db"),
            "--xml-dir", str(first),
            "--xml-dir", str(second),
            "--append",
        ]
    )

    assert args.xml_dir == [str(first), str(second)]
    assert args.append is True


def test_preflight_requires_one_report_root(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    result = rebuild.main(
        [
            "--xml-dir", str(first),
            "--xml-dir", str(second),
            "--preflight",
        ]
    )

    assert result == 2
