"""Compatibility API for saved pre-integration Polymarket entry rules.

The editor, runtime and validation share the config module implementation.
"""
from copy import deepcopy

ENTRY_DEFAULTS = dict(entry_signal_mode="anytime", entry_seconds_before_close=30,
                      entry_window_seconds=0, entry_min_seconds_left=5)
LEGACY_KEYS = dict(timing="entry_signal_mode", seconds_before_close="entry_seconds_before_close",
                   min_seconds_left="entry_min_seconds_left")

def normalize_parameters(parameters, *, complete=False):
    if not isinstance(parameters, dict):
        raise ValueError("Параметры Polymarket должны быть объектом")
    out = deepcopy(parameters)
    canonical = any(k in out for k in ENTRY_DEFAULTS)
    for old, new in LEGACY_KEYS.items():
        if old in out:
            if new in out and out[new] != out[old]:
                raise ValueError("Противоречивые параметры Polymarket")
            out[new] = out.pop(old)
    if complete and not canonical:
        for key, value in ENTRY_DEFAULTS.items():
            out.setdefault(key, value)
    return out

def effective_parameters(cfg, coin, duration):
    from .config import resolve_poly_market
    return resolve_poly_market(cfg, coin, duration)

def entry_allowed(parameters, start, end, earliest, latest=None):
    from .config import poly_window_allows
    return poly_window_allows(parameters, start, end, earliest, earliest if latest is None else latest)

def validate_parameters(parameters, durations):
    from .config import validate_poly_rule
    for duration in durations:
        validate_poly_rule(parameters, duration)
