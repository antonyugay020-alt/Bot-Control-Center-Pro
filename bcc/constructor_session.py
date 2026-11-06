"""Isolated, revision-checked constructor drafts for VELES and Polymarket.

A draft is never a live bot configuration.  All writes and deletes use one
cross-process lock and an optimistic revision, so two Qt windows cannot
silently overwrite (or delete) one another's unfinished trading settings.
"""

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import os
import errno
import time
from pathlib import Path

from .config import (STRATEGIES, normalize_config, reject_private_data,
                     valid_symbol, poly_rule_keys)
from .storage import atomic_json

_ANY_REVISION = object()
_EDIT_ENVELOPE_FORMAT = "bcc-constructor-edit-workspace-v1"


def _bound_document(config, binding):
    """Encode edit provenance outside the executable bot configuration."""
    if binding is None:
        return config
    _check_binding(binding)
    return {"format": _EDIT_ENVELOPE_FORMAT, "binding": deepcopy(binding),
            "config": config}


def _check_binding(binding):
    owner = binding.get("owner") if isinstance(binding, dict) else None
    components = owner.split("/") if isinstance(owner, str) else []
    if (not isinstance(binding, dict)
            or set(binding) != {"owner", "source_digest"}
            or not isinstance(binding["owner"], str)
            or len(components) < 3 or components[0] != "MY_BOTS"
            or any(part in ("", ".", "..") or "\\" in part for part in components)
            or not isinstance(binding["source_digest"], str)
            or len(binding["source_digest"]) != 64
            or any(ch not in "0123456789abcdef" for ch in binding["source_digest"])):
        raise ValueError("Неверная привязка черновика к исходному боту")


def _unbound_document(value, binding, *, allow_legacy=False):
    """Reject a different bot/version before any draft is presented to Qt."""
    if binding is None:
        if isinstance(value, dict) and value.get("format") == _EDIT_ENVELOPE_FORMAT:
            raise ValueError("Черновик редактирования требует проверки владельца и версии")
        return value
    _check_binding(binding)
    if isinstance(value, dict) and value.get("format") == _EDIT_ENVELOPE_FORMAT:
        if (set(value) != {"format", "binding", "config"}
                or value["binding"] != binding):
            raise ValueError("Черновик принадлежит другому боту или версии конфигурации")
        return value["config"]
    if allow_legacy:
        # Previous checkpoints wrote raw configs to the already owner-bound
        # path. The Qt UI requires an explicit Resume decision before this
        # one-time migration to the provenance-bearing envelope.
        return value
    raise ValueError("Черновик не содержит проверяемую привязку к боту")


def _draft_document(config, *, expected_strategy=None):
    if not isinstance(config, dict):
        raise ValueError("Черновик конструктора должен быть объектом")
    strategy = config.get("strategy")
    if strategy not in STRATEGIES:
        raise ValueError("Черновик содержит неизвестную стратегию")
    if expected_strategy is not None and strategy != expected_strategy:
        raise ValueError("Черновик принадлежит другой стратегии; исходный файл сохранён")
    reject_private_data(config)
    candidate = normalize_config(config)
    required = {
        "name": str, "symbols": list, "settings": dict,
        "sections": list, "coin_overrides": dict,
        "capital": (int, float), "reinvest": bool,
        "stop_after_deals": bool,
    }
    for field, kind in required.items():
        if not isinstance(candidate.get(field), kind):
            raise ValueError(f"Повреждено поле черновика: {field}")
    if not all(isinstance(symbol, str) for symbol in candidate["symbols"]):
        raise ValueError("Повреждён список монет черновика")
    if not all(isinstance(section, dict) for section in candidate["sections"]):
        raise ValueError("Повреждены секции черновика")
    # A draft may be incomplete (blank name, invalid leverage, no selected
    # markets), but its nested *structure* must be editable on reopening.
    # Dormant coin/market rules are deliberately not restricted to selected
    # symbols or durations: the workspace must retain them for later reuse.
    for symbol, patch in candidate["coin_overrides"].items():
        if (not valid_symbol(symbol, strategy) or not isinstance(patch, dict)
                or set(patch) - {"settings", "sections", "entry", "exit", "parameters"}):
            raise ValueError("Повреждены индивидуальные настройки монет")
        for field in ("settings", "sections", "parameters"):
            if field in patch and not isinstance(patch[field], dict):
                raise ValueError(f"Повреждены индивидуальные настройки: {symbol}/{field}")
        for section_id, section_patch in patch.get("sections", {}).items():
            if not isinstance(section_id, str) or not isinstance(section_patch, dict):
                raise ValueError(f"Повреждены настройки секции монеты: {symbol}")
    if any(not isinstance(section.get("id"), str) for section in candidate["sections"]):
        raise ValueError("Повреждены идентификаторы секций черновика")
    if strategy == "POLYMARKET":
        poly = candidate.get("polymarket")
        if (not isinstance(poly, dict)
                or not isinstance(poly.get("durations"), list)
                or not isinstance(poly.get("overrides"), dict)):
            raise ValueError("Повреждены рынки черновика Polymarket")
        if any(type(duration) is not int for duration in poly["durations"]):
            raise ValueError("Повреждены длительности рынков Polymarket")
        allowed = poly_rule_keys()
        for key, patch in poly["overrides"].items():
            if not isinstance(key, str) or ":" not in key:
                raise ValueError("Повреждён идентификатор рынка Polymarket")
            symbol, duration = key.rsplit(":", 1)
            if (not valid_symbol(symbol, strategy)
                    or duration not in ("300", "900", "3600")
                    or key != f"{symbol}:{duration}"
                    or not isinstance(patch, dict) or set(patch) - allowed):
                raise ValueError("Повреждены индивидуальные настройки рынков Polymarket")
    try:
        json.dumps(candidate, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Черновик нельзя сохранить без потерь") from exc
    return candidate


def validate_constructor_workspace(config, *, expected_strategy=None):
    """Structural contract shared by draft persistence and Qt publication.

    Inactive policies need not be executable yet, but their keys and nested
    shapes must remain valid so a successful bot save cannot silently lose a
    malformed dormant rule when the editor's version-bound draft is rebased.
    """
    return _draft_document(config, expected_strategy=expected_strategy)


def _revision(path):
    """Hash raw bytes, not normalized JSON: detect even external edits."""
    try:
        return sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def draft_revision(path):
    return _revision(path)


def load_constructor_draft_snapshot(path, *, expected_strategy=None,
                                    binding=None, allow_legacy=False):
    """Read config and revision from *the same* atomic file snapshot."""
    data = Path(path).read_bytes()
    value = json.loads(data.decode("utf-8-sig"),
                       parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    return (_draft_document(_unbound_document(value, binding, allow_legacy=allow_legacy),
                            expected_strategy=expected_strategy), sha256(data).hexdigest())


def load_constructor_draft(path, *, expected_strategy=None, binding=None,
                           allow_legacy=False):
    return load_constructor_draft_snapshot(
        path, expected_strategy=expected_strategy, binding=binding,
        allow_legacy=allow_legacy)[0]


@contextmanager
def _draft_lock(path, *, timeout_seconds=3.0):
    """Bounded cross-process lock shared by save and delete transactions.

    The lock file is stable across atomic JSON replacement. Contention is
    expected when two Modern Qt windows save at once, but waiting forever
    freezes the UI and can stall the entire acceptance run. A timeout leaves
    the original draft untouched and allows a later retry. Both Windows and
    POSIX use non-blocking OS locks with the same deadline semantics.
    """
    if type(timeout_seconds) not in (int, float) or not 0 <= timeout_seconds <= 60:
        raise ValueError("Неверный срок ожидания блокировки черновика")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        if os.name == "nt":
            import msvcrt

            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Черновик занят другим окном; изменения не записаны, "
                        "повторите сохранение позже") from exc
                time.sleep(min(0.025, remaining))
        try:
            yield
        finally:
            release()


def save_constructor_draft_with_revision(path, config, *, expected_revision=_ANY_REVISION,
                                         binding=None, allow_legacy=False):
    """Compare-and-swap a draft, then verify it before returning its revision.

    ``expected_revision=None`` means the file must *not* exist. Callers with
    a loaded snapshot pass its hash; legacy explicit saves can omit it.
    """
    candidate = _draft_document(config)
    document = _bound_document(candidate, binding)
    path = Path(path)
    with _draft_lock(path):
        actual_revision = _revision(path)
        if (expected_revision is not _ANY_REVISION
                and actual_revision != expected_revision):
            raise ValueError("Черновик изменён в другом окне; сохранение отменено, исходный файл сохранён")
        if actual_revision is not None:
            # Even the legacy unconditional-save API must never replace a
            # corrupt file or a draft belonging to another strategy. A new
            # bot is created in a distinct workspace instead of clobbering it.
            load_constructor_draft_snapshot(
                path, expected_strategy=candidate["strategy"],
                binding=binding, allow_legacy=allow_legacy)
        atomic_json(path, document)
        restored, revision = load_constructor_draft_snapshot(
            path, expected_strategy=candidate["strategy"], binding=binding)
        if restored != candidate:
            raise OSError("Черновик изменился после записи; проверьте файл")
        return deepcopy(restored), revision


def save_constructor_draft(path, config, *, expected_revision=_ANY_REVISION,
                           binding=None, allow_legacy=False):
    """Backward-compatible single-value API, optionally revision-checked."""
    saved, _ = save_constructor_draft_with_revision(
        path, config, expected_revision=expected_revision,
        binding=binding, allow_legacy=allow_legacy)
    return saved


def remove_constructor_draft_if_revision(path, expected_revision):
    """Remove only this editor's draft, never another window's newer work."""
    path = Path(path)
    with _draft_lock(path):
        if _revision(path) != expected_revision:
            return False
        path.unlink(missing_ok=True)
        return True
