import csv
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest

import audit_child_repair as audit
import rebuild_irs990_slim_clean as rebuild
from rebuild_irs990_slim_clean import MULTIROW_CHILD_TABLES


MANIFEST_SCAN_ID = "fixture-completed-scan"
MANIFEST_SCANNED_AT = "2026-08-15T00:00:00+00:00"


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


def add_return(
    conn: sqlite3.Connection,
    filing_id: str,
    grants_paid=None,
    *,
    return_type: str = "990",
) -> None:
    conn.execute(
        "INSERT INTO returns VALUES (?,?,?,?,?,?)",
        (
            filing_id,
            f"xml/{filing_id}.xml",
            "123456789",
            2024,
            return_type,
            "Fixture Organization",
        ),
    )
    conn.execute("INSERT INTO core_hot VALUES (?,?)", (filing_id, grants_paid))


def replace_pf_officer_table(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        DROP TABLE irs990_pf_officer_dir_trst_key_empl_info_grp;
        CREATE TABLE irs990_pf_officer_dir_trst_key_empl_info_grp (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          filing_id TEXT NOT NULL,
          person_nm TEXT,
          title_txt TEXT,
          average_hrs_per_wk_devoted_to_pos_rt TEXT,
          compensation_amt NUMERIC,
          employee_benefits_amt NUMERIC,
          expense_account_amt NUMERIC
        );
        CREATE INDEX idx_fixture_pf_officer
        ON irs990_pf_officer_dir_trst_key_empl_info_grp(filing_id);
        """
    )
    conn.commit()
    conn.close()


def replace_grants_table(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE grants")
    definitions = []
    for column in audit.GRANT_PAYLOAD_COLUMNS:
        affinity = "NUMERIC" if column in audit.GRANT_NUMERIC_COLUMNS else "TEXT"
        definitions.append(f'"{column}" {affinity}')
    conn.execute(
        "CREATE TABLE grants (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "filing_id TEXT NOT NULL, "
        + ",".join(definitions)
        + ")"
    )
    conn.execute("CREATE INDEX idx_fixture_grants ON grants(filing_id)")
    conn.commit()
    conn.close()


def replace_schedule_c_supplemental_table(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        DROP TABLE irs990_schedule_c_supplemental_info;
        CREATE TABLE irs990_schedule_c_supplemental_info (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          filing_id TEXT NOT NULL,
          form_and_line_reference_desc TEXT,
          explanation_txt TEXT
        );
        CREATE INDEX idx_fixture_schedule_c_supplemental
        ON irs990_schedule_c_supplemental_info(filing_id);
        """
    )
    conn.commit()
    conn.close()


def set_return_source(path: Path, filing_id: str, source_file: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE returns SET source_file=? WHERE filing_id=?",
        (str(source_file), filing_id),
    )
    conn.commit()
    conn.close()


def write_selected_xml(
    xml_root: Path,
    filing_id: str,
    body: str,
    *,
    return_type: str = "990PF",
    ein: str = "123456789",
    tax_year: int = 2024,
) -> Path:
    archive = xml_root / "fixture_archive"
    archive.mkdir(parents=True, exist_ok=True)
    path = archive / f"{filing_id}.xml"
    path.write_text(
        f"""
<Return>
  <ReturnHeader>
    <ReturnTypeCd>{return_type}</ReturnTypeCd>
    <TaxYr>{tax_year}</TaxYr>
    <Filer><EIN>{ein}</EIN></Filer>
  </ReturnHeader>
  <ReturnData>
"""
        + body
        + """
  </ReturnData>
</Return>
""",
        encoding="utf-8",
    )
    return path


def manifest_source_row(
    xml_root: Path,
    relative_path: str,
    filing_id: str,
    **overrides,
):
    candidate = xml_root.joinpath(*relative_path.split("/"))
    stat = candidate.stat()
    row = {
        "scan_id": MANIFEST_SCAN_ID,
        "source_file": relative_path,
        "relative_path": relative_path,
        "filing_id": filing_id,
        "object_id": rebuild.object_id_from_filing_id(filing_id),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "duplicate_status": "unique",
        "keep_source_file": None,
        "quarantine_status": None,
        "quarantine_file": None,
    }
    row.update(overrides)
    return row


def create_provenance_manifest(
    path: Path,
    source_rows,
    *,
    filing_id: str,
    loaded_source_file: str,
    ein="123456789",
    tax_year=2024,
    return_type="990",
    imported_at: str = MANIFEST_SCANNED_AT,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE scan_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE source_files (
              source_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
              scan_id TEXT NOT NULL,
              source_file TEXT NOT NULL UNIQUE,
              relative_path TEXT NOT NULL,
              filing_id TEXT NOT NULL,
              object_id TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              mtime_ns INTEGER NOT NULL,
              sha256 TEXT,
              duplicate_status TEXT NOT NULL,
              keep_source_file TEXT,
              quarantine_status TEXT,
              quarantine_file TEXT
            );
            CREATE TABLE loaded_filings (
              filing_id TEXT PRIMARY KEY,
              object_id TEXT NOT NULL,
              source_file TEXT,
              ein TEXT,
              tax_year INTEGER,
              return_type TEXT,
              imported_at TEXT NOT NULL
            );
            CREATE INDEX idx_manifest_source_object ON source_files(object_id);
            CREATE INDEX idx_manifest_loaded_object ON loaded_filings(object_id);
            """
        )
        conn.executemany(
            "INSERT INTO scan_meta VALUES (?,?)",
            [
                ("last_scan_id", MANIFEST_SCAN_ID),
                ("last_scanned_at", MANIFEST_SCANNED_AT),
                ("path_format", rebuild.MANIFEST_PATH_FORMAT),
            ],
        )
        source_columns = tuple(source_rows[0])
        conn.executemany(
            f"INSERT INTO source_files ({','.join(source_columns)}) "
            f"VALUES ({','.join('?' for _ in source_columns)})",
            [[row[column] for column in source_columns] for row in source_rows],
        )
        conn.execute(
            """
            INSERT INTO loaded_filings
              (filing_id,object_id,source_file,ein,tax_year,return_type,imported_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                filing_id,
                rebuild.object_id_from_filing_id(filing_id),
                loaded_source_file,
                ein,
                tax_year,
                return_type,
                imported_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def insert_manifest_source(conn: sqlite3.Connection, row) -> None:
    columns = tuple(row)
    conn.execute(
        f"INSERT INTO source_files ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )


def create_manifest_provenance_fixture(
    tmp_path: Path,
    *,
    label: str = "manifest",
    filing_id: str = "202431099349100118_public",
    xml_ein: str = "123456789",
    xml_tax_year: int = 2024,
    xml_return_type: str = "990",
):
    source = tmp_path / f"{label}-source.db"
    repaired = tmp_path / f"{label}-repaired.db"
    manifest = tmp_path / f"{label}-sources.db"
    xml_root = tmp_path / f"{label}-xml"
    create_fixture_db(source)
    create_fixture_db(repaired)
    for database in (source, repaired):
        conn = sqlite3.connect(database)
        add_return(conn, filing_id, return_type=xml_return_type)
        conn.commit()
        conn.close()
    historical = rf"C:\IRSData\Found\2019\{filing_id}.xml"
    conn = sqlite3.connect(source)
    conn.execute(
        "UPDATE returns SET source_file=? WHERE filing_id=?",
        (historical, filing_id),
    )
    conn.commit()
    conn.close()
    selected = write_selected_xml(
        xml_root,
        filing_id,
        "",
        return_type=xml_return_type,
        ein=xml_ein,
        tax_year=xml_tax_year,
    )
    set_return_source(repaired, filing_id, selected)
    relative = selected.relative_to(xml_root).as_posix()
    create_provenance_manifest(
        manifest,
        [manifest_source_row(xml_root, relative, filing_id)],
        filing_id=filing_id,
        loaded_source_file=historical,
        return_type=xml_return_type,
    )
    return source, repaired, manifest, xml_root, selected, historical, filing_id


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
    detail_limit_per_table: int = audit.DEFAULT_DETAIL_LIMIT_PER_TABLE,
    detail_limit_total: int = audit.DEFAULT_DETAIL_LIMIT_TOTAL,
    allow_verified_extractor_enrichments: bool = False,
    extractor_enrichment_xml_root: Path | None = None,
    source_manifest_db: Path | None = None,
    manifest_xml_root: Path | None = None,
):
    summary, detail, detail_json = report_paths(root, label)
    code = audit.run_audit(
        source,
        repaired,
        summary,
        detail,
        detail_json,
        fail_on_new=fail_on_new,
        detail_limit_per_table=detail_limit_per_table,
        detail_limit_total=detail_limit_total,
        allow_verified_extractor_enrichments=allow_verified_extractor_enrichments,
        extractor_enrichment_xml_root=extractor_enrichment_xml_root,
        source_manifest_db=source_manifest_db,
        manifest_xml_root=manifest_xml_root,
    )
    return code, summary, detail, detail_json


def assert_manifest_provenance_fails(
    source: Path,
    repaired: Path,
    manifest: Path,
    xml_root: Path,
    tmp_path: Path,
    label: str,
    expected_note: str,
) -> None:
    code, _summary, detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        label,
        source_manifest_db=manifest,
        manifest_xml_root=xml_root,
    )
    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "returns"
        )
    assert detail["classification"] == "unexplained"
    assert expected_note in detail["notes"]


def test_directional_matcher_accepts_one_null_to_zero_row():
    columns = ["cash_grant_amt", "payload"]
    source_key = audit.mapping_payload_key(
        {"cash_grant_amt": None, "payload": "recipient"},
        columns,
    )
    repaired_key = audit.mapping_payload_key(
        {"cash_grant_amt": 0, "payload": "recipient"},
        columns,
    )

    result = audit.verify_directional_payload_transform(
        Counter({source_key: 1}),
        Counter({repaired_key: 1}),
        audit.grant_null_to_zero_compatibility,
    )

    assert result.verified is True
    assert result.enriched_rows == 1


def test_cash_zero_with_explicit_noncash_zero_is_not_a_blank_grant():
    payload = audit.mapping_payload_key(
        {
            "filer_ein": "123456789",
            "cash_grant_amt": 0,
            "non_cash_assistance_amt": 0,
            "purpose_of_grant_txt": None,
        },
        [
            "filer_ein",
            "cash_grant_amt",
            "non_cash_assistance_amt",
            "purpose_of_grant_txt",
        ],
    )

    assert audit.is_zero_only_blank_grant(audit.decode_payload_key(payload)) is False


@pytest.mark.parametrize(
    ("body", "expected_count"),
    [
        ("<RecipientTable><CashGrantAmt>0</CashGrantAmt></RecipientTable>", 1),
        (
            "<RecipientTable><CashGrantAmt>0</CashGrantAmt>"
            "<NonCashAssistanceAmt>0</NonCashAssistanceAmt></RecipientTable>",
            0,
        ),
        (
            "<RecipientTable><CashGrantAmt>0</CashGrantAmt>"
            "<PurposeOfGrantTxt>Education</PurposeOfGrantTxt></RecipientTable>",
            0,
        ),
        (
            "<GrantOrContributionPdDurYrGrp><Amt>0</Amt>"
            "</GrantOrContributionPdDurYrGrp>",
            0,
        ),
        ("<RecipientTable />", 0),
    ],
)
def test_strict_zero_only_placeholder_is_recipient_scoped_and_fail_closed(
    body: str,
    expected_count: int,
):
    root = ET.fromstring(f"<Return>{body}</Return>")

    placeholders = audit.strict_zero_only_recipient_placeholder_counter(
        root,
        {"ein": "123456789", "org_name": "Fixture Organization"},
    )

    assert sum(placeholders.values()) == expected_count
    if expected_count:
        values = audit.decode_payload_key(next(iter(placeholders)))
        assert tuple(values) == audit.GRANT_PAYLOAD_COLUMNS
        assert values["filer_ein"] == ("text", "123456789")
        assert values["filer_name"] == ("text", "Fixture Organization")
        assert audit.token_is_numeric_zero(values["cash_grant_amt"])


def test_selected_xml_verifier_rejects_path_outside_explicit_root(tmp_path: Path):
    xml_root = tmp_path / "xml-root"
    xml_root.mkdir()
    outside = tmp_path / "OUTSIDE_public.xml"
    outside.write_text("<Return />", encoding="utf-8")

    selected, reason = audit.resolve_selected_xml_path(
        outside,
        "OUTSIDE_public",
        xml_root.resolve(),
    )

    assert selected is None
    assert "outside the verified XML root" in reason


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

    xml_root = tmp_path / "xml-root-for-replay-opt-in"
    xml_root.mkdir()
    opt_code, _summary, opt_detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "replay-with-enrichment-opt-in",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )
    assert opt_code == 0
    with opt_detail_path.open(newline="", encoding="utf-8") as fh:
        opt_detail = next(csv.DictReader(fh))
    assert opt_detail["table_name"] == "grants"
    assert opt_detail["classification"] == "expected_exact_replay_cleanup"
    assert opt_detail["gate_failure"] == "0"


def test_grant_null_to_zero_with_replay_requires_explicit_enrichment_opt_in(
    tmp_path: Path,
):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    xml_root = tmp_path / "xml"
    xml_root.mkdir()
    create_fixture_db(source)
    create_fixture_db(repaired)
    replace_grants_table(source)
    replace_grants_table(repaired)
    filing_id = "GRANT_ENRICH_public"
    selected_xml = write_selected_xml(
        xml_root,
        filing_id,
        """
    <IRS990>
      <GrantOrContributionPdDurYrGrp>
        <RecipientEIN>987654321</RecipientEIN>
        <Amt>0</Amt>
        <PurposeOfGrantTxt>Education</PurposeOfGrantTxt>
      </GrantOrContributionPdDurYrGrp>
    </IRS990>
""",
        return_type="990",
    )
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, filing_id, grants_paid=0)
        conn.commit()
        conn.close()
        set_return_source(path, filing_id, selected_xml)
    conn = sqlite3.connect(source)
    conn.executemany(
        "INSERT INTO grants(filing_id,filer_ein,recipient_ein,cash_grant_amt,"
        "purpose_of_grant_txt) VALUES (?,?,?,NULL,?)",
        [
            (filing_id, "123456789", "987654321", "Education"),
            (filing_id, "123456789", "987654321", "Education"),
        ],
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(repaired)
    conn.execute(
        "INSERT INTO grants(filing_id,filer_ein,recipient_ein,cash_grant_amt,"
        "purpose_of_grant_txt) VALUES (?,?,?,?,?)",
        (filing_id, "123456789", "987654321", 0, "Education"),
    )
    conn.commit()
    conn.close()

    default_code, *_ = run_fixture_audit(
        source, repaired, tmp_path, "grant-enrichment-default"
    )
    allowed_code, summary_path, detail_path, json_path = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "grant-enrichment-allowed",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )

    assert default_code == 2
    assert allowed_code == 0
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(csv.DictReader(fh))
    assert detail["classification"] == "verified_extractor_enrichment"
    assert detail["gate_failure"] == "0"
    assert detail["exact_extra_rows"] == "1"
    assert "grants_cash_null_to_zero" in detail["notes"]
    with summary_path.open(newline="", encoding="utf-8") as fh:
        grants = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "grants"
        )
    assert grants["verified_extractor_enrichment"] == "1"
    assert grants["source_extra_rows"] == "1"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["gates"]["verified_extractor_enrichments_enabled"] is True

    selected_xml.write_text(
        selected_xml.read_text(encoding="utf-8").replace(
            "<PurposeOfGrantTxt>Education</PurposeOfGrantTxt>",
            "<PurposeOfGrantTxt>Healthcare</PurposeOfGrantTxt>",
        ),
        encoding="utf-8",
    )
    forged_code, _summary, forged_detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "grant-enrichment-forged-xml",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )
    assert forged_code == 2
    with forged_detail_path.open(newline="", encoding="utf-8") as fh:
        forged_detail = next(csv.DictReader(fh))
    assert "full repaired grant multiset differs" in forged_detail["notes"]


def test_zero_only_recipient_placeholder_removal_requires_exact_selected_xml(
    tmp_path: Path,
):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    xml_root = tmp_path / "xml"
    create_fixture_db(source)
    create_fixture_db(repaired)
    replace_grants_table(source)
    replace_grants_table(repaired)
    filing_id = "ZERO_ONLY_PLACEHOLDER_public"
    selected_xml = write_selected_xml(
        xml_root,
        filing_id,
        """
    <IRS990ScheduleI>
      <RecipientTable>
        <RecipientEIN>987654321</RecipientEIN>
        <CashGrantAmt>100</CashGrantAmt>
        <PurposeOfGrantTxt>Education</PurposeOfGrantTxt>
      </RecipientTable>
      <RecipientTable>
        <CashGrantAmt>0</CashGrantAmt>
      </RecipientTable>
    </IRS990ScheduleI>
""",
        return_type="990",
    )
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, filing_id, grants_paid=100)
        conn.commit()
        conn.close()
        set_return_source(path, filing_id, selected_xml)
    source_conn = sqlite3.connect(source)
    source_conn.executemany(
        "INSERT INTO grants(filing_id,filer_ein,recipient_ein,cash_grant_amt,"
        "purpose_of_grant_txt) VALUES (?,?,?,?,?)",
        [
            (filing_id, "123456789", "987654321", 100, "Education"),
            (filing_id, "123456789", None, 0, None),
        ],
    )
    source_conn.commit()
    source_conn.close()
    repaired_conn = sqlite3.connect(repaired)
    repaired_conn.execute(
        "INSERT INTO grants(filing_id,filer_ein,recipient_ein,cash_grant_amt,"
        "purpose_of_grant_txt) VALUES (?,?,?,?,?)",
        (filing_id, "123456789", "987654321", 100, "Education"),
    )
    repaired_conn.commit()
    repaired_conn.close()

    default_code, *_ = run_fixture_audit(
        source, repaired, tmp_path, "zero-only-placeholder-default"
    )
    allowed_code, summary_path, detail_path, json_path = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "zero-only-placeholder-verified",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )

    assert default_code == 2
    assert allowed_code == 0
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(csv.DictReader(fh))
    assert detail["classification"] == "verified_zero_only_placeholder_removal"
    assert detail["gate_failure"] == "0"
    assert detail["exact_extra_rows"] == "1"
    assert "grants_zero_only_recipient_placeholder_removal" in detail["notes"]
    with summary_path.open(newline="", encoding="utf-8") as fh:
        grants = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "grants"
        )
    assert grants["verified_zero_only_placeholder_removal"] == "1"
    assert grants["verified_extractor_enrichment"] == "0"
    assert grants["source_extra_rows"] == "1"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert (
        "grants_strict_zero_only_recipient_placeholder_removal_with_exact_"
        "source_delta_and_selected_xml_proof"
        in payload["metadata"]["verified_extractor_enrichments"]["policies"]
    )

    selected_xml.write_text(
        selected_xml.read_text(encoding="utf-8").replace(
            "<CashGrantAmt>0</CashGrantAmt>",
            "<!-- zero-only placeholder removed -->",
        ),
        encoding="utf-8",
    )
    forged_code, _summary, forged_detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "zero-only-placeholder-forged-xml",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )
    assert forged_code == 2
    with forged_detail_path.open(newline="", encoding="utf-8") as fh:
        forged_detail = next(csv.DictReader(fh))
    assert forged_detail["classification"] == "unexplained"
    assert "do not exactly match strict zero-only RecipientTable" in forged_detail["notes"]


def test_zero_only_blank_grant_is_never_an_allowed_enrichment(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    xml_root = tmp_path / "xml"
    xml_root.mkdir()
    create_fixture_db(source)
    create_fixture_db(repaired)
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, "BLANK_GRANT_public", grants_paid=0)
        conn.commit()
        conn.close()
    conn = sqlite3.connect(source)
    conn.execute(
        "INSERT INTO grants(filing_id,cash_grant_amt,non_cash_assistance_amt,payload) "
        "VALUES ('BLANK_GRANT_public',NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(repaired)
    conn.execute(
        "INSERT INTO grants(filing_id,cash_grant_amt,non_cash_assistance_amt,payload) "
        "VALUES ('BLANK_GRANT_public',0,NULL,NULL)"
    )
    conn.commit()
    conn.close()

    code, _summary, detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "blank-grant-rejected",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )

    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(csv.DictReader(fh))
    assert detail["classification"] == "unexplained"
    assert "zero-only blank grant row is not allowed" in detail["notes"]


def test_pf_enrichment_requires_directional_match_and_exact_selected_xml(
    tmp_path: Path,
):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    xml_root = tmp_path / "xml"
    create_fixture_db(source)
    create_fixture_db(repaired)
    replace_pf_officer_table(source)
    replace_pf_officer_table(repaired)
    filing_id = "PF_ENRICH_public"
    selected_xml = write_selected_xml(
        xml_root,
        filing_id,
        """
    <IRS990PF>
      <OfficerDirTrstKeyEmplGrp>
        <PersonNm>Alex Example</PersonNm>
        <TitleTxt>Trustee</TitleTxt>
        <AverageHrsPerWkDevotedToPosRt>10</AverageHrsPerWkDevotedToPosRt>
        <CompensationAmt>100</CompensationAmt>
        <EmployeeBenefitProgramAmt>25</EmployeeBenefitProgramAmt>
        <ExpenseAccountOtherAllwncAmt>5</ExpenseAccountOtherAllwncAmt>
      </OfficerDirTrstKeyEmplGrp>
    </IRS990PF>
""",
    )
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, filing_id, return_type="990PF")
        conn.commit()
        conn.close()
        set_return_source(path, filing_id, selected_xml)
    source_conn = sqlite3.connect(source)
    source_conn.executemany(
        """
        INSERT INTO irs990_pf_officer_dir_trst_key_empl_info_grp(
          filing_id,person_nm,title_txt,average_hrs_per_wk_devoted_to_pos_rt,
          compensation_amt,employee_benefits_amt,expense_account_amt
        ) VALUES (?,?,?,?,?,?,?)
        """,
        [
            (filing_id, "Alex Example", "Trustee", "10", 100, None, None),
            (filing_id, "Alex Example", "Trustee", "10", 100, None, None),
        ],
    )
    source_conn.commit()
    source_conn.close()
    repaired_conn = sqlite3.connect(repaired)
    repaired_conn.execute(
        """
        INSERT INTO irs990_pf_officer_dir_trst_key_empl_info_grp(
          filing_id,person_nm,title_txt,average_hrs_per_wk_devoted_to_pos_rt,
          compensation_amt,employee_benefits_amt,expense_account_amt
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (filing_id, "Alex Example", "Trustee", "10", 100, 25, 5),
    )
    repaired_conn.commit()
    repaired_conn.close()

    default_code, *_ = run_fixture_audit(
        source, repaired, tmp_path, "pf-enrichment-default"
    )
    allowed_code, _summary, detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "pf-enrichment-allowed",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )

    assert default_code == 2
    assert allowed_code == 0
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(csv.DictReader(fh))
    assert detail["classification"] == "verified_extractor_enrichment"
    assert "pf_officer_benefit_expense_selected_xml_enrichment" in detail["notes"]

    repaired_conn = sqlite3.connect(repaired)
    repaired_conn.execute(
        "UPDATE irs990_pf_officer_dir_trst_key_empl_info_grp "
        "SET employee_benefits_amt=26"
    )
    repaired_conn.commit()
    repaired_conn.close()
    forged_code, _summary, forged_detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "pf-enrichment-forged",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )
    assert forged_code == 2
    with forged_detail_path.open(newline="", encoding="utf-8") as fh:
        forged_detail = next(csv.DictReader(fh))
    assert "full repaired PF officer multiset differs" in forged_detail["notes"]

    repaired_conn = sqlite3.connect(repaired)
    repaired_conn.execute(
        "UPDATE irs990_pf_officer_dir_trst_key_empl_info_grp "
        "SET employee_benefits_amt=25"
    )
    repaired_conn.commit()
    repaired_conn.close()
    primary_only_xml = selected_xml.read_text(encoding="utf-8").replace(
        "EmployeeBenefitProgramAmt", "EmployeeBenefitsAmt"
    ).replace(
        "ExpenseAccountOtherAllwncAmt", "ExpenseAccountAmt"
    )
    selected_xml.write_text(primary_only_xml, encoding="utf-8")
    primary_code, _summary, primary_detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "pf-enrichment-primary-tag-rejected",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )
    assert primary_code == 2
    with primary_detail_path.open(newline="", encoding="utf-8") as fh:
        primary_detail = next(csv.DictReader(fh))
    assert "PF directional/origin proof failed" in primary_detail["notes"]


def test_schedule_c_new_rows_require_full_selected_xml_counter_equality(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    xml_root = tmp_path / "xml"
    create_fixture_db(source)
    create_fixture_db(repaired)
    replace_schedule_c_supplemental_table(source)
    replace_schedule_c_supplemental_table(repaired)
    filing_id = "SCHEDULE_C_ENRICH_public"
    selected_xml = write_selected_xml(
        xml_root,
        filing_id,
        """
    <IRS990ScheduleC>
      <SupplementalInformationDetail>
        <FormAndLineReferenceDesc>Part I</FormAndLineReferenceDesc>
        <ExplanationTxt>Existing explanation</ExplanationTxt>
      </SupplementalInformationDetail>
      <SupplementalInformationDetail>
        <FormAndLineReferenceDesc>Part II</FormAndLineReferenceDesc>
        <ExplanationTxt>New explanation</ExplanationTxt>
      </SupplementalInformationDetail>
    </IRS990ScheduleC>
""",
        return_type="990",
    )
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, filing_id)
        conn.commit()
        conn.close()
        set_return_source(path, filing_id, selected_xml)
    source_conn = sqlite3.connect(source)
    source_conn.execute(
        "INSERT INTO irs990_schedule_c_supplemental_info "
        "(filing_id,form_and_line_reference_desc,explanation_txt) VALUES (?,?,?)",
        (filing_id, "Part I", "Existing explanation"),
    )
    source_conn.commit()
    source_conn.close()
    repaired_conn = sqlite3.connect(repaired)
    repaired_conn.executemany(
        "INSERT INTO irs990_schedule_c_supplemental_info "
        "(filing_id,form_and_line_reference_desc,explanation_txt) VALUES (?,?,?)",
        [
            (filing_id, "Part I", "Existing explanation"),
            (filing_id, "Part II", "New explanation"),
        ],
    )
    repaired_conn.commit()
    repaired_conn.close()

    default_code, *_ = run_fixture_audit(
        source, repaired, tmp_path, "schedule-c-enrichment-default"
    )
    allowed_code, _summary, detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "schedule-c-enrichment-allowed",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )

    assert default_code == 2
    assert allowed_code == 0
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(csv.DictReader(fh))
    assert detail["classification"] == "verified_extractor_enrichment"
    assert detail["new_payload_rows"] == "1"
    assert "schedule_c_selected_xml_enrichment" in detail["notes"]

    repaired_conn = sqlite3.connect(repaired)
    repaired_conn.execute(
        "UPDATE irs990_schedule_c_supplemental_info "
        "SET explanation_txt='Forged' WHERE form_and_line_reference_desc='Part II'"
    )
    repaired_conn.commit()
    repaired_conn.close()
    forged_code, _summary, forged_detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "schedule-c-enrichment-forged",
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )
    assert forged_code == 2
    with forged_detail_path.open(newline="", encoding="utf-8") as fh:
        forged_detail = next(csv.DictReader(fh))
    assert "full repaired Schedule C multiset differs" in forged_detail["notes"]


def test_failed_schedule_c_verification_gates_even_without_fail_on_new(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    xml_root = tmp_path / "xml"
    create_fixture_db(source)
    create_fixture_db(repaired)
    replace_schedule_c_supplemental_table(source)
    replace_schedule_c_supplemental_table(repaired)
    filing_id = "SCHEDULE_C_UNVERIFIED_public"
    selected_xml = write_selected_xml(
        xml_root,
        filing_id,
        """
    <IRS990ScheduleC>
      <SupplementalInformationDetail>
        <FormAndLineReferenceDesc>Part I</FormAndLineReferenceDesc>
        <ExplanationTxt>XML value</ExplanationTxt>
      </SupplementalInformationDetail>
    </IRS990ScheduleC>
""",
        return_type="990",
        ein="999999999",
    )
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, filing_id)
        conn.commit()
        conn.close()
        set_return_source(path, filing_id, selected_xml)
    repaired_conn = sqlite3.connect(repaired)
    repaired_conn.execute(
        "INSERT INTO irs990_schedule_c_supplemental_info "
        "(filing_id,form_and_line_reference_desc,explanation_txt) VALUES (?,?,?)",
        (filing_id, "Part I", "XML value"),
    )
    repaired_conn.commit()
    repaired_conn.close()

    default_code, _summary, default_detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "schedule-c-new-without-opt-in",
        fail_on_new=False,
    )
    code, _summary, detail_path, _json = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "schedule-c-unverified-new",
        fail_on_new=False,
        allow_verified_extractor_enrichments=True,
        extractor_enrichment_xml_root=xml_root,
    )

    assert default_code == 2
    with default_detail_path.open(newline="", encoding="utf-8") as fh:
        default_detail = next(csv.DictReader(fh))
    assert default_detail["classification"] == "new_in_rebuild"
    assert default_detail["gate_failure"] == "1"
    assert "require_explicit_verified_enrichment_opt_in" in default_detail["notes"]
    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(csv.DictReader(fh))
    assert detail["classification"] == "unexplained"
    assert detail["gate_failure"] == "1"
    assert "selected XML header ein differs" in detail["notes"]


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


def test_child_audit_rejects_typed_return_metadata_null_to_zero(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    for path, tax_year, copies in ((source, None, 2), (repaired, 0, 1)):
        conn = sqlite3.connect(path)
        add_return(conn, "METADATA_TYPED")
        conn.execute(
            "UPDATE returns SET tax_year=? WHERE filing_id='METADATA_TYPED'",
            (tax_year,),
        )
        conn.executemany(
            "INSERT INTO officers(filing_id,payload) VALUES ('METADATA_TYPED','same')",
            [()] * copies,
        )
        conn.commit()
        conn.close()

    code, _summary, detail_path, _json = run_fixture_audit(
        source, repaired, tmp_path, "typed-metadata"
    )

    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "officers"
        )
    assert detail["classification"] == "unexplained"
    assert "filing_metadata_changed:tax_year" in detail["notes"]


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


def test_relocated_xml_root_with_same_archive_suffix_is_equivalent(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    for path in (source, repaired):
        conn = sqlite3.connect(path)
        add_return(conn, "201600089349200000_public")
        conn.commit()
        conn.close()

    conn = sqlite3.connect(source)
    conn.execute(
        r"UPDATE returns SET source_file=?",
        (r"C:\IRSData\xml_missing_2015\201600089349200000_public.xml",),
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(repaired)
    conn.execute(
        r"UPDATE returns SET source_file=?",
        (
            r"C:\Projects\IRSDB\XML\xml_missing_2015"
            r"\201600089349200000_public.xml",
        ),
    )
    conn.commit()
    conn.close()

    code, summary_path, detail_path, json_path = run_fixture_audit(
        source, repaired, tmp_path, "relocated-root"
    )

    assert code == 0
    with detail_path.open(newline="", encoding="utf-8") as fh:
        assert list(csv.DictReader(fh)) == []
    with summary_path.open(newline="", encoding="utf-8") as fh:
        returns = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "returns"
        )
    assert returns["mismatched_filings"] == "0"
    assert returns["gate_failures"] == "0"
    assert json.loads(json_path.read_text(encoding="utf-8"))["gates"]["passed"] is True


def test_same_object_in_different_archive_directory_is_a_real_path_mismatch(
    tmp_path: Path,
):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    for path, archive in ((source, "archive-a"), (repaired, "archive-b")):
        conn = sqlite3.connect(path)
        add_return(conn, "201600089349200000_public")
        conn.execute(
            "UPDATE returns SET source_file=?",
            (f"C:/relocated/{archive}/201600089349200000_public.xml",),
        )
        conn.commit()
        conn.close()

    code, summary_path, detail_path, _json = run_fixture_audit(
        source, repaired, tmp_path, "different-archive"
    )

    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        detail = next(csv.DictReader(fh))
    assert detail["table_name"] == "returns"
    assert detail["classification"] == "content_changed"
    assert detail["source_object_id"] == detail["repaired_object_id"]
    assert detail["gate_failure"] == "1"
    with summary_path.open(newline="", encoding="utf-8") as fh:
        returns = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "returns"
        )
    assert returns["mismatched_filings"] == "1"
    assert returns["content_changed"] == "1"


def test_completed_manifest_verifies_authoritative_path_migration_read_only(
    tmp_path: Path,
):
    (
        source,
        repaired,
        manifest,
        xml_root,
        selected,
        _historical,
        _filing_id,
    ) = create_manifest_provenance_fixture(tmp_path)
    protected = (source, repaired, manifest, selected)
    before = {path: file_hash(path) for path in protected}

    default_code, *_ = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "manifest-not-opted-in",
    )
    code, summary_path, detail_path, json_path = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "manifest-verified",
        source_manifest_db=manifest,
        manifest_xml_root=xml_root,
    )

    assert default_code == 2
    assert code == 0
    with summary_path.open(newline="", encoding="utf-8") as fh:
        returns = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "returns"
        )
    assert returns["manifest_provenance_matches"] == "1"
    assert returns["mismatched_filings"] == "0"
    assert returns["gate_failures"] == "0"
    with detail_path.open(newline="", encoding="utf-8") as fh:
        assert list(csv.DictReader(fh)) == []
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    provenance = payload["metadata"]["manifest_provenance"]
    assert provenance["enabled"] is True
    assert provenance["scan_id"] == MANIFEST_SCAN_ID
    assert provenance["scanned_at"] == MANIFEST_SCANNED_AT
    assert provenance["manifest"]["query_only"] == 1
    assert Path(provenance["manifest"]["path"]) == manifest.resolve()
    assert payload["gates"]["manifest_provenance_enabled"] is True
    assert {path: file_hash(path) for path in protected} == before


@pytest.mark.parametrize(
    ("scenario", "expected_note"),
    [
        ("stale_source_scan", "source row is not from the completed manifest scan"),
        ("stale_loaded_import", "loaded row is not from the completed manifest scan"),
        (
            "source_loaded_mismatch",
            "historical returns source does not match manifest loaded source",
        ),
        (
            "different_parent_same_tail",
            "historical returns source does not match manifest loaded source",
        ),
        ("loaded_basename_only", "manifest loaded source lacks an archive directory"),
        ("historical_traversal", "historical returns source path is invalid"),
        ("manifest_traversal", "invalid manifest relative path"),
        ("metadata_null_to_zero", "typed tax_year differs"),
        ("metadata_nonnull_change", "typed ein differs"),
        ("wrong_xml_header", "selected XML header ein differs"),
        ("changed_selected_file", "selected source size differs from manifest"),
    ],
)
def test_manifest_provenance_rejects_non_authoritative_evidence(
    tmp_path: Path,
    scenario: str,
    expected_note: str,
):
    fixture_options = {"xml_ein": "987654321"} if scenario == "wrong_xml_header" else {}
    (
        source,
        repaired,
        manifest,
        xml_root,
        selected,
        historical,
        filing_id,
    ) = create_manifest_provenance_fixture(
        tmp_path,
        label=scenario,
        **fixture_options,
    )

    if scenario in {
        "stale_source_scan",
        "stale_loaded_import",
        "source_loaded_mismatch",
        "different_parent_same_tail",
        "loaded_basename_only",
        "historical_traversal",
        "manifest_traversal",
    }:
        conn = sqlite3.connect(manifest)
        if scenario == "stale_source_scan":
            conn.execute("UPDATE source_files SET scan_id='older-scan'")
        elif scenario == "stale_loaded_import":
            conn.execute("UPDATE loaded_filings SET imported_at='older-scan-time'")
        elif scenario == "source_loaded_mismatch":
            conn.execute(
                "UPDATE loaded_filings SET source_file=?",
                (rf"C:\IRSData\OtherArchive\{filing_id}.xml",),
            )
        elif scenario == "different_parent_same_tail":
            conn.execute(
                "UPDATE loaded_filings SET source_file=?",
                (rf"OtherParent\2019\{filing_id}.xml",),
            )
        elif scenario == "loaded_basename_only":
            conn.execute(
                "UPDATE loaded_filings SET source_file=?",
                (f"{filing_id}.xml",),
            )
        elif scenario == "historical_traversal":
            traversing = rf"C:\IRSData\old\..\Found\2019\{filing_id}.xml"
            source_conn = sqlite3.connect(source)
            source_conn.execute(
                "UPDATE returns SET source_file=? WHERE filing_id=?",
                (traversing, filing_id),
            )
            source_conn.commit()
            source_conn.close()
            conn.execute(
                "UPDATE loaded_filings SET source_file=?",
                (rf"Found\2019\{filing_id}.xml",),
            )
        else:
            conn.execute(
                "UPDATE source_files SET source_file=?, relative_path=?",
                (f"../outside/{filing_id}.xml", f"../outside/{filing_id}.xml"),
            )
        conn.commit()
        conn.close()
    elif scenario == "metadata_null_to_zero":
        conn = sqlite3.connect(source)
        conn.execute("UPDATE returns SET tax_year=NULL WHERE filing_id=?", (filing_id,))
        conn.commit()
        conn.close()
        conn = sqlite3.connect(repaired)
        conn.execute("UPDATE returns SET tax_year=0 WHERE filing_id=?", (filing_id,))
        conn.commit()
        conn.close()
        conn = sqlite3.connect(manifest)
        conn.execute("UPDATE loaded_filings SET tax_year=0")
        conn.commit()
        conn.close()
    elif scenario == "metadata_nonnull_change":
        conn = sqlite3.connect(repaired)
        conn.execute(
            "UPDATE returns SET ein='987654321' WHERE filing_id=?",
            (filing_id,),
        )
        conn.commit()
        conn.close()
    elif scenario == "changed_selected_file":
        selected.write_text(
            selected.read_text(encoding="utf-8") + "\n<!-- changed -->\n",
            encoding="utf-8",
        )

    assert historical != str(selected)
    assert_manifest_provenance_fails(
        source,
        repaired,
        manifest,
        xml_root,
        tmp_path,
        f"reject-{scenario}",
        expected_note,
    )


def test_manifest_provenance_rejects_repaired_same_basename_wrong_directory(
    tmp_path: Path,
):
    source, repaired, manifest, xml_root, selected, _old, filing_id = (
        create_manifest_provenance_fixture(tmp_path, label="wrong-directory")
    )
    wrong = xml_root / "wrong_archive" / selected.name
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(selected.read_bytes())
    set_return_source(repaired, filing_id, wrong)

    assert_manifest_provenance_fails(
        source,
        repaired,
        manifest,
        xml_root,
        tmp_path,
        "wrong-selected-directory",
        "repaired source path is not the exact manifest-selected file",
    )


def test_manifest_provenance_rejects_outside_root_with_selected_tail(tmp_path: Path):
    source, repaired, manifest, xml_root, selected, _old, filing_id = (
        create_manifest_provenance_fixture(tmp_path, label="outside-tail")
    )
    outside = tmp_path / "outside" / selected.relative_to(xml_root)
    outside.parent.mkdir(parents=True)
    outside.write_bytes(selected.read_bytes())
    set_return_source(repaired, filing_id, outside)

    assert_manifest_provenance_fails(
        source,
        repaired,
        manifest,
        xml_root,
        tmp_path,
        "outside-selected-tail",
        "repaired source path resolves outside the manifest XML root",
    )


def test_manifest_provenance_rejects_wrong_object_conflict_variant(tmp_path: Path):
    source, repaired, manifest, xml_root, selected, _old, filing_id = (
        create_manifest_provenance_fixture(tmp_path, label="object-conflict")
    )
    wrong = xml_root / "conflict_wrong" / selected.name
    wrong.parent.mkdir(parents=True)
    wrong.write_text(
        selected.read_text(encoding="utf-8").replace("</Return>", "<!-- variant --></Return>"),
        encoding="utf-8",
    )
    selected_relative = selected.relative_to(xml_root).as_posix()
    wrong_relative = wrong.relative_to(xml_root).as_posix()
    loaded_historical = rf"C:\IRSData\{selected_relative.replace('/', chr(92))}"
    conn = sqlite3.connect(source)
    conn.execute(
        "UPDATE returns SET source_file=? WHERE filing_id=?",
        (loaded_historical, filing_id),
    )
    conn.commit()
    conn.close()
    set_return_source(repaired, filing_id, wrong)
    conn = sqlite3.connect(manifest)
    conn.execute(
        "UPDATE source_files SET duplicate_status='object_id_conflict'"
    )
    insert_manifest_source(
        conn,
        manifest_source_row(
            xml_root,
            wrong_relative,
            filing_id,
            duplicate_status="object_id_conflict",
        ),
    )
    conn.execute(
        "UPDATE loaded_filings SET source_file=?",
        (loaded_historical,),
    )
    conn.commit()
    conn.close()

    assert_manifest_provenance_fails(
        source,
        repaired,
        manifest,
        xml_root,
        tmp_path,
        "wrong-conflict-variant",
        "repaired source path is not the exact manifest-selected file",
    )


def test_manifest_provenance_rejects_nonselected_exact_duplicate(tmp_path: Path):
    source, repaired, manifest, xml_root, selected, _old, filing_id = (
        create_manifest_provenance_fixture(tmp_path, label="exact-duplicate")
    )
    duplicate = xml_root / "duplicate_copy" / selected.name
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(selected.read_bytes())
    selected_relative = selected.relative_to(xml_root).as_posix()
    duplicate_relative = duplicate.relative_to(xml_root).as_posix()
    set_return_source(repaired, filing_id, duplicate)
    conn = sqlite3.connect(manifest)
    conn.execute(
        "UPDATE source_files SET duplicate_status='primary_duplicate_group', "
        "keep_source_file=?",
        (selected_relative,),
    )
    insert_manifest_source(
        conn,
        manifest_source_row(
            xml_root,
            duplicate_relative,
            filing_id,
            duplicate_status="exact_duplicate",
            keep_source_file=selected_relative,
        ),
    )
    conn.commit()
    conn.close()

    assert_manifest_provenance_fails(
        source,
        repaired,
        manifest,
        xml_root,
        tmp_path,
        "nonselected-exact-duplicate",
        "repaired source path is not the exact manifest-selected file",
    )


def test_detail_evidence_is_bounded_while_summary_counts_remain_exact(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    filing_ids = [f"MISMATCH_{number}" for number in range(5)]
    for path, archive in ((source, "archive-a"), (repaired, "archive-b")):
        conn = sqlite3.connect(path)
        for filing_id in filing_ids:
            add_return(conn, filing_id)
            conn.execute(
                "UPDATE returns SET source_file=? WHERE filing_id=?",
                (f"C:/relocated/{archive}/{filing_id}.xml", filing_id),
            )
        conn.commit()
        conn.close()

    code, summary_path, detail_path, json_path = run_fixture_audit(
        source,
        repaired,
        tmp_path,
        "bounded-detail",
        detail_limit_per_table=2,
        detail_limit_total=10,
    )

    assert code == 2
    with detail_path.open(newline="", encoding="utf-8") as fh:
        details = list(csv.DictReader(fh))
    assert len(details) == 2
    assert {row["table_name"] for row in details} == {"returns"}
    with summary_path.open(newline="", encoding="utf-8") as fh:
        returns = next(
            row for row in csv.DictReader(fh) if row["table_name"] == "returns"
        )
    assert returns["mismatched_filings"] == "5"
    assert returns["gate_failures"] == "5"
    assert returns["detail_evidence_rows"] == "5"
    assert returns["detail_rows_written"] == "2"
    assert returns["detail_rows_suppressed"] == "3"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["detail_reporting"]["rows_written"] == 2
    assert payload["detail_reporting"]["rows_suppressed"] == 3
    assert payload["gates"]["gate_failures"] == 5


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
    manifest = tmp_path / "manifest.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    create_fixture_db(manifest)
    safe_outputs = [
        tmp_path / "summary.csv",
        tmp_path / "detail.csv",
        tmp_path / "detail.json",
    ]
    for database in (source, repaired, manifest):
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
                        additional_databases=(manifest,),
                    )


@pytest.mark.parametrize("manifest_only", [True, False])
def test_manifest_provenance_inputs_must_be_supplied_together(
    tmp_path: Path,
    manifest_only: bool,
):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    manifest = tmp_path / "manifest.db"
    xml_root = tmp_path / "xml"
    create_fixture_db(source)
    create_fixture_db(repaired)
    create_fixture_db(manifest)
    xml_root.mkdir()
    summary, detail, detail_json = report_paths(tmp_path, "manifest-pair")

    with pytest.raises(ValueError, match="must be supplied together"):
        audit.run_audit(
            source,
            repaired,
            summary,
            detail,
            detail_json,
            source_manifest_db=manifest if manifest_only else None,
            manifest_xml_root=None if manifest_only else xml_root,
        )


def test_enrichment_opt_in_requires_an_explicit_xml_root(tmp_path: Path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    create_fixture_db(source)
    create_fixture_db(repaired)
    summary, detail, detail_json = report_paths(tmp_path, "missing-xml-root")

    with pytest.raises(ValueError, match="requires --extractor-enrichment-xml-root"):
        audit.run_audit(
            source,
            repaired,
            summary,
            detail,
            detail_json,
            allow_verified_extractor_enrichments=True,
        )
