"""Offline integration tests for screenshot-documented Kie Responses endpoints.

No real HTTP, API keys, account/billing changes, GitHub writes, or bot access.
"""
import json
import unittest
from tools.bcc_kie_responses_protocol import (
    API_HOST, ENDPOINTS, NETWORK_ENABLED, PAID_CALLS_ENABLED,
    ResponsesBlocked, build_request, describe, endpoint, parse_response,
    send_paid_request,
)


def sample_reply(*, reasoning=True, text="BCC_OFFLINE_RESPONSE", usage=None):
    out = []
    if reasoning:
        out.append({
            "type": "reasoning", "id": "rs_test", "status": "completed",
            "summary": [{"type": "summary_text", "text": "internal example"}],
        })
    out.append({"type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]})
    return {
        "object": "response", "status": "completed",
        "output": out,
        "usage": usage if usage is not None else {
            "input_tokens": 500, "output_tokens": 60,
            "input_tokens_details": {"cached_tokens": 100},
            "output_tokens_details": {"reasoning_tokens": 10},
            "total_tokens": 560,
        },
    }


class KieResponsesOfflineTests(unittest.TestCase):
    def test_terra_pinned_documented_contract(self):
        p = build_request("Explain a synthetic code boundary", model="terra",
                          max_output_tokens=100)
        self.assertEqual(p.host, "api.kie.ai")
        self.assertEqual(p.path, "/codex/v1/responses")
        body = json.loads(p.body)
        self.assertEqual(body["model"], "gpt-6-1-terra")
        self.assertFalse(body["stream"])
        self.assertEqual(body["max_output_tokens"], 100)
        self.assertEqual(body["input"][0]["role"], "user")
        self.assertEqual(body["input"][0]["content"][0]["type"], "input_text")
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertFalse(p.agent_execution_enabled)
        self.assertFalse(p.network_enabled)

    def test_kimi_pinned_documented_chat_contract(self):
        p = build_request("Review a synthetic patch", model="kimi")
        self.assertEqual(p.path, "/openai/v1/responses")
        self.assertEqual(json.loads(p.body)["model"], "kimi-k3")
        self.assertFalse(endpoint("kimi").agent_supported_by_page)
        self.assertFalse(p.agent_execution_enabled)

    def test_astra_and_unknown_model_blocked(self):
        for model in ("astra", "gpt-6-astra", "claude", "", 1):
            with self.subTest(model=model):
                with self.assertRaises(ResponsesBlocked):
                    build_request("test", model=model)

    def test_request_has_only_one_text_item(self):
        raw = json.loads(build_request("hello", model="terra").body)
        self.assertEqual(len(raw["input"]), 1)
        self.assertEqual(len(raw["input"][0]["content"]), 1)
        self.assertEqual(raw["input"][0]["content"][0]["text"], "hello")

    def test_bad_inputs_rejected(self):
        for value in ("", "  ", "x\x00x", "x\rx", "x" * 24_001, None):
            with self.subTest(value=str(value)[:5]):
                with self.assertRaises(ResponsesBlocked):
                    build_request(value, model="terra")

    def test_output_cap_invalid_rejected(self):
        for value in (0, -1, 8193, True, 10.2, "100"):
            with self.subTest(value=value):
                with self.assertRaises(ResponsesBlocked):
                    build_request("test", model="terra", max_output_tokens=value)

    def test_request_does_not_claim_live_approval(self):
        status = describe()
        self.assertEqual(status["status"], "DOCUMENTED_OFFLINE_NOT_LIVE_VERIFIED")
        self.assertFalse(status["transport_enabled"])
        self.assertFalse(status["paid_calls"])
        self.assertFalse(status["endpoints"]["kimi"]["agent_supported_by_page"])
        self.assertTrue(status["endpoints"]["terra"]["agent_supported_by_page"])
        self.assertFalse(status["endpoints"]["terra"]["live_compatibility_verified"])

    def test_terra_response_allows_reasoning_without_exposing_it(self):
        r = parse_response(sample_reply(), model="terra",
                           output_cap=100, input_cap=1000)
        self.assertEqual(r.text, "BCC_OFFLINE_RESPONSE")
        self.assertEqual(r.input_tokens, 500)
        self.assertEqual(r.output_tokens, 60)
        self.assertEqual(r.cached_input_tokens, 100)
        self.assertFalse(r.provider_charge_verified)
        self.assertFalse(r.paid_calls_enabled)
        self.assertNotIn("internal example", str(r))

    def test_kimi_response_allows_reasoning_without_agent_execution(self):
        r = parse_response(sample_reply(), model="kimi",
                           output_cap=100, input_cap=1000)
        self.assertEqual(r.model, "kimi")
        self.assertEqual(r.text, "BCC_OFFLINE_RESPONSE")

    def test_non_reasoning_simple_text_reply(self):
        r = parse_response(sample_reply(reasoning=False),
                           model="terra", output_cap=100)
        self.assertEqual(r.text, "BCC_OFFLINE_RESPONSE")

    def test_function_call_rejected(self):
        raw = sample_reply()
        raw["output"].append({"type": "function_call",
                              "name": "terminal", "arguments": "{}"})
        with self.assertRaises(ResponsesBlocked):
            parse_response(raw, model="terra", output_cap=100)

    def test_web_search_and_file_search_rejected(self):
        for kind in ("web_search_call", "file_search_call", "computer_call"):
            with self.subTest(kind=kind):
                raw = sample_reply()
                raw["output"].insert(0, {"type": kind, "status": "completed"})
                with self.assertRaises(ResponsesBlocked):
                    parse_response(raw, model="terra", output_cap=100)

    def test_invalid_or_incomplete_status_rejected(self):
        for status in ("in_progress", "incomplete", "failed", None):
            with self.subTest(status=status):
                raw = sample_reply()
                raw["status"] = status
                with self.assertRaises(ResponsesBlocked):
                    parse_response(raw, model="terra", output_cap=100)

    def test_incomplete_message_and_reasoning_rejected(self):
        for index in (0, 1):
            raw = sample_reply()
            raw["output"][index]["status"] = "in_progress"
            with self.assertRaises(ResponsesBlocked):
                parse_response(raw, model="terra", output_cap=100)

    def test_empty_or_unbounded_text_rejected(self):
        for value in ("", "x" * 131073):
            with self.subTest(size=len(value)):
                with self.assertRaises(ResponsesBlocked):
                    parse_response(sample_reply(text=value),
                                   model="terra", output_cap=100)

    def test_missing_usage_or_exceeding_tokens_rejected(self):
        for bad in (
            None, {"input_tokens": 2000, "output_tokens": 10},
            {"input_tokens": 10, "output_tokens": 200},
            {"input_tokens": 10, "output_tokens": -1},
            {"input_tokens": True, "output_tokens": 10},
            {"input_tokens": 10, "output_tokens": 10,
             "input_tokens_details": {"cached_tokens": 11}},
        ):
            raw = sample_reply()
            raw["usage"] = bad
            with self.subTest(usage=repr(bad)[:50]):
                with self.assertRaises(ResponsesBlocked):
                    parse_response(raw, model="terra", output_cap=100, input_cap=1000)

    def test_unknown_billable_usage_categories_rejected(self):
        raw = sample_reply()
        raw["usage"]["additional_hidden_charge"] = 100
        with self.assertRaises(ResponsesBlocked):
            parse_response(raw, model="terra", output_cap=100)

    def test_unknown_annotations_are_rejected(self):
        raw = sample_reply()
        raw["output"][-1]["content"][0]["annotations"] = [
            {"type": "url_citation", "url": "https://example.com"}]
        with self.assertRaises(ResponsesBlocked):
            parse_response(raw, model="terra", output_cap=100)

    def test_duplicate_assistant_messages_rejected(self):
        raw = sample_reply()
        raw["output"].append(raw["output"][-1].copy())
        with self.assertRaises(ResponsesBlocked):
            parse_response(raw, model="terra", output_cap=100)

    def test_reasoning_only_response_is_not_a_valid_answer(self):
        raw = sample_reply()
        raw["output"] = raw["output"][:1]
        with self.assertRaises(ResponsesBlocked):
            parse_response(raw, model="terra", output_cap=100)

    def test_network_is_hard_disabled_without_secrets(self):
        self.assertFalse(NETWORK_ENABLED)
        self.assertFalse(PAID_CALLS_ENABLED)
        with self.assertRaises(ResponsesBlocked):
            send_paid_request("PRIVATE_SYNTHETIC_KEY", model="terra", task="test")


if __name__ == "__main__":
    unittest.main()
