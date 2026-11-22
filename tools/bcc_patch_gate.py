"""No-execution, no-network checker for a proposed Bybit recovery diff.

Only a clean, pinned PR #12 detached snapshot may be inspected. A passing
report means git could apply the patch, NOT that the change is safe, reviewed,
applied, pushed, tested, approved, or ready to release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from tools.bcc_openai_job import JobBlocked, TOKEN_PATTERN, check_snapshot

PINNED_SHA = "12816756c13f9e979db26c9ffa01b4be8374c9a8"
PINNED_PROMPT_SHA256 = "962cb7375a37f877c5932277ae94a6fe83ed2ebfe00e7611609b1e4bab797092"
MAX_DIFF_BYTES = 150_000
MAX_CHANGED_LINES = 1_000
ALLOWED_FILES = frozenset({
    "bcc/ws_quotes.py",
    "bcc/market_hub.py",
    "bcc/scheduler.py",
    "bcc/runtime.py",
    "bcc/market.py",
    "tests/test_scanner_retry_fairness.py",
    "tests/test_live_history_freshness.py",
    "tests/test_worker_observation_owner.py",
    "tests/test_824_configured_admission_recovery.py",
    "tests/test_quote_history_isolation.py",
    "tests/test_hub_demand_recovery.py",
})
DIFF_HEADER = re.compile(r"^diff --git a/([^\n\r\t ]+) b/([^\n\r\t ]+)$", re.M)
FORBIDDEN_DIRECTIVES = (
    "GIT binary patch", "Binary files ", "new file mode ",
    "deleted file mode ", "old mode ", "new mode ",
    "rename from ", "rename to ", "copy from ", "copy to ",
    "Subproject commit ", "similarity index ", "dissimilarity index ",
)


def _git_apply(root: Path, mode: str, patch: bytes) -> str:
    # Git apply --check, --numstat and --summary inspect without writing.
    # Never invoke bare git apply or a shell command.
    if mode not in ("--check", "--numstat", "--summary"):
        raise JobBlocked("Unsupported Git inspection mode")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "apply", mode, "-"],
            input=patch, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise JobBlocked("Git patch inspection unavailable") from None
    if result.returncode:
        raise JobBlocked("Proposed patch failed Git validation")
    try:
        return result.stdout.decode("utf-8", errors="strict")
    except UnicodeError:
        raise JobBlocked("Unrecognized Git inspection output") from None


def inspect_patch(root: Path, source_sha: str, prompt_sha256: str,
                  patch: bytes) -> dict[str, object]:
    if source_sha != PINNED_SHA or prompt_sha256 != PINNED_PROMPT_SHA256:
        raise JobBlocked("Diff is not bound to the reviewed PR12 task")
    if type(patch) is not bytes or not 0 < len(patch) <= MAX_DIFF_BYTES:
        raise JobBlocked("Empty or oversized patch")
    if b"\x00" in patch or b"\r" in patch:
        raise JobBlocked("Binary or noncanonical patch")
    try:
        text = patch.decode("utf-8", errors="strict")
    except UnicodeError:
        raise JobBlocked("Patch is not UTF-8") from None
    if any(line.startswith(FORBIDDEN_DIRECTIVES) for line in text.splitlines()):
        raise JobBlocked("Patch metadata may not create, delete or move files")
    if TOKEN_PATTERN.search(text):
        raise JobBlocked("Possible secret in proposed patch")
    headers = DIFF_HEADER.findall(text)
    if not headers or len(headers) > len(ALLOWED_FILES):
        raise JobBlocked("Missing or excessive Git patch headers")
    names = [left for left, right in headers if left == right]
    if (len(names) != len(headers) or len(set(names)) != len(names)
            or not set(names).issubset(ALLOWED_FILES)):
        raise JobBlocked("Unreviewed, renamed or duplicated file in patch")
    for path in names:
        target = root / path
        if (not target.is_file() or target.is_symlink()
                or not target.resolve().is_relative_to(root)):
            raise JobBlocked("Patch targets unsafe or missing file")
    verified = check_snapshot(root, source_sha)
    if _git_apply(verified, "--summary", patch).strip():
        raise JobBlocked("Patch contains file modes or other structural changes")
    numstat = _git_apply(verified, "--numstat", patch)
    counts = {}
    changed = 0
    for line in numstat.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or fields[2] not in ALLOWED_FILES:
            raise JobBlocked("Unexpected path in Git patch summary")
        a, d, path = fields
        if (path in counts or not a.isascii() or not d.isascii()
                or not a.isdecimal() or not d.isdecimal()):
            raise JobBlocked("Invalid Git changed line counts")
        counts[path] = {"added": int(a), "removed": int(d)}
        changed += int(a) + int(d)
    if set(counts) != set(names) or not 0 < changed <= MAX_CHANGED_LINES:
        raise JobBlocked("Diff file or change count mismatch")
    _git_apply(verified, "--check", patch)
    return {
        "status": "PATCH_PROPOSAL_VALIDATED",
        "source_sha": source_sha,
        "prompt_sha256": prompt_sha256,
        "diff_sha256": hashlib.sha256(patch).hexdigest(),
        "diff_bytes": len(patch),
        "changed_lines": changed,
        "files": counts,
        "patch_applied": False,
        "model_calls": "DISABLED",
        "paid_calls": "DISABLED",
        "github_writes": "DISABLED",
        "review_complete": False,
        "tests_run": False,
        "windows_qt_e2e": "REQUIRED",
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--prompt-sha256", required=True)
    p.add_argument("--diff", type=Path, required=True)
    a = p.parse_args(argv)
    try:
        # Patch data are never passed to the shell or executed.
        if (not a.diff.is_file() or a.diff.is_symlink()
                or a.diff.stat().st_size > MAX_DIFF_BYTES):
            raise JobBlocked("Patch file missing, linked or too large")
        report = inspect_patch(a.root, a.sha, a.prompt_sha256, a.diff.read_bytes())
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (JobBlocked, OSError, ValueError, FileNotFoundError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
