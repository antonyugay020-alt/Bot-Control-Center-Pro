"""Apply and prove the production candidate WS-only profile during native QA."""
import json
import zlib
from pathlib import Path

FLAGS = (
    'BCC_PUBLIC_DEMAND_HUB_CANDIDATE',
    'BCC_BYBIT_DUAL_WS_CANDIDATE',
    'BCC_BYBIT_MAINTAINED_MARK_CANDIDATE',
    'BCC_BYBIT_MARK_DELIVERY_CANDIDATE',
    'BCC_BYBIT_SHARDED_TICKERS_CANDIDATE',
)
PREFIX = 'Market Hub WS health: '


def step_environment(env, name, arguments):
    """Only the isolated central native WS subprocess receives the profile."""
    result = dict(env)
    if name == 'bybit_ws' and '--central-demand' in arguments:
        result.update(dict.fromkeys(FLAGS, '1'))
        result.pop('BCC_BYBIT_REST_MARK_FALLBACK', None)
    return result


def profile_evidence(log, symbols):
    """Read actual Hub telemetry, not an environment-variable assertion.

    Require at least one fully subscribed observation across all four pairs.
    Coverage and latency are still independently required by the WS report.
    A missing or forbidden hybrid lane is never treated as a passing profile.
    """
    counts = [0] * 4
    for symbol in symbols:
        counts[zlib.crc32(symbol.encode('ascii')) % 4] += 1
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError('Invalid native WS symbol inventory')
    samples = proven = 0
    violations = []
    for line in Path(log).read_text(encoding='utf-8', errors='strict').splitlines():
        if not line.startswith(PREFIX):
            continue
        row = json.loads(line[len(PREFIX):])
        if not isinstance(row, dict):
            raise ValueError('Invalid native WS health record')
        samples += 1
        delivery = row.get('mark_delivery', {})
        streams = row.get('streams', {})
        stream = streams.get('bybit_quotes', {}) if isinstance(streams, dict) else {}
        if not isinstance(stream, dict):
            raise ValueError('Invalid native WS ticker health')
        if (not isinstance(delivery, dict) or
                'rest_fallback' not in delivery or delivery['rest_fallback'] is not None or
                'hybrid_cache' not in delivery or delivery['hybrid_cache'] is not None):
            violations.append('WS-only native profile: REST or hybrid evidence present or unproven')
            continue
        if (not all(delivery.get(k) is True for k in ('enabled', 'balanced_batch', 'sharded'))
                or stream.get('mode') != 'persistent_sharded_redundancy'
                or stream.get('shard_count') != 4):
            violations.append('WS-only native profile: production ticker mode not observed')
            continue
        shards = stream.get('shards')
        if (not isinstance(shards, list) or len(shards) != 4 or
                any(not isinstance(s, dict) or type(s.get('index')) is not int for s in shards) or
                sorted(s['index'] for s in shards) != list(range(4))):
            violations.append('WS-only native profile: shard inventory invalid')
            continue
        if (not isinstance(stream.get('mark_union'), dict) or
                stream['mark_union'].get('active') != len(symbols) or
                not isinstance(stream.get('maintained'), dict) or
                not isinstance(stream.get('maintained_recovery'), dict)):
            continue
        if any(not isinstance(s.get('union'), dict) or
               s['union'].get('active') != counts[s['index']] or
               any(not isinstance(s.get(lane), dict) or
                   s[lane].get('subscribed') != counts[s['index']] or
                   s[lane].get('connection') != 'connected'
                   for lane in ('primary', 'recovery')) for s in shards):
            continue
        if any(not isinstance(stream.get(lane), dict) or
               stream[lane].get('subscribed') != len(symbols)
               for lane in ('primary', 'recovery')):
            continue
        proven += 1
    if not proven:
        violations.append('WS-only native profile: no complete four-pair subscription evidence')
    return dict(status='PASS' if not violations else 'FAIL', samples=samples,
                proven_samples=proven, expected_shard_counts=counts,
                errors=list(dict.fromkeys(violations)))
