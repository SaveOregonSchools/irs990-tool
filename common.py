import os
import sqlite3
import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent

# Default portable layout:
# irs990-tool/
#   common.py
#   db/irs990.db
DEFAULT_DB = APP_ROOT / "db" / "irs990.db"

# Optional override for any machine-specific setup:
# Windows CMD:
#   set IRS_DB_PATH=C:\some\other\path\irs990.db
# PowerShell:
#   $env:IRS_DB_PATH="C:\some\other\path\irs990.db"
DB_PATH = Path(os.getenv("IRS_DB_PATH", DEFAULT_DB)).expanduser().resolve()

def current_db_path():
    return str(DB_PATH)

def connect_ro():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"IRS 990 database not found at: {DB_PATH}\n\n"
            "Either place the database at db/irs990.db under the project folder, "
            "or set the IRS_DB_PATH environment variable."
        )

    # Open read-only; immutable avoids file locks and is faster if the file isn't changing.
    uri = f"file:{DB_PATH.as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)

    try:
        conn.execute("PRAGMA mmap_size = 2147483648;")   # 2GB
        conn.execute("PRAGMA cache_size = -500000;")     # ~500MB page cache
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA read_uncommitted = ON;")
    except Exception:
        pass

    return conn

def normalize_eins(text: str):
    tokens = re.split(r"[,\s;]+", (text or "").strip())
    out, seen = [], set()
    for t in tokens:
        d = re.sub(r"\D", "", t)
        if len(d) == 9 and d not in seen:
            seen.add(d)
            out.append(d)
    return out