import argparse
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import grant_ai_assist_v1 as grant_stats


APP_STATS_TABLE = "app_data_stats"
APP_STATS_META_TABLE = "app_data_stats_meta"


def _pct(part: Any, total: Any) -> Optional[float]:
    try:
        total_f = float(total or 0)
        if total_f == 0:
            return None
        return round(100.0 * float(part or 0) / total_f, 2)
    except Exception:
        return None


def _add_stat(
    rows: List[Dict[str, Any]],
    section: str,
    metric: str,
    bucket: str = "",
    count: Any = None,
    total_amount: Any = None,
    pct_of_grants: Any = None,
    pct_of_section: Any = None,
    signatures: Any = None,
    grants_represented: Any = None,
    notes: str = "",
) -> None:
    rows.append(
        {
            "section": section,
            "metric": metric,
            "bucket": bucket or "",
            "count": count,
            "signatures": signatures,
            "grants_represented": grants_represented,
            "total_amount": round(float(total_amount), 2) if total_amount not in (None, "") else total_amount,
            "pct_of_grants": pct_of_grants,
            "pct_of_section": pct_of_section,
            "notes": notes,
        }
    )


def _scalar(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return row[0]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return grant_stats.table_exists(conn, name)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0


def _database_file_stats(db_path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    main_size = _file_size(db_path)
    wal_size = _file_size(Path(str(db_path) + "-wal"))
    shm_size = _file_size(Path(str(db_path) + "-shm"))
    total_size = main_size + wal_size + shm_size
    _add_stat(rows, "database", "file_size_bytes", "main_db", count=main_size, notes=str(db_path))
    _add_stat(rows, "database", "file_size_bytes", "wal", count=wal_size, notes=str(db_path) + "-wal")
    _add_stat(rows, "database", "file_size_bytes", "shm", count=shm_size, notes=str(db_path) + "-shm")
    _add_stat(rows, "database", "file_size_bytes", "total_sqlite_files", count=total_size)
    return rows


def _filing_stats(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not _table_exists(conn, "returns"):
        _add_stat(rows, "filings", "table_missing", "returns", notes="returns table has not been created yet")
        return rows

    total = int(_scalar(conn, "SELECT COUNT(*) FROM returns") or 0)
    distinct_eins = int(_scalar(conn, "SELECT COUNT(DISTINCT ein) FROM returns") or 0)
    _add_stat(rows, "filings", "total_filings", count=total)
    _add_stat(rows, "filings", "distinct_filer_eins", count=distinct_eins)

    for row in conn.execute(
        """
        SELECT COALESCE(CAST(tax_year AS TEXT), '(missing)') AS bucket,
               COUNT(*) AS n
        FROM returns
        GROUP BY bucket
        ORDER BY bucket DESC
        """
    ):
        _add_stat(rows, "filings", "tax_year", row["bucket"], count=row["n"], pct_of_section=_pct(row["n"], total))

    for row in conn.execute(
        """
        SELECT COALESCE(NULLIF(return_type, ''), '(missing)') AS bucket,
               COUNT(*) AS n
        FROM returns
        GROUP BY bucket
        ORDER BY n DESC
        """
    ):
        _add_stat(rows, "filings", "return_type", row["bucket"], count=row["n"], pct_of_section=_pct(row["n"], total))

    return rows


def _grant_match_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    total_grants = int(_scalar(conn, "SELECT COUNT(*) FROM grants") or 0) if _table_exists(conn, "grants") else 0

    if _table_exists(conn, grant_stats.APPLIED_TABLE):
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS grant_rows,
                   COUNT(DISTINCT signature_hash) AS sigs
            FROM {grant_stats.APPLIED_TABLE}
            """
        ).fetchone()
        _add_stat(
            rows,
            "grant_match_summary",
            "enhanced_grant_outcomes",
            "enhanced_match",
            count=row["grant_rows"],
            signatures=row["sigs"],
            pct_of_grants=_pct(row["grant_rows"], total_grants),
            notes="Grant rows with an applied enhanced AI/rule recipient match",
        )
    else:
        _add_stat(
            rows,
            "grant_match_summary",
            "enhanced_grant_outcomes",
            "enhanced_match",
            notes=f"{grant_stats.APPLIED_TABLE} has not been created yet",
        )

    if _table_exists(conn, grant_stats.DECISION_TABLE):
        if _table_exists(conn, grant_stats.SIG_TABLE):
            human = conn.execute(
                f"""
                SELECT COUNT(*) AS sigs,
                       COALESCE(SUM(s.grant_count), 0) AS grants,
                       COALESCE(SUM(s.total_amount), 0) AS amount
                FROM {grant_stats.DECISION_TABLE} d
                LEFT JOIN {grant_stats.SIG_TABLE} s ON s.signature_hash = d.signature_hash
                WHERE COALESCE(d.needs_human_review, 0) = 1
                   OR UPPER(COALESCE(d.decision, '')) = 'HUMAN_REVIEW'
                """
            ).fetchone()
            no_match = conn.execute(
                f"""
                SELECT COUNT(*) AS sigs,
                       COALESCE(SUM(s.grant_count), 0) AS grants,
                       COALESCE(SUM(s.total_amount), 0) AS amount
                FROM {grant_stats.DECISION_TABLE} d
                LEFT JOIN {grant_stats.SIG_TABLE} s ON s.signature_hash = d.signature_hash
                WHERE UPPER(COALESCE(d.decision, '')) = 'NO_MATCH'
                """
            ).fetchone()
            _add_stat(
                rows,
                "grant_match_summary",
                "enhanced_grant_outcomes",
                "human_review",
                signatures=human["sigs"],
                grants_represented=human["grants"],
                total_amount=human["amount"],
                pct_of_grants=_pct(human["grants"], total_grants),
                notes="Decision signatures marked for human review",
            )
            _add_stat(
                rows,
                "grant_match_summary",
                "enhanced_grant_outcomes",
                "no_match",
                signatures=no_match["sigs"],
                grants_represented=no_match["grants"],
                total_amount=no_match["amount"],
                pct_of_grants=_pct(no_match["grants"], total_grants),
                notes="Decision signatures marked NO_MATCH",
            )
        else:
            human_count = int(
                _scalar(
                    conn,
                    f"""
                    SELECT COUNT(*)
                    FROM {grant_stats.DECISION_TABLE}
                    WHERE COALESCE(needs_human_review, 0) = 1
                       OR UPPER(COALESCE(decision, '')) = 'HUMAN_REVIEW'
                    """,
                )
                or 0
            )
            no_match_count = int(
                _scalar(
                    conn,
                    f"""
                    SELECT COUNT(*)
                    FROM {grant_stats.DECISION_TABLE}
                    WHERE UPPER(COALESCE(decision, '')) = 'NO_MATCH'
                    """,
                )
                or 0
            )
            _add_stat(rows, "grant_match_summary", "enhanced_grant_outcomes", "human_review", signatures=human_count)
            _add_stat(rows, "grant_match_summary", "enhanced_grant_outcomes", "no_match", signatures=no_match_count)
    else:
        _add_stat(
            rows,
            "grant_match_summary",
            "enhanced_grant_outcomes",
            "human_review",
            notes=f"{grant_stats.DECISION_TABLE} has not been created yet",
        )
        _add_stat(
            rows,
            "grant_match_summary",
            "enhanced_grant_outcomes",
            "no_match",
            notes=f"{grant_stats.DECISION_TABLE} has not been created yet",
        )

    if _table_exists(conn, grant_stats.SIG_TABLE) and _table_exists(conn, grant_stats.CAND_TABLE):
        decision_filter = ""
        if _table_exists(conn, grant_stats.DECISION_TABLE):
            decision_filter = f"""
            AND NOT EXISTS (
              SELECT 1
              FROM {grant_stats.DECISION_TABLE} d
              WHERE d.signature_hash = s.signature_hash
            )
            """
        pending = conn.execute(
            f"""
            SELECT COUNT(*) AS sigs,
                   COALESCE(SUM(s.grant_count), 0) AS grants,
                   COALESCE(SUM(s.total_amount), 0) AS amount
            FROM {grant_stats.SIG_TABLE} s
            WHERE EXISTS (
              SELECT 1
              FROM {grant_stats.CAND_TABLE} c
              WHERE c.signature_hash = s.signature_hash
            )
            {decision_filter}
            """
        ).fetchone()
        _add_stat(
            rows,
            "grant_match_summary",
            "enhanced_grant_outcomes",
            "pending_ai_adjudication",
            signatures=pending["sigs"],
            grants_represented=pending["grants"],
            total_amount=pending["amount"],
            pct_of_grants=_pct(pending["grants"], total_grants),
            notes="Signatures with candidates and no AI/human decision row",
        )
    else:
        _add_stat(
            rows,
            "grant_match_summary",
            "enhanced_grant_outcomes",
            "pending_ai_adjudication",
            notes="Signature or candidate tables have not been created yet",
        )

    return rows


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {APP_STATS_TABLE} (
          section TEXT NOT NULL,
          metric TEXT NOT NULL,
          bucket TEXT NOT NULL DEFAULT '',
          count INTEGER,
          signatures INTEGER,
          grants_represented INTEGER,
          total_amount NUMERIC,
          pct_of_grants NUMERIC,
          pct_of_section NUMERIC,
          notes TEXT,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_app_data_stats_lookup
          ON {APP_STATS_TABLE}(section, metric, bucket);

        CREATE TABLE IF NOT EXISTS {APP_STATS_META_TABLE} (
          key TEXT PRIMARY KEY,
          value TEXT,
          updated_at TEXT NOT NULL
        );
        """
    )


def refresh_stats(db_path: str, top_n: int = 50, include_final_view: bool = True) -> int:
    db = Path(db_path).expanduser().resolve()
    conn = grant_stats.connect(str(db), readonly=False)
    try:
        rows: List[Dict[str, Any]] = []
        rows.extend(_database_file_stats(db))
        rows.extend(_filing_stats(conn))
        rows.extend(_grant_match_summary(conn))
        rows.extend(grant_stats.collect_stats(conn, top_n=top_n, include_final_view=include_final_view))

        refreshed_at = grant_stats.now_stamp()
        _create_schema(conn)
        conn.execute(f"DELETE FROM {APP_STATS_TABLE}")
        fieldnames = [
            "section",
            "metric",
            "bucket",
            "count",
            "signatures",
            "grants_represented",
            "total_amount",
            "pct_of_grants",
            "pct_of_section",
            "notes",
            "updated_at",
        ]
        insert_sql = f"""
            INSERT INTO {APP_STATS_TABLE} ({",".join(fieldnames)})
            VALUES ({",".join("?" for _ in fieldnames)})
        """
        conn.executemany(
            insert_sql,
            [tuple(row.get(k, refreshed_at if k == "updated_at" else None) for k in fieldnames) for row in rows],
        )
        conn.execute(
            f"""
            INSERT OR REPLACE INTO {APP_STATS_META_TABLE} (key, value, updated_at)
            VALUES ('refreshed_at', ?, ?)
            """,
            (refreshed_at, refreshed_at),
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh cached statistics for the IRS 990 Flask app")
    parser.add_argument("--db", default=grant_stats.DEFAULT_DB, help=f"SQLite database path (default: {grant_stats.DEFAULT_DB})")
    parser.add_argument("--top-n", type=int, default=50, help="Maximum rows for grouped grant matching breakdowns")
    parser.add_argument("--skip-final-view", action="store_true", help="Skip final resolved view counts")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        count = refresh_stats(args.db, top_n=args.top_n, include_final_view=not args.skip_final_view)
        print(f"Refreshed {count} cached data-stat rows.")
        return 0
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
