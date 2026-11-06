"""Reporting has no authority to mutate lifecycle; safety invariant across layers."""
import pytest
from bcc.storage import Repository
from bcc.support import collect


def test_diagnostic_collection_is_read_only_by_default(tmp_path):
    root = tmp_path / 'bots'
    Repository(root)
    report = collect(root, tmp_path / 'report')
    assert report['workers_stopped_for_report'] is False


def test_diagnostics_cannot_bypass_supervisor_stop_authority(tmp_path):
    with pytest.raises(ValueError, match='cannot stop worker'):
        collect(tmp_path / 'bots', tmp_path / 'report', stop_workers=True)
