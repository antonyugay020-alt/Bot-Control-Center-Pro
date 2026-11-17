"""Linux-safe tests for interactive key setup, without storing real credentials."""

from __future__ import annotations

import io
import ctypes
import os
from contextlib import redirect_stderr
import unittest

from tools.bcc_kie_credentials import (
    CredentialError,
    TARGET_NAME,
    WindowsCredentialStore,
    CREDENTIALW,
    PCREDENTIALW,
    CRED_TYPE_GENERIC,
    CRED_PERSIST_LOCAL_MACHINE,
    configure,
    main,
    remove,
    status,
    validate_secret,
)


class MemoryCredentialStore:
    def __init__(self, secret=None):
        self.secret = secret
        self.writes = 0
        self.deletes = 0

    def is_present(self):
        return self.secret is not None

    def save(self, secret):
        self.secret = secret
        self.writes += 1

    def delete(self):
        self.deletes += 1
        self.secret = None
        return True


class CredentialsTests(unittest.TestCase):
    def test_target_is_development_only(self):
        self.assertEqual(TARGET_NAME, "BCC.DevAgent.KieAI.APIKey.v1")

    def test_setup_saves_exact_key_and_never_prints_it(self):
        store = MemoryCredentialStore()
        messages = []
        prompt_items = iter(["test_key_no_network_123456", "test_key_no_network_123456"])
        self.assertTrue(configure(store, prompt=lambda _: next(prompt_items),
                                  report=messages.append))
        self.assertEqual(store.secret, "test_key_no_network_123456")
        self.assertEqual(store.writes, 1)
        self.assertNotIn(store.secret, " ".join(messages))
        messages.clear()
        self.assertTrue(status(store, report=messages.append))
        self.assertNotIn(store.secret, " ".join(messages))

    def test_mismatch_blocks_save(self):
        store = MemoryCredentialStore()
        prompts = iter(["first", "second"])
        self.assertFalse(configure(store, prompt=lambda _: next(prompts),
                                   report=lambda _: None))
        self.assertIsNone(store.secret)

    def test_existing_key_cannot_be_overwritten_without_confirmation(self):
        store = MemoryCredentialStore("old_key")
        self.assertFalse(configure(store, prompt=lambda _: self.fail("prompt should not run"),
                                   confirm=lambda _: "no", report=lambda _: None))
        self.assertEqual(store.secret, "old_key")
        self.assertEqual(store.writes, 0)

    def test_replace_requires_exact_confirmation(self):
        store = MemoryCredentialStore("old_key")
        prompts = iter(["new_key", "new_key"])
        self.assertTrue(configure(store, prompt=lambda _: next(prompts),
                                  confirm=lambda _: "REPLACE", report=lambda _: None))
        self.assertEqual(store.secret, "new_key")
        self.assertEqual(store.writes, 1)

    def test_deletion_requires_exact_confirmation(self):
        store = MemoryCredentialStore("safe_key")
        self.assertFalse(remove(store, confirm=lambda _: "delete", report=lambda _: None))
        self.assertEqual(store.secret, "safe_key")
        self.assertTrue(remove(store, confirm=lambda _: "DELETE", report=lambda _: None))
        self.assertIsNone(store.secret)
        self.assertEqual(store.deletes, 1)

    def test_invalid_secret_formats(self):
        for secret in ("", " ", "bad\nline", "bad\rline", "bad\x00key", " pre", "post ", "x" * 2561):
            with self.subTest(secret=repr(secret)[:32]), self.assertRaises(CredentialError):
                validate_secret(secret)

    def test_valid_ascii_and_unicode(self):
        validate_secret("ki_test_sample")
        validate_secret("a" * 2560)
        with self.assertRaises(CredentialError):
            validate_secret("😀" * 1281)

    def test_native_credential_write_read_and_delete_wire_shape(self):
        # Fake the Windows APIs, without accessing OS vaults or Kie.ai.
        class FakeAdvapi:
            def __init__(self):
                self.secret_bytes = None
                self.saved_credential = None
                self.saved_blob = None
                self.frees = 0
                self.reads = 0

            def CredWriteW(self, pointer, flags):
                c = ctypes.cast(pointer, PCREDENTIALW).contents
                self.assert_target = c.TargetName
                self.assert_type = c.Type
                self.assert_persist = c.Persist
                self.secret_bytes = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
                self.saved_credential = CREDENTIALW()
                self.saved_credential.Type = c.Type
                self.saved_credential.TargetName = c.TargetName
                self.saved_credential.CredentialBlobSize = c.CredentialBlobSize
                self.saved_blob = (ctypes.c_ubyte * len(self.secret_bytes)).from_buffer_copy(self.secret_bytes)
                self.saved_credential.CredentialBlob = ctypes.cast(self.saved_blob, ctypes.POINTER(ctypes.c_ubyte))
                return 1

            def CredReadW(self, target, type_id, reserved, out):
                self.reads += 1
                self.pointer = ctypes.pointer(self.saved_credential)
                ctypes.cast(out, ctypes.POINTER(PCREDENTIALW))[0] = self.pointer
                return 1

            def CredFree(self, pointer):
                self.frees += 1

            def CredDeleteW(self, target, type_id, flags):
                self.deleted = (target, type_id, flags)
                return 1

        fake = FakeAdvapi()
        store = WindowsCredentialStore.__new__(WindowsCredentialStore)
        store._dll = fake
        secret = "sample-not-a-real-key-123"
        store.save(secret)
        self.assertEqual(fake.secret_bytes, secret.encode("utf-16-le"))
        self.assertEqual(fake.assert_target, TARGET_NAME)
        self.assertEqual(fake.assert_type, CRED_TYPE_GENERIC)
        self.assertEqual(fake.assert_persist, CRED_PERSIST_LOCAL_MACHINE)
        self.assertTrue(store.is_present())
        self.assertEqual(store.load_for_agent(), secret)
        self.assertEqual(fake.frees, 2)
        self.assertTrue(store.delete())
        self.assertEqual(fake.deleted, (TARGET_NAME, CRED_TYPE_GENERIC, 0))

    def test_non_windows_store_refuses_access(self):
        if os.name == "nt":
            self.skipTest("test is specific to offline Linux CI")
        with self.assertRaises(CredentialError):
            WindowsCredentialStore()

    def test_cli_is_offline_and_windows_only(self):
        if os.name == "nt":
            self.skipTest("test is specific to offline Linux CI")
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(main(["status"]), 2)
        self.assertIn("requires Windows", output.getvalue())


if __name__ == "__main__":
    unittest.main()
