from pathlib import Path

import pytest

import data_import


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
    assert "append_xml_1" in keys
    assert "copy_bmf" not in keys
    assert keys[-1] == "checkpoint"
    assert keys.index("preflight_1") < keys.index("append_xml_1") < keys.index("resolve_grants")
    assert {
        "build_identity",
        "build_signatures",
        "candidates_fast",
        "candidates_balanced",
        "reported_ein_triage",
        "nonadjudicable_triage",
        "rules_high_confidence",
        "rules_single_high",
        "rules_exact_state",
        "rules_large_safe",
        "rules_address_name",
        "rules_distinctive_name",
        "apply_decisions",
        "grant_stats",
        "app_stats",
    }.issubset(keys)

    append_command = steps[keys.index("append_xml_1")].command
    assert "--append" in append_command
    assert "--db" in append_command
    assert "adjudicate" not in " ".join(" ".join(step.command) for step in steps)


def test_updated_bmf_copy_occurs_after_preflight_and_keeps_backup(tmp_path, monkeypatch):
    monkeypatch.delenv("IRS_XML_ROOT", raising=False)
    options = make_options(tmp_path, bmf_updated=True, source=True)
    steps = data_import.build_pipeline(options, "test-run")
    keys = [step.key for step in steps]
    assert keys.index("preflight_1") < keys.index("copy_bmf") < keys.index("append_xml_1")

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

    assert keys[:4] == ["preflight_1", "preflight_2", "append_xml_1", "append_xml_2"]
    assert str(options.xml_dirs[0]) in steps[0].command
    assert str(second) in steps[1].command


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
