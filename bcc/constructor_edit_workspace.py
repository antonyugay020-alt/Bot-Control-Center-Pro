"""Version-bound, non-trading edit workspaces for existing bots.

An edit draft is never an executable configuration. Its path is bound to both
an existing MY_BOTS owner and the exact source config.json revision. A newer
config revision has a different draft identity, so stale unsaved form data is
never silently presented as the current bot's settings. Publication still goes
through Supervisor.save_bot with the original repository digest and locks.
"""

from hashlib import sha256
from pathlib import Path
import re


def edit_workspace_path(repository_root, bot_folder, config_digest):
    """Return a private draft path without reading/writing live bot state."""
    root = Path(repository_root).resolve(strict=True)
    owner = Path(bot_folder).resolve(strict=True)
    bots = root / "MY_BOTS"
    if not owner.is_relative_to(bots) or owner == bots:
        raise ValueError("Черновик редактирования доступен только для MY_BOTS")
    if not isinstance(config_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", config_digest):
        raise ValueError("Неверная версия конфигурации для черновика")
    identity = sha256((str(owner) + "\0" + config_digest).encode("utf-8")).hexdigest()
    return root / f"draft_edit_{identity}.json"


def edit_workspace_binding(repository_root, bot_folder, config_digest):
    """Bind a draft to the canonical owner and source config revision.

    The binding is stored outside the bot config so it cannot become an
    executable setting or be included in the repository publication.
    """
    path = edit_workspace_path(repository_root, bot_folder, config_digest)
    root = Path(repository_root).resolve(strict=True)
    owner = Path(bot_folder).resolve(strict=True)
    assert path.parent == root
    return {"owner": owner.relative_to(root).as_posix(),
            "source_digest": config_digest}


def rebase_dormant_workspace(workspace, published):
    """Carry *only* inactive editor policies across a successful publication.

    A bot's executable config never contains deselected coins, sections or
    markets. When an existing bot is saved, its digest changes and a draft
    bound to the old digest is no longer offered by the Qt editor. Rebuild the
    next draft from the exact published config and copy only the dormant
    policies; never revive an older active policy or mutate the repository.

    Callers must persist the returned draft with a new digest-bound identity
    and compare-and-swap revision. Failure to persist it must not be reported
    as failure of an already committed bot configuration.
    """
    from copy import deepcopy
    from .constructor_transaction import validate_constructor_publish

    executable = validate_constructor_publish(workspace)
    if executable != published:
        raise ValueError("Черновик не соответствует сохранённой конфигурации бота")
    rebased = deepcopy(published)
    selected_symbols = set(published['symbols'])
    section_ids = {section['id'] for section in published['sections']}
    for symbol, patch in workspace['coin_overrides'].items():
        if symbol not in selected_symbols:
            rebased['coin_overrides'][symbol] = deepcopy(patch)
            continue
        inactive_sections = {
            sid: deepcopy(value)
            for sid, value in patch.get('sections', {}).items()
            if sid not in section_ids
        }
        if inactive_sections:
            rebased['coin_overrides'].setdefault(symbol, {}).setdefault(
                'sections', {}).update(inactive_sections)
    if published['strategy'] == 'POLYMARKET':
        active = {f'{symbol}:{duration}' for symbol in selected_symbols
                  for duration in published['polymarket']['durations']}
        for key, patch in workspace['polymarket']['overrides'].items():
            if key not in active:
                rebased['polymarket']['overrides'][key] = deepcopy(patch)
    if validate_constructor_publish(rebased) != published:
        raise ValueError("Перенос черновика изменяет торговые настройки")
    return rebased


def retain_workspace_after_commit(repository, folder, workspace, expected_published,
                                  *, previous_path=None, previous_revision=None):
    """Preserve inactive edits after either Qt Create or Edit commits a bot.

    This deliberately runs *after* the Supervisor/repository commit. It only
    creates a revision-checked, version-bound draft; it never writes the bot's
    config, state, or execution files. A stale or corrupt destination cannot
    be overwritten, and an old draft is removed only after a successful move.
    """
    from .constructor_session import (
        remove_constructor_draft_if_revision, save_constructor_draft_with_revision,
    )
    saved, digest = repository.load(folder)
    if saved != expected_published:
        raise ValueError("Конфигурация бота изменилась после сохранения")
    carried = rebase_dormant_workspace(workspace, saved)
    old = Path(previous_path) if previous_path is not None else None
    if old is not None:
        # Only drafts managed by this repository may be retired after a bot
        # commit. An imported/opened JSON file belongs to the user and must
        # never be removed by the constructor's post-commit cleanup.
        from .config import STRATEGIES
        resolved_root = Path(repository.root).resolve(strict=True)
        if (old.resolve(strict=False).parent != resolved_root
                or not re.fullmatch(r"draft_(?:" + "|".join(STRATEGIES)
                                    + r")(?:_[0-9a-f]{32})?\.json|draft_edit_[0-9a-f]{64}\.json",
                                    old.name)):
            raise ValueError("Удаление внешнего черновика запрещено")
    target = None
    if carried != saved:
        target = edit_workspace_path(repository.root, folder, digest)
        binding = edit_workspace_binding(repository.root, folder, digest)
        revision = previous_revision if old == target else None
        save_constructor_draft_with_revision(
            target, carried, expected_revision=revision,
            binding=binding, allow_legacy=True)
    old_removed = None
    if old is not None and (target is None or old != target):
        old_removed = remove_constructor_draft_if_revision(old, previous_revision)
    return {'dormant_preserved': target is not None,
            'draft_path': target, 'old_draft_removed': old_removed}


def preserve_published_constructor_workspace(repository, folder, workspace, published,
                                             *, owned_draft_path=None,
                                             owned_draft_revision=None):
    """Shared post-commit path for Create, Edit and Open Draft in Modern Qt.

    The live configuration has already been committed by the Supervisor.
    Only VELES/Polymarket have dormant policies; other strategies must not
    enter their constructor-specific validator. A user-selected JSON draft
    is external: callers pass no ``owned_draft_path`` so it remains intact.
    """
    if not isinstance(published, dict) or published.get('strategy') not in ('VELES', 'POLYMARKET'):
        return {'dormant_preserved': False, 'draft_path': None,
                'old_draft_removed': None, 'not_applicable': True}
    result = retain_workspace_after_commit(
        repository, folder, workspace, published,
        previous_path=owned_draft_path,
        previous_revision=owned_draft_revision)
    result['not_applicable'] = False
    return result
