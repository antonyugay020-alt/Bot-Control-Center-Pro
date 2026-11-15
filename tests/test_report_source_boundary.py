"""Saving reports in a source directory must not invalidate its own seal."""
from pathlib import Path
import zipfile

import pytest

from bcc import support
from bcc.config import defaults
from bcc.storage import Repository, Store
from tools.seal_source import seal
from tools.verify_package import verify, SourceIntegrityError


def test_report_staging_prior_reports_and_runtime_directory_preserve_source_seal(tmp_path,monkeypatch):
    archive=tmp_path/'source.zip';seal(Path(__file__).resolve().parents[1],archive,checkpoint='SOURCE')
    with zipfile.ZipFile(archive) as z:z.extractall(tmp_path/'unpack')
    source=tmp_path/'unpack/SOURCE';data=source/'check_workspaces/runtime'
    folder=Repository(data).create({**defaults('VELES'),'name':'Report boundary'});Store(folder,500)
    monkeypatch.setattr(support,'__file__',str(source/'bcc/support.py'))
    target=source/'BCC_CURRENT_REPORT_20261115_170128.zip'
    for _ in range(2):
        summary=support.export_current_report(data,target)
        assert summary['source_integrity']['status']=='PASS'
        assert summary['workers_stopped_for_report'] is False
        assert verify(source)['status']=='PASS'
        with zipfile.ZipFile(target) as z:
            assert z.testzip() is None
            assert not any('REPORTS/' in name or '.bcc-current-report-' in name for name in z.namelist())
    (source/'BCC_CURRENT_REPORT_bad.zip.py').write_text('tampered source',encoding='utf-8')
    with pytest.raises(SourceIntegrityError):verify(source)
