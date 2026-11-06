"""Read-only, bounded-error preflight of effective Modern Qt constructor rules.

Unlike the first-error repository publication gate, this diagnostic streams
selected VELES coin/range and Polymarket coin/duration combinations and reports
multiple independent defects in one pass. It never modifies an editor draft,
repository, account, worker, or exchange. Publication remains authoritative.
"""
from .config import (
    coin_section, effective, resolve_poly_market, validate_group,
    validate_poly_rule, validate_position_settings,
)
from .constructor_contract import validate_constructor_roundtrip
from .constructor_session import validate_constructor_workspace
from .wizard_state import selected_configuration


def constructor_preflight(workspace, *, max_errors=30):
    """Return validation diagnostics for selected effective rules only.

    ``max_errors`` bounds the output and short-circuits invalid fleets without
    inventing a fixed fleet-size limit. A valid report always passes the exact
    publication roundtrip gate. Dormant overrides are retained in ``workspace``
    but excluded from executable policy checks.
    """
    if type(max_errors) is not int or not 1 <= max_errors <= 1000:
        raise ValueError('Неверное ограничение количества ошибок проверки')
    errors = []
    checked = 0
    truncated = False

    def add(scope, message):
        errors.append({'scope': str(scope), 'message': str(message)})

    try:
        checked_workspace = validate_constructor_workspace(workspace)
        projected, dormant = selected_configuration(checked_workspace)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        add('Черновик', exc)
        return {'valid': False, 'checked_rules': 0, 'errors': errors,
                'truncated': False, 'dormant_excluded': []}

    strategy = projected['strategy']
    if strategy == 'POLYMARKET':
        for symbol in projected['symbols']:
            for duration in projected['polymarket']['durations']:
                checked += 1
                try:
                    validate_poly_rule(resolve_poly_market(projected, symbol, duration), duration)
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    add(f'{symbol}:{duration}', exc)
                if len(errors) >= max_errors:
                    truncated = True
                    break
            if truncated:
                break
    elif strategy == 'VELES':
        for symbol in projected['symbols']:
            for section in projected['sections']:
                checked += 1
                try:
                    resolved = coin_section(projected, symbol, section)
                    if resolved['side'] not in ('LONG', 'SHORT') or type(resolved['enabled']) is not bool:
                        raise ValueError('Неверное направление или состояние диапазона')
                    validate_group(resolved['entry'])
                    validate_group(resolved['exit'])
                    validate_position_settings(effective(projected, symbol, section), resolved['side'])
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    add(f'{symbol}/{section.get("id", "?")}', exc)
                if len(errors) >= max_errors:
                    truncated = True
                    break
            if truncated:
                break

    # Run the authoritative publication check too. The diagnostic must never
    # claim readiness when global config fields, JSON persistence, or a rule
    # outside the manually inspected layers fails.
    if not truncated:
        try:
            validate_constructor_roundtrip(projected)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            message = str(exc)
            if not any(item['message'] == message for item in errors):
                add('Конфигурация', message)

    return {'valid': not errors and not truncated, 'checked_rules': checked,
            'errors': errors, 'truncated': truncated,
            'dormant_excluded': dormant}
