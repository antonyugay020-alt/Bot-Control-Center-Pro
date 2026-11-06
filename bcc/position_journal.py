"""Replay position receipts independently of mutable PAPER position documents.

Legacy accounts retain their original history. Only cycles opened with an
explicit receipt identity participate: no invented backdated fills or balances.
The same replay checks proposed transactions and restart/import snapshots.
"""
import math
import re
import hashlib
import json
from .margin import MODEL


class PositionJournalError(ValueError):
    pass


def cycle_reference(key, position):
    """Stable across Close/Open and clock corrections; preserve legacy keys."""
    if 'position_id' in position:
        return 'position:' + position['position_id']
    return f"{key}:{position['opened']}"


def receipt(position, key):
    if 'position_id' not in position:
        return {}
    return dict(position_id=position['position_id'], position_key=key,
                position_kind=position.get('kind', 'PERPETUAL'))


def terms_digest(position):
    fields = (('settings', 'entry_rule', 'entry_observation', 'slug', 'token', 'end', 'opened')
              if position.get('kind') == 'POLYMARKET' else
              ('settings', 'orders', 'budget', 'entry', 'opened', 'reinvest_pct', 'execution_rules'))
    raw = json.dumps({field: position[field] for field in fields}, sort_keys=True,
                     separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def number(value, field, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or positive and value <= 0):
        raise PositionJournalError('Повреждена запись позиции: ' + field)
    return value


def same(actual, expected, field):
    number(actual, field)
    if not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-9):
        raise PositionJournalError('Позиция не согласуется с журналом: ' + field)


def identity(value, key):
    pid, kind = value.get('position_id'), value.get('position_kind', value.get('kind', 'PERPETUAL'))
    side = value.get('side')
    if (not isinstance(pid, str) or not re.fullmatch('[0-9a-f]{32}', pid)
            or not isinstance(key, str) or not key
            or kind not in ('PERPETUAL', 'POLYMARKET')
            or side not in (('UP', 'DOWN') if kind == 'POLYMARKET' else ('LONG', 'SHORT'))
            or any(not isinstance(value.get(f), str) or not value[f]
                   for f in ('symbol', 'section'))):
        raise PositionJournalError('Повреждён идентификатор PAPER-позиции')
    return pid, (key, kind, side, value['symbol'], value['section'])


def capture(positions, archives=()):
    """Keep only immutable receipt facts, not grids, charts or funding timelines."""
    accounts = {}
    for p in archives:
        if 'position_id' in p:
            pid, meta = identity(p, p.get('position_key'))
            if pid in accounts:
                raise PositionJournalError('Повторный закрытый PAPER-цикл')
            accounts[pid] = dict(meta=meta, qty=0, average=0, fees=0, leverage=None, mode=None,
                                 realized=p['cycle_pnl'], funding_total=0, original_qty=0,
                                 spent=0, terms=None)
    for key, p in positions.items():
        if 'position_id' not in p:
            continue
        pid, meta = identity(p, key)
        if pid in accounts:
            raise PositionJournalError('Повторный идентификатор PAPER-позиции')
        accounts[pid] = dict(meta=meta, qty=p['qty'], average=p['average'],
                             fees=p.get('fee', 0) if meta[1] == 'POLYMARKET' else p['fees'],
                             leverage=None if meta[1] == 'POLYMARKET' else p['settings']['leverage'],
                             mode=None if meta[1] == 'POLYMARKET' else p['settings']['margin_mode'],
                             realized=p.get('realized', 0), funding_total=p.get('funding_total', 0),
                             original_qty=p.get('original_qty', p['qty']),
                             spent=p.get('spent', p['qty'] * p['average']), terms=terms_digest(p))
    return accounts


def reconcile(positions, events, *, baseline=None, archives=()):
    """Replay in SQLite insertion order, never by adjustable market timestamps."""
    accounts = {pid: dict(p) for pid, p in (baseline or {}).items()}
    for e in events:
        if 'position_id' not in e:
            continue
        pid, meta = identity(e, e.get('position_key'))
        action = e['action']
        qty = number(e.get('qty'), 'qty', positive=True)
        price = number(e.get('price'), 'price')
        fee = number(e.get('fee'), 'fee')
        poly = meta[1] == 'POLYMARKET'
        if (price < 0 or (price == 0 and action != 'SETTLE')
                or poly and price > 1 or fee < 0 and action != 'FUNDING'):
            raise PositionJournalError('Некорректная цена или комиссия PAPER-позиции')
        if action == 'OPEN':
            if pid in accounts:
                raise PositionJournalError('Повторное открытие одного PAPER-цикла')
            leverage = None if poly else number(e.get('leverage'), 'leverage', positive=True)
            mode = None if poly else e.get('margin_mode')
            if not poly and mode not in ('CROSS', 'ISOLATED'):
                raise PositionJournalError('Неизвестный режим маржи PAPER-позиции')
            terms = e.get('position_terms_sha256')
            if not isinstance(terms, str) or not re.fullmatch('[0-9a-f]{64}', terms):
                raise PositionJournalError('Отсутствуют зафиксированные условия PAPER-позиции')
            accounts[pid] = p = dict(meta=meta, qty=qty, average=price,
                                     fees=fee, leverage=leverage, mode=mode, terms=terms,
                                     realized=0, funding_total=0, original_qty=qty, spent=qty * price)
            delta = -fee - (qty * price if poly else 0)
            net = -fee
        else:
            p = accounts.get(pid)
            if p is None or p['meta'] != meta:
                raise PositionJournalError('Исполнение не относится к открытому PAPER-циклу')
            if action == 'FUNDING' and not poly:
                rate = number(e.get('rate'), 'funding rate')
                same(fee, qty * price * rate * (1 if meta[2] == 'LONG' else -1), 'funding fee')
                # Delayed funding can legitimately arrive after a full close.
                if p['qty'] > 0:
                    p['fees'] += fee
                    p['funding_total'] += fee
                else:
                    p['realized'] -= fee
                delta = net = -fee
            elif action == 'DCA' and not poly and p['qty'] > 0:
                p['average'] = (p['average'] * p['qty'] + price * qty) / (p['qty'] + qty)
                p['qty'] += qty
                p['fees'] += fee
                p['original_qty'] += qty
                p['spent'] += price * qty
                delta = net = -fee
            elif action in ('CLOSE', 'PARTIAL', 'SETTLE') and p['qty'] > 0:
                old_qty = p['qty']
                if qty > old_qty and not math.isclose(qty, old_qty, rel_tol=1e-10, abs_tol=1e-12):
                    raise PositionJournalError('Закрытие превышает объём PAPER-позиции')
                full = qty / old_qty >= 1 - 1e-10
                if (action == 'PARTIAL' and full or action != 'PARTIAL' and not full
                        or poly != (action == 'SETTLE')):
                    raise PositionJournalError('Неверный объём или тип закрытия PAPER-позиции')
                if poly:
                    delta, net = qty * price, qty * (price - p['average'])
                else:
                    coverage = number(e.get('paper_isolated_loss_coverage', 0), 'loss coverage')
                    execution = (price - p['average']) * qty * (1 if meta[2] == 'LONG' else -1) - fee
                    expected_coverage = (max(0, -p['average'] * qty / p['leverage'] - execution)
                                         if p['mode'] == 'ISOLATED' and e.get('margin_model') == MODEL
                                         and e.get('reason') == 'PAPER_LIQUIDATION' else 0)
                    same(coverage, expected_coverage, 'loss coverage')
                    if coverage:
                        same(e.get('execution_cash_delta'), execution, 'execution_cash_delta')
                    delta = net = execution + coverage
                cycle_net = delta - (qty * p['average'] if poly else 0) - p['fees'] * qty / old_qty
                same(e.get('cycle_net_pnl'), cycle_net, 'cycle_net_pnl')
                p['realized'] += cycle_net
                p['fees'] *= max(0, 1 - qty / old_qty)
                p['qty'] = 0 if full else old_qty - qty
            else:
                raise PositionJournalError('Неизвестная операция PAPER-позиции')
        same(e.get('cash_delta'), delta, 'cash_delta')
        same(e.get('net_pnl'), net, 'net_pnl')

    remaining = {pid for pid, p in accounts.items() if p['qty'] > 0}
    for key, position in positions.items():
        if 'position_id' not in position:
            continue
        pid, meta = identity(position, key)
        p = accounts.get(pid)
        if pid not in remaining or p['meta'] != meta:
            raise PositionJournalError('PAPER-позиция отсутствует в журнале исполнений')
        remaining.remove(pid)
        if terms_digest(position) != p['terms']:
            raise PositionJournalError('Условия PAPER-позиции не согласуются с её открытием')
        same(position.get('qty'), p['qty'], 'qty')
        same(position.get('average'), p['average'], 'average')
        if meta[1] == 'POLYMARKET':
            same(position.get('shares'), p['qty'], 'shares')
            same(position.get('cost'), p['qty'] * p['average'], 'cost')
            same(position.get('fee'), p['fees'], 'fee')
        else:
            same(position.get('fees'), p['fees'], 'fees')
            for field in ('realized', 'funding_total', 'original_qty', 'spent'):
                same(position.get(field), p[field], field)
            same(position['settings'].get('leverage'), p['leverage'], 'leverage')
            if position['settings'].get('margin_mode') != p['mode']:
                raise PositionJournalError('Режим маржи позиции не согласуется с журналом')
            same(position.get('margin'), p['qty'] * p['average'] / p['leverage'], 'margin')
    if remaining:
        raise PositionJournalError('Открытая PAPER-позиция потеряна из сохранённого состояния')
    archived_ids = set()
    for archived in archives:
        if 'position_id' not in archived:
            continue
        pid, meta = identity(archived, archived.get('position_key'))
        if pid in archived_ids:
            raise PositionJournalError('Повторный закрытый PAPER-цикл')
        archived_ids.add(pid)
        p = accounts.get(pid)
        if p is None or p['qty'] != 0 or p['meta'] != meta:
            raise PositionJournalError('Закрытый PAPER-цикл отсутствует в журнале')
        same(archived.get('cycle_pnl'), p['realized'], 'archived cycle_pnl')
