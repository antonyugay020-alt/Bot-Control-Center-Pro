"""The unattended live smoke must own its account and fail closed."""
import json
from pathlib import Path

import pytest

from tools import auto_live_paper


def test_live_smoke_config_is_public_paper_with_finite_test_budget():
    cfg = auto_live_paper.config()
    assert cfg['mode'] == 'PAPER' and cfg['exchange'] == 'BYBIT_LINEAR'
    assert cfg['symbols'] == ['BTCUSDT'] and cfg['capital'] == 500
    assert cfg['settings']['orders']['count'] == 1
    assert cfg['sections'][0]['entry']['conditions'][0]['indicator'] == 'PRICE'


@pytest.mark.parametrize('fresh', [True, False])
def test_owned_paper_cycle_requires_entry_exit_ledger_freshness_and_cleanup(tmp_path, monkeypatch, fresh):
    calls = []
    class Owner:
        draining = False
        closed = False
        def create_bot(self, cfg):
            assert cfg['mode'] == 'PAPER'
            calls.append('create')
            return tmp_path / 'owned_bot'
        def request_start(self, folder): calls.append('start')
        def submit_drain(self, folder, *, draining):
            assert draining
            self.draining = True
            calls.append('drain')
        def submit_manual_close(self, folder):
            assert self.draining
            self.closed = True
            calls.append('close')
    owner = Owner()
    class Session:
        def __init__(self, root, output, **kwargs):
            self.root, self.output, self.owner = Path(root), Path(output), owner
        def __enter__(self):
            self.root.mkdir(parents=True)
            self.output.mkdir(parents=True)
            return self
        def __exit__(self, *args):
            (self.output / 'session.json').write_text(json.dumps({
                'status': 'STOPPED_STATE_PRESERVED', 'services_retired': True}), encoding='utf8')
    class FakeStore:
        def __init__(self, folder): pass
        def read(self, include_charts=False):
            return {'strategy': {'draining': owner.draining},
                    'positions': {} if owner.closed else {'test': {}}}
        def history(self, limit=500):
            result = [{'action': 'OPEN'}]
            if owner.closed: result.append({'action': 'CLOSE'})
            return result
        def audit(self): calls.append('audit')
    monkeypatch.setattr(auto_live_paper, 'AcceptanceSession', Session)
    monkeypatch.setattr(auto_live_paper, 'Store', FakeStore)
    monkeypatch.setattr(auto_live_paper, 'execution_freshness',
                        lambda *_: {'status': 'PASS' if fresh else 'PARTIAL_EVIDENCE'})
    monkeypatch.setattr(auto_live_paper, 'export_current_report', lambda *a, **k: None)
    monkeypatch.setattr(auto_live_paper.time, 'sleep', lambda _: None)
    result = auto_live_paper.run(tmp_path / 'fresh_output', duration=15)
    assert calls == ['create', 'start', 'drain', 'close', 'audit']
    assert result['status'] == ('PAPER_CYCLE_PASS' if fresh else 'NOT_CONFIRMED')
    assert result['real_orders'] is False and result['release_ready'] is False
    assert (tmp_path / 'fresh_output' / 'live_paper_report.json').is_file()


def test_owned_paper_rejects_existing_workspace(tmp_path):
    existing = tmp_path / 'existing'
    existing.mkdir()
    with pytest.raises(ValueError, match='Fresh'):
        auto_live_paper.run(existing)
