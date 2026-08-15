import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

import rebuild_irs990_slim_clean as rebuild


SCAN_ID = "fixture-scan"
SCANNED_AT = "2026-08-14T00:00:00+00:00"


def _write_xml(root: Path, relative: str, text: str = "<Return />") -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _source_row(root: Path, relative: str, object_id: str, **overrides):
    path = root.joinpath(*relative.split("/"))
    current_stat = path.stat() if path.exists() else None
    row = {
        "scan_id": SCAN_ID,
        "source_file": relative,
        "relative_path": relative,
        "filing_id": path.stem,
        "object_id": object_id,
        "size_bytes": current_stat.st_size if current_stat else 10,
        "mtime_ns": current_stat.st_mtime_ns if current_stat else 1,
        "sha256": None,
        "duplicate_status": "unique",
        "keep_source_file": None,
        "quarantine_status": None,
        "quarantine_file": None,
    }
    row.update(overrides)
    return row


def _loaded_row(filing_id: str, object_id: str, source_file: str):
    return {
        "filing_id": filing_id,
        "object_id": object_id,
        "source_file": source_file,
        "imported_at": SCANNED_AT,
    }


def _create_manifest(path: Path, sources, loaded) -> None:
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
              imported_at TEXT NOT NULL
            );
            CREATE INDEX idx_source_object ON source_files(object_id);
            CREATE INDEX idx_loaded_object ON loaded_filings(object_id);
            """
        )
        conn.executemany(
            "INSERT INTO scan_meta VALUES (?,?)",
            [
                ("last_scan_id", SCAN_ID),
                ("last_scanned_at", SCANNED_AT),
                ("path_format", rebuild.MANIFEST_PATH_FORMAT),
            ],
        )
        source_columns = tuple(sources[0]) if sources else ()
        if source_columns:
            conn.executemany(
                f"INSERT INTO source_files ({','.join(source_columns)}) "
                f"VALUES ({','.join('?' for _ in source_columns)})",
                [[row[column] for column in source_columns] for row in sources],
            )
        loaded_columns = tuple(loaded[0]) if loaded else ()
        if loaded_columns:
            conn.executemany(
                f"INSERT INTO loaded_filings ({','.join(loaded_columns)}) "
                f"VALUES ({','.join('?' for _ in loaded_columns)})",
                [[row[column] for column in loaded_columns] for row in loaded],
            )
        conn.commit()
    finally:
        conn.close()


def _valid_fixture(tmp_path: Path):
    root = tmp_path / "xml"
    root.mkdir()
    unique = _write_xml(root, "unique/100_public.xml", "<Return>unique</Return>")
    duplicate_primary = _write_xml(root, "dup_a/200_public.xml", "<Return>same</Return>")
    duplicate_copy = _write_xml(root, "dup_b/200_public.xml", "<Return>same</Return>")
    conflict_old = _write_xml(root, "conflict_a/300_public.xml", "<Return>old</Return>")
    conflict_loaded = _write_xml(root, "conflict_b/300_public.xml", "<Return>loaded</Return>")
    sources = [
        _source_row(root, "unique/100_public.xml", "100"),
        _source_row(
            root,
            "dup_a/200_public.xml",
            "200",
            sha256=hashlib.sha256(duplicate_primary.read_bytes()).hexdigest(),
            duplicate_status="primary_duplicate_group",
            keep_source_file="dup_a/200_public.xml",
        ),
        _source_row(
            root,
            "dup_b/200_public.xml",
            "200",
            sha256=hashlib.sha256(duplicate_copy.read_bytes()).hexdigest(),
            duplicate_status="exact_duplicate",
            keep_source_file="dup_a/200_public.xml",
        ),
        _source_row(
            root,
            "conflict_a/300_public.xml",
            "300",
            sha256=hashlib.sha256(conflict_old.read_bytes()).hexdigest(),
            duplicate_status="object_id_conflict",
            keep_source_file="conflict_a/300_public.xml",
        ),
        _source_row(
            root,
            "conflict_b/300_public.xml",
            "300",
            sha256=hashlib.sha256(conflict_loaded.read_bytes()).hexdigest(),
            duplicate_status="object_id_conflict",
            keep_source_file="conflict_b/300_public.xml",
        ),
    ]
    loaded = [
        _loaded_row("100_public", "100", str(unique)),
        _loaded_row("200_public", "200", str(duplicate_copy)),
        _loaded_row("300_public", "300", r"C:\old-root\conflict_b\300_public.xml"),
    ]
    manifest = tmp_path / "irs990_sources.db"
    _create_manifest(manifest, sources, loaded)
    return root, manifest, conflict_loaded, duplicate_primary


def test_manifest_selection_chooses_one_per_object_and_preserves_loaded_conflict(tmp_path: Path):
    root, manifest, conflict_loaded, duplicate_primary = _valid_fixture(tmp_path)

    result = rebuild.select_manifest_xml_files(
        manifest, root, expected_count=3, collect_files=True
    )

    assert result.files is not None
    assert {Path(path).resolve() for path in result.files} == {
        (root / "unique/100_public.xml").resolve(),
        duplicate_primary.resolve(),
        conflict_loaded.resolve(),
    }
    assert result.stats["manifest_source_rows"] == 5
    assert result.stats["selected_objects"] == 3
    assert result.stats["selected_filings"] == 3
    assert result.stats["files_validated"] == 3
    assert result.stats["exact_duplicate_primary"] == 1
    assert result.stats["object_id_conflict_loaded_path"] == 1


@pytest.mark.parametrize("failure", ["missing", "quarantined", "out_of_root"])
def test_manifest_selection_rejects_unsafe_selected_paths(tmp_path: Path, failure: str):
    root = tmp_path / "xml"
    root.mkdir()
    relative = "safe/400_public.xml"
    path = _write_xml(root, relative)
    row = _source_row(root, relative, "400")
    if failure == "missing":
        path.unlink()
    elif failure == "quarantined":
        row["quarantine_status"] = "moved"
        row["quarantine_file"] = "quarantine/safe/400_public.xml"
    else:
        row["source_file"] = "../outside/400_public.xml"
        row["relative_path"] = "../outside/400_public.xml"
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        [row],
        [_loaded_row("400_public", "400", str(path))],
    )

    with pytest.raises(rebuild.ManifestSelectionError):
        rebuild.select_manifest_xml_files(manifest, root)


def test_manifest_selection_rejects_ambiguous_loaded_conflict_path(tmp_path: Path):
    root = tmp_path / "xml"
    root.mkdir()
    first = _write_xml(root, "shared/500_public.xml", "<Return>one</Return>")
    second = _write_xml(root, "nested/shared/500_public.xml", "<Return>two</Return>")
    sources = [
        _source_row(
            root,
            "shared/500_public.xml",
            "500",
            sha256=hashlib.sha256(first.read_bytes()).hexdigest(),
            duplicate_status="object_id_conflict",
        ),
        _source_row(
            root,
            "nested/shared/500_public.xml",
            "500",
            sha256=hashlib.sha256(second.read_bytes()).hexdigest(),
            duplicate_status="object_id_conflict",
        ),
    ]
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        sources,
        [_loaded_row("500_public", "500", "/old/nested/shared/500_public.xml")],
    )

    with pytest.raises(rebuild.ManifestSelectionError, match="ambiguous"):
        rebuild.select_manifest_xml_files(manifest, root)


def test_manifest_mode_refuses_existing_destination_before_selection(tmp_path: Path):
    root = tmp_path / "xml"
    root.mkdir()
    missing = root / "missing/600_public.xml"
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        [_source_row(root, "missing/600_public.xml", "600")],
        [_loaded_row("600_public", "600", str(missing))],
    )
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"do-not-touch")

    result = rebuild.main(
        [
            "--db", str(destination),
            "--xml-dir", str(root),
            "--manifest-db", str(manifest),
            "--manifest-clean-rebuild",
            "--expected-selection-count", "1",
        ]
    )

    assert result == 2
    assert destination.read_bytes() == b"do-not-touch"


def test_manifest_selection_only_uses_irs_xml_root_and_never_touches_unrelated_file(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "xml"
    root.mkdir()
    source = _write_xml(root, "700_public.xml")
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        [_source_row(root, "700_public.xml", "700")],
        [_loaded_row("700_public", "700", str(source))],
    )
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"unchanged")
    monkeypatch.setenv("IRS_XML_ROOT", str(root))

    result = rebuild.main(
        [
            "--manifest-db", str(manifest),
            "--manifest-selection-only",
            "--expected-selection-count", "1",
        ]
    )

    assert result == 0
    assert destination.read_bytes() == b"unchanged"


def test_manifest_expected_count_mismatch_is_rejected(tmp_path: Path):
    root, manifest, _conflict, _primary = _valid_fixture(tmp_path)

    with pytest.raises(rebuild.ManifestSelectionError, match="expected 4"):
        rebuild.select_manifest_xml_files(manifest, root, expected_count=4)


def test_manifest_selection_rejects_same_size_mtime_drift(tmp_path: Path):
    root = tmp_path / "xml"
    root.mkdir()
    source = _write_xml(root, "800_public.xml", "AAAA")
    row = _source_row(root, "800_public.xml", "800")
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        [row],
        [_loaded_row("800_public", "800", str(source))],
    )
    source.write_text("BBBB", encoding="utf-8")
    changed_mtime = row["mtime_ns"] + 10_000_000
    os.utime(source, ns=(changed_mtime, changed_mtime))

    with pytest.raises(rebuild.ManifestSelectionError, match="mtime differs"):
        rebuild.select_manifest_xml_files(manifest, root)


def test_manifest_selection_hash_catches_same_size_tamper_with_restored_mtime(tmp_path: Path):
    root = tmp_path / "xml"
    root.mkdir()
    source = _write_xml(root, "900_public.xml", "AAAA")
    row = _source_row(
        root,
        "900_public.xml",
        "900",
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        [row],
        [_loaded_row("900_public", "900", str(source))],
    )
    source.write_text("BBBB", encoding="utf-8")
    os.utime(source, ns=(row["mtime_ns"], row["mtime_ns"]))

    with pytest.raises(rebuild.ManifestSelectionError, match="SHA-256 differs"):
        rebuild.select_manifest_xml_files(manifest, root)


def test_manifest_mode_refuses_active_database_and_xml_root_destination(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "xml"
    root.mkdir()
    source = _write_xml(root, "1000_public.xml")
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        [_source_row(root, "1000_public.xml", "1000")],
        [_loaded_row("1000_public", "1000", str(source))],
    )
    active = tmp_path / "active.db"
    monkeypatch.setenv("IRS_DB_PATH", str(active))

    active_result = rebuild.main(
        [
            "--db", str(active),
            "--xml-dir", str(root),
            "--manifest-db", str(manifest),
            "--manifest-clean-rebuild",
        ]
    )
    inside_result = rebuild.main(
        [
            "--db", str(root / "unsafe.db"),
            "--xml-dir", str(root),
            "--manifest-db", str(manifest),
            "--manifest-clean-rebuild",
        ]
    )

    assert active_result == 2
    assert inside_result == 2
    assert not active.exists()
    assert not (root / "unsafe.db").exists()


def test_manifest_clean_rebuild_extract_error_leaves_no_destination_or_temp(tmp_path: Path):
    root = tmp_path / "xml"
    root.mkdir()
    source = _write_xml(root, "1100_public.xml", "<not-complete")
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        [_source_row(root, "1100_public.xml", "1100")],
        [_loaded_row("1100_public", "1100", str(source))],
    )
    destination = tmp_path / "staging.db"

    result = rebuild.main(
        [
            "--db", str(destination),
            "--xml-dir", str(root),
            "--manifest-db", str(manifest),
            "--manifest-clean-rebuild",
            "--expected-selection-count", "1",
            "--workers", "1",
        ]
    )

    assert result == 1
    assert not destination.exists()
    assert list(tmp_path.glob("staging.db.building-*.db*")) == []


def test_manifest_clean_rebuild_publishes_only_validated_database(tmp_path: Path):
    root = tmp_path / "xml"
    root.mkdir()
    source = _write_xml(
        root,
        "1200_public.xml",
        """<Return returnVersion="2022v1.0">
        <ReturnHeader>
          <ReturnTypeCd>990</ReturnTypeCd><TaxYr>2022</TaxYr>
          <Filer><EIN>123456789</EIN><BusinessName><BusinessNameLine1Txt>Fixture Org</BusinessNameLine1Txt></BusinessName></Filer>
        </ReturnHeader>
        <ReturnData><IRS990 /></ReturnData>
        </Return>""",
    )
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        [_source_row(root, "1200_public.xml", "1200")],
        [_loaded_row("1200_public", "1200", str(source))],
    )
    destination = tmp_path / "staging.db"

    result = rebuild.main(
        [
            "--db", str(destination),
            "--xml-dir", str(root),
            "--manifest-db", str(manifest),
            "--manifest-clean-rebuild",
            "--expected-selection-count", "1",
            "--workers", "1",
        ]
    )

    assert result == 0
    assert destination.is_file()
    assert list(tmp_path.glob("staging.db.building-*.db*")) == []
    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT filing_id,source_file FROM returns").fetchone() == (
            "1200_public",
            str(source.resolve()),
        )
    finally:
        conn.close()


def test_manifest_coverage_validation_is_a_hard_failure():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE returns (filing_id TEXT PRIMARY KEY, source_file TEXT)"
    )
    conn.execute("INSERT INTO returns VALUES ('one_public','one.xml')")
    selection = rebuild.ManifestSelection(
        files=["one.xml", "two.xml"],
        stats={"selected_objects": 2},
    )
    try:
        with pytest.raises(RuntimeError, match="coverage mismatch"):
            rebuild.validate_manifest_load(conn, selection, processed=2)
    finally:
        conn.close()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_manifest_temp_cleanup_handles_base_exceptions(
    tmp_path: Path, monkeypatch, exception_type
):
    root = tmp_path / "xml"
    root.mkdir()
    source = _write_xml(root, "1300_public.xml")
    manifest = tmp_path / "manifest.db"
    _create_manifest(
        manifest,
        [_source_row(root, "1300_public.xml", "1300")],
        [_loaded_row("1300_public", "1300", str(source))],
    )
    destination = tmp_path / "staging.db"

    def interrupt_build(temp_path, _xml_dirs, _args, _selection):
        temp_path.write_bytes(b"partial")
        Path(str(temp_path) + "-wal").write_bytes(b"wal")
        Path(str(temp_path) + "-shm").write_bytes(b"shm")
        raise exception_type()

    monkeypatch.setattr(rebuild, "build_database", interrupt_build)

    with pytest.raises(exception_type):
        rebuild.main(
            [
                "--db", str(destination),
                "--xml-dir", str(root),
                "--manifest-db", str(manifest),
                "--manifest-clean-rebuild",
                "--expected-selection-count", "1",
            ]
        )

    assert not destination.exists()
    assert list(tmp_path.glob("staging.db.building-*.db*")) == []
