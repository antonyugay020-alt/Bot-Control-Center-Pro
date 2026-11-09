"""Durable public mark-price refresh independent of executable orderbooks.

Only the authoritative account owner writes market evidence into PAPER state.
Invalid/missing data never authorizes entry or changes risk tiers.
"""
from copy import deepcopy

from .market_clock import quote_is_recent


def commit_portfolio_marks(store, marks):
    """Atomically refresh all currently exposed symbols from one public packet."""
    if not isinstance(marks, dict):
        raise ValueError("Invalid mark-price packet")
    with store.transaction(include_charts=False) as (state, events):
        active = {p["symbol"] for p in state["positions"].values()
                  if p.get("kind") != "POLYMARKET"}
        active.update(p["symbol"] for p in state.get("pending", {}).values()
                      if p.get("kind") != "POLYMARKET")
        # A partial response is never allowed to refresh a subset of the
        # portfolio and make a later per-position decision appear complete.
        if not active <= marks.keys():
            return 0
        # Recheck at commit time: a delayed future cannot revive a stale quote.
        if any(not quote_is_recent(marks[symbol], maximum=3) for symbol in active):
            return 0
        changed = 0
        for symbol in active:
            data = state.get("margin_data", {}).get(symbol)
            if isinstance(data, dict) and data.get("symbol") == symbol:
                data["quote"] = deepcopy(marks[symbol])
                changed += 1
        return changed


def commit_public_risk_tiers(store, symbol, risk):
    """Refresh risk tiers without weakening or replacing an executable mark.

    A result from an old request may arrive after the coin was closed. Only
    existing exposed coins with their own current margin model can be updated.
    Every publication is under the same transactional authority as execution.
    """
    from .market_clock import market_now
    from .margin import numeric, parse_tiers

    if (not isinstance(risk, dict) or risk.get('symbol') != symbol
            or not isinstance(risk.get('tiers'), list) or not risk['tiers']):
        raise ValueError('Неполная таблица риска для ' + str(symbol))
    age = market_now() - numeric(risk.get('risk_at'), 'risk_at')
    if not -0.5 <= age <= 300:
        raise ValueError('Устарели публичные ступени риска ' + symbol)
    # Independently revalidate the *full* original public tier invariants
    # at the transactional boundary, not only at the network parser. This
    # prevents incomplete or internally inconsistent responses being written.
    canonical_rows = []
    for tier in risk['tiers']:
        if not isinstance(tier, dict):
            raise ValueError('Повреждена ступень риска')
        canonical_rows.append(dict(
            symbol=symbol, id=tier.get('id'), riskLimitValue=tier.get('limit'),
            maintenanceMargin=tier.get('maintenance_rate'),
            initialMargin=tier.get('initial_rate'),
            maxLeverage=tier.get('max_leverage'),
            mmDeduction=tier.get('deduction'),
        ))
    validated_tiers = parse_tiers(canonical_rows, symbol)
    with store.transaction(include_charts=False) as (state, _events):
        active = any(p.get('symbol') == symbol and p.get('kind') != 'POLYMARKET'
                     for p in list(state['positions'].values()) + list(state['pending'].values()))
        if not active:
            return False
        data = state.get('margin_data', {}).get(symbol)
        if not isinstance(data, dict) or data.get('symbol') != symbol:
            # Unknown market price/collateral cannot be manufactured by a
            # separate risk response. Require complete verified recovery.
            return False
        if risk['risk_at'] < data.get('risk_at', 0):
            return False
        data['tiers'] = deepcopy(validated_tiers)
        data['risk_at'] = risk['risk_at']
        return True
