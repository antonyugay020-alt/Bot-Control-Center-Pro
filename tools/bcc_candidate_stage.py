"""Stage a *previously proposed* Bybit patch in a new, local-only Git worktree.

A local branch and a temporary working tree are created only after a pinned
SHA/prompt/diff check. There is NO model invocation, test execution, commit,
push, GitHub API write, merge, trade, or production checkout modification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tools import bcc_patch_gate
from tools.bcc_openai_job import JobBlocked, check_snapshot

BRANCH_PREFIX = "agent/bcc-pr12-review-"
MAX_STDOUT = 32_000


def _git(root: Path, *args: str, input_data: bytes | None = None,
         allowed_codes: tuple[int, ...] = (0,)) -> tuple[int, str]:
    """Call only fixed Git commands, without shell, provider secrets or hooks."""
    if not root.is_dir():
        raise JobBlocked("Git workspace unavailable")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")
           and k not in {"OPENAI_API_KEY", "KIE_API_KEY", "KIE_AI_API_KEY",
                         "GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY",
                         "GEMINI_API_KEY", "GOOGLE_API_KEY"}}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_LFS_SKIP_SMUDGE": "1"})
    argv = ["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *args]
    try:
        completed = subprocess.run(argv, input=input_data, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=60, check=False,
                                   env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise JobBlocked("Local Git staging command unavailable") from None
    if completed.returncode not in allowed_codes:
        # No Git error output: a local Git config may inadvertently reveal secrets.
        raise JobBlocked("Local Git staging command was rejected")
    if len(completed.stdout) > MAX_STDOUT:
        raise JobBlocked("Unexpectedly large Git status output")
    try:
        out = completed.stdout.decode("utf-8", errors="strict")
    except UnicodeError:
        raise JobBlocked("Invalid Git status encoding") from None
    # Leading whitespace is significant in Git porcelain status (" M file").
    return completed.returncode, out.rstrip("\r\n")


def _line_counts(value: str) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for line in value.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise JobBlocked("Malformed staged change statistics")
        added, removed, path = fields
        if (path in result or path not in bcc_patch_gate.ALLOWED_FILES
                or not added.isascii() or not removed.isascii()
                or not added.isdecimal() or not removed.isdecimal()):
            raise JobBlocked("Unapproved staged change statistics")
        result[path] = {"added": int(added), "removed": int(removed)}
    return result


def _review_stage(root: Path, branch: str, sha: str,
                  expected: dict[str, dict[str, int]]) -> None:
    _, current = _git(root, "rev-parse", "HEAD")
    _, actual_branch = _git(root, "branch", "--show-current")
    if current != sha or actual_branch != branch:
        raise JobBlocked("Stage no longer references the pinned source")
    _, diffcheck = _git(root, "diff", "--check")
    if diffcheck:
        raise JobBlocked("Changed source has unexpected whitespace defects")
    _, stats = _git(root, "diff", "--numstat")
    if _line_counts(stats) != expected:
        raise JobBlocked("Stage diverges from the reviewed diff statistics")
    _, tracked = _git(root, "status", "--porcelain", "--untracked-files=all")
    found = set()
    for line in tracked.splitlines():
        if (len(line) < 4 or line[:2] != " M" or line[2] != " "
                or line[3:] not in expected or line[3:] in found):
            raise JobBlocked("Unexpected file or Git index change in stage")
        found.add(line[3:])
    if found != set(expected):
        raise JobBlocked("Staged file list does not match approved candidate")


def stage_proposal(source_root: Path, source_sha: str, prompt_sha256: str,
                   patch_bytes: bytes) -> dict[str, object]:
    """Apply candidate to a NEW LOCAL branch/worktree. No test or model execution.

    No attempt to claim semantic safety: a change to allowed code can still be
    malicious or incorrect and must undergo independent review before tests.
    """
    source_root = check_snapshot(source_root, source_sha)
    report = bcc_patch_gate.inspect_patch(source_root, source_sha,
                                          prompt_sha256, patch_bytes)
    if report.get("status") != "PATCH_PROPOSAL_VALIDATED":
        raise JobBlocked("Unvalidated patch proposal")
    digest = hashlib.sha256(patch_bytes).hexdigest()
    if report.get("diff_sha256") != digest:
        raise JobBlocked("Patch digest changed since review")
    expected = report["files"]
    if (not isinstance(expected, dict) or not expected
            or not set(expected).issubset(bcc_patch_gate.ALLOWED_FILES)):
        raise JobBlocked("Reviewed patch file list invalid")

    # A configured Git clean/smudge/process filter may invoke arbitrary code
    # during checkout, even when hooks are disabled. Reject it fail-closed.
    _, filters = _git(source_root, "config", "--local", "--name-only",
                      "--get-regexp", r"^filter\.", allowed_codes=(0, 1))
    if filters:
        raise JobBlocked("Configured Git checkout filters require human review")
    branch = BRANCH_PREFIX + digest[:16]
    code, _ = _git(source_root, "show-ref", "--verify", "--quiet",
                   "refs/heads/" + branch, allowed_codes=(0, 1))
    if code == 0:
        raise JobBlocked("Candidate branch already exists; no overwrite")

    temp_base = Path(tempfile.gettempdir()).resolve(strict=True)
    workspace = Path(tempfile.mkdtemp(prefix="bcc-pr12-review-", dir=temp_base))
    created = False
    try:
        _git(source_root, "worktree", "add", "--no-track", "-b", branch,
             str(workspace), source_sha)
        created = True
        # Recheck in newly checked-out candidate; do not trust just the
        # earlier inspection of the detached PR12 source.
        _git(workspace, "apply", "--check", "-", input_data=patch_bytes)
        _git(workspace, "apply", "-", input_data=patch_bytes)
        _review_stage(workspace, branch, source_sha, expected)
        check_snapshot(source_root, source_sha)
        return {
            "status": "LOCAL_CANDIDATE_STAGED_FOR_REVIEW",
            "candidate_branch": branch, "candidate_worktree": str(workspace),
            "source_sha": source_sha, "prompt_sha256": prompt_sha256,
            "diff_sha256": digest, "files": sorted(expected),
            "patch_applied_to_isolated_worktree": True,
            "source_worktree_unchanged": True,
            "git_commit_created": False, "github_writes": "DISABLED",
            "model_calls": "DISABLED", "paid_calls": "DISABLED",
            "tests_run": False, "executable_code_run": False,
            "approval_to_merge": False, "windows_qt_e2e": "REQUIRED",
        }
    except BaseException:
        # Cleanup only the new, uniquely allocated stage. Never clean the
        # user's original PR12 worktree or any other named branch.
        if created:
            try:
                _git(source_root, "worktree", "remove", "--force", str(workspace))
            except JobBlocked:
                pass
            try:
                _git(source_root, "branch", "-D", branch)
            except JobBlocked:
                pass
        if workspace.exists() and not created:
            shutil.rmtree(workspace)
        raise


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--prompt-sha256", required=True)
    p.add_argument("--diff", type=Path, required=True)
    args = p.parse_args(argv)
    try:
        if (args.diff.is_symlink() or not args.diff.is_file()
                or args.diff.stat().st_size > bcc_patch_gate.MAX_DIFF_BYTES):
            raise JobBlocked("Candidate patch unavailable or oversized")
        output = stage_proposal(args.root, args.sha, args.prompt_sha256,
                                args.diff.read_bytes())
        print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (JobBlocked, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
