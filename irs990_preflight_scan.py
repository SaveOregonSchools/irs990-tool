#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone IRS 990 XML preflight scanner.

This companion script imports your existing rebuild_irs990_slim_clean.py module and
uses its actual extract_file() logic to test XML compatibility without writing to
SQLite. Put this file next to rebuild_irs990_slim_clean.py and run it from the
repo root.

Example:
    python irs990_preflight_scan.py --xml-dir /path/to/XML \
      --workers 4 --report preflight_summary.json --csv preflight_files.csv
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


PREFLIGHT_SUPPORTED_RETURN_TYPES = {"990", "990EZ", "990PF"}
PREFLIGHT_KNOWN_GOOD_COMBOS = {
    ("990", "2015v3.0"),
    ("990EZ", "2015v3.0"),
    ("990PF", "2015v3.0"),
    ("990PF", "2016v3.0"),
    ("990PF", "2016v3.1"),
    ("990EZ", "2017v2.2"),
    ("990EZ", "2017v2.3"),
    ("990PF", "2017v2.3"),
}

LOADER = None


def load_loader(script_path: Path):
    spec = importlib.util.spec_from_file_location("irs990_rebuild_loader", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import loader from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def norm_text(x: Optional[str]) -> Optional[str]:
    if x is None:
        return None
    x = str(x).strip()
    return x or None


def norm_num(x: Optional[str]) -> Optional[float]:
    x = norm_text(x)
    if x is None:
        return None
    x = x.replace(",", "").replace("$", "")
    try:
        return float(x)
    except Exception:
        return None


def norm_int(x: Optional[str]) -> Optional[int]:
    n = norm_num(x)
    return None if n is None else int(n)


def truthy_x01(text: Optional[str]) -> Optional[str]:
    t = norm_text(text)
    if t is None:
        return None
    u = t.upper()
    if u in {"X", "1", "TRUE", "T", "YES", "Y"}:
        return "X"
    if u in {"0", "FALSE", "F", "NO", "N"}:
        return "0"
    return t


def find_nodes_path_local(root: ET.Element, abs_path: str) -> List[ET.Element]:
    parts = [p for p in abs_path.strip("/").split("/") if p]
    if not parts:
        return []
    curr = [root]
    for idx, seg in enumerate(parts):
        nxt: List[ET.Element] = []
        for node in curr:
            if idx == 0 and local(node.tag) == seg:
                nxt.append(node)
                continue
            if seg == "*":
                nxt.extend(list(node))
                continue
            for ch in list(node):
                if local(ch.tag) == seg:
                    nxt.append(ch)
        curr = nxt
        if not curr:
            break
    return curr


def first_text_paths(root: ET.Element, candidates: Sequence[str]) -> Optional[str]:
    for xp in candidates:
        nodes = find_nodes_path_local(root, xp)
        if nodes:
            txt = nodes[0].text
            if txt and txt.strip():
                return txt.strip()
    return None


def descendants_text_first(node: ET.Element, candidates: Sequence[str]) -> Optional[str]:
    cands = {c.lower() for c in candidates}
    for sub in node.iter():
        if local(sub.tag).lower() in cands:
            t = norm_text(sub.text)
            if t is not None:
                return t
    return None


def schema_version_from_root(root: ET.Element) -> Optional[str]:
    return next((v for k, v in root.attrib.items() if local(k).lower().endswith("version")), None)


def iter_xml_files(root: Path) -> Iterable[str]:
    for base, _, files in __import__("os").walk(root):
        for fn in files:
            if fn.lower().endswith(".xml"):
                yield str(Path(base, fn))


def preflight_add_caveat(row: Dict[str, Any], code: str, message: str, severity: str = "warning") -> None:
    row.setdefault("caveats", []).append({"code": code, "severity": severity, "message": message})


def any_truthy_descendant(root: ET.Element, tag_names: Sequence[str]) -> bool:
    return any(truthy_x01(descendants_text_first(root, [tag])) == "X" for tag in tag_names)


def any_positive_descendant(root: ET.Element, tag_names: Sequence[str]) -> bool:
    for tag in tag_names:
        val = norm_num(descendants_text_first(root, [tag]))
        if val is not None and val > 0:
            return True
    return False


def recognized_main_forms(root: ET.Element) -> List[str]:
    forms = []
    for sub in root.iter():
        tag = local(sub.tag)
        if tag == "IRS990":
            forms.append("990")
        elif tag == "IRS990EZ":
            forms.append("990EZ")
        elif tag == "IRS990PF":
            forms.append("990PF")
    return sorted(set(forms))


def count_nonblank_values(d: Dict[str, Any], exclude: Sequence[str] = ("filing_id",)) -> int:
    exclude_set = set(exclude)
    return sum(1 for k, v in d.items() if k not in exclude_set and v not in (None, ""))


def preflight_file(file_path: str) -> Dict[str, Any]:
    global LOADER
    if LOADER is None:
        raise RuntimeError("LOADER module is not initialized")

    p = Path(file_path)
    row: Dict[str, Any] = {
        "source_file": str(p),
        "filing_id": p.stem,
        "status": "ok",
        "return_type": None,
        "schema_version": None,
        "tax_year": None,
        "period_end": None,
        "ein": None,
        "recognized_forms": [],
        "core_present_fields": 0,
        "grant_rows": 0,
        "contractor_rows": 0,
        "officer_rows": 0,
        "ez_officer_rows": 0,
        "pf_officer_rows": 0,
        "schedule_l_rows": 0,
        "schedule_r_rows": 0,
        "caveats": [],
    }

    try:
        root = ET.parse(str(p)).getroot()
    except Exception as e:
        row["status"] = "parse_error"
        preflight_add_caveat(row, "parse_error", f"XML parse failed: {e}", "error")
        return row

    row["return_type"] = first_text_paths(root, ["/Return/ReturnHeader/ReturnTypeCd"])
    row["schema_version"] = schema_version_from_root(root)
    row["tax_year"] = norm_int(first_text_paths(root, ["/Return/ReturnHeader/TaxYr", "/Return/ReturnHeader/TaxYear"]))
    row["period_end"] = first_text_paths(root, ["/Return/ReturnHeader/TaxPeriodEndDt"])
    row["ein"] = first_text_paths(root, ["/Return/ReturnHeader/Filer/EIN"])
    row["recognized_forms"] = recognized_main_forms(root)

    missing = []
    if not row["return_type"]:
        missing.append("ReturnTypeCd")
    if not row["tax_year"]:
        missing.append("TaxYr/TaxYear")
    if not row["ein"]:
        missing.append("Filer/EIN")
    if missing:
        row["status"] = "missing_required_header"
        preflight_add_caveat(row, "missing_required_header", "Missing required header field(s): " + ", ".join(missing), "error")
        return row

    rtype = str(row["return_type"] or "")
    schema = row["schema_version"]

    if not schema:
        preflight_add_caveat(row, "schema_version_missing", "Root returnVersion/schema version is missing.")
    if rtype not in PREFLIGHT_SUPPORTED_RETURN_TYPES:
        preflight_add_caveat(row, "unsupported_return_type", f"ReturnTypeCd={rtype!r} is not supported by the slim loader.", "error")
    if not row["recognized_forms"]:
        preflight_add_caveat(row, "no_recognized_main_form", "No IRS990, IRS990EZ, or IRS990PF node was found.", "error")
    elif rtype in PREFLIGHT_SUPPORTED_RETURN_TYPES and rtype not in row["recognized_forms"]:
        preflight_add_caveat(row, "return_type_form_mismatch", f"ReturnTypeCd={rtype!r}, recognized forms={row['recognized_forms']!r}.")
    if schema and rtype in PREFLIGHT_SUPPORTED_RETURN_TYPES and (rtype, schema) not in PREFLIGHT_KNOWN_GOOD_COMBOS:
        preflight_add_caveat(row, "unknown_form_version_combo", f"{rtype} / {schema} is not in the known-good combo inventory. Extraction may still be fine.")

    if len(p.stem) >= 4 and p.stem[:4].isdigit() and row["tax_year"]:
        filename_year = int(p.stem[:4])
        if filename_year != int(row["tax_year"]):
            preflight_add_caveat(row, "filename_year_tax_year_mismatch", f"Filename begins with {filename_year}, but TaxYr is {row['tax_year']}.", "info")

    extracted = LOADER.extract_file(str(p))
    if "error" in extracted:
        row["status"] = "extractor_error"
        preflight_add_caveat(row, "extractor_error", extracted["error"], "error")
        return row

    row["core_present_fields"] = count_nonblank_values(extracted.get("core_hot", {}))
    row["grant_rows"] = len(extracted.get("grants", []) or [])
    row["contractor_rows"] = len(extracted.get("irs990_contractor_compensation_grp", []) or [])
    row["officer_rows"] = len(extracted.get("officers", []) or [])
    row["ez_officer_rows"] = len(extracted.get("irs990_ez_officer_director_trustee_empl_grp", []) or [])
    row["pf_officer_rows"] = len(extracted.get("irs990_pf_officer_dir_trst_key_empl_info_grp", []) or [])
    row["schedule_l_rows"] = sum(len(extracted.get(k, []) or []) for k in [
        "irs990_schedule_l_bus_tr_involve_interested_prsn_grp",
        "irs990_schedule_l_disqualified_person_ex_bnft_tr_grp",
        "irs990_schedule_l_grnt_asst_bnft_interested_prsn_grp",
        "irs990_schedule_l_loans_btwn_org_interested_prsn_grp",
    ])
    row["schedule_r_rows"] = sum(len(extracted.get(k, []) or []) for k in [
        "irs990_schedule_r_id_related_tax_exempt_org_grp",
        "irs990_schedule_r_id_related_org_txbl_corp_tr_grp",
        "irs990_schedule_r_id_related_org_txbl_partnership_grp",
        "irs990_schedule_r_id_disregarded_entities_grp",
        "irs990_schedule_r_transactions_related_org_grp",
        "irs990_schedule_r_unrelated_org_txbl_partnership_grp",
    ])

    if row["core_present_fields"] == 0:
        preflight_add_caveat(row, "all_core_hot_fields_blank", "The file parsed, but no nonblank core_hot fields were extracted.")

    filer_level_ic = first_text_paths(root, ["/Return/ReturnHeader/Filer/InCareOfNm"])
    extracted_ic = (extracted.get("header") or {}).get("in_care_of_name")
    if filer_level_ic and not extracted_ic:
        preflight_add_caveat(row, "filer_incareof_unmapped", "Filer/InCareOfNm exists, but current header extraction did not capture in_care_of_name.")

    grant_signal = (
        any_truthy_descendant(root, ["GrantsToOrganizationsInd", "GrantsToIndividualsInd", "MoreThan5000KToOrgInd", "MoreThan5000KToIndividualsInd"])
        or any_positive_descendant(root, ["ContriPaidRevAndExpnssAmt", "ContriPaidDsbrsChrtblAmt", "CYGrantsAndSimilarPaidAmt", "GrantsAndSimilarAmountsPaidAmt", "GrantAmt", "GrantsAndAllocationsAmt"])
    )
    if grant_signal and row["grant_rows"] == 0:
        preflight_add_caveat(row, "grant_signal_without_detail_rows", "Grant/contribution indicators or amounts exist, but no detailed grant rows were extracted. Spot-check this.")

    if rtype == "990PF" and row["grant_rows"] == 0 and any_positive_descendant(root, ["ContriPaidRevAndExpnssAmt", "ContriPaidDsbrsChrtblAmt"]):
        preflight_add_caveat(row, "pf_contributions_paid_without_detail_rows", "990PF reports contributions paid, but no GrantOrContributionPdDurYrGrp detail rows were extracted.")

    return row


def initializer(loader_path: str) -> None:
    global LOADER
    LOADER = load_loader(Path(loader_path))


def preflight_csv_row(row: Dict[str, Any]) -> Dict[str, Any]:
    caveats = row.get("caveats") or []
    return {
        "source_file": row.get("source_file"),
        "filing_id": row.get("filing_id"),
        "status": row.get("status"),
        "return_type": row.get("return_type"),
        "schema_version": row.get("schema_version"),
        "tax_year": row.get("tax_year"),
        "period_end": row.get("period_end"),
        "ein": row.get("ein"),
        "recognized_forms": "|".join(row.get("recognized_forms") or []),
        "core_present_fields": row.get("core_present_fields"),
        "grant_rows": row.get("grant_rows"),
        "contractor_rows": row.get("contractor_rows"),
        "officer_rows": row.get("officer_rows"),
        "ez_officer_rows": row.get("ez_officer_rows"),
        "pf_officer_rows": row.get("pf_officer_rows"),
        "schedule_l_rows": row.get("schedule_l_rows"),
        "schedule_r_rows": row.get("schedule_r_rows"),
        "caveat_codes": "|".join(c.get("code", "") for c in caveats),
        "caveat_messages": " || ".join(c.get("message", "") for c in caveats),
    }


def run(args: argparse.Namespace) -> int:
    global LOADER
    loader_path = Path(args.loader)
    if not loader_path.exists():
        print(f"ERROR: loader script not found: {loader_path}", file=sys.stderr)
        return 2
    LOADER = load_loader(loader_path)

    xml_dir = Path(args.xml_dir)
    if not xml_dir.exists():
        print(f"ERROR: XML dir not found: {xml_dir}", file=sys.stderr)
        return 2

    files = sorted(iter_xml_files(xml_dir))
    found_count = len(files)
    if args.max_files and args.max_files > 0:
        files = files[:args.max_files]
    print(f"[preflight] XML files found: {found_count:,}; scanning: {len(files):,}")

    status_counts = Counter()
    by_return_type = Counter()
    by_schema_version = Counter()
    by_combo = Counter()
    by_tax_year = Counter()
    caveat_counts = Counter()
    caveat_examples: Dict[str, List[Dict[str, Any]]] = {}
    extraction_totals = Counter()

    csv_handle = None
    writer = None
    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_handle = open(csv_path, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(csv_handle, fieldnames=list(preflight_csv_row({"caveats": []}).keys()))
        writer.writeheader()

    try:
        if args.workers <= 1:
            iterator = map(preflight_file, files)
        else:
            executor = ProcessPoolExecutor(
                max_workers=args.workers,
                initializer=initializer,
                initargs=(str(loader_path),),
            )
            iterator = executor.map(preflight_file, files, chunksize=args.chunksize)

        try:
            for idx, row in enumerate(iterator, 1):
                status_counts[row.get("status") or "unknown"] += 1
                rtype = row.get("return_type") or "<missing>"
                schema = row.get("schema_version") or "<missing>"
                tax_year = row.get("tax_year") or "<missing>"
                by_return_type[str(rtype)] += 1
                by_schema_version[str(schema)] += 1
                by_combo[f"{rtype}|{schema}"] += 1
                by_tax_year[str(tax_year)] += 1
                for key in ["grant_rows", "contractor_rows", "officer_rows", "ez_officer_rows", "pf_officer_rows", "schedule_l_rows", "schedule_r_rows"]:
                    extraction_totals[key] += int(row.get(key) or 0)
                if row.get("grant_rows"):
                    extraction_totals["files_with_grants"] += 1
                if row.get("contractor_rows"):
                    extraction_totals["files_with_contractors"] += 1
                if row.get("officer_rows") or row.get("ez_officer_rows") or row.get("pf_officer_rows"):
                    extraction_totals["files_with_people"] += 1
                for caveat in row.get("caveats") or []:
                    code = caveat.get("code") or "unknown_caveat"
                    caveat_counts[code] += 1
                    examples = caveat_examples.setdefault(code, [])
                    if len(examples) < args.sample_limit:
                        examples.append({
                            "source_file": row.get("source_file"),
                            "return_type": row.get("return_type"),
                            "schema_version": row.get("schema_version"),
                            "tax_year": row.get("tax_year"),
                            "severity": caveat.get("severity"),
                            "message": caveat.get("message"),
                        })
                if writer:
                    writer.writerow(preflight_csv_row(row))
                if idx % 1000 == 0:
                    print(f"[preflight] scanned {idx:,}/{len(files):,}")
        finally:
            if args.workers > 1:
                executor.shutdown(wait=True)
    finally:
        if csv_handle:
            csv_handle.close()

    report = {
        "xml_dir": str(xml_dir),
        "loader": str(loader_path),
        "files_found": found_count,
        "files_scanned": len(files),
        "status_counts": dict(status_counts),
        "by_return_type": dict(by_return_type),
        "by_schema_version": dict(by_schema_version),
        "by_return_type_schema_version": dict(by_combo),
        "by_tax_year": dict(sorted(by_tax_year.items())),
        "extraction_totals": dict(extraction_totals),
        "caveat_counts": dict(caveat_counts),
        "caveat_examples": caveat_examples,
    }

    print("[preflight] complete")
    print(f"[preflight] status: {dict(status_counts)}")
    print(f"[preflight] return types: {dict(by_return_type)}")
    print(f"[preflight] schema versions: {dict(by_schema_version)}")
    print(f"[preflight] extraction totals: {dict(extraction_totals)}")
    if caveat_counts:
        print("[preflight] caveats:")
        for code, count in caveat_counts.most_common():
            print(f"  - {code}: {count:,}")
    else:
        print("[preflight] caveats: none")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"[preflight] wrote JSON summary: {report_path}")
    if args.csv:
        print(f"[preflight] wrote CSV file-level report: {args.csv}")

    error_count = sum(status_counts[s] for s in ("parse_error", "missing_required_header", "extractor_error"))
    return 1 if error_count else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight IRS 990 XML files against the current slim rebuild extractor")
    parser.add_argument("--xml-dir", required=True, help="Folder of XML files; scanned recursively")
    parser.add_argument("--loader", default="rebuild_irs990_slim_clean.py", help="Path to rebuild_irs990_slim_clean.py")
    parser.add_argument("--workers", type=int, default=1, help="Parallel parser processes. Use 1 for easier debugging.")
    parser.add_argument("--chunksize", type=int, default=25)
    parser.add_argument("--max-files", type=int, default=0, help="Optional cap for test scans; 0 means all files")
    parser.add_argument("--report", default=None, help="Optional JSON summary report path")
    parser.add_argument("--csv", default=None, help="Optional CSV file-level report path")
    parser.add_argument("--sample-limit", type=int, default=25, help="Max example files retained per caveat")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
