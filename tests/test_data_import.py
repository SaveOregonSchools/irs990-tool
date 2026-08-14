import json
from pathlib import Path

import pytest

import data_import
import grant_ai_assist_v1 as grant_ai


def make_options(tmp_path: Path, *, bmf_updated=False, source=False):
    project = tmp_path / "project"
    xml_dir = tmp_path / "new-xml"
    db_path = project / "db" / "irs990.db"
    bmf_dir = project / "eo-bmf"
    xml_dir.mkdir(parents=True)
    db_path.parent.mkdir(parents=True)
    bmf_dir.mkdir(parents=True)
    (xml_dir / "filing.xml").write_text("<Return />", encoding="utf-8")
    db_path.write_bytes(b"sqlite fixture")
    for index, name in enumerate(data_import.BMF_FILENAMES, 1):
        (bmf_dir / name).write_text(f"old-{index}", encoding="utf-8")

    source_dir = None
    if source:
        source_dir = tmp_path / "downloaded-bmf"
        source_dir.mkdir()
        for index, name in enumerate(data_import.BMF_FILENAMES, 1):
            (source_dir / name).write_text(f"new-{index}", encoding="utf-8")

    return data_import.ImportOptions(
        xml_dirs=(xml_dir,),
        db_path=db_path,
        work_db_path=db_path.parent / "grant_matching_work.db",
        bmf_updated=bmf_updated,
        bmf_source_dir=source_dir,
        project_dir=project,
        python_executable="python",
    )


def test_pipeline_contains_safe_append_and_all_deterministic_stages(tmp_path, monkeypatch):
    monkeypatch.delenv("IRS_XML_ROOT", raising=False)
    options = make_options(tmp_path)

    steps = data_import.build_pipeline(options, "test-run")
    keys = [step.key for step in steps]

    assert keys[0] == "preflight_1"
    assert "append_xml" in keys
    assert "copy_bmf" not in keys
    assert keys[-1] == "checkpoint"
    assert keys.index("preflight_1") < keys.index("append_xml") < keys.index("resolve_grants")
    assert {
        "build_identity",
        "build_signatures",
        "candidates_fast",
        "candidates_balanced",
        "reported_ein_triage",
        "nonadjudicable_triage",
        "candidate_rules",
        "apply_decisions",
        "app_stats",
    }.issubset(keys)
    assert "grant_stats" not in keys

    append_command = steps[keys.index("append_xml")].command
    assert "--append" in append_command
    assert "--db" in append_command
    assert append_command.count("--xml-dir") == 1
    candidate_command = steps[keys.index("candidate_rules")].command
    assert candidate_command[candidate_command.index("--rules") + 1] == data_import.GUIDED_CANDIDATE_RULES
    assert "--guided-import-rule-plan" in candidate_command
    app_stats_command = steps[keys.index("app_stats")].command
    assert "--grant-stats-csv" in app_stats_command
    assert "adjudicate" not in " ".join(" ".join(step.command) for step in steps)


def test_combined_candidate_rule_pass_contains_every_legacy_rule_bucket():
    expected = {
        "exact_name_zip",
        "exact_name_city_state",
        "exact_address_zip_good_name",
        "single_candidate_high_score",
        "exact_name_state_only",
        *grant_ai.CANDIDATE_RULES_LARGE_SAFE_REMAINING,
        *grant_ai.CANDIDATE_RULES_ADDRESS_NAME_REMAINING,
        *grant_ai.CANDIDATE_RULES_EXACT_NAME_NO_GEO,
    }

    assert grant_ai.parse_rule_list(data_import.GUIDED_CANDIDATE_RULES) == expected


def test_updated_bmf_copy_occurs_after_preflight_and_keeps_backup(tmp_path, monkeypatch):
    monkeypatch.delenv("IRS_XML_ROOT", raising=False)
    options = make_options(tmp_path, bmf_updated=True, source=True)
    steps = data_import.build_pipeline(options, "test-run")
    keys = [step.key for step in steps]
    assert keys.index("preflight_1") < keys.index("copy_bmf") < keys.index("append_xml")

    messages = []
    data_import.copy_bmf_files(options, messages.append)

    destination = options.project_dir / "eo-bmf"
    for index, name in enumerate(data_import.BMF_FILENAMES, 1):
        assert (destination / name).read_text(encoding="utf-8") == f"new-{index}"
    backups = list((destination / "backups").glob("*/eo1.csv"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old-1"


def test_validation_rejects_missing_bmf_file(tmp_path):
    options = make_options(tmp_path)
    (options.project_dir / "eo-bmf" / "eo4.csv").unlink()

    with pytest.raises(ValueError, match="eo4.csv"):
        data_import.validate_options(options)


def test_inventory_rebuild_only_uses_configured_archive_root(tmp_path, monkeypatch):
    options = make_options(tmp_path)
    monkeypatch.setenv("IRS_XML_ROOT", str(tmp_path))
    monkeypatch.setenv("IRS_XML_INVENTORY_PATH", str(tmp_path / "inventory.db"))

    steps = data_import.build_pipeline(options, "test-run")
    inventory = next(step for step in steps if step.key == "source_inventory")

    assert inventory.command
    assert str(tmp_path.resolve()) in inventory.command
    assert str((tmp_path / "inventory.db").resolve()) in inventory.command
    assert "--report-csv" not in inventory.command


def test_inventory_full_audit_csv_is_opt_in(tmp_path, monkeypatch):
    options = make_options(tmp_path)
    monkeypatch.setenv("IRS_XML_ROOT", str(tmp_path))
    monkeypatch.setenv("IRS_XML_WRITE_FULL_AUDIT_CSV", "true")

    inventory = next(
        step for step in data_import.build_pipeline(options, "test-run")
        if step.key == "source_inventory"
    )

    assert "--report-csv" in inventory.command


def test_multiple_xml_directories_are_all_preflighted_before_any_append(tmp_path, monkeypatch):
    monkeypatch.delenv("IRS_XML_ROOT", raising=False)
    options = make_options(tmp_path)
    second = tmp_path / "second-xml"
    second.mkdir()
    (second / "second.xml").write_text("<Return />", encoding="utf-8")
    options = data_import.ImportOptions(
        **{**options.__dict__, "xml_dirs": (options.xml_dirs[0], second)}
    )

    steps = data_import.build_pipeline(options, "test-run")
    keys = [step.key for step in steps]

    assert keys[:3] == ["preflight_1", "preflight_2", "append_xml"]
    assert str(options.xml_dirs[0]) in steps[0].command
    assert str(second) in steps[1].command
    append_command = steps[2].command
    assert append_command.count("--xml-dir") == 2
    assert str(options.xml_dirs[0]) in append_command
    assert str(second) in append_command


def test_manager_writes_timed_machine_readable_summary(tmp_path, monkeypatch):
    options = make_options(tmp_path)
    monkeypatch.setattr(
        data_import,
        "build_pipeline",
        lambda _options, _run_id: [
            data_import.PipelineStep(
                key="fixture",
                label="Fixture action",
                action=lambda log: log("fixture output"),
            )
        ],
    )
    manager = data_import.ImportManager()

    run_id = manager.start(options)
    assert manager._thread is not None
    manager._thread.join(timeout=5)

    state = manager.snapshot()
    summary_path = options.project_dir / "exports" / f"data_import_{run_id}" / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    log_text = Path(state["log_path"]).read_text(encoding="utf-8")
    assert state["status"] == "completed"
    assert summary["steps"][0]["status"] == "completed"
    assert summary["steps"][0]["duration_seconds"] is not None
    assert summary["steps"][0]["started_at"]
    assert summary["steps"][0]["finished_at"]
    assert "fixture output" in log_text
    assert log_text.startswith("[")


def test_adjudication_instructions_include_dry_run_then_real_import(tmp_path):
    options = make_options(tmp_path)
    commands = data_import.adjudication_instructions(options)

    assert "export-adjudication-batches" in commands[0]
    assert "--dry-run" in commands[1]
    assert "--dry-run" not in commands[2]
    assert "apply-decisions" in commands[3]


def test_flask_data_import_page_is_linked_and_renders():
    import app as app_module

    client = app_module.app.test_client()
    home = client.get("/")
    page = client.get("/data-import")

    assert home.status_code == 200
    assert b"Import New IRS Data" in home.data
    assert page.status_code == 200
    assert b"Directories containing new IRS XML files" in page.data


def test_directory_browser_starts_at_configured_xml_root(tmp_path, monkeypatch):
    import app as app_module

    (tmp_path / "2025").mkdir()
    monkeypatch.setenv("IRS_XML_ROOT", str(tmp_path))
    client = app_module.app.test_client()
    response = client.get("/data-import/directories")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["path"] == str(tmp_path.resolve())
    assert payload["parent"] is None
    assert payload["directories"] == [
        {"name": "2025", "path": str((tmp_path / "2025").resolve())}
    ]
