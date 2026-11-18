"""Offline-only tests for read-only Terra incident postmortem.

All database files are synthetic and scoped to a temporary directory.
No provider transport, Windows Credential Manager, subprocess or live data.
"""
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from tools.bcc_kie_sqlite_lifecycle import managed_connection
from tools.bcc_kie_terra_readonly_diagnose import (
    REQUEST_ID, APPROVAL_ID, SENTINEL, inspect
)


class TerraReadonlyIncidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "BCCDevAgent"
        self.root.mkdir()
        self.db = self.root / "claude_budget.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def setup_table(self, *, status=None, charge=None, model="terra",
                    req=REQUEST_ID, approval=APPROVAL_ID):
        with managed_connection(sqlite3.connect(self.db)) as conn:
            conn.execute("""CREATE TABLE kie_model_spending (
                request_id TEXT, approval_id TEXT, model TEXT,
                status TEXT, hold_micro INTEGER, charged_micro INTEGER
            )""")
            if status is not None:
                conn.execute("INSERT INTO kie_model_spending VALUES (?,?,?,?,?,?)",
                             (req, approval, model, status, 10000, charge))

    def marker(self):
        (self.root / SENTINEL).write_text("CONSUMED", encoding="ascii")

    def test_does_not_create_missing_ledger(self):
        result = inspect(self.root)
        self.assertEqual(result["stage"], "NO_LEDGER")
        self.assertFalse(self.db.exists())
        self.assertFalse(result["new_paid_requests_allowed"])

    def test_missing_state_is_safe(self):
        result = inspect(Path(self.tmp.name) / "other")
        self.assertEqual(result["stage"], "DEVELOPER_STATE_MISSING_OR_INVALID")

    def test_no_table_means_no_confirmed_reservation(self):
        with managed_connection(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE unrelated(x INTEGER)")
        self.assertEqual(inspect(self.root)["stage"], "NO_MODEL_TABLE")

    def test_empty_model_table_means_no_terra_hold(self):
        self.setup_table()
        self.assertEqual(inspect(self.root)["stage"], "NO_TERRA_HOLD")

    def test_hold_without_marker_is_pre_network_gate(self):
        self.setup_table(status="HELD")
        result = inspect(self.root)
        self.assertEqual(result["stage"], "HOLD_BEFORE_SENTINEL")
        self.assertEqual(result["local_held_usd"], "0.010000")
        self.assertIsNone(result["local_recorded_charge_usd"])
        self.assertFalse(result["provider_request_confirmed"])

    def test_marker_with_hold_reaches_gate_but_not_proven_http(self):
        self.setup_table(status="HELD")
        self.marker()
        result = inspect(self.root)
        self.assertEqual(result["stage"], "ATTEMPT_GATE_REACHED_HTTP_UNKNOWN")
        self.assertTrue(result["attempt_marker_present"])
        self.assertFalse(result["provider_request_confirmed"])

    def test_settled_is_not_proof_of_invoice(self):
        self.setup_table(status="SETTLED", charge=0)
        result = inspect(self.root)
        self.assertEqual(result["local_recorded_charge_usd"], "0.000000")
        self.assertFalse(result["provider_charge_verified"])

    def test_marker_without_ledger_is_unknown_and_blocked(self):
        self.marker()
        result = inspect(self.root)
        self.assertEqual(result["stage"], "MARKER_WITHOUT_LEDGER")
        self.assertFalse(result["new_paid_requests_allowed"])

    def test_wrong_request_does_not_masquerade_as_terra_hold(self):
        self.setup_table(status="HELD", req="other-request")
        self.assertEqual(inspect(self.root)["stage"], "NO_TERRA_HOLD")

    def test_ledger_is_not_modified_or_printing_secret(self):
        self.setup_table(status="HELD")
        old = self.db.read_bytes()
        first = inspect(self.root)
        second = inspect(self.root)
        self.assertEqual(first, second)
        self.assertEqual(self.db.read_bytes(), old)
        output = json.dumps(first)
        self.assertNotIn(REQUEST_ID, output)
        self.assertNotIn(APPROVAL_ID, output)
        self.assertFalse(first["secret_read"])
        self.assertFalse(first["network_used"])

    def test_corrupt_sqlite_is_unknown_without_recreation(self):
        self.db.write_bytes(b"synthetic invalid sqlite")
        before = self.db.read_bytes()
        self.assertEqual(inspect(self.root)["stage"], "UNKNOWN_READ_ERROR")
        self.assertEqual(before, self.db.read_bytes())

    def test_invalid_charge_is_fail_closed(self):
        self.setup_table(status="HELD", charge=1)
        result = inspect(self.root)
        self.assertEqual(result["stage"], "UNKNOWN_SETTLEMENT_STATE")
        self.assertFalse(result["new_paid_requests_allowed"])


if __name__ == "__main__":
    unittest.main()
