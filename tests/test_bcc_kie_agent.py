"""Provider-independent offline regression; never needs Kie credentials or network."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch
import io
from contextlib import redirect_stdout

from tools.bcc_kie_agent import (GatewayBlocked, key_status, credential_readback, normalize_reply,
                                 offline_probe, paid_request, main)


class GatewayOfflineTests(unittest.TestCase):
    def test_fake_anthropic_response(self):
        r = normalize_reply({"content": [{"type": "text", "text": "Safe"}],
                             "usage": {"input_tokens": 40, "output_tokens": 4}})
        self.assertEqual(r["text_length"], 4)
        self.assertEqual(r["input_tokens"], 40)

    def test_fake_openai_response(self):
        r = normalize_reply({"choices": [{"message": {"content": "Safe"}}],
                             "usage": {"prompt_tokens": 40, "completion_tokens": 4}})
        self.assertEqual(r["output_tokens"], 4)

    def test_missing_usage_fails_closed(self):
        with self.assertRaises(GatewayBlocked):
            normalize_reply({"content": [{"type": "text", "text": "Safe"}]})

    def test_excess_output_fails_closed(self):
        with self.assertRaises(GatewayBlocked):
            normalize_reply({"content": [{"type": "text", "text": "Safe"}],
                             "usage": {"input_tokens": 1, "output_tokens": 4001}})

    def test_tool_call_fails_closed(self):
        with self.assertRaises(GatewayBlocked):
            normalize_reply({"choices": [{"message": {"content": "x", "tool_calls": [{}]}}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 2}})

    def test_nontext_fails_closed(self):
        with self.assertRaises(GatewayBlocked):
            normalize_reply({"content": [{"type": "tool_use", "name": "shell"}],
                             "usage": {"input_tokens": 10, "output_tokens": 10}})

    def test_bool_usage_is_not_integer(self):
        with self.assertRaises(GatewayBlocked):
            normalize_reply({"content": [{"type": "text", "text": "x"}],
                             "usage": {"input_tokens": True, "output_tokens": 2}})

    def test_key_presence_only(self):
        self.assertEqual(key_status(lambda: True), "PRESENT_VALUE_HIDDEN")
        self.assertEqual(key_status(lambda: False), "ABSENT")

    def test_local_vault_readback_never_reveals_value(self):
        self.assertEqual(credential_readback(lambda: "synthetic_secret"), "READABLE_VALUE_HIDDEN")
        self.assertEqual(credential_readback(lambda: ""), "UNREADABLE_OR_INVALID")
        def broken():
            raise RuntimeError("provider denied")
        self.assertEqual(credential_readback(broken), "UNREADABLE_OR_INVALID")

    def test_default_paid_path_always_denies(self):
        with self.assertRaises(GatewayBlocked):
            paid_request(api_key="DO_NOT_SEND", approval=True)

    def test_mock_no_paid_usage(self):
        result = offline_probe()
        self.assertEqual(result["paid_calls"], "DISABLED")
        self.assertEqual(result["mock_contract"], "PASS")
        self.assertEqual(result["real_provider_compatibility"], "UNVERIFIED")

    def test_doctor_shows_no_secret(self):
        out = io.StringIO()
        with patch("tools.bcc_kie_agent.key_status", return_value="PRESENT_VALUE_HIDDEN"), \
             patch("tools.bcc_kie_agent.credential_readback", return_value="READABLE_VALUE_HIDDEN"):
            with redirect_stdout(out):
                self.assertEqual(main(["doctor"]), 0)
        obj = json.loads(out.getvalue())
        self.assertFalse(obj["network_enabled"])
        self.assertEqual(obj["credential"], "PRESENT_VALUE_HIDDEN")
        self.assertEqual(obj["local_readback"], "READABLE_VALUE_HIDDEN")
        self.assertNotIn("secret", out.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
