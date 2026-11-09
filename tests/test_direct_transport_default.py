"""PAPER data transport policy: Hub can never be an implicit dependency."""

import pytest

from bcc.lifecycle import configure_public_market_transport


@pytest.mark.parametrize('strategy', ['VELES', 'GRID', 'PUMP_DUMP', 'POLYMARKET'])
def test_new_worker_defaults_to_direct_shared_cache(tmp_path, monkeypatch, strategy):
    monkeypatch.delenv('BCC_EXPERIMENTAL_MARKET_HUB', raising=False)
    monkeypatch.setenv('BCC_MARKET_HUB_MODE', 'client')
    monkeypatch.setattr('bcc.lifecycle.ensure_hub', lambda root:
                        pytest.fail('default startup must not spawn Market Hub'))
    env = {'BCC_MARKET_HUB_MODE': 'client'}
    assert configure_public_market_transport(env, tmp_path, strategy) == 'direct'
    assert env['BCC_PUBLIC_CACHE'] == str(tmp_path / 'market_cache.sqlite')
    assert 'BCC_MARKET_HUB_MODE' not in env


def test_experimental_hub_requires_explicit_opt_in(tmp_path, monkeypatch):
    touched = []
    monkeypatch.setenv('BCC_EXPERIMENTAL_MARKET_HUB', '1')
    monkeypatch.setattr('bcc.lifecycle.ensure_hub', lambda root: touched.append(root))
    env = {}
    assert configure_public_market_transport(env, tmp_path, 'VELES') == 'hub'
    assert touched == [tmp_path]
    assert env['BCC_MARKET_HUB_MODE'] == 'client'
    assert env['BCC_PUBLIC_CACHE'] == str(tmp_path / 'market_cache.sqlite')


def test_polymarket_never_launches_bybit_market_hub(tmp_path, monkeypatch):
    monkeypatch.setenv('BCC_EXPERIMENTAL_MARKET_HUB', '1')
    monkeypatch.setattr('bcc.lifecycle.ensure_hub', lambda root:
                        pytest.fail('Polymarket does not use Bybit market hub'))
    env = {'BCC_MARKET_HUB_MODE': 'client'}
    assert configure_public_market_transport(env, tmp_path, 'POLYMARKET') == 'direct'
    assert 'BCC_MARKET_HUB_MODE' not in env


def test_invalid_experimental_flag_does_not_enable_hub(tmp_path, monkeypatch):
    monkeypatch.setenv('BCC_EXPERIMENTAL_MARKET_HUB', 'true')
    monkeypatch.setattr('bcc.lifecycle.ensure_hub', lambda root:
                        pytest.fail('Only explicit value 1 can enable experimental transport'))
    env = {}
    assert configure_public_market_transport(env, tmp_path, 'VELES') == 'direct'
