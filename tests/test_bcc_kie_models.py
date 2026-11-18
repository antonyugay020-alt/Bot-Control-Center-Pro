"""Offline-only Kie priced model selection; no network calls or credentials."""
import unittest

from tools.bcc_kie_models import (
    MAX_OUTPUT_TOKENS, NETWORK_ENABLED, PAID_EXECUTION_ENABLED, ModelNotReady,
    PROFILES, estimate_micro_usd, plan, profile, send_paid_request, summary,
    validate_mock_responses_reply,
)


def synthetic_answer(**changes):
    raw = {"status": "completed",
           "output": [{"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": "FAKE_OK"}]}],
           "usage": {"input_tokens": 600, "output_tokens": 57,
                     "input_tokens_details": {"cached_tokens": 100}}}
    raw.update(changes)
    return raw


class PricedRouterTests(unittest.TestCase):
    def test_catalog_is_not_an_api_contract(self):
        self.assertFalse(NETWORK_ENABLED)
        self.assertFalse(PAID_EXECUTION_ENABLED)
        self.assertEqual(set(PROFILES), {"terra", "kimi", "astra"})
        self.assertEqual(PROFILES["terra"].api_model_id, "gpt-6-1-terra")
        self.assertEqual(PROFILES["terra"].api_endpoint, "/codex/v1/responses")
        self.assertEqual(PROFILES["kimi"].api_model_id, "kimi-k3")
        self.assertEqual(PROFILES["kimi"].api_endpoint, "/openai/v1/responses")
        self.assertIsNone(PROFILES["astra"].api_model_id)
        self.assertIsNone(PROFILES["astra"].api_endpoint)
        for candidate in PROFILES.values():
            self.assertFalse(candidate.availability_verified)

    def test_terra_example_100k_20k_costs_six_cents(self):
        amount = estimate_micro_usd("terra", input_tokens=100_000,
                                    output_tokens=20_000)
        self.assertEqual(amount, 60_000)

    def test_kimi_example_100k_20k_costs_sixteen_cents(self):
        self.assertEqual(estimate_micro_usd("kimi", input_tokens=100_000,
                                            output_tokens=20_000), 160_000)

    def test_terra_cached_read_and_write_rates(self):
        self.assertEqual(estimate_micro_usd(
            "terra", input_tokens=1_000_000, output_tokens=0,
            cached_input_tokens=1_000_000), 15_000)
        self.assertEqual(estimate_micro_usd(
            "terra", input_tokens=0, output_tokens=0,
            cache_write_tokens=1_000_000), 375_000)

    def test_kimi_cached_read_rate(self):
        self.assertEqual(estimate_micro_usd(
            "kimi", input_tokens=1_000_000, output_tokens=0,
            cached_input_tokens=1_000_000), 80_000)

    def test_kimi_cache_write_unpriced_and_denied(self):
        with self.assertRaises(ModelNotReady):
            estimate_micro_usd("kimi", input_tokens=10, output_tokens=0,
                               cache_write_tokens=1)

    def test_astra_not_fabricated(self):
        self.assertIsNone(profile("astra").api_model_id)
        self.assertTrue(profile("astra").catalog_listed)
        self.assertEqual(str(profile("astra").advertised_input_floor_usd_per_million), "0.28")
        self.assertEqual(profile("astra").advertised_output_floor_usd_per_million, 56)
        with self.assertRaises(ModelNotReady):
            plan("Analyze dependency boundaries", model="astra")
        with self.assertRaises(ModelNotReady):
            estimate_micro_usd("astra", input_tokens=10, output_tokens=20)

    def test_unknown_model_denied(self):
        for name in ["", "gpt-6.1-terra", "Astra", "claude"]:
            with self.subTest(name=name):
                with self.assertRaises(ModelNotReady):
                    profile(name)

    def test_invalid_token_quantities_rejected(self):
        for kw in [
            {"input_tokens": -1, "output_tokens": 10},
            {"input_tokens": True, "output_tokens": 10},
            {"input_tokens": 2, "output_tokens": 0, "cached_input_tokens": 3},
            {"input_tokens": 2, "output_tokens": "3"},
            {"input_tokens": 1, "output_tokens": 1.5},
        ]:
            with self.subTest(kw=kw):
                with self.assertRaises(ModelNotReady):
                    estimate_micro_usd("terra", **kw)

    def test_plan_never_contains_raw_prompt(self):
        text = "PRIVATE_SYNTHETIC_TASK_123"
        p = plan(text, model="terra", max_output_tokens=100)
        self.assertEqual(p.prompt_bytes, len(text.encode("utf-8")))
        self.assertNotIn(text, str(p))
        self.assertFalse(p.api_ready)
        self.assertFalse(p.network_enabled)
        self.assertFalse(p.paid_execution_enabled)
        self.assertGreater(p.historical_quote_micro_usd, 0)
        self.assertFalse(p.quote_is_spend_cap)
        self.assertTrue(p.price_quote_conflict)

    def test_no_default_model_or_automatic_paid_fallback(self):
        with self.assertRaises(TypeError):
            plan("Explicit model is required")
        self.assertEqual(summary()["selection_mode"], "EXPLICIT_ONLY")

    def test_incomplete_inner_assistant_message_rejected(self):
        altered = synthetic_answer(output=[{"type": "message", "role": "assistant",
            "status": "incomplete", "content": [{"type": "output_text", "text": "fake"}]}])
        with self.assertRaises(ModelNotReady):
            validate_mock_responses_reply(altered, model="terra", input_cap=1000, output_cap=100)

    def test_terra_catalog_conflicts_with_old_email_quote(self):
        t = profile("terra")
        self.assertTrue(t.price_quote_conflict)
        self.assertEqual(str(t.advertised_input_floor_usd_per_million), "0.015")
        self.assertEqual(str(t.advertised_output_floor_usd_per_million), "3")
        self.assertEqual(str(t.output_usd_per_million), "1.50")
        p = plan("no actual network", model="terra")
        self.assertFalse(p.quote_is_spend_cap)

    def test_kimi_plan_is_possible_without_network(self):
        self.assertEqual(plan("test", model="kimi").model, "kimi")

    def test_bad_prompt_and_caps_rejected(self):
        for task, cap in [
            ("", 100), ("test\x00malformed", 100), ("test\rmalformed", 100),
            ("a" * 24_001, 100), ("valid", 0),
            ("valid", MAX_OUTPUT_TOKENS + 1), ("valid", True),
        ]:
            with self.subTest(n=len(task), cap=cap):
                with self.assertRaises(ModelNotReady):
                    plan(task, model="terra", max_output_tokens=cap)

    def test_mock_response_usage_accounting(self):
        r = validate_mock_responses_reply(synthetic_answer(),
            model="terra", input_cap=1000, output_cap=100)
        self.assertEqual(r["status"], "OFFLINE_MOCK_VALID")
        self.assertEqual(r["input_tokens"], 600)
        self.assertEqual(r["cached_input_tokens"], 100)
        self.assertEqual(r["estimated_micro_usd"], 237)
        self.assertFalse(r["provider_invoice_verified"])
        self.assertEqual(r["paid_calls"], "DISABLED")

    def test_mock_response_incomplete_rejected(self):
        with self.assertRaises(ModelNotReady):
            validate_mock_responses_reply(synthetic_answer(status="incomplete"),
                model="terra", input_cap=1000, output_cap=100)

    def test_mock_function_calls_rejected(self):
        mock_call = synthetic_answer(output=[{"type": "function_call",
                                               "name": "delete_files"}])
        with self.assertRaises(ModelNotReady):
            validate_mock_responses_reply(mock_call, model="terra",
                                          input_cap=1000, output_cap=100)

    def test_mock_usage_overrun_denied(self):
        with self.assertRaises(ModelNotReady):
            validate_mock_responses_reply(synthetic_answer(
                usage={"input_tokens": 1001, "output_tokens": 5}),
                model="kimi", input_cap=1000, output_cap=100)

    def test_mock_no_usage_denied(self):
        with self.assertRaises(ModelNotReady):
            validate_mock_responses_reply(synthetic_answer(usage=None),
                model="terra", input_cap=1000, output_cap=100)

    def test_unknown_billable_response_usage_rejected(self):
        altered = synthetic_answer(usage={"input_tokens": 100, "output_tokens": 10,
                                          "cache_creation_input_tokens": 50})
        with self.assertRaises(ModelNotReady):
            validate_mock_responses_reply(altered, model="terra",
                                          input_cap=1000, output_cap=100)

    def test_inconsistent_total_tokens_rejected(self):
        altered = synthetic_answer(usage={"input_tokens": 100, "output_tokens": 10,
                                          "total_tokens": 999})
        with self.assertRaises(ModelNotReady):
            validate_mock_responses_reply(altered, model="terra",
                                          input_cap=1000, output_cap=100)

    def test_price_source_and_disabled_network_visible(self):
        status = summary()
        self.assertEqual(status["price_status"], "HISTORICAL_QUOTE")
        self.assertEqual(status["api_contract"], "DOCUMENTED_NOT_LIVE_VERIFIED")
        self.assertEqual(status["paid_calls"], "DISABLED")
        self.assertIsNone(status["models"]["astra"]["input_per_million"])
        self.assertEqual(status["models"]["astra"]["advertised_output_floor_usd_per_million"], "56")
        self.assertFalse(status["models"]["astra"]["availability_verified"])

    def test_paid_transport_always_denied(self):
        with self.assertRaises(ModelNotReady):
            send_paid_request(model="terra", task="test", approved=True)


if __name__ == "__main__":
    unittest.main()
