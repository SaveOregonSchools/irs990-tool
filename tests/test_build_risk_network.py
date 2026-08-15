import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import build_risk_network as network
from queries import _risk_network


def _create_source(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE returns (
          filing_id TEXT PRIMARY KEY, ein TEXT, org_name TEXT, tax_year INTEGER,
          period_end TEXT, return_ts TEXT, us_address_line1 TEXT,
          us_address_line2 TEXT, city TEXT, state TEXT, zip TEXT,
          foreign_address_line1 TEXT, foreign_city TEXT, foreign_province TEXT,
          foreign_country TEXT, foreign_postal_code TEXT
        );
        CREATE INDEX idx_returns_ein ON returns(ein);

        CREATE TABLE grants (id INTEGER PRIMARY KEY, filing_id TEXT NOT NULL);
        CREATE INDEX idx_grants_filing_id ON grants(filing_id);

        CREATE TABLE grant_recipient_resolved (
          grant_id INTEGER PRIMARY KEY, filing_id TEXT, grantor_ein TEXT,
          grantor_name TEXT, tax_year INTEGER, recipient_reported_ein TEXT,
          recipient_reported_name TEXT, cash_amount NUMERIC,
          noncash_amount NUMERIC, total_amount NUMERIC, purpose TEXT,
          resolved_ein TEXT, resolved_org_name TEXT, match_status TEXT,
          match_method TEXT, confidence NUMERIC, warning_flags TEXT
        );
        CREATE INDEX idx_fixture_grant_filing ON grant_recipient_resolved(filing_id);

        CREATE TABLE officers (
          id INTEGER PRIMARY KEY, filing_id TEXT, person_name TEXT, title_txt TEXT,
          comp_from_org NUMERIC, comp_from_related NUMERIC, other_compensation NUMERIC
        );
        CREATE INDEX idx_fixture_officer_filing ON officers(filing_id);

        CREATE TABLE irs990_contractor_compensation_grp (
          id INTEGER PRIMARY KEY, filing_id TEXT, compensation_amt NUMERIC,
          address_line1_txt TEXT, city_nm TEXT, province_or_state_nm TEXT,
          country_cd TEXT, foreign_postal_cd TEXT,
          usaddress_address_line1_txt TEXT, usaddress_city_nm TEXT,
          state_abbreviation_cd TEXT, zipcd TEXT,
          business_name_line1_txt TEXT, person_nm TEXT, services_desc TEXT
        );
        CREATE INDEX idx_fixture_contractor_filing
          ON irs990_contractor_compensation_grp(filing_id);

        CREATE TABLE irs990_schedule_r_id_related_tax_exempt_org_grp (
          id INTEGER PRIMARY KEY, filing_id TEXT, ein TEXT,
          business_name_line1_txt TEXT, business_name_line2_txt TEXT,
          controlled_organization_ind TEXT, direct_controlling_nacd TEXT,
          primary_activities_txt TEXT
        );
        CREATE INDEX idx_fixture_sched_r_exempt_filing
          ON irs990_schedule_r_id_related_tax_exempt_org_grp(filing_id);

        CREATE TABLE irs990_schedule_r_unrelated_org_txbl_partnership_grp (
          id INTEGER PRIMARY KEY, filing_id TEXT, ein TEXT,
          business_name_line1_txt TEXT, general_or_managing_partner_ind TEXT,
          primary_activities_txt TEXT, ownership_pct NUMERIC,
          share_of_total_income_amt NUMERIC, share_of_eoyassets_amt NUMERIC,
          ubicode_vamt NUMERIC
        );
        CREATE INDEX idx_fixture_sched_r_unrelated_filing
          ON irs990_schedule_r_unrelated_org_txbl_partnership_grp(filing_id);

        INSERT INTO returns VALUES
          ('F1','111111111','Org One',2023,'2023-12-31','2024-05-01',
           '10 Main St','','Springfield','CA','90001','','','','',''),
          ('F2','222222222','Org Two',2024,'2024-12-31','2025-05-01',
           '10 MAIN STREET','','Springfield','CA','90001','','','','',''),
          ('F3','333333333','Org Three',2024,'2024-12-31','2025-05-02',
           '30 Oak Ave','','Oakland','CA','94601','','','','','');

        INSERT INTO grant_recipient_resolved VALUES
          (1,'F1','111111111','Org One',2023,'222222222','Org Two',750,250,1000,
           'Program support','222222222','Org Two','resolved','reported_ein',0.96,''),
          (2,'F1','111111111','Org One',2023,'','Unverified Recipient',50,0,50,
           'Other','','','unresolved','none',0.20,'no_match');
        INSERT INTO grants VALUES (1,'F1'),(2,'F1');

        INSERT INTO officers VALUES
          (1,'F1','Jane Doe','Treasurer',100,20,5),
          (2,'F2','JANE  DOE','Director',80,0,0);

        INSERT INTO irs990_contractor_compensation_grp VALUES
          (1,'F1',500,'','','','','','20 Market St','Los Angeles','CA','90002',
           'Acme Audit LLC','','Audit'),
          (2,'F2',700,'','','','','','20 Market St','Los Angeles','CA','90002',
           'ACME AUDIT LLC','','Audit');

        INSERT INTO irs990_schedule_r_id_related_tax_exempt_org_grp VALUES
          (1,'F1','333333333','Org Three','','Y','','Shared programs');
        INSERT INTO irs990_schedule_r_unrelated_org_txbl_partnership_grp VALUES
          (1,'F1','444444444','Investment LP','N','Investment',25,10,12,3);
        """
    )
    conn.commit()
    conn.close()


def _all_filings(source: Path):
    with closing(network.connect_source_readonly(source)) as conn:
        return network.select_filings(conn, max_filings=10)


def test_rebuild_writes_provenance_year_amounts_indexes_and_hubs(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    before = sqlite3.connect(source).execute(
        "SELECT group_concat(type || ':' || name, '|') FROM sqlite_schema ORDER BY name"
    ).fetchone()[0]

    config = network.BuildConfig(
        min_grant_confidence=0.85,
        person_hub_threshold=1,
        address_hub_threshold=1,
        contractor_hub_threshold=1,
        batch_size=2,
    )
    result = network.rebuild_sidecar(source, sidecar, _all_filings(source), config)
    assert result["filings"] == 3
    assert result["edges"] >= 10

    conn = sqlite3.connect(sidecar)
    conn.row_factory = sqlite3.Row
    grant = conn.execute(
        "SELECT * FROM risk_network_edge WHERE provenance_table='grant_recipient_resolved' AND provenance_row_id='1'"
    ).fetchone()
    assert grant["target_ein"] == "222222222"
    assert grant["tax_year"] == 2023
    assert grant["amount"] == 1000
    assert grant["cash_amount"] == 750
    assert grant["noncash_amount"] == 250
    assert grant["confidence"] == pytest.approx(0.96)
    assert grant["is_scored"] == 1
    assert grant["confidence_basis"] == "deterministic_grant:reported_ein"

    weak_grant = conn.execute(
        "SELECT is_scored,target_type FROM risk_network_edge WHERE provenance_row_id='2' AND edge_type='grant_paid'"
    ).fetchone()
    assert tuple(weak_grant) == (0, "organization_name")

    related = conn.execute(
        "SELECT is_scored,confidence_basis FROM risk_network_edge WHERE edge_type='schedule_r_related_tax_exempt'"
    ).fetchone()
    unrelated = conn.execute(
        "SELECT is_scored,confidence_basis FROM risk_network_edge WHERE edge_type='schedule_r_unrelated_taxable_partnership'"
    ).fetchone()
    assert tuple(related) == (1, "schedule_r_exact_reported_ein")
    assert tuple(unrelated) == (0, "schedule_r_exact_reported_ein")

    person = conn.execute(
        "SELECT DISTINCT hub_degree,hub_suppressed FROM risk_network_edge WHERE target_key='person:JANE DOE'"
    ).fetchone()
    contractor = conn.execute(
        "SELECT DISTINCT hub_degree,hub_suppressed FROM risk_network_edge WHERE target_key='contractor:ACME AUDIT LLC'"
    ).fetchone()
    assert tuple(person) == (2, 1)
    assert tuple(contractor) == (2, 1)

    index_names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='index' AND tbl_name='risk_network_edge'"
    )}
    assert {"idx_risk_edge_source_year", "idx_risk_edge_target_year", "idx_risk_edge_filing"} <= index_names
    assert conn.execute(
        "SELECT value FROM risk_network_build_meta WHERE key='build_status'"
    ).fetchone()[0] == "complete"
    conn.close()

    after = sqlite3.connect(source).execute(
        "SELECT group_concat(type || ':' || name, '|') FROM sqlite_schema ORDER BY name"
    ).fetchone()[0]
    assert after == before

    with closing(_risk_network.connect_readonly(sidecar)) as readonly:
        assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("INSERT INTO risk_network_build_meta VALUES ('bad','write')")
    rows = _risk_network.edges_for_ein(sidecar, "11-1111111", limit=100)
    assert rows and all(row["source_ein"] == "111111111" for row in rows)
    hub_filtered = _risk_network.network_for_ein(
        sidecar, "222222222", main_db_path=str(source)
    )
    assert hub_filtered["shared_neighbors"] == []


def test_po_box_addresses_are_candidate_only_not_scored_identity_edges(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            "UPDATE returns SET us_address_line1='P.O. Box 77' WHERE filing_id IN ('F1','F2')"
        )
        conn.commit()

    network.rebuild_sidecar(source, sidecar, _all_filings(source), network.BuildConfig())
    with closing(sqlite3.connect(sidecar)) as conn:
        rows = conn.execute(
            """SELECT is_scored,confidence_basis,attributes_json
               FROM risk_network_edge
               WHERE edge_type='filed_address' AND filing_id IN ('F1','F2')"""
        ).fetchall()
    assert len(rows) == 2
    assert all(row[0] == 0 for row in rows)
    assert all(row[1] == "po_box_address_candidate_only" for row in rows)
    assert all('"po_box":true' in row[2] for row in rows)


def test_incremental_replaces_only_selected_filings_and_recalculates_old_hub(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    config = network.BuildConfig(person_hub_threshold=1, address_hub_threshold=1,
                                 contractor_hub_threshold=1, batch_size=2)
    network.rebuild_sidecar(source, sidecar, _all_filings(source), config)

    with closing(sqlite3.connect(source)) as conn:
        conn.execute("UPDATE officers SET person_name='John Smith', comp_from_org=150 WHERE filing_id='F1'")
        conn.execute("UPDATE irs990_contractor_compensation_grp SET compensation_amt=900 WHERE filing_id='F1'")
        conn.commit()
    with closing(network.connect_source_readonly(source)) as conn:
        selected = network.select_filings(conn, eins=["111111111"], max_filings=10)
    result = network.incremental_sidecar(source, sidecar, selected, config)
    assert result["filings"] == 1

    conn = sqlite3.connect(sidecar)
    assert conn.execute(
        "SELECT COUNT(*) FROM risk_network_edge WHERE filing_id='F2' AND target_key='person:JANE DOE'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT distinct_org_count,is_hub FROM risk_network_node_stats WHERE target_key='person:JANE DOE'"
    ).fetchone() == (1, 0)
    assert conn.execute(
        "SELECT amount FROM risk_network_edge WHERE filing_id='F1' AND target_key='person:JOHN SMITH'"
    ).fetchone()[0] == 175
    assert conn.execute(
        "SELECT amount FROM risk_network_edge WHERE filing_id='F1' AND target_key='contractor:ACME AUDIT LLC'"
    ).fetchone()[0] == 900
    conn.close()


def test_incremental_preserves_global_coverage_marker_after_full_build(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    config = network.BuildConfig(batch_size=2)
    network.rebuild_full_sidecar(source, sidecar, config, page_size=2)
    with closing(sqlite3.connect(sidecar)) as conn:
        assert conn.execute(
            "SELECT value FROM risk_network_build_meta WHERE key='build_scope'"
        ).fetchone()[0] == "full"

    with closing(network.connect_source_readonly(source)) as conn:
        selected = network.select_filings(conn, eins=["111111111"], max_filings=10)
    network.incremental_sidecar(source, sidecar, selected, config)
    with closing(sqlite3.connect(sidecar)) as conn:
        assert conn.execute(
            "SELECT value FROM risk_network_build_meta WHERE key='build_scope'"
        ).fetchone()[0] == "full_plus_incremental"


def test_incremental_removes_superseded_canonical_filing_edges(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    with closing(sqlite3.connect(source)) as conn:
        conn.executescript(
            """
            CREATE TABLE canonical_by_ein_year (
              ein TEXT NOT NULL, tax_year INTEGER NOT NULL, filing_id TEXT NOT NULL,
              PRIMARY KEY(ein,tax_year)
            ) WITHOUT ROWID;
            INSERT INTO canonical_by_ein_year VALUES
              ('111111111',2023,'F1'),('222222222',2024,'F2'),('333333333',2024,'F3');
            """
        )
        conn.commit()
    config = network.BuildConfig(batch_size=2)
    network.rebuild_full_sidecar(source, sidecar, config, page_size=2)

    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            """INSERT INTO returns
               SELECT 'F1A',ein,org_name,tax_year,period_end,'2024-06-01',
                      '99 New St',us_address_line2,city,state,zip,
                      foreign_address_line1,foreign_city,foreign_province,
                      foreign_country,foreign_postal_code
               FROM returns WHERE filing_id='F1'"""
        )
        conn.execute(
            "UPDATE canonical_by_ein_year SET filing_id='F1A' WHERE ein='111111111' AND tax_year=2023"
        )
        conn.commit()
    with closing(network.connect_source_readonly(source)) as conn:
        selected = network.select_filings(conn, eins=["111111111"], max_filings=10)
    assert [filing.filing_id for filing in selected] == ["F1A"]
    result = network.incremental_sidecar(source, sidecar, selected, config)
    assert result["stale_canonical_filings_removed"] == 1
    with closing(sqlite3.connect(sidecar)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM risk_network_filing_state WHERE filing_id='F1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM risk_network_edge WHERE filing_id='F1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM risk_network_filing_state WHERE filing_id='F1A'"
        ).fetchone()[0] == 1


def test_incremental_selection_is_required_and_capped(tmp_path):
    source = tmp_path / "source.db"
    _create_source(source)
    with closing(network.connect_source_readonly(source)) as conn:
        with pytest.raises(RuntimeError, match="safety cap"):
            network.select_filings(conn, min_tax_year=2023, max_filings=1)
    with pytest.raises(RuntimeError, match="incremental requires"):
        network.main(["incremental", "--db", str(source), "--sidecar", str(tmp_path / "risk.db")])


def test_canonical_filings_are_default_when_index_is_available(tmp_path):
    source = tmp_path / "source.db"
    _create_source(source)
    with sqlite3.connect(source) as conn:
        conn.executescript(
            """
            CREATE TABLE canonical_by_ein_year (
              ein TEXT NOT NULL, tax_year INTEGER NOT NULL, filing_id TEXT NOT NULL,
              PRIMARY KEY(ein,tax_year)
            ) WITHOUT ROWID;
            INSERT INTO canonical_by_ein_year VALUES
              ('111111111',2023,'F1'),('222222222',2024,'F2');
            """
        )
        conn.commit()
    with closing(network.connect_source_readonly(source)) as conn:
        canonical = network.select_filings(conn, max_filings=10)
        all_returns = network.select_filings(conn, max_filings=10, canonical_only=False)
    assert [filing.filing_id for filing in canonical] == ["F1", "F2"]
    assert [filing.filing_id for filing in all_returns] == ["F1", "F2", "F3"]


def test_enhanced_grant_view_takes_precedence_when_present(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    with sqlite3.connect(source) as conn:
        conn.executescript(
            """
            CREATE TABLE grant_recipient_ai_applied (
              grant_id INTEGER PRIMARY KEY, selected_ein TEXT, selected_name TEXT,
              ai_confidence NUMERIC, model TEXT
            );
            INSERT INTO grant_recipient_ai_applied VALUES
              (1,'555555555','AI Selected Recipient',0.99,'rule:test'),
              (2,'666666666','Unverified Filing EIN',0.99,'rule:reported_ein_from_filing_unverified');
            CREATE VIEW grant_recipient_resolved_plus_ai_v1 AS
            SELECT rr.*,
                   aa.selected_ein AS ai_resolved_ein,
                   aa.selected_name AS ai_resolved_name,
                   aa.ai_confidence,
                   CASE WHEN aa.selected_ein IS NOT NULL THEN aa.selected_ein ELSE rr.resolved_ein END AS final_resolved_ein,
                   CASE WHEN aa.selected_ein IS NOT NULL THEN aa.selected_name ELSE rr.resolved_org_name END AS final_resolved_org_name,
                   CASE
                     WHEN aa.model='rule:reported_ein_from_filing_unverified' THEN 'reported_ein_from_filing_unverified'
                     WHEN aa.selected_ein IS NOT NULL THEN 'reported_ein_rule'
                     ELSE 'deterministic'
                   END AS final_match_source,
                   CASE WHEN aa.selected_ein IS NOT NULL THEN aa.ai_confidence ELSE rr.confidence END AS final_confidence
            FROM grant_recipient_resolved rr
            LEFT JOIN grant_recipient_ai_applied aa ON aa.grant_id=rr.grant_id;
            """
        )
        conn.commit()
    network.rebuild_sidecar(source, sidecar, _all_filings(source), network.BuildConfig())
    with sqlite3.connect(sidecar) as conn:
        row = conn.execute(
            """SELECT target_ein,target_name,confidence,confidence_basis,provenance_table
               FROM risk_network_edge WHERE edge_type='grant_paid' AND provenance_row_id='1'"""
        ).fetchone()
    assert row == (
        "555555555", "AI Selected Recipient", 0.99,
        "enhanced_grant:reported_ein_rule", "grant_recipient_resolved_plus_ai_v1",
    )
    with sqlite3.connect(sidecar) as conn:
        unverified = conn.execute(
            """SELECT target_ein,is_scored,confidence_basis
               FROM risk_network_edge WHERE edge_type='grant_paid' AND provenance_row_id='2'"""
        ).fetchone()
    assert unverified == (
        "666666666", 0,
        "enhanced_grant:reported_ein_from_filing_unverified",
    )


def test_runtime_network_returns_indexed_incoming_shared_neighbors_and_metadata(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    network.rebuild_sidecar(source, sidecar, _all_filings(source), network.BuildConfig())

    snapshot = _risk_network.network_for_ein(
        sidecar, "22-2222222", min_tax_year=2023, max_tax_year=2024,
        outgoing_limit=20, incoming_limit=20, shared_target_limit=20,
        shared_edge_limit=20, main_db_path=str(source),
    )
    assert snapshot["build"]["build_status"] == "complete"
    assert snapshot["build"]["build_scope"] == "selected"
    assert snapshot["coverage"]["covered"] is True
    assert snapshot["coverage"]["covered_tax_years"] == [2024]
    assert snapshot["sources"]
    incoming_grant = next(row for row in snapshot["incoming"] if row["edge_type"] == "grant_paid")
    assert incoming_grant["source_ein"] == "111111111"
    assert incoming_grant["tax_year"] == 2023
    assert incoming_grant["amount"] == 1000
    assert incoming_grant["provenance_table"] == "grant_recipient_resolved"
    assert incoming_grant["confidence"] == pytest.approx(0.96)
    assert incoming_grant["attributes"]["purpose"] == "Program support"

    shared = snapshot["shared_neighbors"]
    assert any(row["source_ein"] == "111111111" and row["target_key"] == "person:JANE DOE" for row in shared)
    assert any(row["source_ein"] == "111111111" and row["target_key"] == "contractor:ACME AUDIT LLC" for row in shared)
    assert all(row["hub_suppressed"] == 0 for row in shared)
    assert all(row["provenance_table"] and row["tax_year"] for row in shared)

    default_outgoing = _risk_network.network_for_ein(
        sidecar, "111111111", main_db_path=str(source)
    )["outgoing"]
    all_outgoing = _risk_network.network_for_ein(
        sidecar, "111111111", include_unscored=True,
        main_db_path=str(source),
    )["outgoing"]
    assert not any(row["provenance_row_id"] == "2" and row["edge_type"] == "grant_paid" for row in default_outgoing)
    assert any(row["provenance_row_id"] == "2" and row["edge_type"] == "grant_paid" for row in all_outgoing)
    metadata = _risk_network.build_metadata(sidecar, main_db_path=str(source))
    assert metadata["meta"]["schema_version"] == network.SCHEMA_VERSION


def test_builder_refuses_source_as_sidecar_at_every_write_boundary(tmp_path):
    source = tmp_path / "source.db"
    _create_source(source)
    config = network.BuildConfig()
    filings = _all_filings(source)[:1]

    with pytest.raises(RuntimeError, match="separate file"):
        network.rebuild_sidecar(source, source, filings, config)
    with pytest.raises(RuntimeError, match="separate file"):
        network.rebuild_full_sidecar(source, source, config, eins=["111111111"])
    with pytest.raises(RuntimeError, match="separate file"):
        network.incremental_sidecar(source, source, filings, config)
    with pytest.raises(RuntimeError, match="separate file"):
        network.main([
            "rebuild", "--db", str(source), "--sidecar", str(source),
            "--ein", "111111111", "--yes",
        ])

    with closing(sqlite3.connect(source)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 3


def test_runtime_and_incremental_reject_incompatible_sidecar_lineage(tmp_path):
    source = tmp_path / "source.db"
    other_source = tmp_path / "other.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _create_source(other_source)
    config = network.BuildConfig()
    network.rebuild_sidecar(source, sidecar, _all_filings(source), config)

    with closing(network.connect_source_readonly(other_source)) as conn:
        other_filings = network.select_filings(conn, eins=["111111111"], max_filings=10)
    with pytest.raises(RuntimeError, match="different source database"):
        network.incremental_sidecar(other_source, sidecar, other_filings, config)

    replacement = tmp_path / "replacement.db"
    _create_source(replacement)
    replacement.replace(source)
    with closing(network.connect_source_readonly(source)) as conn:
        replacement_filings = network.select_filings(conn, eins=["111111111"], max_filings=10)
    with pytest.raises(RuntimeError, match="different source database"):
        network.incremental_sidecar(source, sidecar, replacement_filings, config)

    with closing(sqlite3.connect(sidecar)) as conn:
        conn.execute(
            "UPDATE risk_network_build_meta SET value='999' WHERE key='schema_version'"
        )
        conn.commit()
    assert _risk_network.available(environ={"IRS_RISK_NETWORK_DB_PATH": str(sidecar)}) is False
    with pytest.raises(RuntimeError, match="schema-incompatible"):
        _risk_network.network_for_ein(sidecar, "111111111")


def test_runtime_rejects_same_path_atomic_source_replacement(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    network.rebuild_sidecar(
        source, sidecar, _all_filings(source), network.BuildConfig()
    )
    runtime_env = {
        "IRS_DB_PATH": str(source),
        "IRS_RISK_NETWORK_DB_PATH": str(sidecar),
    }

    assert _risk_network.available(environ=runtime_env) is True
    assert _risk_network.build_metadata(sidecar, environ=runtime_env)["meta"][
        "source_lineage_id"
    ] == network.source_lineage_id(source)
    assert _risk_network.network_for_ein(
        sidecar, "111111111", environ=runtime_env
    )["coverage"]["covered"] is True

    replacement = tmp_path / "replacement.db"
    _create_source(replacement)
    replacement.replace(source)

    assert _risk_network.available(environ=runtime_env) is False
    with pytest.raises(RuntimeError, match="sidecar is stale"):
        _risk_network.build_metadata(sidecar, environ=runtime_env)
    with pytest.raises(RuntimeError, match="sidecar is stale"):
        _risk_network.network_for_ein(
            sidecar, "111111111", environ=runtime_env
        )


def test_runtime_rejects_in_place_source_stat_change(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    network.rebuild_sidecar(
        source, sidecar, _all_filings(source), network.BuildConfig()
    )
    original_lineage = network.source_lineage_id(source)
    original_stat = source.stat()

    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            "UPDATE returns SET org_name='Changed After Network Build' "
            "WHERE filing_id='F1'"
        )
        conn.commit()
    changed_stat = source.stat()
    if (
        changed_stat.st_size == original_stat.st_size
        and changed_stat.st_mtime_ns == original_stat.st_mtime_ns
    ):
        os.utime(
            source,
            ns=(changed_stat.st_atime_ns, changed_stat.st_mtime_ns + 1_000_000_000),
        )
        changed_stat = source.stat()

    assert network.source_lineage_id(source) == original_lineage
    assert (
        changed_stat.st_size != original_stat.st_size
        or changed_stat.st_mtime_ns != original_stat.st_mtime_ns
    )
    runtime_env = {"IRS_RISK_NETWORK_DB_PATH": str(sidecar)}
    assert _risk_network.available(
        main_db_path=str(source), environ=runtime_env
    ) is False
    with pytest.raises(RuntimeError, match="sidecar is stale"):
        _risk_network.build_metadata(sidecar, main_db_path=str(source))
    with pytest.raises(RuntimeError, match="sidecar is stale"):
        _risk_network.network_for_ein(
            sidecar, "111111111", main_db_path=str(source)
        )


def test_source_lineage_allows_in_place_amendments_but_detects_replacement(tmp_path):
    source = tmp_path / "source.db"
    _create_source(source)
    original_lineage = network.source_lineage_id(source)

    # Mutate the first ordered return and add an amendment. The old lineage
    # algorithm hashed these rows and incorrectly treated this as a new DB.
    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            "UPDATE returns SET org_name='Org One Amended', return_ts='2024-06-01' "
            "WHERE filing_id='F1'"
        )
        conn.execute(
            """INSERT INTO returns
               SELECT 'F0-AMEND',ein,'Earlier Amendment',tax_year,period_end,
                      '2024-06-02',us_address_line1,us_address_line2,city,state,zip,
                      foreign_address_line1,foreign_city,foreign_province,
                      foreign_country,foreign_postal_code
               FROM returns WHERE filing_id='F1'"""
        )
        conn.commit()
    assert network.source_lineage_id(source) == original_lineage

    replacement = tmp_path / "replacement.db"
    _create_source(replacement)
    replacement.replace(source)
    assert network.source_lineage_id(source) != original_lineage
