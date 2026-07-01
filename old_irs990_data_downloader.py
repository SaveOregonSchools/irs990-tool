import csv
import os
import re
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


INDEX_CSV = r"C:\IRSData\indexes\gt990_index_all_years.csv"
OUT_DIR = Path(r"C:\IRSData\xml_missing_2015")
OUT_DIR.mkdir(parents=True, exist_ok=True)

TARGET_TAX_YEAR = "2015"

# Optional: restrict to only missing EINs if you have a text file, one EIN per line.
MISSING_EINS_FILE = r"C:\IRSData\missing_2015_eins.txt"

# Tune this. Good starting range: 12-24. Too high can cause throttling/timeouts.
MAX_WORKERS = 16

# Retry settings for occasional network/S3 hiccups.
MAX_RETRIES = 3
REQUEST_TIMEOUT = 60

# Print progress every N completed download attempts.
PROGRESS_EVERY = 100


_thread_local = threading.local()


def get_session() -> requests.Session:
    """Use one requests.Session per worker thread for connection reuse."""
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
            max_retries=0,
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_local.session = s
    return _thread_local.session


def load_missing_eins(path: str):
    missing_eins = None

    if os.path.exists(path):
        for enc in ("utf-8-sig", "utf-16", "cp1252"):
            try:
                with open(path, "r", encoding=enc) as f:
                    missing_eins = {
                        re.sub(r"\D", "", line)
                        for line in f
                        if re.sub(r"\D", "", line)
                    }
                print(f"Loaded {len(missing_eins):,} missing EINs from {path} using {enc}")
                break
            except UnicodeDecodeError:
                continue

        if missing_eins is None:
            raise RuntimeError(f"Could not read {path} with supported encodings.")

    return missing_eins


def pick(row, *names):
    lower = {k.lower(): k for k in row.keys()}
    for name in names:
        key = lower.get(name.lower())
        if key:
            return row.get(key)
    return None


def normalize_url(row):
    url = pick(row, "URL", "url", "XmlURL", "xml_url")
    if url:
        return url.replace(" ", "")

    object_id = pick(row, "ObjectId", "ObjectID", "OBJECTID", "object_id")
    if not object_id:
        return None

    object_id = str(object_id).replace("OID-", "").strip()
    return f"https://gt990datalake-rawdata.s3.amazonaws.com/EfileData/XmlFiles/{object_id}_public.xml"


def is_valid_return_xml(content: bytes) -> bool:
    """Light sanity check so we do not save S3/HTML error pages as XML filings."""
    if not content:
        return False

    check = content.lstrip()
    if check.startswith(b"\xef\xbb\xbf"):
        check = check[3:].lstrip()

    return b"<Return" in check[:1000]


def build_tasks():
    """
    Scan the index once and build the list of XML URLs to download.

    This preserves the original behavior:
    - tax year must match TARGET_TAX_YEAR
    - if MISSING_EINS_FILE exists, only those EINs are downloaded
    - files already present in OUT_DIR are skipped
    """
    missing_eins = load_missing_eins(MISSING_EINS_FILE)

    tasks = []
    skipped = 0
    no_url = 0
    matching_rows = 0

    with open(INDEX_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            ein = pick(row, "EIN", "ORG_EIN", "ein")
            ein = re.sub(r"\D", "", ein or "")

            tax_year = pick(row, "TaxYear", "TAX_YEAR", "tax_year")
            tax_period = pick(row, "TaxPeriod", "TAX_PERIOD", "tax_period")

            # Prefer explicit TaxYear if present; otherwise use TaxPeriod YYYYMM.
            row_year = str(tax_year).strip() if tax_year else str(tax_period or "")[:4]

            if row_year != TARGET_TAX_YEAR:
                skipped += 1
                continue

            if missing_eins is not None and ein not in missing_eins:
                skipped += 1
                continue

            matching_rows += 1

            url = normalize_url(row)
            if not url:
                no_url += 1
                continue

            filename = url.rsplit("/", 1)[-1]
            out_path = OUT_DIR / filename

            if out_path.exists() and out_path.stat().st_size > 0:
                skipped += 1
                continue

            tasks.append((url, str(out_path)))

    print(
        f"Index scan complete. Matching rows={matching_rows:,}; "
        f"to download={len(tasks):,}; skipped={skipped:,}; missing URL={no_url:,}"
    )

    return tasks, skipped, no_url


def download_one(url: str, out_path_str: str):
    """
    Worker function. Returns:
      ("downloaded", url, None)
      ("failed", url, error_text)
      ("skipped", url, None)
    """
    out_path = Path(out_path_str)

    # Another thread/process may have created it since tasks were built.
    if out_path.exists() and out_path.stat().st_size > 0:
        return "skipped", url, None

    session = get_session()

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            content = r.content

            if r.status_code == 200 and is_valid_return_xml(content):
                tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
                tmp_path.write_bytes(content)
                tmp_path.replace(out_path)
                return "downloaded", url, None

            preview = content[:200].decode("utf-8", errors="replace")
            last_error = f"HTTP {r.status_code}; preview={preview!r}"

        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"

        # Small backoff before retrying.
        if attempt < MAX_RETRIES:
            time.sleep(0.5 * attempt)

    return "failed", url, last_error


def main():
    start = time.time()

    tasks, initial_skipped, no_url = build_tasks()

    downloaded = 0
    failed = 0
    skipped_during_download = 0

    if not tasks:
        print(
            f"Done. downloaded=0, skipped={initial_skipped:,}, "
            f"errors={no_url:,}, elapsed={time.time() - start:,.1f}s"
        )
        return

    print(f"Starting parallel download with MAX_WORKERS={MAX_WORKERS}...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(download_one, url, out_path) for url, out_path in tasks]

        completed = 0
        for fut in as_completed(futures):
            completed += 1
            status, url, error = fut.result()

            if status == "downloaded":
                downloaded += 1
            elif status == "skipped":
                skipped_during_download += 1
            else:
                failed += 1
                print(f"Failed: {url}")
                print(f"  {error}")

            if completed % PROGRESS_EVERY == 0 or completed == len(tasks):
                elapsed = time.time() - start
                rate = completed / elapsed if elapsed > 0 else 0
                print(
                    f"Progress: {completed:,}/{len(tasks):,} attempted; "
                    f"downloaded={downloaded:,}; failed={failed:,}; "
                    f"rate={rate:,.1f}/sec"
                )

    total_skipped = initial_skipped + skipped_during_download
    total_errors = no_url + failed

    print(
        f"Done. downloaded={downloaded:,}, skipped={total_skipped:,}, "
        f"errors={total_errors:,}, elapsed={time.time() - start:,.1f}s"
    )


if __name__ == "__main__":
    main()
