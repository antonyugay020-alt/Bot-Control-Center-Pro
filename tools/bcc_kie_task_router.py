"""Offline task handoff for BCC development agents; no provider access.

Explicit one-model, one-stage selection. Does not read, transmit or write
repository files. A task packet is only a non-sensitive manifest to be
reviewed by GPT Work; it is NOT a token granting API use or GitHub writes.

The purpose is to keep Terra implementation, Kimi independent review, and
exceptional Astra architectural review separate and source-SHA bound.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any

from tools.bcc_kie_models import ModelNotReady, plan, profile

REPO = "antonyugay020-alt/Bot-Control-Center-Pro"
VALID_SHA = re.compile(r"[0-9a-f]{40}\Z")
TASK_TAG = re.compile(r"[a-z][a-z0-9_-]{2,63}\Z")
STAGES = {
    "implementation": frozenset(("terra", "kimi")),
    "independent_review": frozenset(("terra", "kimi")),
    "architecture": frozenset(("terra", "kimi", "astra")),
}
SOURCE_PREFIXES = ("bcc/", "tests/", "tools/", "docs/")
ENTRYPOINTS = frozenset(("app.py", "launch.pyw"))
EXTENSIONS = frozenset((".py", ".pyw", ".md", ".json", ".bat", ".cmd", ".ps1"))
BANNED_PARTS = frozenset((
    ".git", ".github", ".env", "my_bots", "reports", "verification_reports",
    ".check_venv", ".venv", "venv", "__pycache__", "check_workspaces",
    "secrets", "credentials", "private", "token", "keys",
))
MAX_FILES = 32
MAX_TASK_BYTES = 24_000


class RouteBlocked(ValueError):
    """No user secret, raw task or repository data is included in errors."""


@dataclass(frozen=True)
class Handoff:
    task_tag: str
    model: str
    stage: str
    repo: str
    source_sha: str
    context_paths: tuple[str, ...]
    context_hash_sha256: str
    prompt_sha256: str
    prompt_bytes: int
    output_tokens_requested: int
    historical_price_micro_usd: int | None
    quote_conflict: bool
    provider_contract_verified: bool
    authorization: str = "NONE"
    paid_calls: str = "DISABLED"
    work_graphify_connected: bool = False

    def safe_dict(self) -> dict[str, Any]:
        """Never include the raw prompt, credentials, file bytes or content."""
        return {
            "task_tag": self.task_tag, "model": self.model,
            "stage": self.stage, "repo": self.repo,
            "source_sha": self.source_sha,
            "context_paths": list(self.context_paths),
            "context_hash_sha256": self.context_hash_sha256,
            "prompt_sha256": self.prompt_sha256,
            "prompt_bytes": self.prompt_bytes,
            "output_tokens_requested": self.output_tokens_requested,
            "historical_price_micro_usd": self.historical_price_micro_usd,
            "quote_conflict": self.quote_conflict,
            "provider_contract_verified": False,
            "authorization": self.authorization, "paid_calls": self.paid_calls,
            "work_graphify_connected": False,
        }


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 240 or "\\" in value:
        raise RouteBlocked("Invalid source path")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise RouteBlocked("Invalid source path")
    p = PurePosixPath(value)
    if (p.is_absolute() or value != p.as_posix()
            or any(part in ("", ".", "..") for part in value.split("/"))
            or not (value.startswith(SOURCE_PREFIXES) or value in ENTRYPOINTS)
            or p.suffix.lower() not in EXTENSIONS):
        raise RouteBlocked("Invalid source path")
    if any(part.lower() in BANNED_PARTS for part in p.parts):
        raise RouteBlocked("Sensitive or runtime file not allowed")
    if p.name.lower().startswith(".env") or p.suffix.lower() in (
            ".pem", ".key", ".pfx", ".p12", ".db", ".sqlite", ".sqlite3"):
        raise RouteBlocked("Sensitive or runtime file not allowed")
    return value


def prepare_handoff(*, task_tag: str, task_text: str, model: str,
                    stage: str, source_sha: str,
                    context_paths: tuple[str, ...] | list[str],
                    max_output_tokens: int = 2048) -> Handoff:
    if not isinstance(task_tag, str) or TASK_TAG.fullmatch(task_tag) is None:
        raise RouteBlocked("Task identifier is invalid")
    if not isinstance(source_sha, str) or VALID_SHA.fullmatch(source_sha) is None:
        raise RouteBlocked("Pinned BCC source commit SHA required")
    if stage not in STAGES or model not in STAGES[stage]:
        raise RouteBlocked("Model/stage combination is not supported")
    if (not isinstance(task_text, str) or not task_text.strip()
            or len(task_text.encode("utf-8")) > MAX_TASK_BYTES
            or "\x00" in task_text):
        raise RouteBlocked("Task text invalid or too long")
    if (not isinstance(context_paths, (tuple, list))
            or len(context_paths) > MAX_FILES or not context_paths):
        raise RouteBlocked("Explicit nonempty source context path list is required")
    paths = tuple(_path(value) for value in context_paths)
    if len(set(paths)) != len(paths):
        raise RouteBlocked("Duplicate source path")
    # For independence, review receives a pinned source SHA and explicit
    # filenames; no inference that a changed GitHub branch was reviewed.
    context_digest = hashlib.sha256(
        json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    target = profile(model)
    historical_cost: int | None = None
    try:
        quote = plan(task_text, model=model, max_output_tokens=max_output_tokens)
        historical_cost = quote.historical_quote_micro_usd
    except ModelNotReady:
        # An Astra task can be drafted offline despite unknown invoice pricing,
        # but cannot become an authorized provider request.
        if model != "astra":
            raise RouteBlocked("Cannot estimate quoted model") from None
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192:
            raise RouteBlocked("Invalid output cap")
    return Handoff(
        task_tag=task_tag, model=model, stage=stage, repo=REPO,
        source_sha=source_sha, context_paths=paths,
        context_hash_sha256=context_digest,
        prompt_sha256=hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
        prompt_bytes=len(task_text.encode("utf-8")),
        output_tokens_requested=max_output_tokens,
        historical_price_micro_usd=historical_cost,
        quote_conflict=target.price_quote_conflict,
        provider_contract_verified=False,
    )


def pair_for_independent_review(implementation: Handoff, review: Handoff) -> dict[str, Any]:
    """Review must use a different model and the exact same pinned source.

    No automated review result is claimed here. Reviewer inputs/changes still
    require explicit Work approval before being submitted to a provider.
    """
    if not isinstance(implementation, Handoff) or not isinstance(review, Handoff):
        raise RouteBlocked("Invalid task handoff")
    if implementation.stage != "implementation" or review.stage != "independent_review":
        raise RouteBlocked("Wrong task stages for an independent review")
    if implementation.model == review.model:
        raise RouteBlocked("Reviewer must be a different model")
    if implementation.source_sha != review.source_sha or implementation.repo != review.repo:
        raise RouteBlocked("Review must be bound to the same source snapshot")
    if not set(review.context_paths).issubset(set(implementation.context_paths)):
        raise RouteBlocked("Reviewer requests context outside approved implementation scope")
    return {
        "implementation": implementation.task_tag,
        "review": review.task_tag,
        "snapshot": implementation.source_sha,
        "independent_models": True,
        "ready_for_external_api": False,
        "no_paid_calls_authorized": True,
    }


def dispatch_paid(*_args: Any, **_kwargs: Any) -> None:
    raise RouteBlocked("Kie API dispatch is disabled; Work handoffs are only offline manifests")
