"""Offline OpenAI budget checks: never grant actual spending authority."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.bcc_openai_budget import (
    OpenAIDenied, OpenAIOfflineBudget, PAID_TRANSPORT_ENABLED,
)


def permit(*_arguments):
    # Deliberately fake host verifier for OFFLINE tests, never production consent.
    return True


class OfflineOpenAIBudgetTests(unittest.TestCase):
    SHA = "1" * 40
    PROMPT = "a" * 64

    def setUp(self):
        self.context = tempfile.TemporaryDirectory()
        self.addCleanup(self.context.cleanup)
        self.path = Path(self.context.name) / "offline.sqlite"
        self.budget = OpenAIOfflineBudget(self.path, verifier=permit)

    def args(self, **overrides):
        base = dict(request_id="job:1", approval_id="test:1",
                    source_sha=self.SHA, prompt_sha256=self.PROMPT,
                    model="gpt-test-offline", max_micro_usd=100_000)
        base.update(overrides)
        return base

    def test_missing_ledger_fails_closed(self):
        with self.assertRaises(OpenAIDenied):
            self.budget.snapshot()
        self.assertFalse(self.path.exists())

    def test_explicit_cap_required(self):
        for value in (0, -1, None, True, 1.5):
            with self.subTest(value=value), self.assertRaises(OpenAIDenied):
                self.budget.initialize_simulation(monthly_cap_micro_usd=value)

    def test_single_use_init_no_reset(self):
        self.budget.initialize_simulation(monthly_cap_micro_usd=200_000)
        with self.assertRaises(OpenAIDenied):
            self.budget.initialize_simulation(monthly_cap_micro_usd=1_000_000)
        self.assertEqual(self.budget.snapshot()["configured_cap_micro_usd"], 200_000)

    def test_no_verifier_no_hold(self):
        self.budget.initialize_simulation(monthly_cap_micro_usd=200_000)
        untrusted = OpenAIOfflineBudget(self.path)
        with self.assertRaises(OpenAIDenied):
            untrusted.reserve_simulation(**self.args())
        self.assertEqual(self.budget.snapshot()["unresolved_holds_micro_usd"], 0)

    def test_hash_bound_held_separately(self):
        self.budget.initialize_simulation(monthly_cap_micro_usd=200_000)
        hold = self.budget.reserve_simulation(**self.args())
        self.assertEqual(hold.source_sha, self.SHA)
        self.assertEqual(hold.prompt_sha256, self.PROMPT)
        self.assertFalse(hold.paid_transport_enabled)
        report = self.budget.snapshot()
        self.assertEqual(report["remaining_micro_usd"], 100_000)
        self.assertFalse(report["paid_transport_enabled"])

    def test_duplicate_request_or_approval_is_rejected(self):
        self.budget.initialize_simulation(monthly_cap_micro_usd=300_000)
        self.budget.reserve_simulation(**self.args())
        with self.assertRaises(OpenAIDenied):
            self.budget.reserve_simulation(**self.args(approval_id="test:2"))
        with self.assertRaises(OpenAIDenied):
            self.budget.reserve_simulation(**self.args(request_id="job:2"))
        self.assertEqual(self.budget.snapshot()["unresolved_holds_micro_usd"], 100_000)

    def test_cap_failures_preserve_existing_holds(self):
        self.budget.initialize_simulation(monthly_cap_micro_usd=150_000)
        self.budget.reserve_simulation(**self.args())
        with self.assertRaises(OpenAIDenied):
            self.budget.reserve_simulation(**self.args(
                request_id="job:2", approval_id="test:2", max_micro_usd=60_000))
        self.assertEqual(self.budget.snapshot()["unresolved_holds_micro_usd"], 100_000)

    def test_holds_survive_month_boundary(self):
        self.budget.initialize_simulation(monthly_cap_micro_usd=150_000)
        self.budget.reserve_simulation(**self.args(
            now=datetime(2026, 11, 22, tzinfo=timezone.utc)))
        with self.assertRaises(OpenAIDenied):
            self.budget.reserve_simulation(**self.args(
                request_id="job:2", approval_id="test:2", max_micro_usd=70_000,
                now=datetime(2026, 12, 1, tzinfo=timezone.utc)))
        self.assertEqual(self.budget.snapshot()["unresolved_holds_micro_usd"], 100_000)

    def test_invalid_model_and_hashes(self):
        self.budget.initialize_simulation(monthly_cap_micro_usd=200_000)
        for kwargs in (
            {"source_sha": "a" * 39},
            {"prompt_sha256": "not-a-hash"},
            {"model": "gpt-test\nEXFIL"},
            {"model": ""},
            {"max_micro_usd": True},
            {"approval_id": ""},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(OpenAIDenied):
                self.budget.reserve_simulation(**self.args(**kwargs))
        self.assertEqual(self.budget.snapshot()["unresolved_holds_micro_usd"], 0)

    def test_sqlite_corruption_fails_closed(self):
        self.budget.initialize_simulation(monthly_cap_micro_usd=200_000)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE ledger_meta SET schema_version=999")
        with self.assertRaises(OpenAIDenied):
            self.budget.snapshot()

    def test_settlement_and_transport_always_blocked(self):
        self.budget.initialize_simulation(monthly_cap_micro_usd=200_000)
        self.assertFalse(PAID_TRANSPORT_ENABLED)
        with self.assertRaises(OpenAIDenied):
            self.budget.settle("job:1", 1)
        with self.assertRaises(OpenAIDenied):
            self.budget.send_paid_request("secret-key")

    def test_symlink_budget_refused(self):
        target = Path(self.context.name) / "target.sqlite"
        self.path.symlink_to(target)
        with self.assertRaises(OpenAIDenied):
            self.budget.initialize_simulation(monthly_cap_micro_usd=100_000)


if __name__ == "__main__":
    unittest.main()
