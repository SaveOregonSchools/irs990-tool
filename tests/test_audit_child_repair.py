import csv
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import audit_child_repair as audit
from rebuild_irs990_slim_clean import MULTIROW_CHILD_TABLES


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_fixture_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE returns (
          filing_id TEXT PRIMARY KEY,
          source_file TEXT,
          ein TEXT,
          tax_year INTEGER,
          return_type TEXT,
          org_name TEXT
        );
        CREATE TABLE core_hot (
          filing_id TEXT PRIMARY KEY,
          grants_paid NUMERIC
        );
        """
    )
    for number, table in enumerate(MULTIROW_CHILD_TABLES):
        if table == "grants":
            conn.execute(
                f"""
                CREATE TABLE "{table}" (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  filing_id TEXT,
                  cash_grant_amt NUMERIC,
                  non_cash_assistance_amt NUMERIC,
                  payload TEXT
                )
                """
            )
        else:
            conn.execute(
                f"""
                CREATE TABLE "{table}" (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  filing_id TEXT,
                  payload TEXT
                )
                """
            )
        conn.execute(f'CREATE INDEX "idx_fixture_{number}" ON "{table}"(filing_id)')
    conn.commit()
    conn.close()


def add_return(conn: sqlite3.Connection, filing_id: str, grants_paid=None) -> None:
    conn.execute(
        "INSERT INTO returns VALUES (?,?,?,?,?,?)",
        (
            filing_id,
            f"xml/{filing_id}.xml",
            "123456789",
            2024,
            "990",
            "Fixture Organization",
        ),
    )
    conn.execute("INSERT INTO core_hot VALUES (?,?)", (filing_id, grants_paid))


def report_paths(root: Path, label: str):
    return (
        root / f"{label}-summary.csv",
        root / f"{label}-detail.csv",
        root / f"{label}-detail.json",
    )


def run_fixture_audit(
    source: Path,
    repaired: Path,
    root: Path,
    label: str,
    *,
    fail_on_new: bool = False,
):
    summary, detail, detail_json = report_paths(root, label)
    code = audit.run_audit(
        source,
        repaired,
        summary,
        detail,
        detail_json,
        fail_on_new=fail_on_new,
    )
    return code, summary, detail, detail_json


def test_expected_exact_replay_cleanup_passes_and_reconciles_grants(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)

    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, "BASE", grants_paid=1)
        for table in MULTIROW_CHILD_TABLES:
            if table == "grants":
                conn.execute(
                    "INSERT INTO grants(filing_id,cash_grant_amt,non_cash_assistance_amt,payload) "
                    "VALUES ('BASE',1,0,'same')"
                )
            else:
                conn.execute(
                    f'INSERT INTO "{table}"(filing_id,payload) VALUES (\'BASE\',\'same\')'
                )
        add_return(conn, "REPLAY", grants_paid=15000)
        conn.commit()
        conn.close()

    source_conn = sqlite3.connect(source)
    source_conn.executemany(
        "INSERT INTO grants(filing_id,cash_grant_amt,non_cash_assistance_amt,payload) "
        "VALUES ('REPLAY',15000,0,'recipient-a')",
        [(), ()],
    )
    source_conn.commit()
    source_conn.close()
    repaired_conn = sqlite3.connect(repaired)
    repaired_conn.execute(
        "INSERT INTO grants(filing_id,cash_grant_amt,non_cash_assistance_amt,payload) "
        "VALUES ('REPLAY',15000,0,'recipient-a')"
    )
    repaired_conn.commit()
    repaired_conn.close()

    before = {source: file_hash(source), repaired: file_hash(repaired)}
    code, summary_path, detail_path, json_path = run_fixture_audit(
        source, repaired, tmp_path, "replay"
    )

    assert code == 0
    assert {source: file_hash(source), repaired: file_hash(repaired)} == before
    assert not Path(str(source) + "-wal").exists()
    assert not Path(str(repaired) + "-wal").exists()

    with summary_path.open(newline="", encoding="utf-8") as fh:
        summaries = list(csv.DictReader(fh))
    assert len(summaries) == len(MULTIROW_CHILD_TABLES) + 2
    returns_summary = next(row for row in summaries if row["table_name"] == "returns")
    assert returns_summary["source_rows"] == "2"
    assert returns_summary["repaired_rows"] == "2"
    assert returns_summary["source_file_covered_rows"] == "2"
    assert returns_summary["source_object_covered_filings"] == "2"
    assert returns_summary["source_distinct_files"] == "2"
    assert returns_summary["source_distinct_objects"] == "2"
    grant_summary = next(row for row in summaries if row["table_name"] == "grants")
    assert grant_summary["mismatched_filings"] == "1"
    assert grant_summary["expected_exact_replay_cleanup"] == "1"
    assert grant_summary["gate_failures"] == "0"
    assert grant_summary["grant_source_inflated"] == "1"
    assert grant_summary["grant_repaired_inflated"] == "0"

    with detail_path.open(newline="", encoding="utf-8") as fh:
        details = list(csv.DictReader(fh))
    assert len(details) == 1
    detail = details[0]
    assert detail["classification"] == "expected_exact_replay_cleanup"
    assert detail["source_count"] == "2"
    assert detail["repaired_count"] == "1"
    assert detail["whole_set_replay_factor"] == "2"
    assert detail["source_grant_detail_total"] == "30000"
    assert detail["repaired_grant_detail_total"] == "15000"
    assert detail["source_grant_inflated"] == "1"
    assert detail["repaired_grant_inflated"] == "0"

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["gates"]["passed"] is True
    assert payload["metadata"]["source"]["query_only"] == 1
    assert payload["metadata"]["repaired"]["query_only"] == 1


def test_missing_and_content_changed_are_hard_failures_but_new_is_reported(
    tmp_path: Path,
):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        for filing_id in ("CONTENT", "MISSING", "NEW"):
            add_return(conn, filing_id)
        conn.commit()
        conn.close()

    source_conn = sqlite3.connect(source)
    source_conn.executemany(
        "INSERT INTO officers(filing_id,payload) VALUES ('CONTENT',?)",
        [("a",), ("a",)],
    )
    source_conn.execute(
        "INSERT INTO former_key_people(filing_id,payload) VALUES ('MISSING','old')"
    )
    source_conn.commit()
    source_conn.close()

    repaired_conn = sqlite3.connect(repaired)
    repaired_conn.executemany(
        "INSERT INTO officers(filing_id,payload) VALUES ('CONTENT',?)",
        [("a",), ("a",), ("b",)],
    )
    repaired_conn.execute(
        "INSERT INTO highest_comp_employees(filing_id,payload) VALUES ('NEW','new')"
    )
    repaired_conn.commit()
    repaired_conn.close()

    code, _summary, detail_path, json_path = run_fixture_audit(
        source, repaired, tmp_path, "hard-failures"
    )
    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        details = {row["filing_id"]: row for row in csv.DictReader(fh)}
    assert details["CONTENT"]["classification"] == "content_changed"
    assert details["CONTENT"]["gate_failure"] == "1"
    assert details["MISSING"]["classification"] == "missing_in_rebuild"
    assert details["MISSING"]["gate_failure"] == "1"
    assert details["NEW"]["classification"] == "new_in_rebuild"
    assert details["NEW"]["gate_failure"] == "0"
    assert json.loads(json_path.read_text(encoding="utf-8"))["gates"]["passed"] is False


def test_same_count_changed_payload_is_detected_and_gates(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    for path, payloads in (
        (source, ("alpha", "beta")),
        (repaired, ("alpha", "changed")),
    ):
        conn = sqlite3.connect(path)
        add_return(conn, "SAMECOUNT")
        conn.executemany(
            "INSERT INTO officers(filing_id,payload) VALUES ('SAMECOUNT',?)",
            [(payload,) for payload in payloads],
        )
        conn.commit()
        conn.close()

    code, summary_path, detail_path, _json = run_fixture_audit(
        source, repaired, tmp_path, "same-count-content"
    )
    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(
            row
            for row in csv.DictReader(fh)
            if row["table_name"] == "officers"
        )
    assert detail["classification"] == "content_changed"
    assert detail["source_count"] == detail["repaired_count"] == "2"
    assert detail["source_payload_digest"] != detail["repaired_payload_digest"]
    assert detail["gate_failure"] == "1"
    with summary_path.open(newline="", encoding="utf-8") as fh:
        officers = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "officers"
        )
    assert officers["mismatched_filings"] == "1"
    assert officers["content_changed"] == "1"


def test_same_payload_multiset_in_different_order_passes(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    for path, payloads in (
        (source, ("alpha", "beta", "alpha")),
        (repaired, ("beta", "alpha", "alpha")),
    ):
        conn = sqlite3.connect(path)
        add_return(conn, "REORDERED")
        conn.executemany(
            "INSERT INTO officers(filing_id,payload) VALUES ('REORDERED',?)",
            [(payload,) for payload in payloads],
        )
        conn.commit()
        conn.close()

    code, _summary, detail_path, _json = run_fixture_audit(
        source, repaired, tmp_path, "reordered"
    )
    assert code == 0
    with detail_path.open(newline="", encoding="utf-8") as fh:
        assert list(csv.DictReader(fh)) == []


def test_missing_return_is_detected_without_a_child_count_difference(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    conn = sqlite3.connect(source)
    add_return(conn, "MISSING_RETURN")
    conn.commit()
    conn.close()

    code, summary_path, detail_path, _json = run_fixture_audit(
        source, repaired, tmp_path, "missing-return"
    )
    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(
            row
            for row in csv.DictReader(fh)
            if row["table_name"] == "returns"
        )
    assert detail["filing_id"] == "MISSING_RETURN"
    assert detail["classification"] == "missing_in_rebuild"
    assert detail["source_count"] == "1"
    assert detail["repaired_count"] == "0"
    assert detail["gate_failure"] == "1"
    with summary_path.open(newline="", encoding="utf-8") as fh:
        returns = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "returns"
        )
    assert returns["source_rows"] == "1"
    assert returns["repaired_rows"] == "0"
    assert returns["source_object_covered_filings"] == "1"
    assert returns["missing_in_rebuild"] == "1"


def test_changed_source_file_and_invalid_object_coverage_gate(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, "PROVENANCE")
        conn.commit()
        conn.close()
    conn = sqlite3.connect(repaired)
    conn.execute(
        "UPDATE returns SET source_file='xml/OTHER_OBJECT.xml' "
        "WHERE filing_id='PROVENANCE'"
    )
    conn.commit()
    conn.close()

    code, summary_path, detail_path, _json = run_fixture_audit(
        source, repaired, tmp_path, "object-coverage"
    )
    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(
            row
            for row in csv.DictReader(fh)
            if row["table_name"] == "returns"
        )
    assert detail["classification"] == "unexplained"
    assert "source_file_object_mismatch" in detail["notes"]
    with summary_path.open(newline="", encoding="utf-8") as fh:
        returns = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "returns"
        )
    assert returns["source_object_covered_filings"] == "1"
    assert returns["repaired_object_covered_filings"] == "0"


def test_new_rows_can_be_made_a_gate_with_fail_on_new(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, "NEW")
        conn.commit()
        conn.close()
    conn = sqlite3.connect(repaired)
    conn.execute("INSERT INTO officers(filing_id,payload) VALUES ('NEW','new')")
    conn.commit()
    conn.close()

    default_code, *_ = run_fixture_audit(source, repaired, tmp_path, "new-default")
    strict_code, _summary, detail_path, _json = run_fixture_audit(
        source, repaired, tmp_path, "new-strict", fail_on_new=True
    )
    assert default_code == 0
    assert strict_code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(csv.DictReader(fh))
    assert detail["classification"] == "new_in_rebuild"
    assert detail["gate_failure"] == "1"


def test_missing_filing_index_is_unexplained_and_fails_closed(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    conn = sqlite3.connect(repaired)
    index_name = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='officers'"
    ).fetchone()[0]
    conn.execute(f'DROP INDEX "{index_name}"')
    conn.commit()
    conn.close()

    code, summary_path, detail_path, _json = run_fixture_audit(
        source, repaired, tmp_path, "missing-index"
    )
    assert code == 2
    with summary_path.open(newline="", encoding="utf-8") as fh:
        summary = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "officers"
        )
    assert summary["status"] == "failed"
    assert summary["unexplained"] == "1"
    with detail_path.open(newline="", encoding="utf-8") as fh:
        details = list(csv.DictReader(fh))
    row = next(item for item in details if item["table_name"] == "officers")
    assert row["classification"] == "unexplained"
    assert "no index led by filing_id" in row["notes"]


def test_readonly_connection_rejects_writes(tmp_path: Path):
    database = tmp_path / "readonly.db"
    create_fixture_db(database)
    before = file_hash(database)
    conn = audit.connect_readonly(database)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO returns(filing_id) VALUES ('NOPE')")
    finally:
        conn.close()
    assert file_hash(database) == before


def test_report_path_cannot_overwrite_either_database_or_companion(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    safe_outputs = [
        tmp_path / "summary.csv",
        tmp_path / "detail.csv",
        tmp_path / "detail.json",
    ]
    for database in (source, repaired):
        for suffix in ("", "-wal", "-shm", "-journal"):
            protected = Path(str(database) + suffix)
            for output_position in range(3):
                outputs = list(safe_outputs)
                outputs[output_position] = protected
                with pytest.raises(
                    ValueError, match="database or SQLite companion"
                ):
                    audit.validate_output_paths(
                        source,
                        repaired,
                        outputs[0],
                        outputs[1],
                        outputs[2],
                    )
