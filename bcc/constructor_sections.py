"""Atomic, non-trading section projection shared by VELES Qt editing and validation.

A section edit must be checked against *every* selected coin's inherited risk,
entry, and exit rules before Qt accepts it.  This projection never writes a
config, mutates a draft, starts a worker, or places an order.  The repository
still owns the final optimistic commit and running-bot safety checks.
"""

from copy import deepcopy
import re

from .config import (coin_section, effective, reject_private_data,
                     validate_group, validate_position_settings)


_SECTION_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


def project_constructor_sections_transaction(config, *, sections):
    """Return a fully checked copy with the proposed complete section list.

    Validate the entire candidate set, not only the clicked section: a rename
    can affect a parent group, a side change affects risk for every coin, and
    an existing coin override can invalidate an otherwise valid range.
    Unselected coin overrides remain in the editor workspace; publication
    continues to prune them via ``selected_configuration``.
    """
    if not isinstance(config, dict) or config.get('strategy') not in ('VELES', 'POLYMARKET'):
        raise ValueError('Секции доступны только конструкторам VELES и Polymarket')
    if not isinstance(sections, list) or not 1 <= len(sections) <= 100:
        raise ValueError('Требуется от 1 до 100 диапазонов/секций')
    symbols = config.get('symbols')
    if (not isinstance(symbols, list) or not symbols
            or any(not isinstance(s, str) for s in symbols)
            or len(symbols) != len(set(symbols))):
        raise ValueError('Выберите уникальные монеты перед проверкой секций')
    if not isinstance(config.get('coin_overrides'), dict):
        raise ValueError('Повреждены индивидуальные настройки монет')

    reject_private_data(sections)
    staged = deepcopy(config)
    staged['sections'] = deepcopy(sections)
    seen, parents = set(), {}
    for section in staged['sections']:
        if not isinstance(section, dict):
            raise ValueError('Диапазон должен быть объектом')
        sid = section.get('id')
        if not isinstance(sid, str) or not _SECTION_ID.fullmatch(sid) or sid in seen:
            raise ValueError('ID диапазона должен быть уникальным и корректным')
        seen.add(sid)
        if (not isinstance(section.get('label'), str)
                or not 1 <= len(section['label'].strip()) <= 120):
            raise ValueError(f'Диапазон {sid}: неверное название')
        if section.get('side') not in ('LONG', 'SHORT'):
            raise ValueError(f'Диапазон {sid}: неверное направление')
        if type(section.get('enabled')) is not bool:
            raise ValueError(f'Диапазон {sid}: неверное состояние')
        if not isinstance(section.get('overrides'), dict):
            raise ValueError(f'Диапазон {sid}: неверные индивидуальные настройки')
        parent_id, parent_name = (section.get('range_parent_id'),
                                  section.get('range_parent_label'))
        if parent_id is not None or parent_name is not None:
            if (config['strategy'] != 'VELES' or not isinstance(parent_id, str)
                    or not _SECTION_ID.fullmatch(parent_id)
                    or not isinstance(parent_name, str)
                    or not 1 <= len(parent_name.strip()) <= 120):
                raise ValueError(f'Диапазон {sid}: неверная родительская секция')
            identity = (parent_name, section['side'])
            if parent_id in parents and parents[parent_id] != identity:
                raise ValueError('Диапазоны одной секции имеют разные названия/направления')
            parents[parent_id] = identity
        try:
            validate_group(section['entry'])
            validate_group(section['exit'])
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f'Диапазон {sid}: {exc}') from exc

    for symbol in symbols:
        for section in staged['sections']:
            sid = section['id']
            try:
                resolved = coin_section(staged, symbol, section)
                if resolved['side'] not in ('LONG', 'SHORT') or type(resolved['enabled']) is not bool:
                    raise ValueError('Неверное направление или состояние')
                validate_group(resolved['entry'])
                validate_group(resolved['exit'])
                validate_position_settings(effective(staged, symbol, section), resolved['side'])
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f'{config["strategy"]} {symbol}/{sid}: {exc}') from exc
    return staged
