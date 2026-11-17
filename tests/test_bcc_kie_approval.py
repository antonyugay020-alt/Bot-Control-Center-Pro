"""Offline Kie spending/approval integration tests, no real credentials or calls."""
import tempfile
import unittest
from pathlib import Path

from tools.bcc_claude_budget import BudgetLedger, SpendingDenied
from tools.bcc_kie_approval import (
    LIVE_EXECUTION_ENABLED, plan_call, simulate_authorized_call, run_paid_call,
)
from tools.bcc_kie_protocol import MODEL_ID, ProtocolBlocked


def reply(**changes):
    base = {"model": MODEL_ID, "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "FAKE_ONLY"}],
            "usage": {"input_tokens": 20, "output_tokens": 2}}
    base.update(changes)
    return base


class OfflineApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "mock_ledger.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def simulate(self, task="offline-test", **kw):
        return simulate_authorized_call(task, approval_id=kw.get("approval_id", "human-approval-1"),
                                        verifier=kw.get("verifier", lambda *_: True),
                                        ledger_path=self.db,
                                        fake_response=kw.get("fake_response", reply()),
                                        output_cap=kw.get("output_cap", 4000))

    def test_preview_is_stable_and_no_prompt_leaked(self):
        a = plan_call("synthetic BCC task")
        b = plan_call("synthetic BCC task")
        self.assertEqual(a, b)
        self.assertEqual(len(a.request_id), 64)
        self.assertNotIn("synthetic BCC task", str(a))
        self.assertGreater(a.held_micro_usd, 0)

    def test_trusted_verifier_required(self):
        with self.assertRaises(SpendingDenied):
            self.simulate(verifier=None)

    def test_unapproved_is_rejected_before_reservation(self):
        with self.assertRaises(SpendingDenied):
            self.simulate(verifier=lambda *_: False)
        self.assertEqual(BudgetLedger(self.db).accounting()["unresolved_holds_micro_usd"], 0)

    def test_approved_fake_response_only_settles_estimated_amount(self):
        result = self.simulate()
        self.assertEqual(result["mode"], "OFFLINE_MOCK_ONLY")
        self.assertEqual(result["estimated_micro_usd"], 24)
        self.assertEqual(result["paid_calls"], "DISABLED")
        acct = BudgetLedger(self.db).accounting()
        self.assertEqual(acct["charged_micro_usd"], 24)
        self.assertEqual(acct["unresolved_holds_micro_usd"], 0)

    def test_unknown_response_retains_hold(self):
        with self.assertRaises(ProtocolBlocked):
            self.simulate(fake_response=reply(usage=None))
        self.assertGreater(BudgetLedger(self.db).accounting()["unresolved_holds_micro_usd"], 0)

    def test_wrong_model_retains_hold(self):
        with self.assertRaises(ProtocolBlocked):
            self.simulate(fake_response=reply(model="other"))
        self.assertGreater(BudgetLedger(self.db).accounting()["unresolved_holds_micro_usd"], 0)

    def test_duplicate_prompt_cannot_be_resent_with_new_approval(self):
        self.simulate()
        with self.assertRaises(SpendingDenied):
            self.simulate(approval_id="human-approval-2")

    def test_token_overrun_retains_hold(self):
        over = reply(usage={"input_tokens": 900000, "output_tokens": 2})
        with self.assertRaises(ProtocolBlocked):
            self.simulate(fake_response=over)
        self.assertGreater(BudgetLedger(self.db).accounting()["unresolved_holds_micro_usd"], 0)

    def test_execution_hard_disabled(self):
        self.assertFalse(LIVE_EXECUTION_ENABLED)
        with self.assertRaises(ProtocolBlocked):
            run_paid_call("FAKE_KEY")


if __name__ == "__main__":
    unittest.main()
