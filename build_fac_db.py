"""CLI for building the local Federal Audit Clearinghouse sidecar."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Optional, Sequence

from fac_bulk import (
    CURRENT_DOWNLOAD_PAGE,
    CURRENT_DOWNLOAD_URLS,
    DEFAULT_DOWNLOAD_MAX_BYTES,
    DEFAULT_FAC_DB_PATH,
    HISTORIC_DOWNLOAD_PAGE,
    HISTORIC_DOWNLOAD_URL,
    HISTORIC_SHA1_URL,
    MIN_CLI_DOWNLOAD_MAX_BYTES,
    build_fac_database,
    download_official_fac_sources,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build or resume an atomic, local FAC SQLite sidecar from official "
            "2016-present GSA CSVs and/or 1998-2015 Census CSV/ZIP files."
        ),
        epilog=(
            "The destination is never modified in place. Import work is committed "
            "in bounded checkpoints to <name>.building.db and resumes when the exact "
            "same command/input set is rerun. Download flags can fetch and build in one command."
        ),
    )
    parser.add_argument(
        "--input-dir",
        action="append",
        default=[],
        help="FAC CSV/ZIP file or directory; repeat for multiple locations",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_FAC_DB_PATH),
        help=f"Finished sidecar path (default: {DEFAULT_FAC_DB_PATH})",
    )
    parser.add_argument(
        "--source-as-of",
        help=(
            "Date the source snapshot was downloaded/published (YYYY-MM-DD). "
            "If omitted, today's build date is recorded and explicitly marked as inferred."
        ),
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Atomically replace an existing finished sidecar after the new build validates",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Discard only this destination's unfinished staging DB and rebuild it",
    )
    parser.add_argument(
        "--print-download-urls",
        action="store_true",
        help="Print stable official source and documentation URLs, then exit",
    )
    parser.add_argument(
        "--download-current",
        action="store_true",
        help="Download the seven allowlisted 2016-present FAC full CSVs",
    )
    parser.add_argument(
        "--download-historic",
        action="store_true",
        help="Download the allowlisted 1998-2015 Census ZIP and published SHA1",
    )
    parser.add_argument(
        "--download-dir",
        default="imports/fac",
        help="Root for current/ and historic/ downloads (default: imports/fac)",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Download and validate selected sources without building SQLite",
    )
    parser.add_argument(
        "--refresh-downloads",
        action="store_true",
        help="Stage fresh copies even when finished downloads already exist",
    )
    parser.add_argument(
        "--restart-downloads",
        action="store_true",
        help="Discard only unfinished .part state before selected downloads",
    )
    parser.add_argument(
        "--download-max-gib",
        type=float,
        default=DEFAULT_DOWNLOAD_MAX_BYTES / (1024 ** 3),
        help="Per-file safety cap in GiB; minimum 2, default 8",
    )
    parser.add_argument(
        "--download-timeout",
        type=float,
        default=60.0,
        help="Network read/connect timeout in seconds (default: 60)",
    )
    return parser


def _url_payload() -> dict[str, object]:
    return {
        "current_download_page": CURRENT_DOWNLOAD_PAGE,
        "current_full_csvs": list(CURRENT_DOWNLOAD_URLS),
        "historic_download_page": HISTORIC_DOWNLOAD_PAGE,
        "historic_1998_2015_zip": HISTORIC_DOWNLOAD_URL,
        "historic_1998_2015_sha1": HISTORIC_SHA1_URL,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.print_download_urls:
        print(json.dumps(_url_payload(), indent=2))
        return 0
    download_requested = args.download_current or args.download_historic
    if args.download_only and not download_requested:
        parser.error("--download-only requires --download-current and/or --download-historic")
    if not args.input_dir and not download_requested:
        parser.error(
            "at least one --input-dir or download selection is required "
            "(or use --print-download-urls)"
        )
    if not math.isfinite(args.download_max_gib):
        parser.error("--download-max-gib must be a finite number")
    max_download_bytes = int(args.download_max_gib * (1024 ** 3))
    if download_requested and max_download_bytes < MIN_CLI_DOWNLOAD_MAX_BYTES:
        parser.error("--download-max-gib must be at least 2")
    if args.download_timeout <= 0:
        parser.error("--download-timeout must be positive")

    last_progress: dict[str, int] = {}

    def show_progress(filename: str, downloaded: int, total: Optional[int]) -> None:
        previous = last_progress.get(filename, -64 * 1024 * 1024)
        finished = total is not None and downloaded >= total
        if not finished and downloaded - previous < 64 * 1024 * 1024:
            return
        last_progress[filename] = downloaded
        amount = f"{downloaded / (1024 ** 2):,.1f} MiB"
        if total:
            amount += f" / {total / (1024 ** 2):,.1f} MiB ({downloaded / total:.0%})"
        print(f"[download] {filename}: {amount}", file=sys.stderr, flush=True)

    try:
        download_summary = None
        input_paths = [Path(item) for item in args.input_dir]
        if download_requested:
            download_summary = download_official_fac_sources(
                Path(args.download_dir),
                include_current=args.download_current,
                include_historic=args.download_historic,
                refresh=args.refresh_downloads,
                restart_partials=args.restart_downloads,
                max_bytes=max_download_bytes,
                timeout=args.download_timeout,
                progress=show_progress,
            )
            if args.download_current:
                input_paths.append(Path(download_summary["current_directory"]))
            if args.download_historic:
                input_paths.append(Path(download_summary["historic_directory"]))
        if args.download_only:
            summary = {"downloads": download_summary}
        else:
            build_summary = build_fac_database(
                input_paths,
                Path(args.db),
                source_as_of_date=args.source_as_of,
                replace=args.replace,
                restart=args.restart,
            )
            summary = (
                {"downloads": download_summary, "build": build_summary}
                if download_summary is not None
                else build_summary
            )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
