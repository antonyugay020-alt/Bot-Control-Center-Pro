"""Offline tests for the *separately* user-confirmed Terra one-time call.

No network, paid providers, real credentials, trading files, or live database.
Sender fakes and local temporary SQLite ledgers are used exclusively.
"""
from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.bcc_claude_budget import SpendingDenied
from tools.bcc_kie_model_budget import KieModelBudget
from tools.bcc_kie_sqlite_lifecycle import managed_connection
from tools.bcc_kie_responses_protocol import ResponsesBlocked, build_request
from tools.bcc_kie_terra_one_shot import (
    APPROVAL_ID, CONFIRM_PHRASE, INPUT_CAP, OUTPUT_CAP, PROMPT,
    REQUEST_ID, RESERVE_MICRO_USD, SENTINEL_FILENAME, SOURCE_SHA,
    EXPECTED_TEXT, OneShotBlocked, SafeOutcome, _send_one_https,
    _TRANSPORT_GRANT,
    perform_one_attempt, request_body, verify_preflight,
    assess_terra_response,
)


class FakeStore:
    def __init__(self, key="FAKE_ONLY_NO_REAL_KEY"):
        self.key = key
        self.read_count = 0
    def load_for_agent(self):
        self.read_count += 1
        return self.key


def answer(text=EXPECTED_TEXT, **updates):
    response = {
        "object": "response", "model": "gpt-6.1-terra",
        "status": "completed",
        "output": [
            {"type": "reasoning", "status": "completed", "summary": []},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": text, "annotations": []}]},
        ],
        "usage": {"input_tokens": 18, "output_tokens": 31, "total_tokens": 49},
    }
    response.update(updates)
    return response


class TerraOneTimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "BCCDevAgent"
        self.state.mkdir()
        self.store = FakeStore()
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, secret, req):
        self.calls.append((secret, req))
        return 200, answer()

    def run_test(self, *, typed=CONFIRM_PHRASE, sender=None, store=None, state=None):
        return perform_one_attempt(
            approval_text=typed, store=store or self.store,
            state_directory=state or self.state,
            sender=sender or self.fake,
        )

    def test_spent_authorization_is_blocked_even_with_green_qa(self):
        with patch("tools.bcc_kie_terra_one_shot.verify_preflight",
                   return_value=None) as qa:
            with self.assertRaises(OneShotBlocked):
                self.run_test(sender=_send_one_https)
        self.assertEqual(qa.call_count, 1)
        self.assertEqual(self.store.read_count, 0)
        self.assertFalse((self.state / "claude_budget.sqlite3").exists())

    def test_direct_actual_sender_cannot_bypass_windows_qa(self):
        with patch("tools.bcc_kie_terra_one_shot.verify_preflight",
                   side_effect=OneShotBlocked("QA_FAIL")) as qa:
            with self.assertRaises(OneShotBlocked):
                self.run_test(sender=_send_one_https)
        self.assertEqual(qa.call_count, 1)
        self.assertEqual(self.store.read_count, 0)
        self.assertFalse((self.state / "claude_budget.sqlite3").exists())

    def test_canonical_request_is_small_and_has_no_tools(self):
        req = request_body()
        p = json.loads(req.body)
        self.assertEqual(req.host, "api.kie.ai")
        self.assertEqual(req.path, "/codex/v1/responses")
        self.assertEqual(p["model"], "gpt-6-1-terra")
        self.assertFalse(p["stream"])
        self.assertEqual(p["max_output_tokens"], 256)
        self.assertNotIn("tools", p)
        self.assertNotIn("web_search", req.body.decode("utf-8"))
        self.assertEqual(p["input"][0]["content"][0]["text"], PROMPT)
        self.assertEqual(RESERVE_MICRO_USD, 10_000)

    def test_wrong_confirmation_does_not_read_key_or_create_db(self):
        with self.assertRaises(OneShotBlocked):
            self.run_test(typed="APPROVE 0.01")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.read_count, 0)
        self.assertFalse((self.state / "claude_budget.sqlite3").exists())

    def test_missing_key_does_not_reserve(self):
        with self.assertRaises(OneShotBlocked):
            self.run_test(store=FakeStore(""))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.state / "claude_budget.sqlite3").exists())

    def test_invalid_state_directory_denies_network(self):
        other = Path(self.temp.name) / "not_BCCDevAgent"
        other.mkdir()
        with self.assertRaises(OneShotBlocked):
            self.run_test(state=other)
        self.assertEqual(self.store.read_count, 0)
        self.assertEqual(self.calls, [])

    def test_success_one_request_and_persistent_budget_hold(self):
        out = self.run_test()
        self.assertEqual(out.result, "CONNECTED")
        self.assertEqual(out.http_status, 200)
        self.assertEqual(out.input_tokens, 18)
        self.assertEqual(out.output_tokens, 31)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "FAKE_ONLY_NO_REAL_KEY")
        self.assertTrue((self.state / SENTINEL_FILENAME).is_file())
        budget = KieModelBudget(self.state / "claude_budget.sqlite3")
        snap = budget.snapshot()
        self.assertEqual(snap["model_occupied_micro_usd"], 10_000)
        with managed_connection(sqlite3.connect(str(self.state / "claude_budget.sqlite3"))) as conn:
            row = conn.execute(
                "SELECT model, approval_id, source_sha, prompt_sha256, status "
                "FROM kie_model_spending WHERE request_id=?", (REQUEST_ID,)).fetchone()
        self.assertEqual(row[0], "terra")
        self.assertEqual(row[1], APPROVAL_ID)
        self.assertEqual(row[2], SOURCE_SHA)
        self.assertEqual(row[3], request_body().input_sha256)
        self.assertEqual(row[4], "HELD")
        # The fake test is not an actual provider charge.
        self.assertEqual(out.charged_usd, "UNVERIFIED")

    def test_second_run_blocked_before_reloading_credential(self):
        self.run_test()
        prior_reads = self.store.read_count
        with self.assertRaises(OneShotBlocked):
            self.run_test()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.read_count, prior_reads)

    def test_deleted_ledger_still_blocked_by_sentinel(self):
        # Sentinel guard prevents re-run if someone moves the original
        # ledger; test does NOT actually remove a ledger.
        (self.state / SENTINEL_FILENAME).write_text("already used", encoding="ascii")
        with self.assertRaises(OneShotBlocked):
            self.run_test()
        self.assertEqual(self.calls, [])

    def test_existing_budget_request_blocks_without_sentinel(self):
        self.run_test()
        (self.state / SENTINEL_FILENAME).unlink()  # Synthetic temp fixture only.
        with self.assertRaises(SpendingDenied):
            self.run_test()
        self.assertEqual(len(self.calls), 1)

    def test_http_502_consumes_attempt_and_preserves_hold(self):
        def failed(secret, req):
            self.calls.append("HTTP_ATTEMPT")
            return 502, None
        out = self.run_test(sender=failed)
        self.assertEqual(out.result, "HTTP_ERROR_NO_RETRY")
        self.assertEqual(out.http_status, 502)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(OneShotBlocked):
            self.run_test(sender=failed)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(KieModelBudget(self.state / "claude_budget.sqlite3")
                         .snapshot()["model_occupied_micro_usd"], 10_000)

    def test_network_exception_keeps_one_time_ledger_hold(self):
        def broken(secret, req):
            self.calls.append("STARTED")
            raise OneShotBlocked("FAKE_CONNECTION_FAILURE")
        with self.assertRaises(OneShotBlocked):
            self.run_test(sender=broken)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(OneShotBlocked):
            self.run_test(sender=broken)
        self.assertEqual(len(self.calls), 1)

    def test_wrong_model_response_blocked_without_retry(self):
        def wrong(secret, req):
            self.calls.append("SENT")
            return 200, answer(model="moonshotai/kimi-k3")
        with self.assertRaises(ResponsesBlocked):
            self.run_test(sender=wrong)
        self.assertEqual(self.calls, ["SENT"])
        self.assertTrue((self.state / SENTINEL_FILENAME).exists())

    def test_function_call_blocked_without_retry(self):
        def tools(secret, req):
            self.calls.append("SENT")
            r = answer()
            r["output"].append({"type": "function_call", "name": "delete_bot"})
            return 200, r
        with self.assertRaises(ResponsesBlocked):
            self.run_test(sender=tools)
        self.assertEqual(len(self.calls), 1)

    def test_unexpected_text_not_reported_as_connected(self):
        def text_reply(secret, req):
            return 200, answer(text="something else")
        out = self.run_test(sender=text_reply)
        self.assertEqual(out.result, "VALID_RESPONSE_UNEXPECTED_TEXT")
        self.assertFalse(out.charged_usd == "0.000000")

    def test_safe_outcome_never_includes_user_prompt_or_key(self):
        out = self.run_test()
        report = out.as_json()
        self.assertNotIn(PROMPT, report)
        self.assertNotIn("FAKE_ONLY_NO_REAL_KEY", report)
        self.assertIn('"result": "CONNECTED"', report)

    def test_saved_terra_reply_exact_text_with_missing_usage_is_preserved(self):
        raw = answer()
        raw.pop("usage")
        result = assess_terra_response(raw)
        self.assertEqual(result.result, "CONTENT_CONFIRMED_USAGE_UNVERIFIED")
        self.assertEqual(result.http_status, 200)
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        self.assertEqual(result.charged_usd, "UNVERIFIED")
        self.assertEqual(result.attempts_remaining, 0)

    def test_saved_terra_reply_with_unknown_usage_does_not_settle_hold(self):
        raw = answer()
        raw["usage"]["provider_extra"] = "UNKNOWN_CREDIT_TYPE"
        result = assess_terra_response(raw)
        self.assertEqual(result.result, "CONTENT_CONFIRMED_USAGE_UNVERIFIED")
        self.assertEqual(result.charged_usd, "UNVERIFIED")
        self.assertFalse(result.result == "CONNECTED")

    def test_unexpected_content_without_usage_must_not_claim_confirmation(self):
        raw = answer(text="UNEXPECTED_TEXT")
        raw.pop("usage")
        with self.assertRaises(ResponsesBlocked):
            assess_terra_response(raw)

    def test_unattested_terra_model_cannot_claim_connected(self):
        def no_model(secret, request):
            raw = answer()
            raw.pop("model")
            return 200, raw
        result = self.run_test(sender=no_model)
        self.assertEqual(result.result, "MODEL_NOT_ATTESTED")
        self.assertNotEqual(result.result, "CONNECTED")

    def _preflight_fixture(self):
        root = Path(self.temp.name) / "BCC_AGENT_GATEWAY"
        (root / ".git").mkdir(parents=True)
        qa = root / "QA"
        qa.mkdir()
        sha = "e" * 40
        report = {
            "check": "BCC_KIE_OFFLINE_QA",
            "result": "OFFLINE_PASS_ONLY",
            "exit_code": 0,
            "source_branch": "agent/bcc-work-claude-gateway-20261117",
            "source_sha": sha,
            "paid_provider_calls": 0,
            "github_actions_requested": 0,
        }
        (qa / "BCC_KIE_QA_SUMMARY.json").write_text(json.dumps(report), encoding="utf-8")
        return root, sha

    def test_preflight_blocks_missing_windows_qa(self):
        root = Path(self.temp.name) / "BCC_AGENT_GATEWAY"
        (root / ".git").mkdir(parents=True)
        with self.assertRaises(OneShotBlocked):
            verify_preflight(root)

    def test_preflight_requires_same_source_sha(self):
        root, sha = self._preflight_fixture()
        with patch("tools.bcc_kie_terra_one_shot.subprocess.run", side_effect=[
            SimpleNamespace(returncode=0, stdout="a" * 40 + "\n"),
            SimpleNamespace(returncode=0, stdout=""),
        ]):
            with self.assertRaises(OneShotBlocked):
                verify_preflight(root)

    def test_preflight_blocks_dirty_checkout(self):
        root, sha = self._preflight_fixture()
        with patch("tools.bcc_kie_terra_one_shot.subprocess.run", side_effect=[
            SimpleNamespace(returncode=0, stdout=sha + "\n"),
            SimpleNamespace(returncode=0, stdout=" M tools/unsafe.py\n"),
        ]):
            with self.assertRaises(OneShotBlocked):
                verify_preflight(root)

    def test_preflight_accepts_only_mocked_clean_matching_sha(self):
        root, sha = self._preflight_fixture()
        with patch("tools.bcc_kie_terra_one_shot.subprocess.run", side_effect=[
            SimpleNamespace(returncode=0, stdout=sha + "\n"),
            SimpleNamespace(returncode=0, stdout=""),
        ]) as fake:
            verify_preflight(root)
        self.assertEqual(fake.call_count, 2)

    def test_old_claude_reservations_are_counted_and_preserved(self):
        from tools.bcc_claude_budget import BudgetLedger
        from datetime import datetime, timezone
        prev = BudgetLedger(self.state / "claude_budget.sqlite3",
                            approval_verifier=lambda *_args: True)
        prev.reserve("synthetic-claude", approval_id="synthetic-old-claude",
                     max_input_tokens=10000, max_output_tokens=1000)
        out = self.run_test()
        self.assertEqual(out.result, "CONNECTED")
        snap = KieModelBudget(self.state / "claude_budget.sqlite3").snapshot()
        self.assertGreater(snap["legacy_occupied_micro_usd"], 0)
        self.assertEqual(snap["model_occupied_micro_usd"], 10000)

    def test_sent_request_requires_exact_canonical_body(self):
        req = request_body()
        forged = replace(req, path="/openai/v1/responses")
        with self.assertRaises((OneShotBlocked, ResponsesBlocked)):
            _send_one_https("FAKE", forged, approval_guard=_TRANSPORT_GRANT,
                            connection_factory=lambda *a, **kw: None)


class FakeResp:
    def __init__(self, status, payload=None):
        self.status = status
        self.body = json.dumps(payload if payload is not None else answer()).encode()
        self.reads = 0
    def read(self, amount):
        self.reads += 1
        return self.body


class FakeConn:
    instances = []
    status = 200
    model = "gpt-6.1-terra"
    def __init__(self, host, timeout):
        self.host = host
        self.timeout = timeout
        self.sent = []
        self.closed = False
        self.response = FakeResp(type(self).status, answer(model=type(self).model))
        self.__class__.instances.append(self)
    def request(self, method, path, *, body, headers):
        self.sent.append((method, path, body, headers))
    def getresponse(self):
        return self.response
    def close(self):
        self.closed = True


class FakeHttpsTransportTests(unittest.TestCase):
    def setUp(self):
        FakeConn.instances.clear()
        FakeConn.status = 200
        FakeConn.model = "gpt-6.1-terra"

    def test_direct_terra_paid_transport_without_hold_guard_is_denied(self):
        with self.assertRaises(OneShotBlocked):
            _send_one_https("FAKE_ONLY", request_body(),
                            connection_factory=FakeConn)
        self.assertEqual(FakeConn.instances, [])

    def test_exact_terra_post_one_fake_socket(self):
        status, raw = _send_one_https("FAKE_ONLY", request_body(),
                                      approval_guard=_TRANSPORT_GRANT,
                                      connection_factory=FakeConn)
        self.assertEqual(status, 200)
        self.assertEqual(raw["status"], "completed")
        self.assertEqual(len(FakeConn.instances), 1)
        conn = FakeConn.instances[0]
        self.assertEqual(conn.host, "api.kie.ai")
        self.assertEqual(conn.timeout, 20)
        self.assertEqual(len(conn.sent), 1)
        method, path, body, headers = conn.sent[0]
        self.assertEqual((method, path), ("POST", "/codex/v1/responses"))
        self.assertEqual(body, request_body().body)
        self.assertEqual(headers["Authorization"], "Bearer FAKE_ONLY")
        self.assertTrue(conn.closed)

    def test_http_503_does_not_read_error_body_or_retry(self):
        FakeConn.status = 503
        code, raw = _send_one_https("FAKE_ONLY", request_body(),
                                    approval_guard=_TRANSPORT_GRANT,
                                    connection_factory=FakeConn)
        self.assertEqual((code, raw), (503, None))
        self.assertEqual(len(FakeConn.instances), 1)
        self.assertEqual(len(FakeConn.instances[0].sent), 1)
        self.assertEqual(FakeConn.instances[0].response.reads, 0)

    def test_redirect_302_not_followed(self):
        FakeConn.status = 302
        code, payload = _send_one_https("FAKE_ONLY", request_body(),
                                         approval_guard=_TRANSPORT_GRANT,
                                         connection_factory=FakeConn)
        self.assertEqual((code, payload), (302, None))
        self.assertEqual(len(FakeConn.instances), 1)


if __name__ == "__main__":
    unittest.main()
