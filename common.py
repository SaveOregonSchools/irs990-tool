import os
import sqlite3
import re
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - requirements.txt installs python-dotenv
    load_dotenv = None

if load_dotenv:
    load_dotenv(APP_ROOT / ".env")

# Default portable layout:
# irs990-tool/
#   common.py
#   db/irs990.db
DEFAULT_DB = APP_ROOT / "db" / "irs990.db"
DEFAULT_OLMS_DB = APP_ROOT / "db" / "olms.db"

# Optional override for any machine-specific setup:
# Windows CMD:
#   set IRS_DB_PATH=C:\some\other\path\irs990.db
# PowerShell:
#   $env:IRS_DB_PATH="C:\some\other\path\irs990.db"
DB_PATH = Path(os.getenv("IRS_DB_PATH", DEFAULT_DB)).expanduser().resolve()
OLMS_DB_PATH = Path(os.getenv("OLMS_DB_PATH", DEFAULT_OLMS_DB)).expanduser().resolve()
GRANT_WORK_DB_PATH = Path(
    os.getenv("IRS_GRANT_WORK_DB_PATH") or DB_PATH.parent / "grant_matching_work.db"
).expanduser().resolve()


def configured_xml_root() -> Optional[Path]:
    """Return the machine-local XML root, or None when it is not configured."""
    value = (os.getenv("IRS_XML_ROOT") or "").strip()
    return Path(value).expanduser().resolve() if value else None


def configured_olms_data_root() -> Optional[Path]:
    """Return the machine-local OLMS annual-data root, if configured."""
    value = (os.getenv("OLMS_DATA_ROOT") or "").strip()
    return Path(value).expanduser().resolve() if value else None

def current_db_path():
    return str(DB_PATH)


def current_olms_db_path():
    return str(OLMS_DB_PATH)


def current_grant_work_db_path():
    return str(GRANT_WORK_DB_PATH)

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


def _readonly_uri(path: Path) -> str:
    return f"file:{path.as_posix()}?mode=ro&immutable=1"


def _configure_read_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    try:
        conn.execute("PRAGMA mmap_size = 2147483648;")
        conn.execute("PRAGMA cache_size = -250000;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA read_uncommitted = ON;")
    except Exception:
        pass
    return conn


def attach_grant_work_ro(conn: sqlite3.Connection, schema: str = "grant_work") -> bool:
    """Attach the configured grant-work sidecar read-only when safe and available.

    The main database identity check deliberately skips fixture/in-memory
    connections.  This prevents a unit test that patches ``connect_ro`` from
    accidentally reaching the workstation's multi-gigabyte sidecar.
    """
    if schema != "grant_work":
        raise ValueError("Only the fixed grant_work schema name is supported")

    databases = {row[1]: row[2] for row in conn.execute("PRAGMA database_list")}
    if schema in databases:
        return True

    main_path = databases.get("main") or ""
    if not main_path:
        return False
    try:
        if Path(main_path).resolve() != DB_PATH:
            return False
    except (OSError, ValueError):
        return False

    if not GRANT_WORK_DB_PATH.exists():
        return False
    conn.execute(
        f"ATTACH DATABASE ? AS {schema}",
        (_readonly_uri(GRANT_WORK_DB_PATH),),
    )
    return True


def connect_olms_ro():
    """Open the OLMS sidecar read-only for application/query use."""
    if not OLMS_DB_PATH.exists():
        raise FileNotFoundError(
            f"OLMS database not found at: {OLMS_DB_PATH}\n\n"
            "Build it with build_olms_db.py, place it at db/olms.db, "
            "or set the OLMS_DB_PATH environment variable."
        )
    conn = sqlite3.connect(
        _readonly_uri(OLMS_DB_PATH), uri=True, check_same_thread=False
    )
    return _configure_read_connection(conn)


def connect_olms_irs_ro():
    """Open an in-memory read-only connection with OLMS and IRS attached."""
    missing = [str(path) for path in (OLMS_DB_PATH, DB_PATH) if not path.exists()]
    if missing:
        raise FileNotFoundError("Required database not found: " + ", ".join(missing))
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("ATTACH DATABASE ? AS olms", (_readonly_uri(OLMS_DB_PATH),))
    conn.execute("ATTACH DATABASE ? AS irs", (_readonly_uri(DB_PATH),))
    _configure_read_connection(conn)
    conn.execute("PRAGMA query_only = ON;")
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
