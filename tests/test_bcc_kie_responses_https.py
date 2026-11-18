"""Mocked HTTPS tests only. The Kie network gate remains closed in source."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from tools import bcc_kie_responses_https as wire
from tools.bcc_kie_responses_protocol import (
    API_HOST, ResponsesBlocked, build_request,
)


def fixture(*, response_model="gpt-6.1-terra"):
    return {
        "object": "response", "status": "completed",
        "model": response_model,
        "output": [
            {"type": "reasoning", "status": "completed", "summary": []},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "FAKE_OK", "annotations": []}]},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }


class Response:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload if payload is not None else fixture()
        self.read_count = 0

    def read(self, limit):
        self.read_count += 1
        return json.dumps(self.payload).encode("utf-8")


class Connection:
    instances = []
    status = 200
    payload = None

    def __init__(self, host, timeout):
        self.host = host
        self.timeout = timeout
        self.requests = []
        self.closed = False
        self.resp = Response(self.__class__.status, self.__class__.payload)
        self.__class__.instances.append(self)

    def request(self, method, path, *, body, headers):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.resp

    def close(self):
        self.closed = True


class KieResponsesHttpsOfflineTests(unittest.TestCase):
    def setUp(self):
        Connection.instances.clear()
        Connection.status = 200
        Connection.payload = None
        self.req = build_request("synthetic only", model="terra",
                                 max_output_tokens=20)

    def run_mock(self, *, request=None):
        with patch.object(wire, "LIVE_TRANSPORT_ENABLED", True):
            return wire._post_one_mockable(
                "FAKE_TEST_KEY_ONLY", request or self.req,
                connection_factory=Connection, input_token_limit=1000)

    def test_hard_off_without_approval_never_connects(self):
        with self.assertRaises(ResponsesBlocked):
            wire._post_one_mockable("FAKE_TEST_KEY_ONLY", self.req,
                                    connection_factory=Connection)
        self.assertEqual(Connection.instances, [])

    def test_public_paid_entrypoint_unconditionally_blocks(self):
        with self.assertRaises(ResponsesBlocked):
            wire.send_paid_request(model="terra", approval="FAKE")
        self.assertEqual(Connection.instances, [])

    def test_mock_post_terra_one_request_expected_origin(self):
        result = self.run_mock()
        self.assertEqual(result.text, "FAKE_OK")
        self.assertFalse(result.provider_charge_verified)
        self.assertEqual(len(Connection.instances), 1)
        c = Connection.instances[0]
        self.assertEqual(c.host, API_HOST)
        self.assertEqual(c.timeout, 20)
        self.assertEqual(len(c.requests), 1)
        method, path, body, headers = c.requests[0]
        self.assertEqual((method, path), ("POST", "/codex/v1/responses"))
        self.assertEqual(body, self.req.body)
        self.assertEqual(headers["Authorization"], "Bearer FAKE_TEST_KEY_ONLY")
        self.assertTrue(c.closed)

    def test_mock_post_kimi_different_path_no_agent_tool(self):
        Connection.payload = fixture(response_model="moonshotai/kimi-k3")
        req = build_request("synthetic review only", model="kimi")
        result = self.run_mock(request=req)
        self.assertEqual(result.model, "kimi")
        self.assertEqual(Connection.instances[0].requests[0][1],
                         "/openai/v1/responses")
        self.assertNotIn(b"tools", req.body)

    def test_redirect_is_not_followed_and_error_body_not_read(self):
        Connection.status = 302
        with self.assertRaises(ResponsesBlocked):
            self.run_mock()
        self.assertEqual(len(Connection.instances), 1)
        self.assertEqual(len(Connection.instances[0].requests), 1)
        self.assertEqual(Connection.instances[0].resp.read_count, 0)
        self.assertTrue(Connection.instances[0].closed)

    def test_http_502_is_not_retried(self):
        Connection.status = 502
        with self.assertRaisesRegex(ResponsesBlocked, "no retry"):
            self.run_mock()
        self.assertEqual(len(Connection.instances), 1)
        self.assertEqual(len(Connection.instances[0].requests), 1)
        self.assertTrue(Connection.instances[0].closed)

    def test_response_with_wrong_model_rejected_after_one_attempt(self):
        Connection.payload = fixture(response_model="moonshotai/kimi-k3")
        with self.assertRaises(ResponsesBlocked):
            self.run_mock()
        self.assertEqual(len(Connection.instances[0].requests), 1)

    def test_function_call_response_cannot_execute(self):
        response = fixture()
        response["output"].append({"type": "function_call", "name": "run_terminal"})
        Connection.payload = response
        with self.assertRaises(ResponsesBlocked):
            self.run_mock()
        self.assertTrue(Connection.instances[0].closed)

    def test_wrong_origin_or_route_rejected_before_socket(self):
        for bad in (
            replace(self.req, host="api.unknown.invalid"),
            replace(self.req, path="/openai/v1/responses"),
            replace(self.req, agent_execution_enabled=True),
            replace(self.req, network_enabled=True),
        ):
            with self.subTest(bad=repr(bad)[:60]):
                with self.assertRaises(ResponsesBlocked):
                    self.run_mock(request=bad)
                self.assertEqual(Connection.instances, [])

    def test_injected_tool_payload_rejected_before_socket(self):
        data = json.loads(self.req.body)
        data["tools"] = [{"type": "function", "name": "delete_bot"}]
        altered = replace(self.req, body=json.dumps(data).encode("utf-8"))
        with self.assertRaises(ResponsesBlocked):
            self.run_mock(request=altered)
        self.assertEqual(Connection.instances, [])

    def test_modified_task_body_rejected_even_when_schema_valid(self):
        data = json.loads(self.req.body)
        data["input"][0]["content"][0]["text"] = "different"
        altered = replace(self.req, body=json.dumps(
            data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        with self.assertRaises(ResponsesBlocked):
            self.run_mock(request=altered)
        self.assertEqual(Connection.instances, [])

    def test_invalid_credential_rejected_before_socket(self):
        for bad in ("", "key\nInjected: yes", " key ", None):
            with self.subTest(bad=str(bad)):
                with patch.object(wire, "LIVE_TRANSPORT_ENABLED", True):
                    with self.assertRaises(ResponsesBlocked):
                        wire._post_one_mockable(
                            bad, self.req, connection_factory=Connection)
                self.assertEqual(Connection.instances, [])

    def test_remote_response_is_not_an_invoice(self):
        result = self.run_mock()
        self.assertFalse(result.provider_charge_verified)
        self.assertFalse(result.paid_calls_enabled)


if __name__ == "__main__":
    unittest.main()
