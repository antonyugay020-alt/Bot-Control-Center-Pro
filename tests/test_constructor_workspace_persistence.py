"""Headless contract: drafts are workspace snapshots; commits are projections."""
import ast
from copy import deepcopy
from pathlib import Path

import pytest

from bcc.config import defaults, validate
from bcc.constructor_session import save_constructor_draft_with_revision, load_constructor_draft
from bcc.wizard_state import selected_configuration


@pytest.mark.parametrize('strategy', ['VELES', 'POLYMARKET'])
@pytest.mark.parametrize('seed', range(40))
def test_inactive_coin_rule_survives_draft_but_not_commit(tmp_path, strategy, seed):
    cfg=defaults(strategy)
    cfg['name']='Workspace'
    cfg['symbols']=['BTC','ETH'] if strategy=='POLYMARKET' else ['BTCUSDT','ETHUSDT']
    inactive=cfg['symbols'][1]
    if strategy=='POLYMARKET':
        cfg['coin_overrides'][inactive]={'parameters':{'entry_seconds_before_close':seed+20}}
        cfg['polymarket']['entry_signal_mode']='before_close'
    else:
        cfg['coin_overrides'][inactive]={'settings':{'leverage':seed%9+2}}
    original=deepcopy(cfg)
    cfg['symbols']=cfg['symbols'][:1]
    workspace=deepcopy(cfg)
    selected, removed=selected_configuration(workspace)
    assert inactive not in selected['coin_overrides']
    assert inactive in workspace['coin_overrides']
    assert removed==[f'монета {inactive}']
    path=tmp_path/'draft.json'
    saved,rev=save_constructor_draft_with_revision(path,workspace,expected_revision=None)
    assert saved==workspace and rev
    restored=load_constructor_draft(path,expected_strategy=strategy)
    restored['symbols']=original['symbols']
    assert restored['coin_overrides'][inactive]==original['coin_overrides'][inactive]
    assert validate(selected)['coin_overrides']=={}


@pytest.mark.parametrize('seed',range(40))
def test_inactive_polymarket_market_seconds_are_not_lost(tmp_path,seed):
    cfg=defaults('POLYMARKET')
    cfg['name']='Workspace'
    cfg['symbols']=['BTC','ETH']
    cfg['polymarket']['entry_signal_mode']='before_close'
    cfg['polymarket']['durations']=[300,900]
    cfg['polymarket']['overrides']['ETH:900']={'entry_seconds_before_close':seed+40}
    cfg['polymarket']['durations']=[300]
    projected,removed=selected_configuration(cfg)
    assert 'ETH:900' not in projected['polymarket']['overrides']
    assert 'ETH:900' in cfg['polymarket']['overrides']
    assert 'рынок ETH:900' in removed
    path=tmp_path/'draft.json'
    save_constructor_draft_with_revision(path,cfg,expected_revision=None)
    loaded=load_constructor_draft(path)
    loaded['polymarket']['durations']=[300,900]
    selected,_=selected_configuration(loaded)
    assert selected['polymarket']['overrides']['ETH:900']['entry_seconds_before_close']==seed+40
    assert validate(selected)['polymarket']['overrides']['ETH:900']['entry_seconds_before_close']==seed+40


def test_qt_wizard_save_and_failed_commit_persist_workspace_not_projection():
    """AST-level safety guard; real native Qt tests are separate and mandatory."""
    tree=ast.parse((Path(__file__).resolve().parents[1]/'bcc'/'wizard.py').read_text(encoding='utf-8'))
    wizard=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='BotWizard')
    methods={n.name:n for n in wizard.body if isinstance(n,ast.FunctionDef)}
    data=methods['data'];save=methods['save_draft'];commit=methods['commit']
    assert any(a.arg=='include_inactive' for a in data.args.kwonlyargs)
    for fn in (save,commit):
        draft_saves=[n for n in ast.walk(fn) if isinstance(n,ast.Call)
                     and isinstance(n.func,ast.Name) and n.func.id=='save_constructor_draft_with_revision']
        assert len(draft_saves)==1
        assert any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
                   and n.func.attr=='data' and any(kw.arg=='include_inactive'
                   and isinstance(kw.value,ast.Constant) and kw.value.value is True for kw in n.keywords)
                   for n in ast.walk(draft_saves[0]))
    # The shared publication contract now performs the projection and
    # effective-rule roundtrip in one boundary, rather than requiring the
    # Qt form to call data() in projected mode before validation.
    assert any(isinstance(n,ast.Assign)
               and any(isinstance(t,ast.Name) and t.id=='workspace' for t in n.targets)
               and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute)
               and n.value.func.attr=='data'
               and any(kw.arg=='include_inactive' and isinstance(kw.value,ast.Constant)
                       and kw.value.value is True for kw in n.value.keywords)
               for n in ast.walk(commit))
    assert any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
               and n.func.id=='publish_wizard_configuration'
               and any(isinstance(arg,ast.Name) and arg.id=='workspace' for arg in n.args)
               for n in ast.walk(commit))
