import sqlite3
import unittest

from queries import ngo_core_data as mod
from rebuild_irs990_slim_clean import (
    IRS990_COLS,
    IRS990EZ_COLS,
    IRS990PF_COLS,
    PF_ANA_COLS,
    PF_BS_COLS,
    SCHEDC_COLS,
)


def _create_singleton(conn, table, cols):
    conn.execute(
        f"CREATE TABLE {table} (filing_id TEXT PRIMARY KEY, "
        + ", ".join(f"{col} NUMERIC" for col in cols)
        + ")"
    )


def _insert_singleton(conn, table, cols, filing_id, values):
    row = [filing_id] + [values.get(col) for col in cols]
    conn.execute(
        f"INSERT INTO {table} (filing_id,{','.join(cols)}) VALUES ("
        + ",".join("?" for _ in row)
        + ")",
        row,
    )


def build_fixture_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE canonical_by_ein_year (
          ein TEXT,
          tax_year INTEGER,
          return_type TEXT,
          period_end TEXT,
          filing_id TEXT
        );

        CREATE TABLE returns (
          filing_id TEXT PRIMARY KEY,
          org_name TEXT,
          dba_name TEXT,
          us_address_line1 TEXT,
          city TEXT,
          state TEXT,
          zip TEXT
        );

        CREATE TABLE return_header_all (
          filing_id TEXT PRIMARY KEY,
          tax_period_begin_dt TEXT
        );

        CREATE TABLE core_hot (
          filing_id TEXT PRIMARY KEY,
          contributions NUMERIC,
          total_revenue NUMERIC,
          total_expenses NUMERIC,
          government_grants NUMERIC
        );

        CREATE TABLE irs990_ez_form990_total_assets_grp (
          filing_id TEXT PRIMARY KEY,
          boyamt NUMERIC,
          eoyamt NUMERIC
        );

        CREATE TABLE irs990_ez_sum_of_total_liabilities_grp (
          filing_id TEXT PRIMARY KEY,
          boyamt NUMERIC,
          eoyamt NUMERIC
        );
        """
    )
    _create_singleton(conn, "irs990_root", IRS990_COLS)
    _create_singleton(conn, "irs990_ez_root", IRS990EZ_COLS)
    _create_singleton(conn, "irs990_pf_root", IRS990PF_COLS)
    _create_singleton(conn, "irs990_schedule_c_root", SCHEDC_COLS)
    _create_singleton(conn, "irs990_pf_analysis_of_revenue_and_expenses", PF_ANA_COLS)
    _create_singleton(conn, "irs990_pf_form990_pfbalance_sheets_grp", PF_BS_COLS)

    filings = [
        ("111111111", 2024, "990", "2024-12-31", "F1"),
        ("111111111", 2023, "990", "2023-12-31", "F2"),
    ]
    conn.executemany("INSERT INTO canonical_by_ein_year VALUES (?,?,?,?,?)", filings)
    conn.executemany(
        "INSERT INTO returns VALUES (?,?,?,?,?,?,?)",
        [
            ("F1", "Lobbying Org", "", "1 Main", "Portland", "OR", "97201"),
            ("F2", "Lobbying Org", "", "1 Main", "Portland", "OR", "97201"),
        ],
    )
    conn.executemany(
        "INSERT INTO core_hot VALUES (?,?,?,?,?)",
        [("F1", 0, 100000, 50000, 0), ("F2", 0, 100000, 50000, 0)],
    )
    _insert_singleton(
        conn,
        "irs990_schedule_c_root",
        SCHEDC_COLS,
        "F1",
        {
            "total_lobbying_expenditures_amt": None,
            "total_lobbying_expend_grp_amt": 10137,
            "total_direct_lobbying_amt": 10137,
        },
    )
    _insert_singleton(
        conn,
        "irs990_schedule_c_root",
        SCHEDC_COLS,
        "F2",
        {
            "total_lobbying_expenditures_amt": None,
            "total_lobbying_expend_grp_amt": None,
            "total_direct_lobbying_amt": 700,
            "total_grassroots_lobbying_amt": 300,
        },
    )
    conn.commit()
    return conn


class CoreDataLobbyingTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture_db()
        self.orig_connect = mod.connect_ro
        mod.connect_ro = lambda: self.conn

    def tearDown(self):
        mod.connect_ro = self.orig_connect
        self.conn.close()

    def test_lobbying_expense_uses_schedule_c_grouped_and_component_fallbacks(self):
        headers, rows = mod.run({"ein_list": "11-1111111"})
        idx = {name: pos for pos, name in enumerate(headers)}
        by_year = {row[idx["tax_year"]]: row for row in rows}

        self.assertEqual(by_year[2024][idx["lobbying_expense"]], 10137)
        self.assertEqual(by_year[2023][idx["lobbying_expense"]], 1000)


if __name__ == "__main__":
    unittest.main()
