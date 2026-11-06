"""Cross-process command / deletion fence and restart behavior."""
from threading import Event, Thread
import pytest

from bcc.coordination import Coordinator
from bcc.config import defaults
from bcc.storage import Repository
from bcc.supervisor import Supervisor


def test_retirement_fences_every_command_from_other_process(tmp_path):
    path = tmp_path / 'coordination.sqlite'
    writer = Coordinator(path)
    writer.initialize()
    writer.submit_desired('section/bot', 'RUNNING')
    revision = writer.command_status('section/bot')['revision']
    assert writer.begin_retirement('section/bot')
    other = Coordinator(path)
    for invoke in (
        lambda: other.submit_desired('section/bot', 'RUNNING'),
        lambda: other.submit_desired('section/bot', 'STOPPED'),
        lambda: other.submit_drain_intent('section/bot', True),
        lambda: other.submit_manual_close_intent('section/bot'),
        lambda: other.set_desired('section/bot', 'RUNNING'),
        lambda: other.set_drain('section/bot', True),
    ):
        with pytest.raises(ValueError, match='удаляется'):
            invoke()
    row = other.get_registration('section/bot')
    assert row['desired'] == 'STOPPED'
    assert row['command_version'] > revision
    assert other.unregister('section/bot')
    assert other.get_registration('section/bot') is None
    # A late GUI command, already resolved before deletion, cannot recreate
    # a RUNNING registry entry after the owner row has been physically removed.
    for late in (lambda: other.submit_desired('section/bot', 'RUNNING'),
                 lambda: other.submit_drain_intent('section/bot', True),
                 lambda: other.submit_manual_close_intent('section/bot'),
                 lambda: other.register('section/bot')):
        with pytest.raises(ValueError, match='уже удалён'):
            late()
    assert other.get_registration('section/bot') is None


def test_deletion_waits_for_active_reconciliation_and_revokes_future_start(tmp_path):
    repo = Repository(tmp_path)
    cfg = defaults('VELES')
    cfg['name'] = 'Race test'
    folder = repo.create(cfg)
    sup = Supervisor(repo)
    sup.request_start(folder)
    reached = Event()
    release = Event()
    started = []
    class FakeLifecycle:
        def status(self, _folder):
            reached.set()
            assert release.wait(5), 'test lost the reconciliation gate'
            return 'Остановлен'
        def start(self, _folder):
            started.append('start')
            raise RuntimeError('synthetic controlled failure')
    sup.lifecycle = FakeLifecycle()
    results = []
    reconcile = Thread(target=lambda: results.append(sup.reconcile_one(folder)))
    reconcile.start()
    assert reached.wait(5)
    deletion = Thread(target=lambda: sup.delete_bot(folder, discard_paper_state=True))
    deletion.start()
    # The deletion cannot revoke the registry while the reconciler owns
    # the same per-bot control stripe.
    assert deletion.is_alive()
    release.set()
    reconcile.join(5)
    deletion.join(5)
    assert not reconcile.is_alive() and not deletion.is_alive()
    assert results and results[0].action == 'error'
    assert not folder.exists()
    with sup.coordinator.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM owners').fetchone()[0] == 0
