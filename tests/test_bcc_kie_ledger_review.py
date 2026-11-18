import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
import io
from tools.bcc_kie_ledger_review import review, main, REQUEST_ID, APPROVAL_ID


class LedgerReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def prepare_db(self, status="HELD", charge=None, hold=10000):
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE claude_spending (request_id TEXT, approval_id TEXT, "
                      "status TEXT, hold_micro INTEGER, charged_micro INTEGER, period TEXT)")
            c.execute("INSERT INTO claude_spending VALUES (?,?,?,?,?,?)",
                      (REQUEST_ID, APPROVAL_ID, status, hold, charge, "2026-11"))

    def test_missing_is_blocked_and_not_created(self):
        self.assertEqual(review(self.db)["state"], "UNKNOWN_BLOCKED")
        self.assertFalse(self.db.exists())

    def test_hold_is_consumed_but_no_provider_charge_claim(self):
        self.prepare_db()
        result = review(self.db)
        self.assertEqual(result["state"], "CONSUMED_HELD")
        self.assertEqual(result["reserved_usd"], "0.010000")
        self.assertIsNone(result["locally_recorded_charge_usd"])
        self.assertFalse(result["provider_invoice_verified"])

    def test_read_only_does_not_change_db(self):
        self.prepare_db()
        before = self.db.read_bytes()
        review(self.db)
        after = self.db.read_bytes()
        self.assertEqual(before, after)

    def test_settled_stays_consumed(self):
        self.prepare_db(status="SETTLED", charge=0)
        self.assertEqual(review(self.db)["state"], "CONSUMED_SETTLED")
        self.assertEqual(review(self.db)["locally_recorded_charge_usd"], "0.000000")

    def test_invalid_record_fails_closed(self):
        self.prepare_db(status="HELD", charge=1)
        self.assertEqual(review(self.db)["state"], "UNKNOWN_BLOCKED")

    def test_missing_row_fails_closed(self):
        self.prepare_db()
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM claude_spending")
        self.assertEqual(review(self.db)["state"], "UNKNOWN_BLOCKED")

    def test_different_approval_row_fails_closed(self):
        self.prepare_db()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE claude_spending SET approval_id='wrong'")
        self.assertEqual(review(self.db)["state"], "UNKNOWN_BLOCKED")

    def test_corrupt_file_fails_closed(self):
        self.db.write_bytes(b"not a SQLite database")
        self.assertEqual(review(self.db)["state"], "UNKNOWN_BLOCKED")

    def test_cli_sanitizes_result(self):
        self.prepare_db()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--ledger", str(self.db)]), 0)
        j = json.loads(output.getvalue())
        self.assertEqual(j["state"], "CONSUMED_HELD")
        self.assertNotIn(REQUEST_ID, output.getvalue())
        self.assertNotIn(APPROVAL_ID, output.getvalue())


if __name__ == "__main__":
    unittest.main()
