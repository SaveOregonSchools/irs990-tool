import argparse
import sqlite3

import grant_ai_assist_v1 as grant_ai


def test_existing_candidate_indexes_can_be_ensured_without_reanalyzing(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(grant_ai, "CAND_TABLE", "candidate_fixture")
    analyzed = []
    monkeypatch.setattr(
        grant_ai,
        "analyze_tables",
        lambda _conn, tables: analyzed.append(tuple(tables)),
    )
    grant_ai.create_candidate_schema(conn, full_refresh=True, create_indexes=False)

    grant_ai.create_candidate_indexes(conn, analyze=False)

    assert analyzed == []
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_ai_cand_%'"
        )
    }
    assert indexes == {"idx_ai_cand_sig_rank", "idx_ai_cand_ein"}

    grant_ai.create_candidate_indexes(conn)
    assert analyzed == [("candidate_fixture",)]


def test_guided_rule_plan_preserves_default_phase_before_address_overrides(monkeypatch):
    args = argparse.Namespace(
        guided_import_rule_plan=True,
        addr_name_min_name_score=0.75,
        high_address_geo_min_name_score=0.75,
    )
    selected = set().union(*grant_ai.GUIDED_IMPORT_RULE_PHASES)

    def classify(_row, current_args):
        if current_args.addr_name_min_name_score == 0.70:
            return "same_ein_exact_address_zip_moderate_name", "", 0.94
        return "exact_address_zip_good_name", "", 0.97

    monkeypatch.setattr(grant_ai, "classify_candidate_rule", classify)

    assert grant_ai.classify_selected_candidate_rule({}, args, selected) == (
        "exact_address_zip_good_name",
        "",
        0.97,
    )


def test_guided_rule_plan_reaches_reviewed_address_threshold_phase(monkeypatch):
    args = argparse.Namespace(
        guided_import_rule_plan=True,
        addr_name_min_name_score=0.75,
        high_address_geo_min_name_score=0.75,
    )
    selected = set().union(*grant_ai.GUIDED_IMPORT_RULE_PHASES)

    def classify(_row, current_args):
        if current_args.addr_name_min_name_score == 0.70:
            return "same_ein_exact_address_zip_moderate_name", "", 0.94
        return "needs_ai_or_review", "", 0.0

    monkeypatch.setattr(grant_ai, "classify_candidate_rule", classify)

    assert grant_ai.classify_selected_candidate_rule({}, args, selected) == (
        "same_ein_exact_address_zip_moderate_name",
        "",
        0.94,
    )
