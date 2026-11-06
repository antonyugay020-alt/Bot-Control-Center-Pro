from bcc.coordination import Coordinator
from bcc.supervisor import Supervisor


def test_drain_is_durable_convergent_and_acknowledged(tmp_path):
    c = Coordinator(tmp_path / 'coord.sqlite')
    c.initialize()
    c.register('A')
    c.set_drain('A', True)
    c.set_drain('A', False)
    assert c.pending_drain('A') == (2, 0)
    c.acknowledge_drain('A', 1)  # stale revision cannot cancel newer intent
    assert c.pending_drain('A') == (2, 0)
    c2 = Coordinator(tmp_path / 'coord.sqlite')
    c2.acknowledge_drain('A', 2)
    assert c.pending_drain('A') is None


def test_supervisor_contract_is_explicit():
    assert hasattr(Supervisor, 'submit_drain')
