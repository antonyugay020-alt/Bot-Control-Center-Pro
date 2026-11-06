"""Canonical runtime paths shared by UI, guardian and runtime processes.

Code is immutable; bot state is writable and has one canonical location.
The BCC_DATA_ROOT override supports portable installations and test isolation.
"""
import os
from pathlib import Path


def python_environment(base=None):
    """UTF-8 redirected Python logs; leave file/default/system locale intact."""
    env = dict(os.environ if base is None else base)
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def code_root():
    return Path(__file__).resolve().parent.parent


def data_root(override=None):
    if override is not None:
        candidate = override
    elif os.environ.get('BCC_DATA_ROOT'):
        candidate = os.environ['BCC_DATA_ROOT']
    elif os.name == 'nt':
        candidate = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'BotControlCenterPro'
    else:
        candidate = Path.home() / '.local' / 'share' / 'BotControlCenterPro'
    path = Path(candidate).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path
