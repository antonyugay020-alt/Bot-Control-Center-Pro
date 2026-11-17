"""Network-free fake HTTPS tests: paid route stays disabled in deployed code."""
import json
import unittest
from unittest import mock
from tools import bcc_kie_https
from tools.bcc_kie_protocol import API_PATH, API_HOST, MODEL_ID, ProtocolBlocked, build_payload


def response_payload():
    return {"role": "assistant", "model": MODEL_ID, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "MOCK_OK"}],
            "usage": {"input_tokens": 20, "output_tokens": 2}}


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = json.dumps(data if data is not None else response_payload()).encode("utf-8")

    def read(self, cap):
        return self.data[:cap]


class FakeConnection:
    instances = []

    def __init__(self, host, *, timeout):
        self.host, self.timeout = host, timeout
        self.closed = False
        self.calls = []
        self.reply = FakeResponse()
        type(self).instances.append(self)

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def getresponse(self):
        return self.reply

    def close(self):
        self.closed = True


class FixedOriginTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances.clear()
        self.payload = build_payload("OFFLINE TEST", max_tokens=200)

    def call(self, factory=FakeConnection):
        with mock.patch.object(bcc_kie_https, "LIVE_TRANSPORT_ENABLED", True):
            return bcc_kie_https._post_fixed_origin(
                "FAKE_KEY", self.payload, connection_factory=factory)

    def test_default_denies_before_opening_connection(self):
        with self.assertRaises(ProtocolBlocked):
            bcc_kie_https._post_fixed_origin(
                "FAKE_KEY", self.payload, connection_factory=FakeConnection)
        self.assertEqual(FakeConnection.instances, [])

    def test_live_mock_transport_pins_host_path_and_headers(self):
        reply = self.call()
        self.assertEqual(reply["content"][0]["text"], "MOCK_OK")
        self.assertEqual(len(FakeConnection.instances), 1)
        client = FakeConnection.instances[0]
        self.assertTrue(client.closed)
        self.assertEqual(client.host, API_HOST)
        self.assertEqual(client.timeout, 20)
        args, kwargs = client.calls[0]
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], API_PATH)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer FAKE_KEY")
        self.assertEqual(json.loads(kwargs["body"])["model"], MODEL_ID)

    def test_payload_augmentation_denied(self):
        self.payload = dict(self.payload, tools=[{"name": "run_shell"}])
        with self.assertRaises(ProtocolBlocked):
            self.call()
        self.assertEqual(FakeConnection.instances, [])

    def test_non_200_does_not_echo_response_or_key(self):
        class BadConnection(FakeConnection):
            def __init__(self, host, *, timeout):
                super().__init__(host, timeout=timeout)
                self.reply = FakeResponse(503, {"error": "FAKE_KEY"})
        with self.assertRaises(ProtocolBlocked) as err:
            self.call(BadConnection)
        self.assertNotIn("FAKE_KEY", str(err.exception))
        self.assertTrue(FakeConnection.instances[0].closed)

    def test_invalid_usage_denied(self):
        class BadConnection(FakeConnection):
            def __init__(self, host, *, timeout):
                super().__init__(host, timeout=timeout)
                data = response_payload()
                data.pop("usage")
                self.reply = FakeResponse(200, data)
        with self.assertRaises(ProtocolBlocked):
            self.call(BadConnection)

    def test_http_connection_failure_never_retries(self):
        class BadConnection(FakeConnection):
            def request(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                raise OSError("FAKE_KEY internal exception")
        with self.assertRaises(ProtocolBlocked) as err:
            self.call(BadConnection)
        self.assertNotIn("FAKE_KEY", str(err.exception))
        self.assertEqual(len(FakeConnection.instances), 1)
        self.assertTrue(FakeConnection.instances[0].closed)

    def test_public_paid_route_always_denied(self):
        with self.assertRaises(ProtocolBlocked):
            bcc_kie_https.send_paid_request(approval="FAKE")


if __name__ == "__main__":
    unittest.main()
