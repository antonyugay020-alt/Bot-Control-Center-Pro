"""Offline HTTP -> independent Hub -> real adapters/engines -> audited PAPER.

The subprocess runs the production Hub service; only exchange HTTP responses
and the parent's UTC reference are synthetic. This is not live-market or
native Windows acceptance evidence when executed on Linux.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from bcc import market_clock as mc, process_os
from bcc.config import defaults
from bcc.hub_supervisor import ensure_hub, _matches
from bcc.market import DataUnavailable, PolymarketData, PublicData
from bcc.market_hub import HubRegistry
from bcc.monitoring import snapshot_all
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick
from bcc.shared_risk import portfolio_snapshot, risk_transaction
from bcc.storage import Store, Repository, atomic_json
from bcc.strategies import Veles, Grid, PumpDump
from bcc.supervisor import Supervisor
from bcc.worker import process_polymarket
from test_portfolio_recovery_integration import STRATEGIES, account


def _install_http_replay(feed_path):
    """Installed in the test-owned producer only; forbid all unlisted HTTP."""
    def get(_session, url, params=None, *, timeout, allow_redirects):
        assert allow_redirects is False
        feed = json.loads(Path(feed_path).read_text(encoding='utf-8'))
        now = feed['anchor'] + time.time() - feed['wall']
        stamp, price = int(now * 1000), feed['price']
        params = params or {}
        symbol = params.get('symbol', 'BTCUSDT')
        route = url.rsplit('/', 1)[-1]
        if 'api.bybit.com/v5/market/' in url:
            if route == 'kline':
                values = [100] * 97 + [110, 115, 120] if feed['strategy'] == 'PUMP_DUMP' else [99] * 99 + [101]
                if feed.get('exit_bar'):
                    values[-1] = 210
                last = int(now) // 60 * 60000 - 60000
                rows = [[str(last - (99-i)*60000), str(v), str(v+1), str(v-1), str(v),
                         '1000' if i >= 97 else '10'] for i, v in enumerate(values)]
                result = {'list': list(reversed(rows[-int(params['limit']):]))}
            elif route == 'instruments-info':
                result = {'list': [dict(symbol=symbol, status='Trading', contractType='LinearPerpetual', quoteCoin='USDT',
                    lotSizeFilter=dict(qtyStep='.0001', minOrderQty='.0001', minNotionalValue='.01', maxMktOrderQty='10000'),
                    priceFilter=dict(tickSize='.01'))]}
            elif route == 'risk-limit':
                result = {'list': [dict(id='1', symbol=symbol, riskLimitValue='1000000000', initialMargin='.01',
                                       maintenanceMargin='.005', mmDeduction='0', maxLeverage='100')]}
            elif route == 'tickers':
                result = {'list': [dict(symbol='BTCUSDT', markPrice=str(price))]}
            elif route == 'orderbook':
                result = dict(s=symbol, ts=stamp, b=[[str(price-.01), '10000']], a=[[str(price+.01), '10000']])
            elif route == 'history':
                result = {'list': []}
            else:
                raise AssertionError('Unlisted replay route: ' + url)
            payload = dict(retCode=0, time=stamp, result=result)
        elif url == 'https://gamma-api.polymarket.com/markets':
            start = feed['poly_start']
            payload = [dict(id='1', question='Bitcoin Up or Down', slug=params['slug'], conditionId='0x'+'a'*64,
                            endDate=datetime.fromtimestamp(start+300, timezone.utc).isoformat(),
                            outcomes=['Up','Down'], clobTokenIds=['123','456'], acceptingOrders=True,
                            closed=False, feesEnabled=False, orderMinSize=1, orderPriceMinTickSize=.01)]
        elif url == 'https://api.binance.com/api/v3/klines':
            payload = [[params['startTime'], '100']]
        elif url == 'https://api.binance.com/api/v3/ticker/price':
            payload = dict(symbol='BTCUSDT', price='101')
        elif url == 'https://clob.polymarket.com/book':
            payload = dict(asset_id=params['token_id'], timestamp=str(stamp),
                           bids=[dict(price='.49', size='10000')], asks=[dict(price='.5', size='10000')])
        elif url == 'https://data-api.polymarket.com/v2/tokens':
            payload = {'data': [dict(asset_id=params['token_id'], resolved=feed['resolved'], final_price=1)]}
        else:
            raise AssertionError('Forbidden replay HTTP: ' + url)
        class Response:
            status_code = 200
            headers = {}
            def raise_for_status(self): pass
            def json(self): return payload
        return Response()
    requests.Session.get = get


def decision(folder, cfg, client, depth=None):
    # The real worker retries unavailable market admission on its next tick.
    # A cached closed candle can cross a minute boundary between delivery and
    # validation; never relabel it or weaken freshness just for replay timing.
    deadline = time.monotonic() + 8
    while True:
        try:
            return _decision_once(folder, cfg, client, depth)
        except DataUnavailable:
            if time.monotonic() >= deadline:
                raise
            depth = None  # Reacquire depth after waiting on candle delivery.
            time.sleep(.05)


def _decision_once(folder, cfg, client, depth=None):
    store = Store(folder)
    if cfg['strategy'] == 'POLYMARKET':
        with risk_transaction(store) as (state, events):
            process_polymarket(cfg, state, events, client, mc.market_now())
    else:
        depth = depth or client.orderbook('BTCUSDT')
        rows = client.candles('BTCUSDT', 100)
        rates = client.funding('BTCUSDT')
        engine = {'VELES': Veles, 'GRID': Grid, 'PUMP_DUMP': PumpDump}[cfg['strategy']]()
        process_tick(cfg, store, engine, MarketTick('BTCUSDT', rows, depth, rates, {}))


def fresh_depth(client, price):
    """Wait for the producer's next frame, retaining all freshness checks."""
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        try:
            # This helper supplies an entry decision, not just a mark for an
            # already-open position. It must acquire the independent margin
            # risk tiers and mark-price instead of bypassing entry admission
            # with the lighter WS position-only path.
            depth = client.orderbook('BTCUSDT')
            midpoint = (depth['bids'][0][0] + depth['asks'][0][0]) / 2
            if abs(midpoint - price) < .001:
                return depth
        except DataUnavailable:
            pass
        time.sleep(.05)
    raise AssertionError('Recovered producer did not deliver a fresh frame')


@pytest.mark.parametrize('strategy', STRATEGIES)
def test_hub_restart_preserves_strategy_risk_monitor_and_closed_ledger(tmp_path, monkeypatch, strategy):
    import bcc.hub_supervisor as supervisor
    repo = Repository(tmp_path / 'fleet')
    owner = Supervisor(repo)
    folder, cfg = account(repo, strategy, 'Hub ' + strategy)
    wall = time.time()
    anchor = int(wall // 300)*300 + 100 if strategy == 'POLYMARKET' else wall
    frame = dict(wall=wall, anchor=anchor, poly_start=int(anchor//300)*300,
                 strategy=strategy, price=120 if strategy == 'PUMP_DUMP' else 101, resolved=False)
    feed = tmp_path / 'http-replay.json'
    atomic_json(feed, frame)
    reference = mc.MarketClock()
    reference._reference = (anchor, anchor, wall, time.monotonic())
    monkeypatch.setattr(mc, 'clock', reference)
    monkeypatch.setattr('bcc.market.clock', reference)
    script = tmp_path / 'offline_hub_service.py'
    script.write_text('import sys\n'
        + 'sys.path.insert(0, ' + repr(str(Path(__file__).resolve().parent)) + ')\n'
        + 'from test_market_fleet_recovery_e2e import _install_http_replay\n'
        + '_install_http_replay(' + repr(str(feed)) + ')\n'
        + 'sys.argv = [sys.argv[1], *sys.argv[2:]]\n'
        + 'from tools.market_hub_service import main\nmain()\n', encoding='utf-8')
    launches = []
    def spawn(args, **kwargs):
        # Keep production startup challenge/cache/module arguments intact.
        child = subprocess.Popen([args[0], str(script), args[2], *args[3:]], **kwargs)
        launches.append(child)
        return child
    monkeypatch.setattr(supervisor, '_spawn_hub', spawn)
    monkeypatch.setenv('BCC_PUBLIC_CACHE', str(repo.root / 'market_cache.sqlite'))
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    client = PolymarketData() if strategy == 'POLYMARKET' else PublicData()
    stale_reads = []
    if strategy == 'VELES':
        candles = client.candles
        def delayed_candle(*args, **kwargs):
            stale_reads.append(1)
            if len(stale_reads) == 1:
                raise DataUnavailable('Synthetic candle expired before admission')
            return candles(*args, **kwargs)
        monkeypatch.setattr(client, 'candles', delayed_candle)
    record_path = repo.root / 'market_hub_process.json'
    registry = HubRegistry(repo.root / 'market_cache.sqlite')
    try:
        ensure_hub(repo.root)
        for price in ([120, 119, 118, 114] if strategy == 'PUMP_DUMP' else [101]):
            frame['price'] = price
            atomic_json(feed, frame)
            decision(folder, cfg, client, fresh_depth(client, price) if strategy != 'POLYMARKET' else None)
            if Store(folder).read()['positions']:
                break
        saved = Store(folder).read()
        assert saved['positions'], saved['diagnostics']
        if strategy == 'VELES':
            assert len(stale_reads) >= 2
        events = Store(folder).history()
        Store(folder).audit()
        slots = portfolio_snapshot(repo.root)[0]
        atomic_json(repo.root / 'shared_risk.json', dict(max_positions=slots, max_committed_usdt=0.0))
        contender, other = account(repo, strategy, 'Same last slot')
        decision(contender, other, client)
        assert not Store(contender).read()['positions']
        assert not Store(contender).history()
        rows = snapshot_all(repo, owner.lifecycle, include_stopped=True)
        assert len(rows) == 2 and not any(row['error'] for row in rows)

        previous = json.loads(record_path.read_text(encoding='utf-8'))
        previous_token = registry.leader_token(previous['leader_owner'])
        proc = process_os.Process(previous['pid'])
        proc.kill()
        proc.wait(timeout=5)
        ensure_hub(repo.root)
        recovered = json.loads(record_path.read_text(encoding='utf-8'))
        assert recovered['leader_owner'] != previous['leader_owner']
        assert registry.leader_token(recovered['leader_owner']) != previous_token
        assert _matches(recovered, registry.path)
        assert Store(folder).read() == saved and Store(folder).history() == events

        with Store(folder).transaction() as (state, _events):
            state['strategy']['draining'] = True
        if strategy == 'POLYMARKET':
            frame['resolved'] = True
            atomic_json(feed, frame)
            end = max(p['end'] for p in saved['positions'].values())
            with risk_transaction(Store(folder)) as (state, pending):
                process_polymarket(cfg, state, pending, client, end+1)
        else:
            position = next(iter(saved['positions'].values()))
            frame['price'] = round(position['average'] * (1.1 if position['side'] == 'LONG' else .9), 2)
            if strategy == 'VELES':
                # Signal exits require a *new closed bar*, not a rewrite of
                # the entry's already consumed candle or a fabricated TP.
                frame.update(price=210, exit_bar=True, anchor=frame['anchor'] + 60)
                wall_now = time.time()
                remote_now = frame['anchor'] + wall_now - frame['wall']
                reference._reference = (remote_now, remote_now, wall_now, time.monotonic())
            atomic_json(feed, frame)
            depth = fresh_depth(client, frame['price'])
            deadline = time.monotonic() + 12
            while Store(folder).read()['positions']:
                try:
                    decision(folder, cfg, client, depth)
                except DataUnavailable:
                    pass
                assert time.monotonic() < deadline, Store(folder).read()['diagnostics']
                time.sleep(.05)
        final = Store(folder)
        assert not final.read()['positions'], final.read()['diagnostics']
        history = final.history()
        assert any(e['action'] in ('CLOSE', 'SETTLE') for e in history)
        assert final.read()['cash'] == pytest.approx(cfg['capital'] + sum(e['cash_delta'] for e in history))
        assert not Store(contender).read()['positions']
        final.audit()
    finally:
        client.close()
        if record_path.exists():
            record = json.loads(record_path.read_text(encoding='utf-8'))
            if _matches(record, registry.path):
                process = process_os.Process(record['pid'])
                process.terminate()
                process.wait(timeout=5)
        for child in launches:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
