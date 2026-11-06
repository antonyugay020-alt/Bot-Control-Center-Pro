"""Shared lossless constructor-to-repository contract for PAPER bot settings.

The Qt wizard and Repository use the same check before committing a new or
edited VELES/Polymarket configuration. This verifies that JSON persistence
preserves the complete effective rules (including inherited per-coin/market
entry seconds) without starting a worker or touching PAPER account state.
"""

import json
from copy import deepcopy
from .config import (
    coin_section, effective, poly_rule_keys, resolve_poly_market, validate,
    merge_poly_rule, difference_poly_rule,
)


# The same typed controls are used at all three levels of the Polymarket
# constructor. In particular, ``disabled`` is a persisted field, not a
# market-only checkbox: all three forms must expose it so users can actually
# edit the inherited entry policy, not merely preserve a hidden JSON value.
POLY_FORM_SPECS = (
    ("impulse_mode", "Единица импульса", "choice", [
        ("Процент", "percent"), ("Изменение цены в USD", "usd")]),
    ("impulse", "Порог импульса", "float", (0.000001, 1e9)),
    ("entry_signal_mode", "Когда входить", "choice", [
        ("В любое время", "anytime"),
        ("За N секунд до закрытия", "before_close")]),
    ("entry_seconds_before_close", "Вход за N секунд до закрытия", "float", (0, 3600)),
    ("entry_window_seconds", "Длительность окна, секунд (0 — до отсечки)", "float", (0, 3600)),
    ("entry_min_seconds_left", "Защитный порог до закрытия, секунд", "float", (0, 3600)),
    ("ask_min", "ASK от", "float", (0.0001, 0.9999)),
    ("ask_max", "ASK до", "float", (0.0001, 0.9999)),
    ("stake_pct", "Ставка от депозита, %", "float", (0.001, 100)),
    ("fee_mode", "Комиссия", "choice", [
        ("По публичной формуле рынка", "public"),
        ("Своя оценка", "estimate")]),
    ("fee_rate", "Своя оценка: доля от стоимости", "float", (0, 1)),
    ("disabled", "Отключить новые входы", "bool", None),
)


def validate_poly_form_schema():
    """Fail closed if the persisted schema and Qt editor ever diverge."""
    fields = [row[0] for row in POLY_FORM_SPECS]
    expected = poly_rule_keys() - {"indicator_filter"}
    if len(fields) != len(set(fields)) or set(fields) != expected:
        raise ValueError("Конструктор Polymarket не охватывает все сохраняемые параметры")
    return True


def _effective_rules(config):
    """Stream effective rules; do not allocate a fleet-sized comparison map."""
    strategy = config['strategy']
    if strategy == 'POLYMARKET':
        for symbol in config['symbols']:
            for duration in config['polymarket']['durations']:
                yield (symbol, duration), resolve_poly_market(config, symbol, duration)
    elif strategy == 'VELES':
        for symbol in config['symbols']:
            for section in config['sections']:
                yield (symbol, section['id']), {
                    'section': coin_section(config, symbol, section),
                    'settings': effective(config, symbol, section),
                }


def validate_constructor_roundtrip(config):
    """Validate and prove that a constructor's effective rules survive JSON.

    Return a validated copy. No changes to the input, file system, trading
    settings, running workers, or open positions are made here.
    """
    candidate = validate(config)
    if candidate['strategy'] not in ('VELES', 'POLYMARKET'):
        return candidate
    try:
        encoded = json.dumps(candidate, ensure_ascii=False, allow_nan=False)
        restored = validate(json.loads(encoded))
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError('Конструктор: параметры нельзя сохранить без потерь') from exc
    if restored != candidate or any(
        original != replayed for original, replayed in
        zip(_effective_rules(candidate), _effective_rules(restored))
    ):
        raise ValueError('Конструктор: параметры меняются после сохранения/загрузки')
    return candidate


def market_override_parent(config, symbol, duration):
    """Return the complete editable market policy before market-specific edits.

    Both the Qt form and the validator must start with exactly the same
    global + coin rule. In particular, ``disabled`` is a persisted policy
    field. The same typed Qt schema now exposes it at global, coin and market
    levels, so inheritance and reset remain explicit.

    This function intentionally excludes any existing market-specific patch:
    resetting a market must restore the *inherited* rule, not its old edit.
    """
    from .config import merge_poly_rule

    if config.get('strategy') != 'POLYMARKET':
        raise ValueError('Индивидуальные рынки доступны только для Polymarket')
    poly = config.get('polymarket')
    if not isinstance(poly, dict) or not isinstance(poly.get('durations'), list):
        raise ValueError('Отсутствуют настройки рынков Polymarket')
    if symbol not in config.get('symbols', ()) or duration not in poly['durations']:
        raise ValueError('Рынок не выбран в конструкторе')
    coin = config.get('coin_overrides', {}).get(symbol, {})
    parameters = coin.get('parameters', {})
    if not isinstance(parameters, dict):
        raise ValueError('Индивидуальные параметры монеты должны быть объектом')
    base = merge_poly_rule({k: v for k, v in poly.items()
                  if k not in ('overrides', 'durations')}, parameters)
    # Do not allow a coin patch to add collection-wide keys to a market form.
    base.pop('overrides', None)
    base.pop('durations', None)
    return base



def market_editor_rules(config, symbol, duration):
    """Return inherited and effective market rules for the *same* Qt form.

    Indicator filters are persisted policies, but their GroupEditor is separate
    from the typed Fields controls. Returning both complete rules prevents a
    reopened editor from silently treating an existing filter as disabled and
    deleting it on an unrelated ASK/timing edit. No draft is mutated.
    """
    from .config import merge_poly_rule

    inherited = market_override_parent(config, symbol, duration)
    patch = config['polymarket']['overrides'].get(f'{symbol}:{duration}', {})
    if not isinstance(patch, dict) or set(patch) - poly_rule_keys():
        raise ValueError('Повреждены индивидуальные настройки рынка')
    return inherited, merge_poly_rule(inherited, patch)


def validate_coin_policy(config, symbol, proposed_rule):
    """Validate a Polymarket coin edit through the shared atomic policy engine.

    The old standalone validator could drift from the multi-level transaction
    when market filters, timing or ASK inheritance changed. A single staging
    engine now decides whether *all* affected markets remain valid. The
    returned minimal patch is the same patch that a batch edit would commit.
    No Qt state, repository, PAPER account or worker is touched.
    """
    if config.get('strategy') != 'POLYMARKET':
        raise ValueError('Индивидуальная политика доступна только для Polymarket')
    if symbol not in config.get('symbols', ()):
        raise ValueError('Монета не выбрана в конструкторе')
    if not isinstance(proposed_rule, dict):
        raise ValueError('Параметры монеты должны быть объектом')
    base = config.get('polymarket', {})
    if not isinstance(base, dict):
        raise ValueError('Отсутствуют настройки рынков Polymarket')
    if set(proposed_rule) - (set(base) - {'durations', 'overrides'}):
        raise ValueError('Неизвестные параметры монеты')
    staged = project_poly_policy_transaction(config, coin_rules={symbol: proposed_rule})
    return deepcopy(staged.get('coin_overrides', {}).get(symbol, {}).get('parameters', {}))


def validate_market_override(config, symbol, duration, proposed_rule):
    """Validate one Qt market form with the same policy transaction as bulk edits.

    A form can be edited while unrelated wizard pages are unfinished. Only
    affected effective market rules are checked; a failed edit is not applied.
    The minimal returned patch preserves the complete inherited filter tree.
    """
    base = market_override_parent(config, symbol, duration)
    if not isinstance(proposed_rule, dict):
        raise ValueError('Параметры рынка должны быть объектом')
    if set(proposed_rule) - set(base):
        raise ValueError('Неизвестные параметры рынка')
    key = f'{symbol}:{duration}'
    staged = project_poly_policy_transaction(config, market_rules={key: proposed_rule})
    return deepcopy(staged['polymarket']['overrides'].get(key, {}))


def project_poly_policy_transaction(config, *, global_rule=None,
                                    coin_rules=None, market_rules=None):
    """Stage a multi-level Polymarket constructor edit without side effects.

    A bulk edit may change a global cutoff, multiple coin policies and several
    market exceptions in one user action. Validate their *combined* effective
    rules, not an intermediate order of edits. A failed edit returns no partial
    configuration, and no repository/worker/account is touched.

    ``coin_rules`` and ``market_rules`` contain editable policy values (partial
    or complete); they replace the affected layer's patch, rather than merging
    with its stale patch. Unmentioned layers remain untouched.
    """
    from copy import deepcopy
    from .config import validate_poly_rule

    if config.get('strategy') != 'POLYMARKET':
        raise ValueError('Транзакция политик доступна только для Polymarket')
    staged = deepcopy(config)
    poly = staged.get('polymarket')
    if not isinstance(poly, dict) or not isinstance(poly.get('overrides'), dict):
        raise ValueError('Повреждены настройки рынков Polymarket')
    durations = poly.get('durations')
    symbols = staged.get('symbols')
    if not isinstance(durations, list) or not isinstance(symbols, list):
        raise ValueError('Не выбраны монеты или длительности рынков')
    if not isinstance(staged.get('coin_overrides'), dict):
        raise ValueError('Повреждены индивидуальные настройки монет')
    allowed = poly_rule_keys()
    coin_rules = {} if coin_rules is None else coin_rules
    market_rules = {} if market_rules is None else market_rules
    if not isinstance(coin_rules, dict) or not isinstance(market_rules, dict):
        raise ValueError('Пакет индивидуальных параметров должен быть объектом')

    base = {key: value for key, value in poly.items()
            if key not in ('overrides', 'durations')}
    if global_rule is not None:
        if not isinstance(global_rule, dict) or set(global_rule) - allowed:
            raise ValueError('Неизвестные общие параметры Polymarket')
        base = merge_poly_rule(base, global_rule)
        for key in allowed:
            if key in base:
                poly[key] = deepcopy(base[key])

    impacted = set()
    for symbol, rule in coin_rules.items():
        if symbol not in symbols:
            raise ValueError(f'Монета не выбрана в конструкторе: {symbol}')
        if not isinstance(rule, dict) or set(rule) - allowed:
            raise ValueError(f'Неизвестные параметры монеты: {symbol}')
        patch = difference_poly_rule(base, merge_poly_rule(base, rule))
        record = staged['coin_overrides'].setdefault(symbol, {})
        if not isinstance(record, dict):
            raise ValueError(f'Повреждены параметры монеты: {symbol}')
        if patch:
            record['parameters'] = patch
        else:
            record.pop('parameters', None)
            if not record:
                del staged['coin_overrides'][symbol]
        impacted.update((symbol, duration) for duration in durations)

    for key, rule in market_rules.items():
        if not isinstance(key, str) or ':' not in key:
            raise ValueError('Неверный идентификатор рынка')
        symbol, raw_duration = key.rsplit(':', 1)
        try:
            duration = int(raw_duration)
        except ValueError as exc:
            raise ValueError('Неверная длительность рынка') from exc
        if symbol not in symbols or duration not in durations or key != f'{symbol}:{duration}':
            raise ValueError(f'Рынок не выбран в конструкторе: {key}')
        if not isinstance(rule, dict) or set(rule) - allowed:
            raise ValueError(f'Неизвестные параметры рынка: {key}')
        parent = market_override_parent(staged, symbol, duration)
        patch = difference_poly_rule(parent, merge_poly_rule(parent, rule))
        if patch:
            poly['overrides'][key] = patch
        else:
            poly['overrides'].pop(key, None)
        impacted.add((symbol, duration))

    if global_rule is not None:
        impacted.update((symbol, duration) for symbol in symbols for duration in durations)
    for symbol, duration in impacted:
        try:
            validate_poly_rule(resolve_poly_market(staged, symbol, duration), duration)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f'Рынок {symbol}:{duration}: {exc}') from exc
    return staged


def project_constructor_coin_transaction(config, *, coin_rules):
    """Stage complete VELES/Polymarket coin edits as one lossless transaction.

    The Qt coin editor edits *both* the shared risk/section settings and the
    strategy-specific policy. Validating only the Polymarket parameters leaves
    an invalid risk/section edit unnoticed until the final Save. Validate all
    affected effective rules before closing the coin dialog, without requiring
    a completed bot name or touching a repository, worker or PAPER account.

    ``coin_rules`` maps symbols to complete override records, or ``None`` to
    reset a coin. Existing overrides for other symbols remain untouched.
    """
    from copy import deepcopy
    from .config import (validate_group, validate_position_settings,
                         validate_poly_rule)

    strategy = config.get('strategy')
    if strategy not in ('VELES', 'POLYMARKET'):
        raise ValueError('Индивидуальные параметры доступны только для VELES и Polymarket')
    if not isinstance(coin_rules, dict):
        raise ValueError('Изменения монет должны быть объектом')
    symbols = config.get('symbols')
    sections = config.get('sections')
    if not isinstance(symbols, list) or not isinstance(sections, list):
        raise ValueError('Не выбраны монеты или секции')
    if not isinstance(config.get('coin_overrides'), dict):
        raise ValueError('Повреждены индивидуальные настройки монет')
    section_ids = {section.get('id') for section in sections
                   if isinstance(section, dict)}
    if len(section_ids) != len(sections):
        raise ValueError('Повреждены секции')
    staged = deepcopy(config)
    allowed = {'settings', 'sections', 'entry', 'exit'}
    if strategy == 'POLYMARKET':
        allowed.add('parameters')
        poly = staged.get('polymarket')
        if not isinstance(poly, dict) or not isinstance(poly.get('durations'), list):
            raise ValueError('Повреждены настройки рынков Polymarket')
        if not isinstance(poly.get('overrides'), dict):
            raise ValueError('Повреждены индивидуальные рынки Polymarket')
        poly_keys = poly_rule_keys()

    for symbol, record in coin_rules.items():
        if symbol not in symbols:
            raise ValueError(f'Монета не выбрана в конструкторе: {symbol}')
        if record is None:
            staged['coin_overrides'].pop(symbol, None)
            continue
        if not isinstance(record, dict) or set(record) - allowed:
            raise ValueError(f'Неизвестные настройки монеты: {symbol}')
        if not isinstance(record.get('settings', {}), dict):
            raise ValueError(f'Повреждены настройки монеты: {symbol}')
        per_section = record.get('sections', {})
        if not isinstance(per_section, dict) or set(per_section) - section_ids:
            raise ValueError(f'Неизвестные секции монеты: {symbol}')
        for section_id, patch in per_section.items():
            if not isinstance(patch, dict) or set(patch) - {
                    'settings', 'entry', 'exit', 'side', 'enabled', 'label'}:
                raise ValueError(f'Неизвестные параметры секции {symbol}/{section_id}')
            if not isinstance(patch.get('settings', {}), dict):
                raise ValueError(f'Повреждены настройки секции {symbol}/{section_id}')
            if 'side' in patch and patch['side'] not in ('LONG', 'SHORT'):
                raise ValueError(f'Направление секции {symbol}/{section_id}')
            if 'enabled' in patch and type(patch['enabled']) is not bool:
                raise ValueError(f'Включение секции {symbol}/{section_id}')
            if 'label' in patch and (not isinstance(patch['label'], str)
                                      or not 1 <= len(patch['label'].strip()) <= 120):
                raise ValueError(f'Название секции {symbol}/{section_id}')
        if strategy == 'POLYMARKET':
            params = record.get('parameters', {})
            if not isinstance(params, dict) or set(params) - poly_keys:
                raise ValueError(f'Неизвестные параметры Polymarket: {symbol}')
        staged['coin_overrides'][symbol] = deepcopy(record)

    for symbol in coin_rules:
        for section in sections:
            try:
                resolved = coin_section(staged, symbol, section)
                if resolved['side'] not in ('LONG', 'SHORT'):
                    raise ValueError('Направление секции')
                for field in ('entry', 'exit'):
                    validate_group(resolved[field])
                validate_position_settings(
                    effective(staged, symbol, section), resolved['side'])
            except (TypeError, KeyError, ValueError) as exc:
                raise ValueError(
                    f'{strategy} {symbol}/{section["id"]}: {exc}') from exc
        if strategy == 'POLYMARKET':
            for duration in poly['durations']:
                try:
                    validate_poly_rule(resolve_poly_market(staged, symbol, duration), duration)
                except (TypeError, KeyError, ValueError) as exc:
                    raise ValueError(
                        f'Polymarket {symbol}:{duration}: {exc}') from exc
    return staged


def project_veles_coin_transaction(config, *, coin_rules):
    """Backward-compatible VELES entry point for the unified coin contract."""
    if config.get('strategy') != 'VELES':
        raise ValueError('Индивидуальные диапазоны доступны только для VELES')
    return project_constructor_coin_transaction(config, coin_rules=coin_rules)
