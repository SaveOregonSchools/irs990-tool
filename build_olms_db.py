"""Build or atomically refresh the OLMS labor-organization sidecar."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from common import APP_ROOT, DB_PATH, OLMS_DB_PATH, configured_olms_data_root
from olms import build_database


def parse_years(value: Optional[str]) -> Optional[list[int]]:
    if not value:
        return None
    years = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        year = int(token)
        if year < 1900 or year > 2200:
            raise argparse.ArgumentTypeError(f"Invalid year: {year}")
        years.append(year)
    return sorted(set(years)) or None


def build_parser() -> argparse.ArgumentParser:
    configured_root = configured_olms_data_root()
    parser = argparse.ArgumentParser(
        description="Build an audited OLMS SQLite sidecar from unpacked annual bulk-data folders."
    )
    parser.add_argument(
        "--input-dir",
        default=str(configured_root) if configured_root else None,
        help="Directory containing numeric year folders (default: OLMS_DATA_ROOT)",
    )
    parser.add_argument("--db", default=str(OLMS_DB_PATH), help="OLMS sidecar destination")
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument("--rebuild", action="store_true", help="Build a replacement database atomically")
    mode.add_argument(
        "--refresh-year",
        type=int,
        help="Atomically replace one year in an existing sidecar",
    )
    parser.add_argument("--years", help="Comma-separated years for a rebuild or targeted refresh")
    parser.add_argument("--as-of-date", help="Compliance data-as-of date (YYYY-MM-DD); default is max RECEIVE_DATE")
    parser.add_argument("--irs-db", default=str(DB_PATH), help="IRS database used for deterministic exact-name matching")
    parser.add_argument("--skip-irs-matching", action="store_true", help="Skip the streaming IRS identity pass")
    parser.add_argument(
        "--skip-counterparty-irs-matching",
        action="store_true",
        help="Match unions but do not attempt exact counterparty-to-IRS matches",
    )
    parser.add_argument(
        "--allow-filing-errors",
        action="store_true",
        help="Complete despite quarantined/conflicting central filing rows (audit review required)",
    )
    parser.add_argument("--exports-dir", default=str(APP_ROOT / "exports"), help="Audit CSV output directory")
    parser.add_argument(
        "--scope-overrides",
        default=str(APP_ROOT / "config" / "olms_scope_overrides.csv"),
    )
    parser.add_argument(
        "--irs-match-overrides",
        default=str(APP_ROOT / "config" / "olms_irs_match_overrides.csv"),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input_dir:
        raise SystemExit("--input-dir is required when OLMS_DATA_ROOT is not configured")
    years = parse_years(args.years)
    rebuild = args.refresh_year is None
    if args.refresh_year is not None:
        if years and years != [args.refresh_year]:
            raise SystemExit("Use either --refresh-year or --years for refresh scope, not conflicting values")
        years = [args.refresh_year]
    elif not args.rebuild and args.years:
        # --years without --rebuild is a safe targeted refresh when the DB exists;
        # otherwise it naturally becomes a partial atomic rebuild.
        rebuild = not Path(args.db).expanduser().exists()

    summary = build_database(
        Path(args.input_dir),
        Path(args.db),
        years=years,
        rebuild=rebuild,
        as_of_date=args.as_of_date,
        allow_filing_errors=args.allow_filing_errors,
        exports_dir=Path(args.exports_dir),
        scope_overrides=Path(args.scope_overrides),
        irs_db_path=Path(args.irs_db),
        irs_match_overrides=Path(args.irs_match_overrides),
        skip_irs_matching=args.skip_irs_matching,
        match_counterparties=not args.skip_counterparty_irs_matching,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
