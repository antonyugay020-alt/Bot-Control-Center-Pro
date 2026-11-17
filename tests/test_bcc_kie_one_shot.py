"""Offline end-to-end mocks for exactly one authorized Kie.ai request.

Never opens a real socket, reads a live Windows credential, or calls the API.
"""
from pathlib import Path
import tempfile
import unittest

from tools.bcc_claude_budget import BudgetLedger, SpendingDenied
from tools.bcc_kie_protocol import MODEL_ID, ProtocolBlocked
from tools.bcc_kie_one_shot import (
    APPROVAL_PHRASE, EXPECTED_ANSWER, OUTPUT_LIMIT_TOKENS,
    RESERVE_MICRO_USD, OneShotError, _post_once, run_one_shot,
)


class FakeStore:
    def __init__(self, value="FAKE_FOR_TESTS_ONLY"):
        self.value = value
        self.reads = 0

    def load_for_agent(self):
        self.reads += 1
        return self.value


def answer(text=EXPECTED_ANSWER, **updates):
    base = {
        "role": "assistant", "model": MODEL_ID, "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 28, "output_tokens": 8},
    }
    base.update(updates)
    return base


class OneShotPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "budget.sqlite"
        self.store = FakeStore()
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def send(self, key, payload):
        self.calls.append((key, payload))
        return 200, answer()

    def go(self, send=None, approval_text=APPROVAL_PHRASE):
        return run_one_shot(approval_text=approval_text, store=self.store,
                            ledger_path=self.db, send=send or self.send)

    def test_reserve_is_exactly_one_cent(self):
        self.assertEqual(RESERVE_MICRO_USD, 10000)
        self.assertEqual(OUTPUT_LIMIT_TOKENS, 32)

    def test_missing_human_confirmation_does_not_read_key_or_send(self):
        with self.assertRaises(OneShotError):
            self.go(approval_text="yes")
        self.assertEqual(self.store.reads, 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.db.exists())

    def test_one_approved_call_and_persistent_hold(self):
        result = self.go()
        self.assertEqual(result["result"], "CONNECTED")
        self.assertEqual(result["attempts_allowed_remaining"], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["max_tokens"], 32)
        self.assertNotIn("tools", self.calls[0][1])
        self.assertEqual(self.calls[0][0], "FAKE_FOR_TESTS_ONLY")
        ledger = BudgetLedger(self.db)
        self.assertEqual(ledger.accounting()["unresolved_holds_micro_usd"], 10000)
        self.assertEqual(ledger.accounting()["charged_micro_usd"], 0)

    def test_second_invocation_never_sends(self):
        self.go()
        with self.assertRaises(SpendingDenied):
            self.go()
        self.assertEqual(len(self.calls), 1)

    def test_http_unauthorized_keeps_hold_and_no_retries(self):
        def denied(key, payload):
            self.calls.append("HTTP_ATTEMPT")
            return 401, None
        result = self.go(send=denied)
        self.assertEqual(result["http_status"], 401)
        self.assertEqual(result["result"], "HTTP_ERROR_NO_RETRY")
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(SpendingDenied):
            self.go(send=denied)
        self.assertEqual(len(self.calls), 1)

    def test_network_crash_keeps_hold(self):
        def failed(key, payload):
            self.calls.append("ATTEMPT")
            raise OneShotError("broken fixture")
        with self.assertRaises(OneShotError):
            self.go(send=failed)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(SpendingDenied):
            self.go(send=failed)

    def test_wrong_response_model_keeps_hold(self):
        def bad(key, payload):
            self.calls.append("ATTEMPT")
            return 200, answer(model="other")
        with self.assertRaises(ProtocolBlocked):
            self.go(send=bad)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(BudgetLedger(self.db).accounting()["unresolved_holds_micro_usd"], 10000)

    def test_missing_credentials_does_not_reserve_or_send(self):
        self.store = FakeStore("")
        with self.assertRaises(OneShotError):
            self.go()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.db.exists())

    def test_valid_but_unexpected_response_not_confirmed(self):
        def different(key, payload):
            return 200, answer(text="Something else")
        r = self.go(send=different)
        self.assertEqual(r["result"], "VALID_RESPONSE_UNEXPECTED_TEXT")

    def test_output_overcap_keeps_hold(self):
        def over(key, payload):
            return 200, answer(usage={"input_tokens": 30, "output_tokens": 999})
        with self.assertRaises(ProtocolBlocked):
            self.go(send=over)
        self.assertEqual(BudgetLedger(self.db).accounting()["unresolved_holds_micro_usd"], 10000)

    def test_input_overcap_keeps_hold(self):
        def over(key, payload):
            return 200, answer(usage={"input_tokens": 12_001, "output_tokens": 2})
        with self.assertRaises(OneShotError):
            self.go(send=over)
        self.assertEqual(BudgetLedger(self.db).accounting()["unresolved_holds_micro_usd"], 10000)


class FakeHttpResponse:
    def __init__(self, status):
        self.status = status

    def read(self, count):
        return b'{"unexpected":"not used"}'


class FakeHttpConnection:
    instances = []

    def __init__(self, host, timeout):
        self.host = host
        self.timeout = timeout
        self.calls = []
        self.closed = False
        self.__class__.instances.append(self)

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def getresponse(self):
        return FakeHttpResponse(503)

    def close(self):
        self.closed = True


class ExactOriginTests(unittest.TestCase):
    def test_http_503_no_redirect_and_no_response_body(self):
        FakeHttpConnection.instances.clear()
        from tools.bcc_kie_protocol import API_HOST, API_PATH, build_payload
        status, data = _post_once(
            "FAKE_FOR_TESTS_ONLY",
            build_payload("Reply with exactly BCC_KIE_CONNECTED and nothing else.", max_tokens=32),
            connection_factory=FakeHttpConnection,
        )
        self.assertEqual((status, data), (503, None))
        conn = FakeHttpConnection.instances[0]
        self.assertEqual(conn.host, API_HOST)
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(conn.calls[0][0][:2], ("POST", API_PATH))
        self.assertTrue(conn.closed)

    def test_unknown_payload_never_opens_socket(self):
        FakeHttpConnection.instances.clear()
        with self.assertRaises(OneShotError):
            _post_once("FAKE", {"model": MODEL_ID}, connection_factory=FakeHttpConnection)
        self.assertEqual(FakeHttpConnection.instances, [])


if __name__ == "__main__":
    unittest.main()
