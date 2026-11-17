"""Offline tests: no HTTP, no GitHub Actions, no paid model requests."""
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.bcc_claude_budget import BudgetLedger, SpendingDenied, quoted_micro_usd


class BudgetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.nov = datetime(2026, 11, 17, tzinfo=timezone.utc)
        self.dec = datetime(2026, 12, 1, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_screenshot_sonnet_prices_and_rounding(self):
        self.assertEqual(quoted_micro_usd(1_000_000, 1_000_000), 4_800_000)
        self.assertEqual(quoted_micro_usd(1, 1), 5)

    def test_default_denies_even_if_approval_id_present(self):
        ledger = BudgetLedger(self.path)
        with self.assertRaises(SpendingDenied):
            ledger.reserve("a", approval_id="opaque", max_input_tokens=1000,
                           max_output_tokens=1000, now=self.nov)
        self.assertEqual(ledger.accounting(self.nov)["unresolved_holds_micro_usd"], 0)

    def test_trusted_verifier_controls_approval(self):
        ledger = BudgetLedger(self.path, lambda approval, request, amount: approval == "approved" and request == "a")
        with self.assertRaises(SpendingDenied):
            ledger.reserve("b", approval_id="approved", max_input_tokens=100,
                           max_output_tokens=100, now=self.nov)
        r = ledger.reserve("a", approval_id="approved", max_input_tokens=100,
                           max_output_tokens=100, now=self.nov)
        self.assertGreater(r.held_micro_usd, 0)

    def test_hard_cap_shared_among_reservations(self):
        ledger = BudgetLedger(self.path, lambda *_: True)
        # 2 million input + 4 million output = $17.60
        ledger.reserve("first", approval_id="ok", max_input_tokens=2_000_000,
                       max_output_tokens=4_000_000, now=self.nov)
        with self.assertRaisesRegex(SpendingDenied, "exhausted"):
            ledger.reserve("second", approval_id="ok", max_input_tokens=1_000_000,
                           max_output_tokens=500_000, now=self.nov)
        self.assertEqual(ledger.accounting(self.nov)["available_admission_micro_usd"], 400_000)

    def test_unknown_charge_and_month_rollover_remain_held(self):
        ledger = BudgetLedger(self.path, lambda *_: True)
        r = ledger.reserve("first", approval_id="ok", max_input_tokens=1_000_000,
                           max_output_tokens=1_000_000, now=self.nov)
        self.assertEqual(ledger.accounting(self.dec)["unresolved_holds_micro_usd"], r.held_micro_usd)
        with self.assertRaisesRegex(SpendingDenied, "already reserved"):
            ledger.reserve("first", approval_id="ok", max_input_tokens=1_000_000,
                           max_output_tokens=1_000_000, now=self.dec)

    def test_verified_settlement_releases_unused_budget(self):
        ledger = BudgetLedger(self.path, lambda *_: True)
        ledger.reserve("first", approval_id="ok", max_input_tokens=1_000_000,
                       max_output_tokens=1_000_000, now=self.nov)
        ledger.settle("first", 500_000)
        a = ledger.accounting(self.nov)
        self.assertEqual(a["charged_micro_usd"], 500_000)
        self.assertEqual(a["unresolved_holds_micro_usd"], 0)
        self.assertEqual(a["available_admission_micro_usd"], 17_500_000)
        with self.assertRaises(SpendingDenied):
            ledger.settle("first", 0)

    def test_settlement_above_hold_denied_and_retained(self):
        ledger = BudgetLedger(self.path, lambda *_: True)
        r = ledger.reserve("first", approval_id="ok", max_input_tokens=100,
                           max_output_tokens=100, now=self.nov)
        with self.assertRaises(SpendingDenied):
            ledger.settle("first", r.held_micro_usd + 1)
        self.assertEqual(ledger.accounting(self.nov)["unresolved_holds_micro_usd"], r.held_micro_usd)

    def test_one_approval_cannot_fund_multiple_requests(self):
        ledger = BudgetLedger(self.path, lambda *_: True)
        ledger.reserve("first", approval_id="once", max_input_tokens=100,
                       max_output_tokens=100, now=self.nov)
        with self.assertRaises(SpendingDenied):
            ledger.reserve("second", approval_id="once", max_input_tokens=100,
                           max_output_tokens=100, now=self.nov)

    def test_concurrent_reservations_have_one_shared_limit(self):
        ledger = BudgetLedger(self.path, lambda *_: True)
        def request(i):
            try:
                ledger.reserve(f"req-{i}", approval_id=f"approval-{i}",
                               max_input_tokens=5_000_000,
                               max_output_tokens=1_000_000, now=self.nov)
                return True
            except SpendingDenied:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(request, range(8)))
        self.assertEqual(sum(accepted), 2)  # $8 per request, limit $18
        self.assertEqual(ledger.accounting(self.nov)["available_admission_micro_usd"], 2_000_000)

    def test_duplicate_ids_do_not_double_reserve(self):
        ledger = BudgetLedger(self.path, lambda *_: True)
        ledger.reserve("same", approval_id="ok", max_input_tokens=100,
                       max_output_tokens=100, now=self.nov)
        with self.assertRaises(SpendingDenied):
            ledger.reserve("same", approval_id="ok", max_input_tokens=100,
                           max_output_tokens=100, now=self.nov)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM claude_spending").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
