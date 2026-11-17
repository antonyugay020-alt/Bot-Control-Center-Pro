"""Offline mock tests of Ctrl+V handling and secret matching; no GUI/API calls."""
import unittest

from tools.bcc_kie_credentials import CredentialError
from tools.bcc_kie_credentials_gui import paste_secret, save_secret


class FakeEntry:
    def __init__(self, value="", has_selection=False):
        self.value = value
        self.has_selection = has_selection
        self.inserted = []
        self.deleted = []

    def selection_present(self):
        return self.has_selection

    def delete(self, start, end):
        self.deleted.append((start, end))
        if self.has_selection:
            self.value = ""
            self.has_selection = False

    def insert(self, index, value):
        self.inserted.append((index, value))
        self.value += value


class FakeStore:
    def __init__(self):
        self.saved = []

    def save(self, value):
        self.saved.append(value)


class GuiTests(unittest.TestCase):
    def test_paste_once(self):
        entry = FakeEntry()
        paste_secret(entry, lambda: "synthetic-secret")
        self.assertEqual(entry.value, "synthetic-secret")
        self.assertEqual(len(entry.inserted), 1)

    def test_paste_replaces_selection(self):
        entry = FakeEntry("old", True)
        paste_secret(entry, lambda: "new")
        self.assertEqual(entry.value, "new")
        self.assertEqual(entry.deleted, [("sel.first", "sel.last")])

    def test_clipboard_failure_does_not_write(self):
        entry = FakeEntry()
        def fail():
            raise RuntimeError("clipboard unavailable")
        with self.assertRaises(RuntimeError):
            paste_secret(entry, fail)
        self.assertEqual(entry.value, "")

    def test_exact_match_stores_only_once(self):
        store = FakeStore()
        save_secret(store, "sample-xyz", "sample-xyz")
        self.assertEqual(store.saved, ["sample-xyz"])

    def test_mismatch_blocks_write(self):
        store = FakeStore()
        with self.assertRaises(CredentialError):
            save_secret(store, "a", "b")
        self.assertEqual(store.saved, [])

    def test_malformed_clipboard_blocks_write(self):
        store = FakeStore()
        with self.assertRaises(CredentialError):
            save_secret(store, "\nsecret", "\nsecret")
        self.assertEqual(store.saved, [])


if __name__ == "__main__":
    unittest.main()
