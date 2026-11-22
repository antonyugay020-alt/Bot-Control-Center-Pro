"""Fail-closed BCC cloud development preflight; no provider or trading I/O.

All commands are offline at the Python layer. `graph` executes the installed
Graphify CLI in code-only mode with LLM backends explicitly disabled. The
operator installs Graphify separately; no cloud instance or API is created here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

REPOSITORY = "antonyugay020-alt/Bot-Control-Center-Pro"
BRANCH = "agent/bcc-cloud-control-20261122"
TARGET_BRANCH = "codex/hub-demand-recovery-20261118"
SHA = re.compile(r"^[0-9a-f]{40}$")
TASK_NAME = "bcc-bybit-market-data-recovery"
APPROVED_PATHS = (
    "bcc/market.py", "bcc/market_hub.py", "bcc/ws_quotes.py", "bcc/scheduler.py",
    "bcc/runtime.py", "bcc/data_cache.py", "bcc/liquidation.py",
    "tests/test_hub_demand_recovery.py", "tests/test_scanner_retry_fairness.py",
    "tests/test_live_history_freshness.py", "tests/test_quote_history_isolation.py",
)
FORBIDDEN_ENV = (
    "OPENAI_API_KEY", "KIE_API_KEY", "KIE_AI_API_KEY", "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY",
    "AZURE_OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "OLLAMA_BASE_URL", "GRAPHIFY_BACKEND",
)


class Blocked(RuntimeError):
    pass


def git(root: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", str(root), *args], text=True,
                           capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked("Git is unavailable") from None
    if p.returncode:
        raise Blocked("Git inspection failed")
    return p.stdout.strip()


def validate_origin(origin: str) -> bool:
    return origin in (
        "https://github.com/" + REPOSITORY,
        "https://github.com/" + REPOSITORY + ".git",
        "git@github.com:" + REPOSITORY,
        "git@github.com:" + REPOSITORY + ".git",
        "ssh://git@github.com/" + REPOSITORY + ".git",
    )


def inspect(root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    top = Path(git(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top != root:
        raise Blocked("Worktree root mismatch")
    if not validate_origin(git(root, "remote", "get-url", "origin")):
        raise Blocked("Unexpected repository remote; refusing all operations")
    branch = git(root, "branch", "--show-current")
    if branch != BRANCH:
        raise Blocked("Wrong controller branch; expected isolated BCC cloud control")
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        raise Blocked("Tracked controller files are modified; inspect before running")
    commit = git(root, "rev-parse", "HEAD").lower()
    if not SHA.fullmatch(commit):
        raise Blocked("Invalid current commit")
    return {
        "status": "OFFLINE_CONTROL_READY", "repository": REPOSITORY,
        "controller_branch": branch, "controller_sha": commit,
        "target_branch": TARGET_BRANCH, "model_calls": "DISABLED",
        "paid_operations": "DISABLED", "github_writes": "DISABLED",
        "trading_operations": "DISABLED", "graphify_installed": bool(shutil.which("graphify")),
        "codespaces_runtime": bool(os.environ.get("CODESPACES")),
    }


def _valid_path(path: str) -> bool:
    if not isinstance(path, str) or path not in APPROVED_PATHS:
        return False
    obj = PurePosixPath(path)
    return not obj.is_absolute() and ".." not in obj.parts


def task_manifest(target_sha: str, paths: list[str] | None = None) -> dict[str, object]:
    if not isinstance(target_sha, str) or not SHA.fullmatch(target_sha):
        raise Blocked("Expected exact 40-character PR12 commit SHA")
    selected = list(APPROVED_PATHS if paths is None else paths)
    if not selected or len(selected) > len(APPROVED_PATHS) or len(set(selected)) != len(selected):
        raise Blocked("Invalid or duplicate task paths")
    if not all(_valid_path(path) for path in selected):
        raise Blocked("Path outside reviewed market-data scope")
    scope = json.dumps({"task": TASK_NAME, "target_sha": target_sha, "paths": selected},
                       sort_keys=True, separators=(",", ":"))
    return {
        "status": "OFFLINE_TASK_PROPOSAL", "task": TASK_NAME,
        "repository": REPOSITORY, "target_branch": TARGET_BRANCH,
        "target_sha": target_sha, "context_paths": selected,
        "scope_sha256": hashlib.sha256(scope.encode()).hexdigest(),
        "required_evidence": ["source-matched regression", "synthetic 824+ symbol recovery",
                              "real process_tick and risk admission", "Windows Qt E2E"],
        "acceptance": "BLOCKED_UNTIL_NATIVE_WINDOWS_QT_E2E",
        "openai_api_calls": "DISABLED", "kie_api_calls": "DISABLED",
        "cloud_paid_usage": "NOT_AUTHORIZED", "github_write": "DISABLED",
        "merge": "DISABLED", "live_trading": "DISABLED",
    }


def run_graph(root: Path, *, graphify: str | None = None) -> dict[str, str]:
    inspect(root)
    program = graphify or shutil.which("graphify")
    if not program:
        raise Blocked("Graphify not installed; no automatic network installation")
    env = {k: v for k, v in os.environ.items() if k not in FORBIDDEN_ENV}
    env["GRAPHIFY_BACKEND"] = "none"
    try:
        completed = subprocess.run(
            [program, "extract", ".", "--code-only", "--backend", "none"],
            cwd=root, env=env, check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked("Graphify extraction unavailable or timed out") from None
    if completed.returncode:
        raise Blocked("Graphify code-only extraction failed")
    return {"status": "CODE_ONLY_GRAPH_CREATED", "backend": "none",
            "api_calls_authorized": "false"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "plan", "graph"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--target-sha", default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            report = inspect(args.root)
        elif args.command == "plan":
            inspect(args.root)
            report = task_manifest(args.target_sha)
        else:
            report = run_graph(args.root)
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (Blocked, FileNotFoundError) as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
