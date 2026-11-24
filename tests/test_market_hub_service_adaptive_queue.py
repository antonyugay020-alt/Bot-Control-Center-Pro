from tools.market_hub_service import create_market_event_queue

def test_production_queue_does_not_keep_fixed_16384_limit():
    q=create_market_event_queue()
    keys={('tickers',f'S{i}USDT') for i in range(11000)}
    keys|={('depth50',f'S{i}USDT') for i in range(11000)}
    q.configure(keys)
    assert q.stats()['event_capacity']==22000
    assert q.stats()['active_over_capacity']==0
    for kind, symbol in keys:assert q.notify(kind,symbol)
    assert q.stats()['pending']==22000
    assert q.stats()['dropped_notifications']==0
