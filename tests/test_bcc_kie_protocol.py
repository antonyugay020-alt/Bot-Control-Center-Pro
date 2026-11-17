"""Offline tests of the Kie Sonnet 5.5 screenshot contract, no provider calls."""
import unittest

from tools.bcc_kie_protocol import (
    API_HOST, API_PATH, MODEL_ID, NETWORK_ENABLED, ProtocolBlocked,
    auth_headers, build_payload, mock_protocol_check, parse_response,
    send_paid_request,
)


def sample(**changes):
    response = {
        "role": "assistant",
        "model": MODEL_ID,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "FAKE_OK"}],
        "usage": {
            "input_tokens": 600, "output_tokens": 57,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation": {
                "ephemeral_1h_input_tokens": 0,
                "ephemeral_5m_input_tokens": 0,
            },
        },
    }
    response.update(changes)
    return response


class KieScreenshotProtocolTests(unittest.TestCase):
    def test_only_documented_fixed_host_and_path(self):
        self.assertEqual(API_HOST, "api.kie.ai")
        self.assertEqual(API_PATH, "/claude/v1/messages")
        self.assertEqual(MODEL_ID, "claude-sonnet-5-5")
        self.assertFalse(NETWORK_ENABLED)

    def test_payload_has_no_tools_streaming_or_cache(self):
        obj = build_payload("Synthetic BCC test")
        self.assertEqual(obj["model"], MODEL_ID)
        self.assertEqual(obj["messages"], [{"role": "user", "content": "Synthetic BCC test"}])
        self.assertFalse(obj["stream"])
        self.assertNotIn("tools", obj)
        self.assertNotIn("cache_control", str(obj))
        self.assertLessEqual(obj["max_tokens"], 4000)

    def test_payload_rejects_invalid_limits_and_task(self):
        for task, cap in [("", 4000), ("hello", 0), ("hello", 4001),
                          ("hello", True), ("a" * 8193, 4000), ("x\x00y", 4000)]:
            with self.subTest(task_length=len(task), cap=cap):
                with self.assertRaises(ProtocolBlocked):
                    build_payload(task, max_tokens=cap)

    def test_header_uses_bearer_not_x_api_key(self):
        h = auth_headers("FAKE_TOKEN")
        self.assertEqual(h["Authorization"], "Bearer FAKE_TOKEN")
        self.assertNotIn("x-api-key", h)
        self.assertEqual(h["Content-Type"], "application/json")

    def test_header_rejects_newline_or_empty(self):
        for key in ["", " x", "x\nHeader: bad", "x\r", "x\x00"]:
            with self.subTest(key=repr(key)):
                with self.assertRaises(ProtocolBlocked):
                    auth_headers(key)

    def test_example_usage_normal(self):
        obj = parse_response(sample())
        self.assertEqual(obj.usage.input_tokens, 600)
        self.assertEqual(obj.usage.output_tokens, 57)
        # 600 input at $0.8/M plus 57 output at $4/M => $0.000708
        self.assertEqual(obj.usage.estimated_micro_usd, 708)

    def test_cache_usage_5m_1h_and_read(self):
        raw = sample()
        raw["usage"] = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1_000_000,
                "ephemeral_1h_input_tokens": 1_000_000,
            },
            "cache_creation_input_tokens": 2_000_000,
            "cache_read_input_tokens": 1_000_000,
        }
        parsed = parse_response(raw)
        self.assertEqual(parsed.usage.cache_5m_tokens, 1_000_000)
        self.assertEqual(parsed.usage.cache_1h_tokens, 1_000_000)
        self.assertEqual(parsed.usage.cache_read_tokens, 1_000_000)
        self.assertEqual(parsed.usage.estimated_micro_usd, 2_680_000)

    def test_inconsistent_cache_receipt_fails(self):
        raw = sample()
        raw["usage"]["cache_creation_input_tokens"] = 1
        with self.assertRaises(ProtocolBlocked):
            parse_response(raw)

    def test_non_numeric_usage_fails(self):
        for wrong in [None, True, -1, "600", 1.2]:
            with self.subTest(value=repr(wrong)):
                raw = sample()
                raw["usage"]["input_tokens"] = wrong
                with self.assertRaises(ProtocolBlocked):
                    parse_response(raw)

    def test_missing_usage_fails(self):
        with self.assertRaises(ProtocolBlocked):
            parse_response(sample(usage=None))

    def test_tool_use_response_fails(self):
        raw = sample(stop_reason="tool_use")
        raw["content"] = [{"type": "tool_use", "name": "shell", "input": {"command": "test"}}]
        with self.assertRaises(ProtocolBlocked):
            parse_response(raw)

    def test_wrong_model_fails(self):
        with self.assertRaises(ProtocolBlocked):
            parse_response(sample(model="different-model"))

    def test_max_tokens_stop_is_not_accepted_as_completed(self):
        with self.assertRaises(ProtocolBlocked):
            parse_response(sample(stop_reason="max_tokens"))

    def test_empty_or_extra_large_response_fails(self):
        for value in ["", "x" * 150_000]:
            with self.subTest(size=len(value)):
                with self.assertRaises(ProtocolBlocked):
                    parse_response(sample(content=[{"type": "text", "text": value}]))

    def test_paid_request_always_blocked(self):
        with self.assertRaises(ProtocolBlocked):
            send_paid_request("ANYTHING")

    def test_mock_is_explicitly_offline(self):
        result = mock_protocol_check()
        self.assertEqual(result["status"], "OFFLINE_PASS")
        self.assertEqual(result["provider_contract"], "SCREENSHOT_ONLY")
        self.assertEqual(result["paid_calls"], "DISABLED")
        self.assertEqual(result["estimated_micro_usd"], 708)


if __name__ == "__main__":
    unittest.main()
