import sqlite3
import unittest

import olms


class OlmsPaymentViewTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE organizations(f_num INTEGER PRIMARY KEY,display_name TEXT,affiliation TEXT,state TEXT);
            CREATE TABLE filing_periods(f_num INTEGER,period_start TEXT,period_end TEXT,latest_form_type TEXT,latest_rpt_id INTEGER);
            CREATE TABLE filings(rpt_id INTEGER PRIMARY KEY,f_num INTEGER);
            CREATE TABLE payer_payee(
              row_id INTEGER PRIMARY KEY,payer_payee_id INTEGER,payer_payee_type INTEGER,
              rcpt_disb_type INTEGER,name TEXT,po_box TEXT,street TEXT,city TEXT,state TEXT,
              zip TEXT,type_or_class TEXT,itemized INTEGER,non_itemized INTEGER,total INTEGER,rpt_id INTEGER
            );
            CREATE TABLE counterparty_assignments(payer_payee_row_id INTEGER PRIMARY KEY,counterparty_id TEXT);
            CREATE TABLE counterparties(counterparty_id TEXT PRIMARY KEY,matched_ein TEXT,match_status TEXT);
            CREATE TABLE erds_codes(code_type TEXT,code INTEGER,code_name TEXT,code_description TEXT);
            CREATE TABLE disbursements_general(oid INTEGER,date TEXT,amount INTEGER,purpose TEXT,payer_payee_id INTEGER,rpt_id INTEGER);
            INSERT INTO organizations VALUES (1,'Teachers Local','NEA','OR');
            INSERT INTO filing_periods VALUES (1,'2024-07-01','2025-06-30','LM-2',10);
            INSERT INTO filings VALUES (10,1);
            INSERT INTO counterparties VALUES ('cp1','123456789','MATCHED_HIGH_CONFIDENCE');
            INSERT INTO counterparty_assignments VALUES (1,'cp1');
            INSERT INTO payer_payee VALUES (1,7,1002,503,'Community Group','','','Portland','OR','97201','GRANTEE',100,50,150,10);
            INSERT INTO payer_payee VALUES (2,8,1001,503,'Receipt Payer','','','Portland','OR','97201','PAYER',999,0,999,10);
            INSERT INTO disbursements_general VALUES (1,'2025-01-01',100,'Education grant',7,10);
            INSERT INTO erds_codes VALUES ('DISBURSEMENT_CODE',503,'CONTRIBUTIONS','CONTRIBUTIONS, GIFTS AND GRANTS');
            """
        )
        olms.create_payment_views(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_summary_and_transactions_remain_separate_and_payers_are_excluded(self):
        summary = self.conn.execute(
            "SELECT itemized_amount,non_itemized_amount,total_amount FROM v_grants_paid_summary"
        ).fetchone()
        transaction = self.conn.execute(
            "SELECT transaction_amount FROM v_grant_transactions"
        ).fetchone()
        self.assertEqual(summary, (100, 50, 150))
        self.assertEqual(transaction, (100,))
        self.assertNotEqual(summary[2] + transaction[0], summary[2])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v_payment_payees").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v_vendor_payments_summary").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
