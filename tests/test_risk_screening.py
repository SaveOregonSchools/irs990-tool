import sqlite3

from build_screening_sidecar import ENTITY_INSERT, SCHEMA_SQL, _entity_row
from queries._risk_screening import lookup_irs_status, lookup_name_candidates


def _screening_db(tmp_path):
    path = tmp_path / "screening.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    datasets = [
        ("irs_pub78", "IRS", "Pub 78", "https://irs.example/page", "https://irs.example/pub78.zip"),
        (
            "irs_auto_revocation",
            "IRS",
            "Auto revocation",
            "https://irs.example/page",
            "https://irs.example/revocation.zip",
        ),
        (
            "ofac_sdn",
            "OFAC",
            "SDN",
            "https://ofac.example/page",
            "https://ofac.example/sdn.csv",
        ),
        (
            "hhs_leie",
            "HHS-OIG",
            "LEIE",
            "https://hhs.example/page",
            "https://hhs.example/leie.csv",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO screening_dataset (
          dataset_key, publisher, title, source_page_url, source_url,
          source_date, retrieved_at, content_sha256, record_count,
          complete_snapshot, components_json, access_note
        ) VALUES (?,?,?,?,?,'2026-08-01','2026-08-02T00:00:00Z',?,1,1,'[]','public')
        """,
        [(*row, row[0].ljust(64, "0")[:64]) for row in datasets],
    )
    conn.execute(
        ENTITY_INSERT,
        _entity_row(
            "irs_pub78",
            "pub-1",
            "organization",
            "Example Charity",
            ein="123456789",
            status="eligible_for_deductible_contributions",
            deductibility_code="PC",
            city="Seattle",
            region="WA",
            country="US",
        ),
    )
    conn.execute(
        ENTITY_INSERT,
        _entity_row(
            "irs_auto_revocation",
            "rev-1",
            "organization",
            "Example Charity",
            ein="123456789",
            status="reinstated_after_auto_revocation",
            status_date="2020-01-01",
            reinstatement_date="2022-01-01",
            city="Seattle",
            region="WA",
            country="US",
        ),
    )
    conn.execute(
        ENTITY_INSERT,
        _entity_row(
            "ofac_sdn",
            "ofac-1",
            "entity",
            "Blocked Trading LLC",
            status="sanctions_listed",
            list_name="SDN",
            program_tags="TEST",
        ),
    )
    conn.execute(
        """
        INSERT INTO screening_alias VALUES (
          'ofac_sdn','ofac-1','alias-1','Acme Charity','ACME CHARITY',
          'a.k.a.',NULL,'source alias'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO screening_address VALUES (
          'ofac_sdn','ofac-1','address-1','1 Main St','Seattle','WA','98101',
          'United States','1 MAIN ST SEATTLE WA 98101 UNITED STATES',''
        )
        """
    )
    conn.execute(
        ENTITY_INSERT,
        _entity_row(
            "hhs_leie",
            "hhs-1",
            "individual",
            "Jane Smith",
            status="active_exclusion",
            city="Portland",
            region="OR",
            country="US",
            exclusion_type="1128a1",
        ),
    )
    conn.execute(
        """
        INSERT INTO screening_address VALUES (
          'hhs_leie','hhs-1','primary','2 Main St','Portland','OR','97201',
          'US','2 MAIN ST PORTLAND OR 97201 US',''
        )
        """
    )
    conn.commit()
    conn.close()
    return path


def test_exact_ein_status_includes_provenance_and_history(tmp_path):
    path = _screening_db(tmp_path)

    response = lookup_irs_status("12-3456789", db_path=path)

    assert response["available"] is True
    assert response["candidate_only"] is False
    assert len(response["results"]) == 2
    assert {item["dataset_key"] for item in response["results"]} == {
        "irs_pub78",
        "irs_auto_revocation",
    }
    assert all(
        item["match_evidence"]["kind"] == "exact_ein"
        for item in response["results"]
    )
    assert all(item["source_date"] == "2026-08-01" for item in response["results"])


def test_name_lookup_is_exact_candidate_only_and_uses_location(tmp_path):
    path = _screening_db(tmp_path)

    response = lookup_name_candidates(
        "Acme Charity",
        city="Seattle",
        region="WA",
        country="USA",
        db_path=path,
    )

    assert response["available"] is True
    assert response["candidate_only"] is True
    assert len(response["results"]) == 1
    candidate = response["results"][0]
    assert candidate["dataset_key"] == "ofac_sdn"
    assert candidate["match_evidence"]["kind"] == "exact_normalized_source_alias"
    assert candidate["match_evidence"]["alias_type"] == "a.k.a."
    assert candidate["location_evidence"]["kind"] == "exact"
    assert candidate["verification_required"] == "manual OFAC identity verification"

    assert (
        lookup_name_candidates("Acme", db_path=path)["results"] == []
    )


def test_hhs_lead_requires_online_identity_verification(tmp_path):
    path = _screening_db(tmp_path)

    response = lookup_name_candidates(
        "Jane Smith", city="Seattle", region="WA", db_path=path
    )

    candidate = response["results"][0]
    assert candidate["dataset_key"] == "hhs_leie"
    assert candidate["location_evidence"]["kind"] == "conflict"
    assert candidate["candidate_only"] is True
    assert candidate["verification_required"] == "OIG online EIN/SSN verification"


def test_missing_sidecar_and_invalid_ein_fail_closed(tmp_path):
    missing = tmp_path / "missing.db"

    assert lookup_irs_status("not-an-ein", db_path=missing)["available"] is False
    name_response = lookup_name_candidates("Example", db_path=missing)
    assert name_response["available"] is False
    assert name_response["results"] == []
