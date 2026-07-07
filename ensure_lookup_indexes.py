import sqlite3

from common import DB_PATH


INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_returns_org_name_nocase ON returns(org_name COLLATE NOCASE)",
]


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        for sql in INDEXES:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()

    print(f"Lookup indexes ensured for {DB_PATH}")


if __name__ == "__main__":
    main()
