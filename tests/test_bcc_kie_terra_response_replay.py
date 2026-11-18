"""No-API replay tests for a *synthetic* Kie Terra Responses envelope."""
import json
import unittest

from tools.bcc_kie_terra_response_replay import (
    MAX_REPLAY_BYTES, replay_bytes,
)


def mock_provider(*, text="BCC_TERRA_CONNECTED", model="gpt-6-1-terra",
                  status="completed", with_usage=True):
    raw = {
        "object": "response", "status": status, "model": model,
        "output": [
            {"type": "reasoning", "status": "completed",
             "summary": [{"type": "summary_text", "text": "SYNTHETIC ONLY"}]},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": text, "annotations": []}]},
        ],
    }
    if with_usage:
        raw["usage"] = {
            "input_tokens": 80, "output_tokens": 32,
            "total_tokens": 112,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 12},
        }
    return raw


def encoded(raw):
    return json.dumps(raw, ensure_ascii=False).encode("utf-8")


class KieTerraReplayTests(unittest.TestCase):
    def test_exact_terra_text_valid_and_usage_is_not_an_invoice(self):
        result = replay_bytes(encoded(mock_provider()))
        self.assertEqual(result["stage"], "CONNECTED")
        self.assertTrue(result["text_verified"])
        self.assertTrue(result["usage_receipt_compatible"])
        self.assertEqual(result["input_tokens"], 80)
        self.assertEqual(result["output_tokens"], 32)
        self.assertFalse(result["provider_invoice_verified"])
        self.assertFalse(result["existing_hold_changed"])
        self.assertEqual(result["new_api_calls"], 0)
        self.assertFalse(result["new_paid_requests_authorized"])

    def test_valid_answer_without_usage_preserved_not_billed(self):
        report = replay_bytes(encoded(mock_provider(with_usage=False)))
        self.assertEqual(report["stage"], "CONTENT_CONFIRMED_USAGE_UNVERIFIED")
        self.assertTrue(report["text_verified"])
        self.assertFalse(report["usage_receipt_compatible"])
        self.assertIsNone(report["input_tokens"])
        self.assertIsNone(report["output_tokens"])
        self.assertFalse(report["provider_invoice_verified"])

    def test_valid_answer_with_unknown_billing_field_preserved(self):
        payload = mock_provider()
        payload["usage"]["unverified_provider_credits"] = 999
        report = replay_bytes(encoded(payload))
        self.assertEqual(report["stage"], "CONTENT_CONFIRMED_USAGE_UNVERIFIED")
        self.assertFalse(report["usage_receipt_compatible"])

    def test_wrong_model_cannot_claim_text_success(self):
        report = replay_bytes(encoded(mock_provider(model="moonshotai/kimi-k3")))
        self.assertEqual(report["stage"], "REJECTED_RESPONSE_SCHEMA")
        self.assertFalse(report["text_verified"])

    def test_missing_model_is_not_attested(self):
        payload = mock_provider()
        payload.pop("model")
        report = replay_bytes(encoded(payload))
        self.assertEqual(report["stage"], "MODEL_NOT_ATTESTED")
        self.assertFalse(report["text_verified"])

    def test_function_call_rejected_even_when_text_correct(self):
        payload = mock_provider()
        payload["output"].append({"type": "function_call", "name": "run_shell"})
        report = replay_bytes(encoded(payload))
        self.assertEqual(report["stage"], "REJECTED_RESPONSE_SCHEMA")
        self.assertFalse(report["text_verified"])

    def test_incomplete_response_does_not_claim_text(self):
        result = replay_bytes(encoded(mock_provider(status="incomplete")))
        self.assertEqual(result["stage"], "REJECTED_RESPONSE_SCHEMA")
        self.assertFalse(result["text_verified"])

    def test_wrong_text_never_counted_as_success(self):
        result = replay_bytes(encoded(mock_provider(text="UNEXPECTED")))
        self.assertEqual(result["stage"], "VALID_RESPONSE_UNEXPECTED_TEXT")
        self.assertFalse(result["text_verified"])

    def test_invalid_utf8_json_is_rejected(self):
        for raw in (b"\xff", b"{bad json", b"null", b"[]"):
            with self.subTest(raw=raw[:20]):
                report = replay_bytes(raw)
                self.assertFalse(report["text_verified"])
                self.assertEqual(report["new_api_calls"], 0)

    def test_large_body_is_rejected(self):
        result = replay_bytes(b" " * (MAX_REPLAY_BYTES + 1))
        self.assertEqual(result["stage"], "INVALID_REPLAY_SIZE")

    def test_zero_bytes_is_rejected(self):
        result = replay_bytes(b"")
        self.assertEqual(result["stage"], "INVALID_REPLAY_SIZE")

    def test_never_echo_unexpected_private_text_or_reasoning(self):
        private = "PRIVATE_SYNTHETIC_SECRET_DO_NOT_PRINT"
        payload = mock_provider(text=private)
        payload["output"][0]["summary"][0]["text"] = private
        report = replay_bytes(encoded(payload))
        shown = json.dumps(report)
        self.assertNotIn(private, shown)
        self.assertNotIn("SYNTHETIC ONLY", shown)
        self.assertFalse(report["text_verified"])

    def test_replay_never_opens_credential_or_paid_transport(self):
        import tools.bcc_kie_terra_one_shot as one_shot
        self.assertTrue(one_shot.KNOWN_TERRA_AUTHORIZATION_CONSUMED)
        report = replay_bytes(encoded(mock_provider()))
        self.assertFalse(report["new_paid_requests_authorized"])
        self.assertFalse(report["existing_hold_changed"])
        self.assertFalse(report["full_windows_e2e_verified"])


if __name__ == "__main__":
    unittest.main()
