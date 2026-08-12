"""Guided XML/EO-BMF import orchestration for the local Flask application.

The heavy lifting remains in the repository's existing, independently runnable
maintenance scripts.  This module validates user input, builds the ordered
command plan, runs one plan at a time, and exposes progress suitable for a web
status page.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
BMF_FILENAMES = ("eo1.csv", "eo2.csv", "eo3.csv", "eo4.csv")


@dataclass(frozen=True)
class ImportOptions:
    xml_dirs: tuple[Path, ...]
    db_path: Path
    work_db_path: Path
    bmf_updated: bool = False
    bmf_source_dir: Optional[Path] = None
    project_dir: Path = PROJECT_ROOT
    python_executable: str = sys.executable

    @classmethod
    def from_values(
        cls,
        *,
        xml_dirs: Sequence[str],
        db_path: str,
        work_db_path: str = "",
        bmf_updated: bool = False,
        bmf_source_dir: str = "",
        project_dir: Path = PROJECT_ROOT,
        python_executable: str = sys.executable,
    ) -> "ImportOptions":
        db = Path(db_path).expanduser().resolve()
        work_db = (
            Path(work_db_path).expanduser().resolve()
            if work_db_path.strip()
            else db.parent / "grant_matching_work.db"
        )
        source = Path(bmf_source_dir).expanduser().resolve() if bmf_source_dir.strip() else None
        resolved_xml_dirs: list[Path] = []
        seen = set()
        for value in xml_dirs:
            if not str(value).strip():
                continue
            path = Path(value).expanduser().resolve()
            key = str(path).casefold()
            if key not in seen:
                seen.add(key)
                resolved_xml_dirs.append(path)
        return cls(
            xml_dirs=tuple(resolved_xml_dirs),
            db_path=db,
            work_db_path=work_db,
            bmf_updated=bmf_updated,
            bmf_source_dir=source,
            project_dir=project_dir.resolve(),
            python_executable=python_executable,
        )


@dataclass(frozen=True)
class PipelineStep:
    key: str
    label: str
    command: tuple[str, ...] = ()
    action: Optional[Callable[[Callable[[str], None]], None]] = field(
        default=None, compare=False, repr=False
    )


def _require_bmf_files(directory: Path, description: str) -> None:
    if not directory.is_dir():
        raise ValueError(f"{description} does not exist or is not a directory: {directory}")
    missing = [name for name in BMF_FILENAMES if not (directory / name).is_file()]
    if missing:
        raise ValueError(f"{description} is missing: {', '.join(missing)}")


def validate_options(options: ImportOptions) -> None:
    if not options.xml_dirs:
        raise ValueError("Enter at least one XML directory.")
    for xml_dir in options.xml_dirs:
        if not xml_dir.is_dir():
            raise ValueError(f"XML directory does not exist: {xml_dir}")
        if not any(xml_dir.rglob("*.xml")):
            raise ValueError(f"No .xml files were found beneath: {xml_dir}")
    if not options.db_path.is_file():
        raise ValueError(f"IRS database does not exist: {options.db_path}")

    if options.bmf_source_dir and not options.bmf_updated:
        raise ValueError("Select 'EO-BMF files were updated' when providing an EO-BMF source directory.")

    bmf_dir = options.project_dir / "eo-bmf"
    if options.bmf_source_dir:
        _require_bmf_files(options.bmf_source_dir, "EO-BMF source directory")
    else:
        _require_bmf_files(bmf_dir, "Project EO-BMF directory")


def copy_bmf_files(options: ImportOptions, log: Callable[[str], None]) -> None:
    source_dir = options.bmf_source_dir
    destination = options.project_dir / "eo-bmf"
    if source_dir is None or source_dir == destination.resolve():
        log("Using EO-BMF files already in the project folder.")
        return

    destination.mkdir(parents=True, exist_ok=True)
    backup_dir = destination / "backups" / datetime.now().strftime("%Y%m%d_%H%M%S")
    existing = [destination / name for name in BMF_FILENAMES if (destination / name).is_file()]
    if existing:
        backup_dir.mkdir(parents=True, exist_ok=False)
        for path in existing:
            shutil.copy2(path, backup_dir / path.name)
        log(f"Backed up existing EO-BMF files to {backup_dir}")

    with tempfile.TemporaryDirectory(prefix="bmf-stage-", dir=destination) as stage_name:
        stage = Path(stage_name)
        for name in BMF_FILENAMES:
            shutil.copy2(source_dir / name, stage / name)
        for name in BMF_FILENAMES:
            os.replace(stage / name, destination / name)
    log(f"Copied {', '.join(BMF_FILENAMES)} into {destination}")


def _python_step(options: ImportOptions, key: str, label: str, *args: str) -> PipelineStep:
    return PipelineStep(
        key=key,
        label=label,
        command=(options.python_executable, str(options.project_dir / args[0]), *args[1:]),
    )


def build_pipeline(options: ImportOptions, run_id: str) -> list[PipelineStep]:
    """Return the complete safe append + deterministic matching plan."""
    validate_options(options)
    run_dir = options.project_dir / "exports" / f"data_import_{run_id}"
    db = str(options.db_path)
    work_db = str(options.work_db_path)
    project = str(options.project_dir)
    common_ai = ("--db", db, "--work-db", work_db)

    steps: list[PipelineStep] = []
    for index, xml_dir in enumerate(options.xml_dirs, 1):
        steps.append(
            _python_step(
                options,
                f"preflight_{index}",
                f"Preflight XML directory {index} of {len(options.xml_dirs)}",
                "rebuild_irs990_slim_clean.py",
                "--xml-dir", str(xml_dir),
                "--preflight",
                "--preflight-report", str(run_dir / f"preflight_{index}_summary.json"),
                "--preflight-csv", str(run_dir / f"preflight_{index}_files.csv"),
            )
        )
    if options.bmf_updated:
        steps.append(
            PipelineStep(
                key="copy_bmf",
                label="Install updated EO-BMF files",
                action=lambda log: copy_bmf_files(options, log),
            )
        )

    for index, xml_dir in enumerate(options.xml_dirs, 1):
        steps.append(
            _python_step(
                options,
                f"append_xml_{index}",
                f"Append XML directory {index} of {len(options.xml_dirs)}",
                "rebuild_irs990_slim_clean.py",
                "--db", db,
                "--xml-dir", str(xml_dir),
                "--append",
            )
        )

    steps.extend(
        [
            _source_inventory_step(options, run_dir),
            _python_step(
                options,
                "resolve_grants",
                "Refresh deterministic grant-recipient resolution",
                "resolve_grant_recipients.py",
                "--db", db,
                "--full-refresh",
                "--batch-size", "100000",
            ),
            _python_step(
                options,
                "verify_bmf",
                "Verify EO-BMF files",
                "grant_ai_assist_v1.py", "verify-bmf", "--project-dir", project,
            ),
            _python_step(
                options,
                "build_identity",
                "Rebuild organization identity",
                "grant_ai_assist_v1.py", "build-identity", *common_ai,
                "--project-dir", project, "--full-refresh",
            ),
            _python_step(
                options,
                "build_signatures",
                "Rebuild grant-recipient signatures",
                "grant_ai_assist_v1.py", "build-signatures", *common_ai, "--full-refresh",
            ),
            _python_step(
                options,
                "candidates_fast",
                "Generate fast candidates",
                "grant_ai_assist_v1.py", "generate-candidates", *common_ai,
                "--full-refresh", "--candidate-mode", "fast",
            ),
            _python_step(
                options,
                "candidates_balanced",
                "Generate balanced candidates for unmatched signatures",
                "grant_ai_assist_v1.py", "generate-candidates", *common_ai,
                "--candidate-mode", "balanced", "--queue-status", "no_candidates",
            ),
            _python_step(
                options,
                "reported_ein_triage",
                "Run reported-EIN triage",
                "grant_ai_assist_v1.py", "reported-ein-triage", *common_ai,
                "--placeholder-action", "human_review",
            ),
            _python_step(
                options,
                "nonadjudicable_triage",
                "Park nonadjudicable and blank-recipient signatures",
                "grant_ai_assist_v1.py", "nonadjudicable-recipient-triage", *common_ai,
                "--action", "human_review", "--include-blank-recipient-name",
            ),
        ]
    )

    rule_groups = (
        ("rules_high_confidence", "Apply highest-confidence candidate rules", "exact_name_zip,exact_name_city_state,exact_address_zip_good_name", ()),
        ("rules_single_high", "Apply single high-score candidate rule", "single_candidate_high_score", ()),
        ("rules_exact_state", "Apply exact name/address/state rule", "exact_name_state_only", ()),
        ("rules_large_safe", "Apply large safe remaining candidate rules", "large_safe_remaining", ()),
        (
            "rules_address_name",
            "Apply reviewed address/name candidate rules",
            "address_name_remaining",
            ("--addr-name-min-name-score", "0.70", "--high-address-geo-min-name-score", "0.70"),
        ),
        ("rules_distinctive_name", "Apply distinctive exact-name/no-geo rule", "exact_name_no_geo_distinctive", ()),
    )
    for key, label, rules, extras in rule_groups:
        steps.append(
            _python_step(
                options, key, label, "grant_ai_assist_v1.py", "candidate-rule-decisions",
                *common_ai, "--rules", rules, *extras,
            )
        )

    steps.extend(
        [
            _python_step(
                options,
                "apply_decisions",
                "Rebuild applied/final enhanced grant layer",
                "grant_ai_assist_v1.py", "apply-decisions", *common_ai, "--full-refresh",
            ),
            _python_step(
                options,
                "grant_stats",
                "Write enhanced grant-match statistics",
                "grant_ai_assist_v1.py", "stats", *common_ai,
                "--csv-out", str(run_dir / "grant_match_stats.csv"),
            ),
            _python_step(
                options,
                "app_stats",
                "Refresh web database statistics",
                "refresh_data_stats.py", "--db", db,
            ),
            PipelineStep(
                key="checkpoint",
                label="Checkpoint SQLite write-ahead logs",
                action=lambda log: checkpoint_databases(options, log),
            ),
        ]
    )
    return steps


def _source_inventory_step(options: ImportOptions, run_dir: Path) -> PipelineStep:
    configured_root = (os.getenv("IRS_XML_ROOT") or "").strip()
    if not configured_root:
        return PipelineStep(
            key="source_inventory",
            label="Check source XML inventory",
            action=lambda log: log(
                "IRS_XML_ROOT is not configured; the source-file inventory was not rebuilt. "
                "The database import is unaffected."
            ),
        )

    xml_root = Path(configured_root).expanduser().resolve()
    directories_in_root = []
    for xml_dir in options.xml_dirs:
        try:
            xml_dir.relative_to(xml_root)
            directories_in_root.append(xml_dir)
        except ValueError:
            pass
    if not directories_in_root:
        return PipelineStep(
            key="source_inventory",
            label="Check source XML inventory",
            action=lambda log: log(
                f"The XML directories are outside IRS_XML_ROOT ({xml_root}); "
                "the source-file inventory was not rebuilt."
            ),
        )

    sidecar = Path(
        os.getenv("IRS_XML_INVENTORY_PATH", options.db_path.parent / "irs990_sources.db")
    ).expanduser().resolve()
    return _python_step(
        options,
        "source_inventory",
        "Rebuild source XML inventory",
        "scan_xml_sources.py",
        "--xml-dir", str(xml_root),
        "--sidecar-db", str(sidecar),
        "--main-db", str(options.db_path),
        "--report-csv", str(run_dir / "xml_source_audit.csv"),
        "--duplicates-csv", str(run_dir / "xml_source_duplicates.csv"),
    )


def checkpoint_databases(options: ImportOptions, log: Callable[[str], None]) -> None:
    for path in (options.db_path, options.work_db_path):
        if not path.exists():
            continue
        conn = sqlite3.connect(path)
        try:
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            log(f"Checkpointed {path}: {result}")
        finally:
            conn.close()


def adjudication_instructions(options: ImportOptions, run_id: str = "latest") -> list[str]:
    py = f'"{options.python_executable}"'
    tool = str(options.project_dir / "grant_ai_assist_v1.py")
    db_args = f'--db "{options.db_path}" --work-db "{options.work_db_path}"'
    packet_dir = options.project_dir / "exports" / f"ai_packets_{run_id}"
    packet_summary = options.project_dir / "exports" / f"ai_packets_{run_id}_summary.csv"
    return [
        f'{py} "{tool}" export-adjudication-batches {db_args} --out-dir "{packet_dir}" --batch-size 10000 --summary-csv "{packet_summary}"',
        f'{py} "{tool}" import-adjudication-decision-dir {db_args} --in-dir "{options.project_dir / "imports" / "ai_decisions"}" --glob "decisions_*.jsonl" --dry-run --audit-dir "{options.project_dir / "imports" / "ai_decisions_audit"}"',
        f'{py} "{tool}" import-adjudication-decision-dir {db_args} --in-dir "{options.project_dir / "imports" / "ai_decisions"}" --glob "decisions_*.jsonl" --audit-dir "{options.project_dir / "imports" / "ai_decisions_audit_real"}"',
        f'{py} "{tool}" apply-decisions {db_args} --full-refresh',
    ]


class ImportManager:
    """Runs at most one import pipeline in the Flask process."""

    def __init__(self, max_log_lines: int = 500):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._max_log_lines = max_log_lines
        self._log_path: Optional[Path] = None
        self._state: dict = {"status": "idle", "steps": [], "logs": []}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                **self._state,
                "steps": [dict(step) for step in self._state.get("steps", [])],
                "logs": list(self._state.get("logs", [])),
                "instructions": list(self._state.get("instructions", [])),
            }

    def start(self, options: ImportOptions) -> str:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("A data import is already running.")
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        plan = build_pipeline(options, run_id)
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("A data import is already running.")
            run_dir = options.project_dir / "exports" / f"data_import_{run_id}"
            run_dir.mkdir(parents=True, exist_ok=False)
            self._log_path = run_dir / "import.log"
            self._state = {
                "status": "running",
                "run_id": run_id,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": "",
                "current_step": "",
                "steps": [{"key": s.key, "label": s.label, "status": "pending"} for s in plan],
                "logs": [],
                "error": "",
                "log_path": str(self._log_path),
                "instructions": [],
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(options, plan),
                name=f"irs-data-import-{run_id}",
                daemon=True,
            )
            self._thread.start()
        return run_id

    def _log(self, message: str) -> None:
        line = message.rstrip()
        if not line:
            return
        with self._lock:
            logs = self._state.setdefault("logs", [])
            logs.append(line)
            del logs[:-self._max_log_lines]
            if self._log_path:
                with self._log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

    def _set_step(self, key: str, status: str) -> None:
        with self._lock:
            for step in self._state["steps"]:
                if step["key"] == key:
                    step["status"] = status
                    break
            self._state["current_step"] = key if status == "running" else ""

    def _run(self, options: ImportOptions, plan: Sequence[PipelineStep]) -> None:
        try:
            for step in plan:
                self._set_step(step.key, "running")
                self._log(f"==> {step.label}")
                if step.action:
                    step.action(self._log)
                else:
                    self._run_command(step.command, options.project_dir)
                self._set_step(step.key, "completed")
            with self._lock:
                self._state["status"] = "completed"
                self._state["finished_at"] = datetime.now().isoformat(timespec="seconds")
                self._state["instructions"] = adjudication_instructions(
                    options, self._state["run_id"]
                )
        except Exception as exc:
            self._log(traceback.format_exc())
            with self._lock:
                current = self._state.get("current_step")
                for step in self._state["steps"]:
                    if step["key"] == current:
                        step["status"] = "failed"
                self._state["status"] = "failed"
                self._state["error"] = str(exc)
                self._state["finished_at"] = datetime.now().isoformat(timespec="seconds")
                self._state["current_step"] = ""

    def _run_command(self, command: Sequence[str], cwd: Path) -> None:
        self._log("Command: " + subprocess.list2cmdline(list(command)))
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            self._log(line)
        return_code = process.wait()
        if return_code:
            raise RuntimeError(f"Command failed with exit code {return_code}: {command[1]}")
