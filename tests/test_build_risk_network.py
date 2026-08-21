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


def _prepare_full_source(path: Path, *, include_enhanced: bool = True) -> None:
    """Add the empty-but-indexed production source families required by --full."""

    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS canonical_by_ein_year (
              ein TEXT NOT NULL, tax_year INTEGER NOT NULL, filing_id TEXT NOT NULL,
              PRIMARY KEY(ein,tax_year)
            ) WITHOUT ROWID;
            INSERT OR IGNORE INTO canonical_by_ein_year VALUES
              ('111111111',2023,'F1'),('222222222',2024,'F2'),('333333333',2024,'F3');
            CREATE INDEX IF NOT EXISTS idx_fixture_canonical_filing
              ON canonical_by_ein_year(filing_id);

            CREATE TABLE IF NOT EXISTS highest_comp_employees (
              id INTEGER PRIMARY KEY, filing_id TEXT, person_name TEXT, title_txt TEXT,
              comp_from_org NUMERIC, comp_from_related NUMERIC, other_compensation NUMERIC
            );
            CREATE INDEX IF NOT EXISTS idx_fixture_highcomp_filing
              ON highest_comp_employees(filing_id);
            CREATE TABLE IF NOT EXISTS former_key_people (
              id INTEGER PRIMARY KEY, filing_id TEXT, person_name TEXT, title_txt TEXT,
              comp_from_org NUMERIC, comp_from_related NUMERIC, other_compensation NUMERIC
            );
            CREATE INDEX IF NOT EXISTS idx_fixture_former_filing
              ON former_key_people(filing_id);
            CREATE TABLE IF NOT EXISTS irs990_ez_officer_director_trustee_empl_grp (
              id INTEGER PRIMARY KEY, filing_id TEXT, person_nm TEXT, title_txt TEXT,
              compensation_amt NUMERIC, employee_benefit_program_amt NUMERIC,
              expense_account_other_allwnc_amt NUMERIC
            );
            CREATE INDEX IF NOT EXISTS idx_fixture_ez_person_filing
              ON irs990_ez_officer_director_trustee_empl_grp(filing_id);
            CREATE TABLE IF NOT EXISTS irs990_pf_officer_dir_trst_key_empl_info_grp (
              id INTEGER PRIMARY KEY, filing_id TEXT, person_nm TEXT, title_txt TEXT,
              compensation_amt NUMERIC, employee_benefits_amt NUMERIC,
              expense_account_amt NUMERIC
            );
            CREATE INDEX IF NOT EXISTS idx_fixture_pf_person_filing
              ON irs990_pf_officer_dir_trst_key_empl_info_grp(filing_id);
            CREATE TABLE IF NOT EXISTS irs990_schedule_j_rltd_org_officer_trst_key_empl_grp (
              id INTEGER PRIMARY KEY, filing_id TEXT, person_nm TEXT, title_txt TEXT,
              total_compensation_filing_org_amt NUMERIC,
              total_compensation_rltd_orgs_amt NUMERIC
            );
            CREATE INDEX IF NOT EXISTS idx_fixture_schedj_person_filing
              ON irs990_schedule_j_rltd_org_officer_trst_key_empl_grp(filing_id);

            CREATE TABLE IF NOT EXISTS irs990_schedule_r_id_related_org_txbl_corp_tr_grp (
              id INTEGER PRIMARY KEY, filing_id TEXT, ein TEXT,
              related_organization_name_business_name_line1_txt TEXT,
              related_organization_name_business_name_line2_txt TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fixture_schedr_corp_filing
              ON irs990_schedule_r_id_related_org_txbl_corp_tr_grp(filing_id);
            CREATE TABLE IF NOT EXISTS irs990_schedule_r_id_related_org_txbl_partnership_grp (
              id INTEGER PRIMARY KEY, filing_id TEXT, ein TEXT,
              related_organization_name_business_name_line1_txt TEXT,
              related_organization_name_business_name_line2_txt TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fixture_schedr_partnership_filing
              ON irs990_schedule_r_id_related_org_txbl_partnership_grp(filing_id);
            CREATE TABLE IF NOT EXISTS irs990_schedule_r_id_disregarded_entities_grp (
              id INTEGER PRIMARY KEY, filing_id TEXT,
              disregarded_entity_name_business_name_line1_txt TEXT,
              disregarded_entity_name_business_name_line2_txt TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fixture_schedr_disregarded_filing
              ON irs990_schedule_r_id_disregarded_entities_grp(filing_id);
            CREATE TABLE IF NOT EXISTS irs990_schedule_r_transactions_related_org_grp (
              id INTEGER PRIMARY KEY, filing_id TEXT,
              business_name_line1_txt TEXT, business_name_line2_txt TEXT,
              involved_amt NUMERIC, transaction_type_txt TEXT,
              method_of_amount_determination_txt TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fixture_schedr_transactions_filing
              ON irs990_schedule_r_transactions_related_org_grp(filing_id);

            CREATE TABLE IF NOT EXISTS grant_recipient_ai_applied (
              grant_id INTEGER PRIMARY KEY,
              signature_hash TEXT NOT NULL,
              selected_ein TEXT NOT NULL,
              selected_name TEXT,
              ai_confidence NUMERIC,
              ai_decision TEXT,
              model TEXT,
              applied_at TEXT
            );
            """
        )
        if include_enhanced:
            conn.executescript(
                """
                DROP VIEW IF EXISTS grant_recipient_resolved_plus_ai_v1;
                CREATE VIEW grant_recipient_resolved_plus_ai_v1 AS
                SELECT rr.*,
                       aa.selected_ein AS ai_resolved_ein,
                       aa.selected_name AS ai_resolved_name,
                       aa.ai_confidence,
                       aa.ai_decision,
                       CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein<>''
                            THEN aa.selected_ein ELSE rr.resolved_ein END AS final_resolved_ein,
                       CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein<>''
                            THEN aa.selected_name ELSE rr.resolved_org_name END AS final_resolved_org_name,
                       CASE
                         WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein<>''
                           AND aa.model='rule:reported_ein_identity_lookup'
                           THEN 'reported_ein_identity_lookup'
                         WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein<>''
                           AND aa.model='rule:reported_ein_address_location'
                           THEN 'reported_ein_address_location'
                         WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein<>''
                           AND aa.model='rule:reported_ein_from_filing_unverified'
                           THEN 'reported_ein_from_filing_unverified'
                         WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein<>''
                           AND aa.model LIKE 'rule:%'
                           THEN 'reported_ein_rule'
                         WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein<>''
                           THEN 'ai_assisted'
                         ELSE 'deterministic'
                       END AS final_match_source,
                       CASE WHEN aa.selected_ein IS NOT NULL AND aa.selected_ein<>''
                            THEN aa.ai_confidence ELSE rr.confidence END AS final_confidence
                FROM grant_recipient_resolved AS rr
                LEFT JOIN grant_recipient_ai_applied AS aa ON aa.grant_id=rr.grant_id;
                """
            )
        conn.commit()


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
    _prepare_full_source(source)
    config = network.BuildConfig(batch_size=2)
    network.rebuild_full_sidecar(source, sidecar, config, page_size=2)
    with closing(sqlite3.connect(sidecar)) as conn:
        assert conn.execute(
            "SELECT value FROM risk_network_build_meta WHERE key='build_scope'"
        ).fetchone()[0] == "full"
        meta = dict(conn.execute("SELECT key,value FROM risk_network_build_meta"))
        assert meta["full_source_preflight"] == "complete"
        assert meta["enhanced_grant_view_name"] == network.ENHANCED_GRANT_VIEW
        assert meta["source_grant_count"] == "2"
        assert meta["source_resolver_count"] == "2"
        assert meta["source_enhanced_grant_count"] == "2"
        assert meta["source_checkpoint_condition"] == "wal_absent_or_empty"
        assert conn.execute(
            "SELECT object_name,available FROM risk_network_source_status WHERE source_name='grants'"
        ).fetchone() == (network.ENHANCED_GRANT_VIEW, 1)

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
    _prepare_full_source(source)
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
              grant_id INTEGER PRIMARY KEY, signature_hash TEXT NOT NULL,
              selected_ein TEXT NOT NULL, selected_name TEXT,
              ai_confidence NUMERIC, ai_decision TEXT, model TEXT
            );
            INSERT INTO grant_recipient_ai_applied VALUES
              (1,'SIG1','555555555','AI Selected Recipient',0.99,'SELECT_CANDIDATE','rule:test'),
              (2,'SIG2','666666666','Unverified Filing EIN',0.99,'KEEP_REPORTED_EIN','rule:reported_ein_from_filing_unverified');
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


def test_full_build_requires_enhanced_grant_view_and_exact_row_parity(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source, include_enhanced=False)

    with pytest.raises(RuntimeError, match="missing required source objects.*grant_recipient_resolved_plus_ai_v1"):
        network.rebuild_full_sidecar(
            source, sidecar, network.BuildConfig(), page_size=2
        )
    assert not sidecar.exists()

    with closing(sqlite3.connect(source)) as conn:
        conn.executescript(
            """
            CREATE VIEW grant_recipient_resolved_plus_ai_v1 AS
            SELECT rr.*,
                   rr.resolved_ein AS final_resolved_ein,
                   rr.resolved_org_name AS final_resolved_org_name,
                   'deterministic' AS final_match_source,
                   rr.confidence AS final_confidence
            FROM grant_recipient_resolved AS rr
            WHERE rr.grant_id=1;
            """
        )
        conn.commit()
    with pytest.raises(RuntimeError, match="enhanced-grant row parity failed"):
        network.rebuild_full_sidecar(
            source, sidecar, network.BuildConfig(), page_size=2
        )
    assert not sidecar.exists()
    assert not list(tmp_path.glob("risk.db.building-*.db"))


def test_full_preflight_requires_every_leading_filing_id_index(tmp_path):
    source = tmp_path / "source.db"
    _create_source(source)
    _prepare_full_source(source)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("DROP INDEX idx_fixture_schedr_transactions_filing")
        conn.execute(
            """CREATE INDEX idx_fixture_schedr_transactions_partial
               ON irs990_schedule_r_transactions_related_org_grp(filing_id)
               WHERE filing_id<>''"""
        )
        conn.commit()
    with closing(network.connect_source_readonly(source)) as conn:
        with pytest.raises(
            RuntimeError,
            match=r"requires leading filing_id indexes.*irs990_schedule_r_transactions_related_org_grp\(filing_id\)",
        ):
            network.validate_full_source(conn, check_row_parity=False)


def test_full_preflight_requires_real_enhanced_view_and_unique_grant_keys(tmp_path):
    source = tmp_path / "source.db"
    _create_source(source)
    _prepare_full_source(source)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            "CREATE TABLE enhanced_copy AS "
            "SELECT * FROM grant_recipient_resolved_plus_ai_v1"
        )
        conn.execute("DROP VIEW grant_recipient_resolved_plus_ai_v1")
        conn.execute(
            "ALTER TABLE enhanced_copy RENAME TO grant_recipient_resolved_plus_ai_v1"
        )
        conn.commit()
    with closing(network.connect_source_readonly(source)) as conn:
        with pytest.raises(RuntimeError, match=r"wrong type:.*expected view"):
            network.validate_full_source(conn, check_row_parity=False)

    source = tmp_path / "nonunique.db"
    _create_source(source)
    _prepare_full_source(source)
    with closing(sqlite3.connect(source)) as conn:
        conn.executescript(
            """
            DROP VIEW grant_recipient_resolved_plus_ai_v1;
            ALTER TABLE grant_recipient_resolved RENAME TO resolver_with_pk;
            CREATE TABLE grant_recipient_resolved AS SELECT * FROM resolver_with_pk;
            DROP TABLE resolver_with_pk;
            CREATE INDEX idx_nonunique_resolver_grant
              ON grant_recipient_resolved(grant_id);
            CREATE INDEX idx_nonunique_resolver_filing
              ON grant_recipient_resolved(filing_id);
            CREATE VIEW grant_recipient_resolved_plus_ai_v1 AS
            SELECT rr.*,
                   rr.resolved_ein AS final_resolved_ein,
                   rr.resolved_org_name AS final_resolved_org_name,
                   'deterministic' AS final_match_source,
                   rr.confidence AS final_confidence
            FROM grant_recipient_resolved AS rr;
            """
        )
        conn.commit()
    with closing(network.connect_source_readonly(source)) as conn:
        with pytest.raises(
            RuntimeError,
            match=r"requires exact unique grant-ID keys.*grant_recipient_resolved\(grant_id\)",
        ):
            network.validate_full_source(conn, check_row_parity=False)


def test_full_preflight_rejects_grant_filing_ownership_mismatch(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            "UPDATE grant_recipient_resolved SET filing_id='F2' WHERE grant_id=1"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="grant filing parity failed"):
        network.rebuild_full_sidecar(
            source, sidecar, network.BuildConfig(), page_size=1
        )
    assert not sidecar.exists()
    assert not list(tmp_path.glob("risk.db.building-*.db"))


def test_full_plan_runs_structural_preflight_without_writing(tmp_path, capsys):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    assert network.main([
        "plan", "--db", str(source), "--sidecar", str(sidecar), "--full",
    ]) == 0
    output = capsys.readouterr().out
    assert network.ENHANCED_GRANT_VIEW in output
    assert "Checkpoint state:   source WAL absent or empty" in output
    assert "exact grant row parity runs at rebuild start" in output
    assert not sidecar.exists()


def test_full_build_refuses_publication_if_source_changes_during_snapshot(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    with closing(sqlite3.connect(source)) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.commit()

    sidecar.write_bytes(b"existing-sidecar-must-survive")
    original_build_edges = network.NetworkBuilder.build_edges
    mutated = False

    def build_then_mutate(builder):
        nonlocal mutated
        result = original_build_edges(builder)
        if not mutated:
            with closing(sqlite3.connect(source, timeout=10)) as writer:
                writer.execute(
                    "UPDATE returns SET org_name='Concurrent mutation' WHERE filing_id='F1'"
                )
                writer.commit()
            mutated = True
        return result

    monkeypatch.setattr(network.NetworkBuilder, "build_edges", build_then_mutate)
    with pytest.raises(RuntimeError, match="changed during the risk-network build"):
        network.rebuild_full_sidecar(
            source, sidecar, network.BuildConfig(), page_size=2
        )
    assert mutated is True
    assert sidecar.read_bytes() == b"existing-sidecar-must-survive"
    assert not list(tmp_path.glob("risk.db.building-*.db"))


@pytest.mark.parametrize(
    "match_source",
    ["REPORTED_EIN_FROM_FILING_UNVERIFIED", "unknown_future_source"],
)
def test_full_preflight_rejects_unattributed_enhanced_source_labels(
    tmp_path, match_source
):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("DROP VIEW grant_recipient_resolved_plus_ai_v1")
        conn.execute(f"""
            CREATE VIEW grant_recipient_resolved_plus_ai_v1 AS
            SELECT rr.*,
                   CASE WHEN rr.grant_id=2 THEN '666666666'
                        ELSE rr.resolved_ein END AS final_resolved_ein,
                   CASE WHEN rr.grant_id=2 THEN 'Unverified Recipient'
                        ELSE rr.resolved_org_name END AS final_resolved_org_name,
                   CASE WHEN rr.grant_id=2 THEN '{match_source}'
                        ELSE 'deterministic' END AS final_match_source,
                   CASE WHEN rr.grant_id=2 THEN 0.99
                        ELSE rr.confidence END AS final_confidence
            FROM grant_recipient_resolved AS rr
        """)
        conn.commit()

    with pytest.raises(RuntimeError, match="enhanced-grant provenance"):
        network.rebuild_full_sidecar(
            source, sidecar, network.BuildConfig(), page_size=2
        )
    assert not sidecar.exists()


def test_full_validator_independently_rejects_unsafe_scored_grant(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    result = network.rebuild_full_sidecar(
        source, sidecar, network.BuildConfig(), page_size=2
    )
    with closing(sqlite3.connect(sidecar)) as conn:
        conn.execute(
            """UPDATE risk_network_edge
               SET is_scored=1,
                   target_ein='666666666',
                   confidence=0.99,
                   confidence_basis='enhanced_grant:REPORTED_EIN_FROM_FILING_UNVERIFIED'
               WHERE edge_type='grant_paid' AND provenance_row_id='2'"""
        )
        conn.commit()
    with closing(network.connect_source_readonly(source)) as conn:
        snapshot = network.begin_source_snapshot(
            conn, source, require_checkpointed=True
        )
        base = network.validate_full_source(conn, check_row_parity=True)
        preflight = network.FullSourcePreflight(
            base.grant_count,
            base.resolver_count,
            base.enhanced_count,
            base.filing_indexes,
            network.count_selected_filings(conn),
        )
    with pytest.raises(RuntimeError, match="without approved enhanced provenance"):
        network.validate_completed_sidecar(
            sidecar,
            expected_filings=result["filings"],
            expected_edges=result["edges"],
            expected_scope="full",
            source_snapshot=snapshot,
            full_preflight=preflight,
        )


def test_full_snapshot_requires_truncated_wal(tmp_path):
    source = tmp_path / "source.db"
    _create_source(source)
    _prepare_full_source(source)
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute(
            "UPDATE returns SET org_name='Committed in WAL' WHERE filing_id='F1'"
        )
        writer.commit()
        with closing(network.connect_source_readonly(source)) as reader:
            with pytest.raises(RuntimeError, match="requires a checkpointed source database"):
                network.begin_source_snapshot(
                    reader, source, require_checkpointed=True
                )


def test_completed_sidecar_validation_rejects_hub_metadata_drift(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    filings = _all_filings(source)
    result = network.rebuild_sidecar(
        source, sidecar, filings, network.BuildConfig()
    )
    with closing(sqlite3.connect(sidecar)) as conn:
        conn.execute(
            "UPDATE risk_network_edge SET hub_degree=hub_degree+1 WHERE edge_id=(SELECT edge_id FROM risk_network_edge LIMIT 1)"
        )
        conn.commit()
    with closing(network.connect_source_readonly(source)) as conn:
        snapshot = network.begin_source_snapshot(
            conn, source, require_checkpointed=False
        )
    with pytest.raises(RuntimeError, match="inconsistent hub metadata"):
        network.validate_completed_sidecar(
            sidecar,
            expected_filings=result["filings"],
            expected_edges=result["edges"],
            expected_scope="selected",
            source_snapshot=snapshot,
        )


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_atomic_rebuild_refuses_populated_destination_auxiliary(tmp_path, suffix):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    sidecar.write_bytes(b"existing-sidecar-must-survive")
    Path(str(sidecar) + suffix).write_bytes(b"populated-old-sqlite-auxiliary")

    with pytest.raises(RuntimeError, match="destination has populated SQLite auxiliary files"):
        network.rebuild_sidecar(
            source, sidecar, _all_filings(source), network.BuildConfig()
        )
    assert sidecar.read_bytes() == b"existing-sidecar-must-survive"
    assert not list(tmp_path.glob("risk.db.building-*.db"))


def test_atomic_rebuild_rechecks_destination_auxiliary_immediately_before_replace(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    sidecar.write_bytes(b"existing-sidecar-must-survive")
    original_validate = network.validate_completed_sidecar

    def validate_then_create_old_wal(*args, **kwargs):
        original_validate(*args, **kwargs)
        Path(str(sidecar) + "-wal").write_bytes(b"late-populated-old-wal")

    monkeypatch.setattr(
        network, "validate_completed_sidecar", validate_then_create_old_wal
    )
    with pytest.raises(RuntimeError, match="destination has populated SQLite auxiliary files"):
        network.rebuild_sidecar(
            source, sidecar, _all_filings(source), network.BuildConfig()
        )
    assert sidecar.read_bytes() == b"existing-sidecar-must-survive"
    assert not list(tmp_path.glob("risk.db.building-*.db"))


def test_full_pagination_is_checked_against_independent_source_count(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    sidecar.write_bytes(b"existing-sidecar-must-survive")
    original_select = network.select_filings

    def truncate_after_first_page(conn, **kwargs):
        if kwargs.get("after_filing_id"):
            return []
        return original_select(conn, **kwargs)

    monkeypatch.setattr(network, "select_filings", truncate_after_first_page)
    with pytest.raises(RuntimeError, match="pagination was incomplete"):
        network.rebuild_full_sidecar(
            source, sidecar, network.BuildConfig(), page_size=2
        )
    assert sidecar.read_bytes() == b"existing-sidecar-must-survive"
    assert not list(tmp_path.glob("risk.db.building-*.db"))


def test_full_build_rejects_invalid_selected_filing_instead_of_truncating(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            """INSERT INTO returns VALUES
               ('F0','','Invalid EIN',2022,'','','','','','','','','','','','')"""
        )
        conn.execute(
            "INSERT INTO canonical_by_ein_year VALUES ('',2022,'F0')"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="blank/invalid filing_id or EIN"):
        network.rebuild_full_sidecar(
            source, sidecar, network.BuildConfig(), page_size=2
        )
    assert not sidecar.exists()
    assert not list(tmp_path.glob("risk.db.building-*.db"))


def test_bounded_builds_verify_selected_filing_metadata_inside_snapshot(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    filings = _all_filings(source)
    sidecar.write_bytes(b"existing-sidecar-must-survive")
    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            "UPDATE returns SET org_name='Changed after selection' WHERE filing_id='F1'"
        )
        conn.commit()
    with pytest.raises(RuntimeError, match="metadata changed before.*snapshot"):
        network.rebuild_sidecar(
            source, sidecar, filings, network.BuildConfig()
        )
    assert sidecar.read_bytes() == b"existing-sidecar-must-survive"

    current = _all_filings(source)
    network.rebuild_sidecar(source, sidecar, current, network.BuildConfig())
    selected = current[:1]
    before = sidecar.read_bytes()
    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            "UPDATE returns SET us_address_line1='Changed after selection' WHERE filing_id='F1'"
        )
        conn.commit()
    with pytest.raises(RuntimeError, match="metadata changed before.*snapshot"):
        network.incremental_sidecar(
            source, sidecar, selected, network.BuildConfig()
        )
    assert sidecar.read_bytes() == before


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


@pytest.mark.parametrize("full", [False, True])
def test_cli_rejects_mixed_valid_and_invalid_ein_without_replacing_sidecar(
    tmp_path, full
):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    sidecar.write_bytes(b"existing-sidecar-must-survive")
    argv = [
        "rebuild", "--db", str(source), "--sidecar", str(sidecar),
        "--ein", "111111111", "--ein", "abc123", "--yes",
    ]
    if full:
        argv.append("--full")

    with pytest.raises(RuntimeError, match="Invalid --ein selector"):
        network.main(argv)

    assert sidecar.read_bytes() == b"existing-sidecar-must-survive"
    assert not list(tmp_path.glob("risk.db.building-*.db"))


def test_selectors_reject_fabricated_eins_missing_filing_ids_and_reversed_years(
    tmp_path,
):
    source = tmp_path / "source.db"
    _create_source(source)
    assert network.normalize_ein("12-3456789") == "123456789"
    assert network.normalize_ein("abc123456789") == ""
    assert network.normalize_ein("123") == ""

    with closing(network.connect_source_readonly(source)) as conn:
        with pytest.raises(RuntimeError, match="Invalid --filing-id selector"):
            network.select_filings(conn, filing_ids=["F1", " "])
        with pytest.raises(RuntimeError, match="were not found"):
            network.select_filings(conn, filing_ids=["F1", "MISSING"])
        with pytest.raises(RuntimeError, match="cannot be greater"):
            network.select_filings(conn, min_tax_year=2024, max_tax_year=2023)


def test_full_rebuild_refuses_zero_match_replacement(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    sidecar.write_bytes(b"existing-sidecar-must-survive")

    with pytest.raises(RuntimeError, match="selected zero filings"):
        network.main([
            "rebuild", "--db", str(source), "--sidecar", str(sidecar),
            "--full", "--ein", "999999999", "--yes",
        ])

    assert sidecar.read_bytes() == b"existing-sidecar-must-survive"
    assert not list(tmp_path.glob("risk.db.building-*.db"))


def test_index_gates_reject_expression_leading_and_nocase_indexes():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE lookup_probe(filing_id TEXT, payload TEXT);
            CREATE INDEX ix_expression_first
              ON lookup_probe(lower(payload), filing_id);
            """
        )
        assert network._index_with_prefix(conn, "lookup_probe", ("filing_id",)) == ""
        plan = [
            str(row[3]).upper()
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM lookup_probe WHERE filing_id=?",
                ("F1",),
            )
        ]
        assert not any("SEARCH" in detail for detail in plan), plan

        conn.executescript(
            """
            DROP INDEX ix_expression_first;
            CREATE INDEX ix_nocase ON lookup_probe(filing_id COLLATE NOCASE);
            """
        )
        assert network._index_with_prefix(conn, "lookup_probe", ("filing_id",)) == ""

        conn.executescript(
            """
            DROP INDEX ix_nocase;
            CREATE INDEX ix_binary ON lookup_probe(filing_id);
            """
        )
        assert network._index_with_prefix(
            conn, "lookup_probe", ("filing_id",)
        ) == "ix_binary"
        plan = [
            str(row[3]).upper()
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM lookup_probe WHERE filing_id=?",
                ("F1",),
            )
        ]
        assert any("SEARCH" in detail for detail in plan), plan


def test_unique_key_gate_rejects_expression_terms_and_nullable_unique_columns():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE expression_key(grant_id INTEGER NOT NULL, payload TEXT);
            CREATE UNIQUE INDEX ux_expression_key
              ON expression_key(grant_id, lower(payload));
            CREATE TABLE nullable_key(grant_id INTEGER UNIQUE);
            CREATE TABLE exact_key(grant_id INTEGER PRIMARY KEY);
            CREATE TABLE descending_integer_key(grant_id INTEGER PRIMARY KEY DESC);
            """
        )
        assert network._unique_index_for_columns(
            conn, "expression_key", ("grant_id",)
        ) == ""
        assert network._unique_index_for_columns(
            conn, "nullable_key", ("grant_id",)
        ) == ""
        assert network._unique_index_for_columns(
            conn, "exact_key", ("grant_id",)
        ) == "PRIMARY KEY"
        assert network._unique_index_for_columns(
            conn, "descending_integer_key", ("grant_id",)
        ) == ""


def test_full_preflight_rejects_sqlite_coercive_grant_id_match(tmp_path):
    source = tmp_path / "source.db"
    _create_source(source)
    _prepare_full_source(source)
    with closing(sqlite3.connect(source)) as conn:
        conn.executescript(
            """
            DROP VIEW grant_recipient_resolved_plus_ai_v1;
            DROP TABLE grants;
            CREATE TABLE grants (
              id TEXT NOT NULL UNIQUE,
              filing_id TEXT NOT NULL
            );
            CREATE INDEX idx_grants_filing_id ON grants(filing_id);
            INSERT INTO grants VALUES ('01','F1'),('2','F1');
            """
        )
        conn.commit()
    _prepare_full_source(source)

    with closing(network.connect_source_readonly(source)) as conn:
        with pytest.raises(RuntimeError, match="grant_id_mismatch"):
            network.validate_full_source(conn, check_row_parity=True)


@pytest.mark.parametrize("full", [False, True])
def test_spoofed_scoreable_enhanced_view_requires_applied_artifact(
    tmp_path, full
):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    sidecar.write_bytes(b"existing-sidecar-must-survive")
    with closing(sqlite3.connect(source)) as conn:
        conn.executescript(
            """
            DROP VIEW grant_recipient_resolved_plus_ai_v1;
            CREATE VIEW grant_recipient_resolved_plus_ai_v1 AS
            SELECT rr.*,
                   rr.grant_id AS __base_grant_id,
                   rr.filing_id AS __base_filing_id,
                   rr.recipient_reported_ein AS __base_recipient_reported_ein,
                   rr.recipient_reported_name AS __base_recipient_reported_name,
                   rr.cash_amount AS __base_cash_amount,
                   rr.noncash_amount AS __base_noncash_amount,
                   rr.total_amount AS __base_total_amount,
                   rr.purpose AS __base_purpose,
                   rr.match_status AS __base_match_status,
                   rr.warning_flags AS __base_warning_flags,
                   rr.resolved_ein AS __base_resolved_ein,
                   rr.resolved_org_name AS __base_resolved_org_name,
                   rr.confidence AS __base_confidence,
                   CASE WHEN rr.grant_id=2 THEN rr.grant_id END AS __applied_grant_id,
                   CASE WHEN rr.grant_id=2 THEN 'SPOOF_SIG' END AS __applied_signature_hash,
                   CASE WHEN rr.grant_id=2 THEN '666666666' END AS __applied_selected_ein,
                   CASE WHEN rr.grant_id=2 THEN 'Spoofed AI Recipient' END AS __applied_selected_name,
                   CASE WHEN rr.grant_id=2 THEN 0.99 END AS __applied_confidence,
                   CASE WHEN rr.grant_id=2 THEN 'SELECT_CANDIDATE' END AS __applied_decision,
                   CASE WHEN rr.grant_id=2 THEN 'model:spoofed' END AS __applied_model,
                   CASE WHEN rr.grant_id=2 THEN '666666666'
                        ELSE rr.resolved_ein END AS final_resolved_ein,
                   CASE WHEN rr.grant_id=2 THEN 'Spoofed AI Recipient'
                        ELSE rr.resolved_org_name END AS final_resolved_org_name,
                   CASE WHEN rr.grant_id=2 THEN 'ai_assisted'
                        ELSE 'deterministic' END AS final_match_source,
                   CASE WHEN rr.grant_id=2 THEN 0.99
                        ELSE rr.confidence END AS final_confidence
            FROM grant_recipient_resolved AS rr;
            """
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="not exactly backed"):
        if full:
            network.rebuild_full_sidecar(
                source, sidecar, network.BuildConfig(), page_size=2
            )
        else:
            network.rebuild_sidecar(
                source, sidecar, _all_filings(source), network.BuildConfig()
            )

    assert sidecar.read_bytes() == b"existing-sidecar-must-survive"
    assert not list(tmp_path.glob("risk.db.building-*.db"))


def test_bounded_sidecar_validator_rejects_unsafe_enhanced_grant(tmp_path):
    source = tmp_path / "source.db"
    sidecar = tmp_path / "risk.db"
    _create_source(source)
    _prepare_full_source(source)
    result = network.rebuild_sidecar(
        source, sidecar, _all_filings(source), network.BuildConfig()
    )
    with closing(sqlite3.connect(sidecar)) as conn:
        conn.execute(
            """UPDATE risk_network_edge
               SET confidence_basis='enhanced_grant:spoofed_source'
               WHERE edge_type='grant_paid' AND is_scored=1"""
        )
        conn.commit()
    with closing(network.connect_source_readonly(source)) as conn:
        snapshot = network.begin_source_snapshot(
            conn, source, require_checkpointed=False
        )
    with pytest.raises(RuntimeError, match="approved enhanced provenance"):
        network.validate_completed_sidecar(
            sidecar,
            expected_filings=result["filings"],
            expected_edges=result["edges"],
            expected_scope="selected",
            source_snapshot=snapshot,
        )
