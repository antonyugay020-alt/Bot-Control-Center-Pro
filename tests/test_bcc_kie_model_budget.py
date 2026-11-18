"""Integrated offline tests: Kie Terra/Kimi/Astra share Claude's real budget DB.

All approvals, tokens, models and invoices in this module are synthetic.
No HTTP requests, secrets, persistent user files or live BCC bots are touched.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.bcc_claude_budget import BudgetLedger, SpendingDenied
from tools.bcc_kie_model_budget import (
    KieModelBudget, SIMULATION_ONLY, send_paid_request,
)


def approved(*_args):
    """Synthetic verifier, strictly confined to offline unit tests."""
    return True


class ModelBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "bcc_developer_budget.sqlite3"
        self.models = KieModelBudget(self.db, approval_verifier=approved)
        self.models.initialize()
        self.legacy = BudgetLedger(self.db, approval_verifier=approved)
        self.nov = datetime(2026, 11, 18, tzinfo=timezone.utc)
        self.dec = datetime(2026, 12, 2, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def new(self, request="terraplan1", approval="approved-terra-1",
            model="terra", limit=10_000, now=None):
        return self.models.reserve(request_id=request, approval_id=approval,
                                   model=model, max_micro_usd=limit,
                                   now=now or self.nov)

    def test_no_real_transport_or_approval_in_module(self):
        self.assertTrue(SIMULATION_ONLY)
        with self.assertRaises(SpendingDenied):
            send_paid_request("terra")
        unapproved = KieModelBudget(self.db)
        with self.assertRaises(SpendingDenied):
            unapproved.reserve(request_id="never", approval_id="never",
                               model="terra", max_micro_usd=1000, now=self.nov)

    def test_new_reservation_is_hold_not_invoice(self):
        result = self.new()
        self.assertEqual(result.reserved_micro_usd, 10_000)
        self.assertEqual(result.model, "terra")
        self.assertFalse(result.paid_transport_enabled)
        snapshot = self.models.snapshot(now=self.nov)
        self.assertEqual(snapshot["model_occupied_micro_usd"], 10_000)
        self.assertEqual(snapshot["legacy_occupied_micro_usd"], 0)
        self.assertEqual(snapshot["total_occupied_micro_usd"], 10_000)
        self.assertFalse(snapshot["paid_transport_enabled"])

    def test_legacy_held_charge_counts_against_model_budget(self):
        self.legacy.reserve("sonnet-first", approval_id="sonnet-approved",
                            max_input_tokens=10_000,
                            max_output_tokens=1000, now=self.nov)
        result = self.new()
        snap = self.models.snapshot(now=self.nov)
        self.assertGreater(snap["legacy_occupied_micro_usd"], 0)
        self.assertEqual(snap["total_occupied_micro_usd"],
                         snap["legacy_occupied_micro_usd"] + 10_000)
        self.assertEqual(self.legacy.accounting(now=self.nov)["unresolved_holds_micro_usd"],
                         snap["total_occupied_micro_usd"])

    def test_original_legacy_admission_respects_new_model_hold(self):
        self.new(limit=17_999_900)
        with self.assertRaises(SpendingDenied):
            self.legacy.reserve("sonnet-after-large-terra",
                                approval_id="sonnet-new-approval",
                                max_input_tokens=100,
                                max_output_tokens=100, now=self.nov)
        self.assertEqual(self.legacy.accounting(now=self.nov)["available_admission_micro_usd"],
                         100)

    def test_new_admission_respects_legacy_hold(self):
        self.legacy.reserve("legacy-high", approval_id="legacy-high-approval",
                            max_input_tokens=10_000_000,
                            max_output_tokens=2_000_000, now=self.nov)
        with self.assertRaises(SpendingDenied):
            self.new(limit=17_000_000)

    def test_legacy_and_new_approval_uniqueness_is_bidirectional(self):
        self.new()
        with self.assertRaises(SpendingDenied):
            self.legacy.reserve("sonnet-new", approval_id="approved-terra-1",
                                max_input_tokens=100,
                                max_output_tokens=100, now=self.nov)
        with self.assertRaises(SpendingDenied):
            self.legacy.reserve("terraplan1", approval_id="different-approved",
                                max_input_tokens=100,
                                max_output_tokens=100, now=self.nov)
        self.legacy.reserve("legacy-first", approval_id="legacy-approval",
                            max_input_tokens=100, max_output_tokens=100, now=self.nov)
        with self.assertRaises(SpendingDenied):
            self.new(request="legacy-first", approval="another-approved")
        with self.assertRaises(SpendingDenied):
            self.new(request="another-model-request", approval="legacy-approval")

    def test_concurrent_cross_model_reservations_are_serialized(self):
        def attempt(i):
            try:
                self.new(request="request-" + str(i), approval="approval-" + str(i),
                         model="kimi" if i else "terra", limit=10_000_000)
                return "RESERVED"
            except SpendingDenied:
                return "DENIED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(attempt, range(2)))
        self.assertEqual(sorted(values), ["DENIED", "RESERVED"])
        self.assertEqual(self.models.snapshot(now=self.nov)["total_occupied_micro_usd"],
                         10_000_000)

    def test_held_reservation_carries_across_utc_month(self):
        self.new()
        snap = self.models.snapshot(now=self.dec)
        self.assertEqual(snap["total_occupied_micro_usd"], 10_000)
        self.assertEqual(snap["remaining_admission_micro_usd"], 17_990_000)

    def test_duplicate_request_or_approval_cannot_be_reused_after_timeout(self):
        self.new()
        for request, approval in [
            ("terraplan1", "other"),
            ("other", "approved-terra-1"),
            ("terraplan1", "approved-terra-1"),
        ]:
            with self.subTest(request=request, approval=approval):
                with self.assertRaises(SpendingDenied):
                    self.new(request=request, approval=approval)
        self.assertEqual(self.models.snapshot(now=self.nov)["model_occupied_micro_usd"], 10_000)

    def test_unknown_model_and_bad_amounts_denied(self):
        for model in ("", "some-unknown-model", "gpt-6-astra"):
            with self.subTest(model=model):
                with self.assertRaises(SpendingDenied):
                    self.new(model=model)
        for limit in (0, -1, 18_000_001, True, 10.5, "1000"):
            with self.subTest(limit=limit):
                with self.assertRaises(SpendingDenied):
                    self.new(limit=limit)

    def test_model_approval_cannot_be_used_without_matching_model(self):
        strictly_kimi = KieModelBudget(self.db,
             approval_verifier=lambda approval, request, model, ceiling:
                model == "kimi" and ceiling == 1000)
        with self.assertRaises(SpendingDenied):
            strictly_kimi.reserve(request_id="misrouted", approval_id="specific",
                                  model="terra", max_micro_usd=1000, now=self.nov)
        self.assertEqual(self.models.snapshot(now=self.nov)["model_occupied_micro_usd"], 0)

    def test_corrupt_legacy_settlement_blocks_combined_accounting(self):
        self.legacy.reserve("legacy-broken", approval_id="legacy-broken-approval",
                            max_input_tokens=100, max_output_tokens=100, now=self.nov)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE claude_spending SET status='SETTLED', charged_micro=NULL")
        with self.assertRaises(SpendingDenied):
            self.new()
        with self.assertRaises(SpendingDenied):
            self.models.snapshot(now=self.nov)

    def test_corrupt_model_settlement_blocks_both_ledger_paths(self):
        self.new()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE kie_model_spending SET status='SETTLED', charged_micro=NULL")
        with self.assertRaises(SpendingDenied):
            self.models.snapshot(now=self.nov)
        with self.assertRaises(SpendingDenied):
            self.legacy.reserve("legacy-after-corruption", approval_id="old-after-corruption",
                                max_input_tokens=100, max_output_tokens=100, now=self.nov)

    def test_invoice_must_not_be_inferred_from_token_counts(self):
        self.new()
        with self.assertRaises(SpendingDenied):
            self.models.settle_from_unverified_usage("terraplan1", 0)
        self.assertEqual(self.models.snapshot(now=self.nov)["model_occupied_micro_usd"], 10_000)

    def test_missing_db_does_not_recreate_on_snapshot(self):
        other = KieModelBudget(Path(self.temp.name) / "absent.sqlite")
        with self.assertRaises(SpendingDenied):
            other.snapshot(now=self.nov)
        self.assertFalse(other.db_path.exists())

    def test_legacy_record_survives_migration(self):
        self.legacy.reserve("old", approval_id="old-approval",
                            max_input_tokens=100, max_output_tokens=100, now=self.nov)
        self.models.initialize()
        self.assertGreater(self.models.snapshot(now=self.nov)["legacy_occupied_micro_usd"], 0)


if __name__ == "__main__":
    unittest.main()
