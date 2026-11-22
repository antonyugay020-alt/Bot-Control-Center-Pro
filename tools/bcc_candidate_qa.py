"""Execute ONLY full source regression of a pinned local patch in an offline Docker sandbox.

No fallback to host Python, no network, no model, no credentials, no GitHub
writes, no trading operations. Docker itself must be operator-installed;
the image must already exist locally and be selected by exact image ID.
The runner never builds/pulls images and cannot attest Docker host security.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import subprocess
import sys
import tempfile
import time

from tools.bcc_candidate_stage import BRANCH_PREFIX, _git, _review_stage
from tools.bcc_openai_job import JobBlocked, check_snapshot
from tools.bcc_patch_gate import (
    MAX_DIFF_BYTES, PINNED_PROMPT_SHA256, PINNED_SHA, inspect_patch,
)

IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
MAX_LOG_BYTES = 2_000_000
TIME_LIMIT_S = 900
MAX_DOCKER_INSPECT_BYTES = 256


def _host_env() -> dict[str, str]:
    """Docker client needs PATH and socket, not developer credentials."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp/bcc-qa-docker-no-config",
            "DOCKER_CONFIG": "/tmp/bcc-qa-docker-no-config",
            "DOCKER_BUILDKIT": "0"}


def _git_common(root: Path) -> Path:
    _, value = _git(root, "rev-parse", "--git-common-dir")
    if not value:
        raise JobBlocked("Git repository common directory unavailable")
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve(strict=True)


def review_candidate(source: Path, candidate: Path, patch: bytes,
                     sha: str, prompt_hash: str) -> dict[str, object]:
    source = check_snapshot(source, sha)
    report = inspect_patch(source, sha, prompt_hash, patch)
    digest = hashlib.sha256(patch).hexdigest()
    if report["diff_sha256"] != digest:
        raise JobBlocked("Patch digest mismatch")
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if candidate.is_symlink():
        raise JobBlocked("Linked candidate path forbidden")
    candidate = candidate.resolve(strict=True)
    if (candidate == source or candidate.parent != temp_root
            or not candidate.name.startswith("bcc-pr12-review-")):
        raise JobBlocked("Candidate must be a separate temporary worktree")
    if not candidate.is_dir() or _git_common(candidate) != _git_common(source):
        raise JobBlocked("Candidate Git repository differs from source")
    branch = BRANCH_PREFIX + digest[:16]
    _review_stage(candidate, branch, sha, report["files"])
    check_snapshot(source, sha)
    return {
        "status": "CANDIDATE_REVIEWED_FOR_OFFLINE_QA",
        "candidate": str(candidate),
        "source_sha": sha,
        "prompt_sha256": prompt_hash,
        "diff_sha256": digest,
        "files": sorted(report["files"]),
        "candidate_branch": branch,
        "candidate_executed": False,
    }


def docker_args(candidate: Path, image_id: str, name: str, *,
                uid: int | None = None, gid: int | None = None) -> list[str]:
    if not isinstance(image_id, str) or IMAGE_ID.fullmatch(image_id) is None:
        raise JobBlocked("An exact local image ID sha256:<64 hex> is required")
    if not re.fullmatch(r"bcc-qa-[a-f0-9]{16}", name):
        raise JobBlocked("Invalid sandbox container name")
    if uid is None:
        uid = os.getuid()
    if gid is None:
        gid = os.getgid()
    if not all(type(x) is int and 0 < x < 65536 for x in (uid, gid)):
        raise JobBlocked("Sandbox must run as an unprivileged Codespaces user")
    candidate = candidate.resolve(strict=True)
    if not candidate.is_dir() or "," in str(candidate) or "\n" in str(candidate):
        raise JobBlocked("Invalid candidate bind mount")
    return [
        "docker", "run", "--rm", "--name", name, "--pull=never",
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=256",
        "--memory=3g", "--cpus=2", "--ipc=private",
        "--user", f"{uid}:{gid}",
        "--mount", f"type=bind,source={candidate},target=/src,readonly",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
        "--env", "HOME=/tmp",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "QT_QPA_PLATFORM=offscreen",
        "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "--env", "PYTHONPATH=/src:/src/tests",
        "--workdir", "/src",
        "--entrypoint", "python",
        image_id, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests",
    ]


def _existing_image(image_id: str) -> None:
    if not IMAGE_ID.fullmatch(image_id):
        raise JobBlocked("Image must be pinned to a locally verified image ID")
    if not shutil.which("docker"):
        raise JobBlocked("Docker is unavailable; no host-Python fallback")
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image_id],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_host_env(), timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise JobBlocked("Docker image inspection failed") from None
    if (result.returncode or len(result.stdout) > MAX_DOCKER_INSPECT_BYTES
            or result.stdout.decode("ascii", errors="replace").strip() != image_id):
        raise JobBlocked("Exact pinned image ID not present locally")


def _cleanup_container(name: str) -> None:
    """Only remove the random container name allocated by this invocation."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", name], env=_host_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _execute(argv: list[str], name: str) -> tuple[int, str]:
    """Bound logs and time; remove only own sandbox on abnormal termination."""
    fd, path = tempfile.mkstemp(prefix="bcc-qa-result-", suffix=".log")
    total = 0
    proc: subprocess.Popen[bytes] | None = None
    try:
        with os.fdopen(fd, "wb") as log:
            try:
                proc = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=_host_env(), close_fds=True)
            except OSError:
                raise JobBlocked("Could not start isolated Docker QA") from None
            assert proc.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                started = time.monotonic()
                done = False
                while not done:
                    if time.monotonic() - started > TIME_LIMIT_S:
                        raise JobBlocked("Isolated QA exceeded 15-minute limit")
                    for key, _ in selector.select(timeout=1):
                        block = os.read(key.fd, 65536)
                        if not block:
                            done = True
                            break
                        total += len(block)
                        if total > MAX_LOG_BYTES:
                            raise JobBlocked("QA output exceeded safe log limit")
                        log.write(block)
                try:
                    code = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    raise JobBlocked("Docker QA process did not exit") from None
        return code, path
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        _cleanup_container(name)
        raise


def plan(source: Path, candidate: Path, patch: bytes,
         source_sha: str, prompt_hash: str, image_id: str) -> dict[str, object]:
    report = review_candidate(source, candidate, patch, source_sha, prompt_hash)
    # Construct args, but do not invoke docker or pull images.
    name = "bcc-qa-" + secrets.token_hex(8)
    args = docker_args(Path(report["candidate"]), image_id, name)
    return {
        **report, "status": "OFFLINE_DOCKER_QA_PLAN",
        "image_id": image_id, "sandbox_network": "NONE",
        "source_mount": "READ_ONLY", "filesystem": "READ_ONLY_WITH_TMPFS",
        "sandbox_user": "UNPRIVILEGED", "cpu_limit": "2",
        "memory_limit": "3g", "runtime_limit_seconds": TIME_LIMIT_S,
        "test_selector": args[-5:], "model_calls": "DISABLED",
        "paid_calls": "DISABLED", "github_writes": "DISABLED",
        "qa_executed": False, "docker_image_available": "NOT_CHECKED",
        "windows_qt_e2e": "STILL_REQUIRED",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--diff", type=Path, required=True)
    parser.add_argument("--sha", default=PINNED_SHA)
    parser.add_argument("--prompt-sha256", default=PINNED_PROMPT_SHA256)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--execute-offline", action="store_true",
                        help="Explicitly run no-network Docker QA of reviewed local candidate")
    a = parser.parse_args(argv)
    try:
        if (a.diff.is_symlink() or not a.diff.is_file()
                or a.diff.stat().st_size > MAX_DIFF_BYTES):
            raise JobBlocked("Patch file missing or oversized")
        patch = a.diff.read_bytes()
        report = plan(a.root, a.candidate, patch,
                      a.sha, a.prompt_sha256, a.image_id)
        if a.execute_offline:
            # The operator must independently inspect/approve the diff before
            # this option; the CLI never grants AI/financial/write authority.
            _existing_image(a.image_id)
            name = "bcc-qa-" + secrets.token_hex(8)
            command = docker_args(Path(report["candidate"]), a.image_id, name)
            exit_code, log = _execute(command, name)
            report.update({
                "status": "OFFLINE_DOCKER_QA_EXITED",
                "qa_executed": True,
                "qa_exit_code": exit_code,
                "test_log": log,
                "passed": exit_code == 0,
                "windows_qt_e2e": "STILL_REQUIRED",
            })
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if not a.execute_offline or report["qa_exit_code"] == 0 else 1
    except (JobBlocked, OSError, ValueError, FileNotFoundError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
