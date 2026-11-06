"""One side-effect-free constructor transaction across VELES/Polymarket layers.

The Qt editor can keep disabled coins/markets in its *workspace*. A publication
projects only selected combinations and proves the effective policies survive
JSON serialization. This module never accesses a repository, a worker or an
exchange; the Supervisor still owns the optimistic live-config commit.
"""
from copy import deepcopy

from .config import (merge_poly_rule, difference_poly_rule, poly_rule_keys,
                     reject_private_data)
from .constructor_contract import validate_constructor_roundtrip
from .wizard_state import selected_configuration


_UNSET = object()


def validate_constructor_publish(workspace):
    """Return exactly the validated executable config, without dormant rules.

    Do not mutate the caller's editor workspace. Publication rejects an
    unfinished name and all invalid inherited effective rules.
    """
    if not isinstance(workspace, dict) or workspace.get('strategy') not in ('VELES', 'POLYMARKET'):
        raise ValueError('Публикация конструктора доступна только VELES/Polymarket')
    from .constructor_session import validate_constructor_workspace
    workspace = validate_constructor_workspace(workspace)
    projected, _ = selected_configuration(workspace)
    return validate_constructor_roundtrip(projected)



def publish_wizard_configuration(workspace):
    """Single publication gateway for every strategy in the Modern Qt wizard.

    VELES/Polymarket retain the strict effective-policy transaction contract.
    Other strategies still need their ordinary configuration validation and
    lossless repository projection; they must not be rejected by a constructor
    validator intended only for VELES/Polymarket. No repository or worker is
    accessed, and the editor workspace is never mutated.
    """
    from .config import STRATEGIES
    from .constructor_session import validate_constructor_workspace

    if not isinstance(workspace, dict) or workspace.get('strategy') not in STRATEGIES:
        raise ValueError('Неизвестная стратегия конструктора')
    if workspace['strategy'] in ('VELES', 'POLYMARKET'):
        return validate_constructor_publish(workspace)
    checked = validate_constructor_workspace(workspace)
    projected, _ = selected_configuration(checked)
    return validate_constructor_roundtrip(projected)

def project_constructor_transaction(config, *, settings=_UNSET, sections=_UNSET,
                                    symbols=_UNSET, durations=_UNSET,
                                    coin_rules=None, poly_global=None,
                                    market_rules=None):
    """Stage a *combined* multi-level edit; validate only the final state.

    Settings/sections/symbols/durations replace the complete corresponding
    form. Coin rules replace complete per-coin override records (``None``
    resets a coin); Polymarket global and market rules are typed policy edits.
    Existing dormant rules remain in the workspace, but are never published.
    A transaction that is invalid at any layer fails without changing input.
    """
    if not isinstance(config, dict) or config.get('strategy') not in ('VELES', 'POLYMARKET'):
        raise ValueError('Пакетное редактирование доступно только VELES/Polymarket')
    reject_private_data(config)
    strategy = config['strategy']
    if strategy != 'POLYMARKET' and (durations is not _UNSET or poly_global is not None
                                     or market_rules is not None):
        raise ValueError('Рынки и время входа доступны только для Polymarket')
    for name, value, expected in (
        ('настройки риска', settings, dict),
        ('секции', sections, list),
        ('монеты', symbols, list),
        ('длительности', durations, list),
    ):
        if value is not _UNSET and not isinstance(value, expected):
            raise ValueError(f'Неверные {name} конструктора')
    if coin_rules is not None and not isinstance(coin_rules, dict):
        raise ValueError('Пакет настроек монет должен быть объектом')
    if poly_global is not None and not isinstance(poly_global, dict):
        raise ValueError('Общие правила Polymarket должны быть объектом')
    if market_rules is not None and not isinstance(market_rules, dict):
        raise ValueError('Пакет правил рынков должен быть объектом')

    staged = deepcopy(config)
    for key, value in (('settings', settings), ('sections', sections),
                       ('symbols', symbols)):
        if value is not _UNSET:
            staged[key] = deepcopy(value)
    if strategy == 'POLYMARKET':
        poly = staged.get('polymarket')
        if not isinstance(poly, dict) or not isinstance(poly.get('overrides'), dict):
            raise ValueError('Повреждены настройки рынков Polymarket')
        if durations is not _UNSET:
            poly['durations'] = deepcopy(durations)
        allowed = poly_rule_keys()
        if poly_global is not None:
            if set(poly_global) - allowed:
                raise ValueError('Неизвестные общие поля Polymarket')
            for key, value in poly_global.items():
                poly[key] = deepcopy(value)

    overrides = staged.get('coin_overrides')
    if not isinstance(overrides, dict):
        raise ValueError('Повреждены настройки монет')
    for symbol, record in (coin_rules or {}).items():
        if not isinstance(symbol, str) or symbol not in staged['symbols']:
            raise ValueError(f'Монета не выбрана: {symbol}')
        if record is None:
            overrides.pop(symbol, None)
        elif not isinstance(record, dict):
            raise ValueError(f'Повреждены настройки монеты: {symbol}')
        else:
            overrides[symbol] = deepcopy(record)

    if strategy == 'POLYMARKET':
        for key, rule in (market_rules or {}).items():
            if not isinstance(key, str) or ':' not in key:
                raise ValueError('Неверный идентификатор рынка')
            symbol, raw_duration = key.rsplit(':', 1)
            try:
                duration = int(raw_duration)
            except ValueError as exc:
                raise ValueError('Неверная длительность рынка') from exc
            if (key != f'{symbol}:{duration}' or symbol not in staged['symbols']
                    or duration not in staged['polymarket']['durations']):
                raise ValueError(f'Рынок не выбран: {key}')
            if not isinstance(rule, dict) or set(rule) - allowed:
                raise ValueError(f'Неизвестные поля рынка: {key}')
            from .constructor_contract import market_override_parent
            parent = market_override_parent(staged, symbol, duration)
            patch = difference_poly_rule(parent, merge_poly_rule(parent, rule))
            if patch:
                staged['polymarket']['overrides'][key] = patch
            else:
                staged['polymarket']['overrides'].pop(key, None)

    # The user may save an incomplete *name* in a draft; it must not prevent
    # coherent multi-field edits. Never publish this temporary name.
    probe = deepcopy(staged)
    if isinstance(probe.get('name'), str) and not probe['name'].strip():
        probe['name'] = 'Проверка черновика'
    validate_constructor_publish(probe)
    return staged
