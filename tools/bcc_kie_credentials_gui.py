"""Offline graphical key setup for the BCC development agent.

Ctrl+V and explicit Paste buttons work in a masked Tkinter Entry. The secret
never appears in a terminal argument, a log file, or a network request.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Callable, Protocol

# The launcher uses Python -I. Add *only* the trusted repository root,
# not the current working directory, to resolve this local module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.bcc_kie_credentials import CredentialError, WindowsCredentialStore, validate_secret


class SecretEntry(Protocol):
    def selection_present(self) -> bool: ...
    def delete(self, first: object, last: object = ...) -> None: ...
    def insert(self, index: object, value: str) -> None: ...


def paste_secret(entry: SecretEntry, clipboard_read: Callable[[], str]) -> None:
    """Insert clipboard text once; return no secret and never log it.

    Called from Ctrl+V and the Paste button. The handler returns 'break' to
    suppress the platform's second/default paste action.
    """
    value = clipboard_read()
    if entry.selection_present():
        entry.delete("sel.first", "sel.last")
    entry.insert("insert", value)


def save_secret(store: object, first: str, second: str) -> None:
    """Reject mismatch/invalid values before making any credential write."""
    validate_secret(first)
    if first != second:
        raise CredentialError("Keys do not match. Nothing was saved.")
    store.save(first)


def main() -> int:
    if os.name != "nt":
        print("The graphical key setup requires Windows. No changes made.", file=sys.stderr)
        return 2
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("Python Tkinter GUI is unavailable. No key saved.", file=sys.stderr)
        return 2

    try:
        store = WindowsCredentialStore()
    except CredentialError as error:
        print(f"Windows credential storage unavailable: {error}", file=sys.stderr)
        return 1

    try:
        root = tk.Tk()
    except tk.TclError:
        print("Could not open Tk graphical window on this desktop. No key saved.", file=sys.stderr)
        return 2
    print("Opening the BCC Kie.ai key window (Ctrl+V or Paste buttons).", flush=True)
    root.title("BCC — Kie.ai API key")
    root.geometry("515x290")
    root.minsize(480, 275)
    root.resizable(True, False)
    saved_successfully = False
    # Raise above PowerShell at startup, then relinquish topmost.
    root.lift()
    root.attributes("-topmost", True)
    root.after(1600, lambda: root.attributes("-topmost", False))
    root.columnconfigure(0, weight=1)
    frame = ttk.Frame(root, padding=18)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=1)
    frame.columnconfigure(1, weight=0)

    ttk.Label(frame, text="Kie.ai API key for BCC development", font=("Segoe UI", 12, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
    ttk.Label(frame, text="Key (Ctrl+V):").grid(row=1, column=0, sticky="w")
    first = ttk.Entry(frame, show="*", width=46)
    first.grid(row=2, column=0, sticky="ew", pady=(2, 10), padx=(0, 8))
    ttk.Label(frame, text="Repeat key (Ctrl+V):").grid(row=3, column=0, sticky="w")
    second = ttk.Entry(frame, show="*", width=46)
    second.grid(row=4, column=0, sticky="ew", pady=(2, 10), padx=(0, 8))

    def clipboard_action(entry: SecretEntry) -> str:
        try:
            paste_secret(entry, root.clipboard_get)
        except tk.TclError:
            messagebox.showwarning("Clipboard", "Clipboard has no text or is unavailable.", parent=root)
        return "break"

    # On different Windows terminal setups getpass's Ctrl+V varies. A native
    # Tk Entry reliably receives it, with a Paste button as a second route.
    for entry in (first, second):
        entry.bind("<Control-v>", lambda _event, target=entry: clipboard_action(target))
        entry.bind("<Control-V>", lambda _event, target=entry: clipboard_action(target))

    ttk.Button(frame, text="Paste", command=lambda: clipboard_action(first)).grid(
        row=2, column=1, sticky="ew", pady=(2, 10))
    ttk.Button(frame, text="Paste", command=lambda: clipboard_action(second)).grid(
        row=4, column=1, sticky="ew", pady=(2, 10))

    ttk.Label(frame, text="Stored only in Windows Credential Manager. No API calls.",
              wraplength=465).grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 14))

    def save() -> None:
        nonlocal saved_successfully
        try:
            # Confirm replacement *before* writing any new secret.
            if store.is_present():
                approved = messagebox.askyesno(
                    "Replace existing key?",
                    "A BCC Kie.ai key is already stored. Replace it?",
                    parent=root,
                )
                if not approved:
                    return
            save_secret(store, first.get(), second.get())
        except CredentialError as error:
            messagebox.showerror("Key not saved", str(error), parent=root)
            return
        finally:
            # Avoid leaving key characters in the UI after a save attempt.
            first.delete(0, "end")
            second.delete(0, "end")
        saved_successfully = True
        messagebox.showinfo("Saved", "Key stored locally. No API request made.", parent=root)
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=6, column=0, columnspan=2, sticky="e")
    ttk.Button(buttons, text="Cancel", command=root.destroy).pack(side="right", padx=(8, 0))
    ttk.Button(buttons, text="Save key", command=save).pack(side="right")
    first.focus_set()
    root.mainloop()
    if not saved_successfully:
        print("Key entry window closed without saving a key.", flush=True)
        return 1
    print("Key-entry GUI confirms save completed (API remains disabled).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
