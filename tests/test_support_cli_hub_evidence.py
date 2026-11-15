"""Windows final collection needs the same safe Hub evidence as the UI exporter."""
import json
import sys

from bcc import support
from bcc.storage import Repository
from bcc.data_cache import PublicCache


def test_cli_collects_sanitized_hub_metadata_without_exporting_database(tmp_path,monkeypatch):
    root=tmp_path/'owned';Repository(root)
    cache=PublicCache(root/'market_cache.sqlite')
    cache._ensure_schema();cache.close()
    (root/'market_hub.log').write_text('WS connected\napi_key=private-value\n',encoding='utf-8')
    out=tmp_path/'report'
    monkeypatch.setattr(sys,'argv',['support','--root',str(root),'--output',str(out)])
    assert support.main()==0
    report=json.loads((out/'collection.json').read_text(encoding='utf-8'))
    assert report['app_diagnostics']['market_cache.sqlite']['exists'] is True
    metadata=json.loads((out/'app_diagnostics/market_cache_diagnostics.json').read_text(encoding='utf-8'))
    assert metadata['snapshot_kind']=='bounded_readonly_metadata'
    assert metadata['cache_rows']==0
    log=(out/'app_diagnostics/market_hub.log').read_text(encoding='utf-8')
    assert 'WS connected' in log and 'private-value' not in log
    assert not list(out.rglob('*.sqlite'))
    assert not report['errors']
