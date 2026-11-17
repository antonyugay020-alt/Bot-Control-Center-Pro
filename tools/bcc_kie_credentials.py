"""Windows-only, offline credential provisioning for the BCC development agent.

This module never makes Kie.ai calls and never prints or exports the secret.
It is intentionally separate from BCC trading code and running processes.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import getpass
import os
import sys
from typing import Callable, Protocol


TARGET_NAME = "BCC.DevAgent.KieAI.APIKey.v1"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_CREDENTIAL_BLOB_BYTES = 5120


class CredentialError(RuntimeError):
    """Credential operation was denied or failed (never contains the key)."""


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


class CredentialStore(Protocol):
    def is_present(self) -> bool: ...
    def save(self, secret: str) -> None: ...
    def delete(self) -> bool: ...


class WindowsCredentialStore:
    """A per-Windows-user GENERIC credential; not a cloud Work secret store.

    Code executed by the same Windows user can access that user's credentials.
    It is not protection against malware running as that user.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialError("Windows Credential Manager requires Windows")
        dll = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        dll.CredWriteW.argtypes = [PCREDENTIALW, wintypes.DWORD]
        dll.CredWriteW.restype = wintypes.BOOL
        dll.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
        dll.CredReadW.restype = wintypes.BOOL
        dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        dll.CredDeleteW.restype = wintypes.BOOL
        dll.CredFree.argtypes = [ctypes.c_void_p]
        dll.CredFree.restype = None
        self._dll = dll

    @staticmethod
    def _error(operation: str) -> CredentialError:
        return CredentialError(f"Windows Credential Manager {operation} failed: Win32 {ctypes.get_last_error()}")

    def _read_handle(self) -> PCREDENTIALW | None:
        pointer = PCREDENTIALW()
        if not self._dll.CredReadW(TARGET_NAME, CRED_TYPE_GENERIC, 0,
                                   ctypes.byref(pointer)):
            if ctypes.get_last_error() == ERROR_NOT_FOUND:
                return None
            raise self._error("read")
        return pointer

    def is_present(self) -> bool:
        pointer = self._read_handle()
        if pointer is None:
            return False
        self._dll.CredFree(ctypes.cast(pointer, ctypes.c_void_p))
        return True

    def save(self, secret: str) -> None:
        validate_secret(secret)
        # A mutable native buffer is wiped immediately after CredWrite. Python
        # string copies cannot be reliably zeroized; avoid any logging/dumps.
        blob = bytearray(secret.encode("utf-16-le"))
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer(blob)
        credential = CREDENTIALW()
        credential.Type = CRED_TYPE_GENERIC
        credential.TargetName = TARGET_NAME
        credential.Comment = "BCC development agent (not trading credentials)"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "BCC Dev Agent"
        try:
            if not self._dll.CredWriteW(ctypes.byref(credential), 0):
                raise self._error("write")
        finally:
            for i in range(len(blob)):
                blob[i] = 0

    def load_for_agent(self) -> str:
        """For a future locally isolated agent only. Never exposed via CLI."""
        pointer = self._read_handle()
        if pointer is None:
            raise CredentialError("Kie.ai key is not configured")
        try:
            credential = pointer.contents
            size = credential.CredentialBlobSize
            if not 0 < size <= MAX_CREDENTIAL_BLOB_BYTES or size % 2:
                raise CredentialError("Stored credential has an invalid size")
            return ctypes.string_at(credential.CredentialBlob, size).decode("utf-16-le")
        finally:
            self._dll.CredFree(ctypes.cast(pointer, ctypes.c_void_p))

    def delete(self) -> bool:
        if self._dll.CredDeleteW(TARGET_NAME, CRED_TYPE_GENERIC, 0):
            return True
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return False
        raise self._error("delete")


def validate_secret(secret: str) -> None:
    if not isinstance(secret, str) or not secret or secret.strip() != secret:
        raise CredentialError("Key must be non-empty and without surrounding spaces")
    if any(ch in secret for ch in ("\x00", "\n", "\r")):
        raise CredentialError("Key must occupy a single line")
    if len(secret.encode("utf-16-le")) > MAX_CREDENTIAL_BLOB_BYTES:
        raise CredentialError("Key is too long for Windows Credential Manager")


def configure(
    store: CredentialStore,
    prompt: Callable[[str], str] = getpass.getpass,
    confirm: Callable[[str], str] = input,
    report: Callable[[str], None] = print,
) -> bool:
    """Enter twice using a hidden terminal prompt; no network access."""
    if store.is_present():
        if confirm("Key already exists. Type REPLACE to overwrite: ") != "REPLACE":
            report("Existing key unchanged.")
            return False
    first = prompt("Kie.ai API key (hidden): ")
    validate_secret(first)
    second = prompt("Repeat API key (hidden): ")
    if first != second:
        report("Keys do not match; nothing was saved.")
        return False
    store.save(first)
    report("Key saved to Windows Credential Manager. No API request made.")
    return True


def status(store: CredentialStore, report: Callable[[str], None] = print) -> bool:
    present = store.is_present()
    report("Kie.ai credential is stored locally (value hidden)." if present
           else "No Kie.ai credential stored for the BCC development agent.")
    report("Claude calls remain disabled; this is not a connection test.")
    return present


def remove(store: CredentialStore, confirm: Callable[[str], str] = input,
           report: Callable[[str], None] = print) -> bool:
    if not store.is_present():
        report("There is no key to remove.")
        return False
    if confirm("Type DELETE to remove this BCC agent key: ") != "DELETE":
        report("Existing key unchanged.")
        return False
    removed = store.delete()
    report("Key removed." if removed else "Key already absent.")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline BCC Kie.ai key setup (Windows only)")
    parser.add_argument("command", choices=("setup", "status", "remove"),
                        help="No API calls; never pass the key as a CLI argument")
    args = parser.parse_args(argv)
    if os.name != "nt":
        print("This command requires Windows; no changes made.", file=sys.stderr)
        return 2
    if args.command in ("setup", "remove") and not sys.stdin.isatty():
        print("Interactive Windows terminal required; no changes made.", file=sys.stderr)
        return 2
    try:
        store = WindowsCredentialStore()
        if args.command == "setup":
            return 0 if configure(store) else 1
        if args.command == "status":
            status(store)
            return 0
        return 0 if remove(store) else 1
    except (CredentialError, KeyboardInterrupt, EOFError) as exc:
        if isinstance(exc, CredentialError):
            print(f"Blocked: {exc}", file=sys.stderr)
        else:
            print("Cancelled; no key printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
