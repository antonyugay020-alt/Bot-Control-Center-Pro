"""End-to-end registry reconciliation: one damaged bot cannot starve healthy peers."""
from types import SimpleNamespace
from pathlib import Path
from bcc.coordination import Coordinator
from bcc.supervisor import Supervisor


def test_corrupted_bot_does_not_abort_runtime_batch(tmp_path):
    root = tmp_path
    bots = root / 'MY_BOTS'
    (bots / 'group' / 'bad').mkdir(parents=True)
    (bots / 'group' / 'good').mkdir(parents=True)
    coord = Coordinator(root / 'coordination.sqlite')
    coord.initialize()
    coord.register('group/bad')
    coord.register('group/good')
    class Repo:
        bots = root / 'MY_BOTS'
        def resolve(self, folder):
            return Path(folder)
    class Lifecycle:
        def status(self, folder):
            if Path(folder).name == 'bad':
                raise RuntimeError('corrupt process metadata')
            return 'Остановлен'
    sup = Supervisor(Repo(), lifecycle=Lifecycle(), coordinator=coord)
    cursor, results = sup.reconcile_batch(limit=2, sync_inventory=False)
    assert len(results) == 2
    assert [r.action for r in results] == ['error', 'none']
    assert 'corrupt process metadata' in results[0].error
    assert cursor == 'group/good'
