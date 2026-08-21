import re
import sqlite3
from pathlib import Path

import pytest

import rebuild_irs990_slim_clean as rebuild
from risk_source_identity import read_risk_source_identity


_SINGLETON_ROW_KEYS = (
    "irs990_root",
    "irs990_ez_root",
    "irs990_pf_root",
    "irs990_schedule_c_root",
    "irs990_ez_form990_total_assets_grp",
    "irs990_ez_sum_of_total_liabilities_grp",
    "irs990_pf_analysis_of_revenue_and_expenses",
    "irs990_pf_form990_pfbalance_sheets_grp",
    "return_header_all",
    "irs990_books_in_care_of_detail",
    "irs990_ez_books_in_care_of_detail",
)


def _fixture_extraction(conn: sqlite3.Connection, filing_id: str, marker: str):
    header_fields = (
        "filing_id", "source_file", "ein", "return_type", "tax_year", "period_end",
        "schema_version", "return_ts", "amended_return_ind", "org_name", "dba_name",
        "in_care_of_name", "us_address_line1", "us_address_line2", "city", "state", "zip",
        "foreign_address_line1", "foreign_city", "foreign_province", "foreign_country",
        "foreign_postal_code", "website",
    )
    header = dict.fromkeys(header_fields)
    header.update({
        "filing_id": filing_id,
        "source_file": f"{filing_id}.xml",
        "ein": "123456789",
        "return_type": "990PF",
        "tax_year": 2022,
        "period_end": "2022-12-31",
        "schema_version": "fixture",
        "return_ts": "2023-01-01T00:00:00Z",
        "org_name": marker,
    })

    core_fields = (
        "filing_id", "total_revenue", "total_expenses", "net_assets_boy", "net_assets_eoy",
        "contributions", "program_service_revenue", "membership_dues", "investment_income",
        "government_grants", "grants_paid", "lobbying_expense", "employees_count",
        "volunteers_count", "mission_desc",
    )
    core = dict.fromkeys(core_fields)
    core["filing_id"] = filing_id

    extracted = {"header": header, "core_hot": core}
    extracted.update({key: {} for key in _SINGLETON_ROW_KEYS})
    for table in rebuild.MULTIROW_CHILD_TABLES:
        payload_column = next(
            row[1]
            for row in conn.execute(f"PRAGMA table_info('{table}')")
            if row[1] not in {"id", "filing_id"}
        )
        extracted[table] = [{"filing_id": filing_id, payload_column: marker}]
    return extracted


def _install_fixture_loader(monkeypatch, current):
    monkeypatch.setattr(
        rebuild,
        "select_xml_files",
        lambda _dirs, _append, _conn: (
            ["fixture.xml"],
            {"total": 1, "selected": 1, "skipped_existing": 0, "skipped_duplicate_input": 0},
        ),
    )
    monkeypatch.setattr(rebuild, "extract_file", lambda _path: current["row"])


def _load_fixture(conn: sqlite3.Connection, tmp_path: Path):
    rebuild.load_data(
        conn,
        (tmp_path,),
        workers=1,
        chunksize=1,
        commit_every=1000,
        append_only=False,
    )


def _first_payload_value(conn: sqlite3.Connection, table: str):
    payload_column = next(
        row[1]
        for row in conn.execute(f"PRAGMA table_info('{table}')")
        if row[1] not in {"id", "filing_id"}
    )
    return conn.execute(f'SELECT "{payload_column}" FROM "{table}"').fetchone()[0]


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


def test_multirow_child_inventory_matches_autoincrement_filing_tables():
    conn = sqlite3.connect(":memory:")
    try:
        rebuild.build_schema(conn)
        discovered = set()
        for table, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL"
        ):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")}
            if "filing_id" in columns and "AUTOINCREMENT" in sql.upper():
                discovered.add(table)
    finally:
        conn.close()

    assert len(rebuild.MULTIROW_CHILD_TABLES) == 19
    assert len(set(rebuild.MULTIROW_CHILD_TABLES)) == 19
    assert set(rebuild.MULTIROW_CHILD_TABLES) == discovered


def test_fresh_build_identities_are_distinct_and_append_rotates_without_committing():
    first_conn = sqlite3.connect(":memory:")
    second_conn = sqlite3.connect(":memory:")
    try:
        first = rebuild.prepare_risk_source_identity(first_conn, append_only=False)
        second = rebuild.prepare_risk_source_identity(second_conn, append_only=False)
        assert first.database_id != second.database_id
        assert first.revision_id != second.revision_id

        first_conn.commit()
        appended = rebuild.prepare_risk_source_identity(first_conn, append_only=True)
        assert appended.database_id == first.database_id
        assert appended.revision_id != first.revision_id
        assert first_conn.in_transaction is True

        first_conn.rollback()
        restored = read_risk_source_identity(first_conn, required=True)
        assert restored is not None
        assert restored.database_id == first.database_id
        assert restored.revision_id == first.revision_id
    finally:
        first_conn.close()
        second_conn.close()


def test_reprocess_replaces_every_child_but_fresh_load_does_not_delete(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    rebuild.build_schema(conn)
    current = {"row": _fixture_extraction(conn, "fixture_public", "old")}
    _install_fixture_loader(monkeypatch, current)
    statements = []
    conn.set_trace_callback(statements.append)

    try:
        _load_fixture(conn, tmp_path)

        assert not any(statement.lstrip().upper().startswith("DELETE FROM") for statement in statements)
        assert not any(statement.lstrip().upper().startswith("SAVEPOINT") for statement in statements)
        assert conn.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 1
        for table in rebuild.MULTIROW_CHILD_TABLES:
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 1
            assert _first_payload_value(conn, table) == "old"

        statements.clear()
        current["row"] = _fixture_extraction(conn, "fixture_public", "new")
        _load_fixture(conn, tmp_path)

        deleted_tables = {
            match.group(1)
            for statement in statements
            if (match := re.match(r'^DELETE FROM "([^"]+)"', statement.lstrip(), re.IGNORECASE))
        }
        assert deleted_tables == set(rebuild.MULTIROW_CHILD_TABLES)
        assert any(statement.lstrip().upper().startswith("SAVEPOINT") for statement in statements)
        assert any(statement.lstrip().upper().startswith("RELEASE") for statement in statements)
        assert conn.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 1
        assert conn.execute("SELECT org_name FROM returns").fetchone()[0] == "new"
        for table in rebuild.MULTIROW_CHILD_TABLES:
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 1
            assert _first_payload_value(conn, table) == "new"
    finally:
        conn.close()


def test_reprocess_failure_rolls_back_parent_and_all_children(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    rebuild.build_schema(conn)
    current = {"row": _fixture_extraction(conn, "fixture_public", "old")}
    _install_fixture_loader(monkeypatch, current)

    try:
        _load_fixture(conn, tmp_path)
        conn.executescript("""
            CREATE TRIGGER fail_late_child_insert
            BEFORE INSERT ON irs990_schedule_r_unrelated_org_txbl_partnership_grp
            WHEN NEW.ein = 'new'
            BEGIN
              SELECT RAISE(ABORT, 'forced child insert failure');
            END;
        """)
        current["row"] = _fixture_extraction(conn, "fixture_public", "new")
        statements = []
        conn.set_trace_callback(statements.append)

        with pytest.raises(sqlite3.IntegrityError, match="forced child insert failure"):
            _load_fixture(conn, tmp_path)

        assert any(statement.lstrip().upper().startswith("ROLLBACK TO") for statement in statements)
        assert conn.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 1
        assert conn.execute("SELECT org_name FROM returns").fetchone()[0] == "old"
        for table in rebuild.MULTIROW_CHILD_TABLES:
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 1
            assert _first_payload_value(conn, table) == "old"
    finally:
        conn.close()
