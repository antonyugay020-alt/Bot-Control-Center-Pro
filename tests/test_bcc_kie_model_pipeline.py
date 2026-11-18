"""BCC Kie offline vertical integration: task -> review -> budget -> receipt.

Exact source and API credentials never leave the test process. The network,
payment approval, GitHub writes, trading orders and Qt runtime are absent.
"""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import socket
import unittest
from unittest.mock import patch

from tools.bcc_claude_budget import BudgetLedger, SpendingDenied
from tools.bcc_kie_model_budget import KieModelBudget, send_paid_request
from tools.bcc_kie_models import (
    ModelNotReady, validate_mock_responses_reply, plan,
)
from tools.bcc_kie_task_router import (
    RouteBlocked, pair_for_independent_review, prepare_handoff, dispatch_paid,
)

BASE = "7bba4909235cc56e0777075422c705a948eb44c2"
CANDIDATE = "e" * 40  # Fixture only; not a verified GitHub commit.
FILES = ("bcc/market.py", "bcc/trading.py", "tests/test_market.py")
TEXT = "Reconcile the Market Data Hub with risk-managed PAPER state in isolated tests"
NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)


def fake_response(text="OFFLINE_PASS", *, model="terra", usage=None):
    return {
        "status": "completed",
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}]}],
        "usage": usage or {"input_tokens": 80, "output_tokens": 20},
    }


class OfflineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "bcc_shared_kie_budget.sqlite3"
        self.budget = KieModelBudget(self.db, approval_verifier=self.verify_approval)
        self.budget.initialize()
        self.legacy = BudgetLedger(self.db, approval_verifier=lambda *_: True)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def verify_approval(approval, request, model, amount, source, prompt):
        return (
            approval == f"offline-approval-{model}"
            and request == f"offline-request-{model}"
            and model in ("terra", "kimi")
            and amount == 10_000
            and source in (BASE, CANDIDATE)
            and len(prompt) == 64
        )

    def make(self, *, model, stage, source):
        return prepare_handoff(
            task_tag=f"offline_{model}_{stage}",
            task_text=TEXT,
            model=model,
            stage=stage,
            source_sha=source,
            context_paths=FILES,
            max_output_tokens=100,
        )

    def hold(self, item):
        return self.budget.reserve(
            request_id=f"offline-request-{item.model}",
            approval_id=f"offline-approval-{item.model}",
            model=item.model,
            max_micro_usd=10_000,
            source_sha=item.source_sha,
            prompt_sha256=item.prompt_sha256,
            now=NOW,
        )

    def test_terra_implementation_kimi_review_common_budget_without_network(self):
        with patch.object(socket, "create_connection",
                          side_effect=AssertionError("No sockets in offline integration")):
            terra = self.make(model="terra", stage="implementation", source=BASE)
            kimi = self.make(model="kimi", stage="independent_review", source=CANDIDATE)
            review = pair_for_independent_review(terra, kimi, candidate_sha=CANDIDATE)
            self.assertFalse(review["git_ancestry_verified"])
            self.assertFalse(review["ready_for_external_api"])
            for item in (terra, kimi):
                p = plan(TEXT, model=item.model, max_output_tokens=100)
                self.assertFalse(p.api_ready)
                self.assertFalse(p.quote_is_spend_cap)
                self.hold(item)
                parsed = validate_mock_responses_reply(
                    fake_response(), model=item.model,
                    input_cap=p.maximum_input_tokens, output_cap=100)
                self.assertEqual(parsed["paid_calls"], "DISABLED")
                self.assertFalse(parsed["provider_invoice_verified"])
            snap = self.budget.snapshot(now=NOW)
            self.assertEqual(snap["total_occupied_micro_usd"], 20_000)
            self.assertEqual(
                self.legacy.accounting(now=NOW)["unresolved_holds_micro_usd"],
                20_000,
            )
            self.assertFalse(snap["paid_transport_enabled"])
            self.assertNotIn(TEXT, str(terra.safe_dict()))

    def test_previous_claude_hold_limits_total_after_model_reservation(self):
        self.legacy.reserve("prior-sonnet", approval_id="prior-approved",
                            max_input_tokens=1000,
                            max_output_tokens=100, now=NOW)
        terra = self.make(model="terra", stage="implementation", source=BASE)
        self.hold(terra)
        snap = self.budget.snapshot(now=NOW)
        self.assertGreater(snap["legacy_occupied_micro_usd"], 0)
        self.assertEqual(snap["total_occupied_micro_usd"],
                         snap["legacy_occupied_micro_usd"] + 10_000)

    def test_bad_model_reply_leaves_hold_until_real_invoice(self):
        terra = self.make(model="terra", stage="implementation", source=BASE)
        self.hold(terra)
        malicious = {"status": "completed",
                     "output": [{"type": "function_call", "name": "run_shell"}],
                     "usage": {"input_tokens": 80, "output_tokens": 20}}
        with self.assertRaises(ModelNotReady):
            validate_mock_responses_reply(malicious, model="terra",
                                          input_cap=5000, output_cap=100)
        self.assertEqual(
            self.budget.snapshot(now=NOW)["total_occupied_micro_usd"], 10_000)
        with self.assertRaises(SpendingDenied):
            self.hold(terra)

    def test_unexpected_model_or_stale_review_source_blocks_before_budget(self):
        terra = self.make(model="terra", stage="implementation", source=BASE)
        wrong_review = self.make(model="kimi", stage="independent_review", source=BASE)
        with self.assertRaises(RouteBlocked):
            pair_for_independent_review(terra, wrong_review, candidate_sha=CANDIDATE)
        self.assertEqual(self.budget.snapshot(now=NOW)["total_occupied_micro_usd"], 0)

    def test_astra_architecture_can_be_drafted_but_not_sent_or_priced(self):
        astra = self.make(model="astra", stage="architecture", source=BASE)
        self.assertIsNone(astra.historical_price_micro_usd)
        with self.assertRaises(ModelNotReady):
            plan(TEXT, model="astra")
        with self.assertRaises(SpendingDenied):
            self.budget.reserve(
                request_id="offline-request-astra",
                approval_id="offline-approval-astra",
                model="astra", max_micro_usd=10_000,
                source_sha=BASE, prompt_sha256=astra.prompt_sha256, now=NOW)
        self.assertEqual(self.budget.snapshot(now=NOW)["total_occupied_micro_usd"], 0)

    def test_every_external_execution_entrance_remains_locked(self):
        with self.assertRaises(SpendingDenied):
            send_paid_request(model="terra", task=TEXT)
        with self.assertRaises(RouteBlocked):
            dispatch_paid(model="kimi", task=TEXT, approved=True)


if __name__ == "__main__":
    unittest.main()
