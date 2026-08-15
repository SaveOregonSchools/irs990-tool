import csv
import hashlib
import io
import json
import sqlite3
import urllib.error
import zipfile
from pathlib import Path

import pytest

import build_screening_sidecar as screening_builder
from build_screening_sidecar import (
    OfacSeries,
    ScreeningInputs,
    SourceFile,
    build_screening_sidecar,
    download_groups,
    download_public_file,
    inputs_from_cache,
    main,
    normalize_ein,
    normalize_name,
    validate_ofac_series,
)


def _zip_text(path: Path, member: str, content: str) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
    return path


def _source(path: Path, url: str, source_date: str) -> SourceFile:
    return SourceFile(
        path=path,
        source_url=url,
        source_date=source_date,
        retrieved_at="2026-08-14T12:00:00Z",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _ofac_series(
    root: Path, prefix: str, entity_number: str, name: str, alias: str
) -> OfacSeries:
    primary = root / f"{prefix}_primary.csv"
    aliases = root / f"{prefix}_aliases.csv"
    addresses = root / f"{prefix}_addresses.csv"
    comments = root / f"{prefix}_comments.csv"
    primary.write_text(
        f'{entity_number},"{name}","Entity","TEST-PROGRAM",'
        '-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"primary remark"\n',
        encoding="utf-8",
    )
    aliases.write_text(
        f'{entity_number},7,"a.k.a.","{alias}","source alias"\n',
        encoding="utf-8",
    )
    addresses.write_text(
        f'{entity_number},4,"1 Test Plaza","Test City","Testland",'
        '"official address"\n',
        encoding="utf-8",
    )
    comments.write_text(f'{entity_number}," extended remark"\n', encoding="utf-8")
    return OfacSeries(
        primary=_source(
            primary, f"https://official.test/{prefix}/primary.csv", "2026-08-07"
        ),
        aliases=_source(
            aliases, f"https://official.test/{prefix}/aliases.csv", "2026-08-07"
        ),
        addresses=_source(
            addresses,
            f"https://official.test/{prefix}/addresses.csv",
            "2026-08-07",
        ),
        comments=_source(
            comments, f"https://official.test/{prefix}/comments.csv", "2026-08-07"
        ),
    )


def _fixture_inputs(tmp_path: Path) -> ScreeningInputs:
    pub78 = _zip_text(
        tmp_path / "pub78.zip",
        "data-download-pub78.txt",
        "\n\n"
        "12-3456789|Café & Community, Inc.|Seattle|WA||PC\n"
        "987654321|Second Foundation|Portland|OR||PF\n",
    )
    revocation = _zip_text(
        tmp_path / "revocation.zip",
        "data-download-revocation.txt",
        "\n\n"
        "111111111|Revoked Charity|Old Charity|10 Main St|Tacoma|WA|98402|US|"
        "03|15-JAN-2022|01-FEB-2022|\n"
        "222222222|Restored Charity||20 Main St|Salem|OR|97301|US|03|"
        "20200115|20200201|20230630\n",
    )
    leie = tmp_path / "leie.csv"
    header = [
        "LASTNAME",
        "FIRSTNAME",
        "MIDNAME",
        "BUSNAME",
        "GENERAL",
        "SPECIALTY",
        "UPIN",
        "NPI",
        "DOB",
        "ADDRESS",
        "CITY",
        "STATE",
        "ZIP",
        "EXCLTYPE",
        "EXCLDATE",
        "REINDATE",
        "WAIVERDATE",
        "WVRSTATE",
    ]
    with leie.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(header)
        writer.writerow(
            [
                "",
                "",
                "",
                "Risky Health LLC",
                "OTHER BUSINESS",
                "PHARMACY",
                "",
                "1234567890",
                "",
                "30 Main St",
                "Denver",
                "CO",
                "80202",
                "1128b8",
                "20220320",
                "00000000",
                "00000000",
                "",
            ]
        )
        writer.writerow(
            [
                "BROWN",
                "JOHN",
                "",
                "",
                "INDIVIDUAL",
                "PHYSICIAN",
                "",
                "0000000000",
                "19600101",
                "50 Main St",
                "Reno",
                "NV",
                "89501",
                "1128a1",
                "20190101",
                "00000000",
                "00000000",
                "",
            ]
        )
        writer.writerow(
            [
                "SMITH",
                "JANE",
                "Q",
                "",
                "INDIVIDUAL",
                "NURSE",
                "U123",
                "0000000000",
                "19700102",
                "40 Main St",
                "Boise",
                "ID",
                "83702",
                "1128a1",
                "20200101",
                "00000000",
                "00000000",
                "",
            ]
        )
    return ScreeningInputs(
        irs_pub78=_source(pub78, "https://apps.irs.gov/pub78.zip", "2026-08-11"),
        irs_auto_revocation=_source(
            revocation, "https://apps.irs.gov/revocation.zip", "2026-08-11"
        ),
        hhs_leie=_source(
            leie, "https://oig.hhs.gov/UPDATED.csv", "2026-08-10"
        ),
        ofac_sdn=_ofac_series(
            tmp_path, "sdn", "100", "Blocked Entity Ltd.", "Blocked Trading"
        ),
        ofac_consolidated=_ofac_series(
            tmp_path,
            "consolidated",
            "200",
            "Restricted Entity",
            "Restricted Alias",
        ),
    )


def test_builds_normalized_provenanced_sidecar_with_aliases(tmp_path, monkeypatch):
    # Force child batches to cross parent-batch boundaries. Production uses
    # 5,000; this catches foreign-key ordering errors with three fixture rows.
    monkeypatch.setattr(screening_builder, "SQL_BATCH_SIZE", 2)
    target = tmp_path / "screening.db"
    counts = build_screening_sidecar(target, _fixture_inputs(tmp_path))

    assert counts == {
        "irs_pub78": 2,
        "irs_auto_revocation": 2,
        "hhs_leie": 3,
        "ofac_sdn": 1,
        "ofac_consolidated": 1,
    }
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        pub78 = conn.execute(
            """
            SELECT e.*, d.source_date, d.source_url, d.content_sha256,
                   d.complete_snapshot, d.record_count
            FROM screening_entity e
            JOIN screening_dataset d USING (dataset_key)
            WHERE e.dataset_key='irs_pub78' AND e.ein='123456789'
            """
        ).fetchone()
        assert pub78["primary_name"] == "Café & Community, Inc."
        assert pub78["normalized_name"] == "CAFÉ AND COMMUNITY INC"
        assert pub78["deductibility_code"] == "PC"
        assert pub78["source_date"] == "2026-08-11"
        assert pub78["source_url"] == "https://apps.irs.gov/pub78.zip"
        assert len(pub78["content_sha256"]) == 64
        assert pub78["complete_snapshot"] == 1
        assert pub78["record_count"] == 2

        statuses = dict(
            conn.execute(
                "SELECT ein, status FROM screening_entity "
                "WHERE dataset_key='irs_auto_revocation'"
            ).fetchall()
        )
        assert statuses == {
            "111111111": "automatically_revoked",
            "222222222": "reinstated_after_auto_revocation",
        }
        irs_alias = conn.execute(
            "SELECT alias_name, alias_type, alias_quality FROM screening_alias "
            "WHERE dataset_key='irs_auto_revocation'"
        ).fetchone()
        assert tuple(irs_alias) == ("Old Charity", "sort_name", "source_reported")

        ofac = conn.execute(
            """
            SELECT e.primary_name, e.program_tags, e.remarks,
                   a.alias_name, a.alias_type, ad.normalized_address
            FROM screening_entity e
            JOIN screening_alias a USING (dataset_key, source_record_id)
            JOIN screening_address ad USING (dataset_key, source_record_id)
            WHERE e.dataset_key='ofac_sdn'
            """
        ).fetchone()
        assert ofac["primary_name"] == "Blocked Entity Ltd."
        assert ofac["program_tags"] == "TEST-PROGRAM"
        assert "primary remark" in ofac["remarks"]
        assert "extended remark" in ofac["remarks"]
        assert ofac["alias_name"] == "Blocked Trading"
        assert ofac["alias_type"] == "a.k.a."
        assert ofac["normalized_address"] == "1 TEST PLAZA TEST CITY TESTLAND"

        hhs = conn.execute(
            "SELECT entity_type, npi, status FROM screening_entity "
            "WHERE dataset_key='hhs_leie' AND primary_name='Risky Health LLC'"
        ).fetchone()
        assert tuple(hhs) == ("organization", "1234567890", "active_exclusion")
        assert (
            conn.execute(
                "SELECT identifier_value FROM screening_identifier "
                "WHERE identifier_type='NPI'"
            ).fetchone()[0]
            == "1234567890"
        )
        derived = conn.execute(
            "SELECT alias_name, alias_quality FROM screening_alias "
            "WHERE dataset_key='hhs_leie' AND alias_name='SMITH, JANE Q'"
        ).fetchone()
        assert tuple(derived) == (
            "SMITH, JANE Q",
            "deterministically_derived",
        )

        names = conn.execute(
            "SELECT name_role, source_date FROM screening_names_v1 "
            "WHERE display_name='Blocked Trading'"
        ).fetchone()
        assert tuple(names) == ("alias", "2026-08-07")
    finally:
        conn.close()


def test_failed_rebuild_preserves_previous_sidecar(tmp_path):
    target = tmp_path / "screening.db"
    build_screening_sidecar(target, _fixture_inputs(tmp_path))
    original_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    bad = tmp_path / "bad-pub78.txt"
    bad.write_text("WRONG|HEADER\nvalue|value\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Required column"):
        build_screening_sidecar(
            target,
            ScreeningInputs(
                irs_pub78=_source(
                    bad, "https://apps.irs.gov/bad.txt", "2026-08-11"
                )
            ),
        )

    assert hashlib.sha256(target.read_bytes()).hexdigest() == original_hash
    assert not list(tmp_path.glob(".screening.db.*.tmp"))


def test_normalizers_are_conservative_and_deterministic():
    assert normalize_ein("12-3456789") == "123456789"
    assert normalize_ein("123") is None
    assert normalize_name("  Café & Community, Inc. ") == "CAFÉ AND COMMUNITY INC"
    assert normalize_name("CAFÉ AND COMMUNITY INC") == "CAFÉ AND COMMUNITY INC"
    assert normalize_name("Cafe Community Inc") != normalize_name(
        "Café & Community, Inc."
    )


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int, headers=None, final_url=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}
        self.final_url = (
            final_url
            or "https://apps.irs.gov/pub/epostcard/data-download-pub78.zip"
        )

    def getcode(self):
        return self.status

    def geturl(self):
        return self.final_url


class _FakeOpener:
    def __init__(self, responses):
        self.responses = (
            list(responses) if isinstance(responses, (list, tuple)) else [responses]
        )
        self.request = None
        self.requests = []

    def open(self, request, timeout):
        self.request = request
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_download_resumes_partial_file_and_records_provenance(tmp_path):
    complete = io.BytesIO()
    with zipfile.ZipFile(complete, "w") as archive:
        archive.writestr(
            "pub78.txt",
            "EIN|NAME|CITY|STATE|COUNTRY|DEDUCTIBILITY_CODE\n"
            "123456789|Example|X|WA||PC\n",
        )
    payload = complete.getvalue()
    split = len(payload) // 2
    destination = tmp_path / "irs_pub78.zip"
    destination.with_name(destination.name + ".part").write_bytes(payload[:split])
    destination.with_name(destination.name + ".part.metadata.json").write_text(
        json.dumps(
            {
                "source_url": (
                    "https://apps.irs.gov/pub/epostcard/"
                    "data-download-pub78.zip"
                ),
                "last_modified": "Tue, 11 Aug 2026 09:21:40 GMT",
            }
        ),
        encoding="utf-8",
    )
    response = _FakeResponse(
        payload[split:],
        206,
        {
            "Content-Length": str(len(payload) - split),
            "Last-Modified": "Tue, 11 Aug 2026 09:21:40 GMT",
            "Content-Range": f"bytes {split}-{len(payload) - 1}/{len(payload)}",
        },
    )
    opener = _FakeOpener(response)

    source = download_public_file("irs_pub78", destination, opener=opener)

    assert destination.read_bytes() == payload
    assert opener.request.get_header("Range") == f"bytes={split}-"
    request_headers = {
        key.lower(): value for key, value in opener.request.header_items()
    }
    assert (
        request_headers["if-range"]
        == "Tue, 11 Aug 2026 09:21:40 GMT"
    )
    assert source.source_date == "2026-08-11"
    metadata = json.loads(
        destination.with_name(destination.name + ".metadata.json").read_text()
    )
    assert metadata["source_url"].startswith("https://apps.irs.gov/")
    assert metadata["sha256"] == hashlib.sha256(payload).hexdigest()


def test_download_416_restarts_full_and_never_publishes_partial(tmp_path):
    complete = io.BytesIO()
    with zipfile.ZipFile(complete, "w") as archive:
        archive.writestr(
            "pub78.txt",
            "EIN|NAME|CITY|STATE|COUNTRY|DEDUCTIBILITY_CODE\n"
            "123456789|Example|X|WA||PC\n",
        )
    payload = complete.getvalue()
    destination = tmp_path / "irs_pub78.zip"
    partial = destination.with_name(destination.name + ".part")
    partial.write_bytes(payload[:20])
    destination.with_name(destination.name + ".part.metadata.json").write_text(
        json.dumps(
            {
                "source_url": (
                    "https://apps.irs.gov/pub/epostcard/"
                    "data-download-pub78.zip"
                ),
                "etag": '"old"',
            }
        ),
        encoding="utf-8",
    )
    range_error = urllib.error.HTTPError(
        "https://apps.irs.gov/pub/epostcard/data-download-pub78.zip",
        416,
        "Range not satisfiable",
        {},
        io.BytesIO(),
    )
    full = _FakeResponse(
        payload,
        200,
        {
            "Content-Length": str(len(payload)),
            "Last-Modified": "Tue, 11 Aug 2026 09:21:40 GMT",
            "ETag": '"new"',
        },
    )
    opener = _FakeOpener([range_error, full])

    download_public_file("irs_pub78", destination, opener=opener)

    assert destination.read_bytes() == payload
    assert opener.requests[0].get_header("Range") == "bytes=20-"
    assert opener.requests[1].get_header("Range") is None


def test_download_rejects_unapproved_redirect_before_publish(tmp_path):
    complete = io.BytesIO()
    with zipfile.ZipFile(complete, "w") as archive:
        archive.writestr("pub78.txt", "valid")
    payload = complete.getvalue()
    response = _FakeResponse(
        payload,
        200,
        {"Content-Length": str(len(payload))},
        final_url="https://evil.example/data-download-pub78.zip",
    )
    destination = tmp_path / "irs_pub78.zip"

    with pytest.raises(RuntimeError, match="unapproved target"):
        download_public_file(
            "irs_pub78", destination, opener=_FakeOpener(response)
        )

    assert not destination.exists()


def test_ofac_redirect_allowlist_accepts_only_expected_signed_object():
    allowed = (
        "https://"
        f"{screening_builder.OFAC_PUBLISHED_HOST}"
        "/Published/8f6d560e-b5b9-4cc8-8df6-2911905f44be/2026-08-07/"
        "4a5da36e-08f3-49c3-a841-24c7dce71d08/SDN.CSV"
        "?X-Amz-Credential=credential&X-Amz-Signature=signature"
    )
    screening_builder._validate_final_url("ofac_sdn_primary", allowed)

    with pytest.raises(RuntimeError, match="unapproved target"):
        screening_builder._validate_final_url(
            "ofac_sdn_primary", allowed.replace("SDN.CSV", "CONS_PRIM.CSV")
        )


def test_ofac_relations_reject_orphan_auxiliary_keys(tmp_path):
    series = _ofac_series(
        tmp_path, "sdn-invalid", "100", "Blocked Entity", "Blocked Alias"
    )
    series.aliases.path.write_text(
        '999,7,"a.k.a.","Orphan Alias",""\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="missing entity key 999"):
        validate_ofac_series(series)


def _fake_irs_download(key, destination, **_kwargs):
    if key == "irs_pub78":
        content = (
            "\n\n123456789|Example Charity|Seattle|WA|United States|PC\n"
        )
    else:
        content = (
            "\n\n123456789|Example Charity||1 Main St|Seattle|WA|98101|US|"
            "03|15-JAN-2020|01-FEB-2020|\n"
        )
    _zip_text(destination, f"{key}.txt", content)
    url = screening_builder.PUBLIC_FILES[key][0]
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    screening_builder._atomic_write_json(
        screening_builder._metadata_path(destination),
        {
            "source_key": key,
            "source_url": url,
            "final_url": url,
            "retrieved_at": "2026-08-14T00:00:00Z",
            "last_modified": "Tue, 11 Aug 2026 09:21:40 GMT",
            "source_date": "2026-08-11",
            "etag": f'"{key}"',
            "size_bytes": destination.stat().st_size,
            "sha256": digest,
        },
    )
    return screening_builder.source_file_from_path(destination, url)


def test_group_refresh_publishes_one_atomic_manifest(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        screening_builder, "download_public_file", _fake_irs_download
    )
    download_groups(cache, ["irs"], refresh=True)
    manifest = cache / "irs.current.json"
    original_manifest = manifest.read_bytes()
    original_inputs = inputs_from_cache(cache, ["irs"], {})
    original_directory = original_inputs.irs_pub78.path.parent

    def fail_second_file(key, destination, **kwargs):
        if key == "irs_auto_revocation":
            raise RuntimeError("simulated second-component failure")
        return _fake_irs_download(key, destination, **kwargs)

    monkeypatch.setattr(
        screening_builder, "download_public_file", fail_second_file
    )
    with pytest.raises(RuntimeError, match="second-component failure"):
        download_groups(cache, ["irs"], refresh=True)

    assert manifest.read_bytes() == original_manifest
    active = inputs_from_cache(cache, ["irs"], {})
    assert active.irs_pub78.path.parent == original_directory
    assert active.irs_auto_revocation.path.parent == original_directory


def test_independent_irs_files_may_have_different_source_dates(tmp_path):
    pub = _fake_irs_download(
        "irs_pub78", tmp_path / "irs_pub78.zip"
    )
    rev = _fake_irs_download(
        "irs_auto_revocation", tmp_path / "irs_auto_revocation.zip"
    )
    rev_metadata_path = screening_builder._metadata_path(rev.path)
    rev_metadata = json.loads(rev_metadata_path.read_text())
    rev_metadata["source_date"] = "2026-08-12"
    screening_builder._atomic_write_json(rev_metadata_path, rev_metadata)
    sources = {
        "irs_pub78": pub,
        "irs_auto_revocation": screening_builder.source_file_from_path(
            rev.path, rev.source_url
        ),
    }

    screening_builder._validate_group_sources("irs", sources)


def test_sidecar_builder_refuses_main_database_destination(tmp_path, monkeypatch):
    main_db = tmp_path / "irs990.db"
    main_db.write_bytes(b"main database sentinel")
    monkeypatch.setenv("IRS_DB_PATH", str(main_db))

    with pytest.raises(RuntimeError, match="must not equal IRS_DB_PATH"):
        build_screening_sidecar(main_db, ScreeningInputs())
    assert main_db.read_bytes() == b"main database sentinel"

    assert (
        main(
            [
                "--db",
                str(main_db),
                "--cache-dir",
                str(tmp_path / "unused-cache"),
            ]
        )
        == 1
    )
    assert main_db.read_bytes() == b"main database sentinel"
