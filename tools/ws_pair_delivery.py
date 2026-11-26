"""Parallel scalar acquisition and source-bound evidence for central Hub QA.

Scalar reads retain their normal transport and policy checks. Their results
are never used as a coherent execution snapshot; the caller still validates
both channels together against original exchange timestamps.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def parallel_reads(book, mark):
    """Wait for both independent reads; never abandon a live QA reader."""
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='ws-pair') as pool:
        depth = pool.submit(book)
        collateral = pool.submit(mark)
        return depth, collateral


def collect_central_evidence(root):
    """Read observed committed-demand counters, not merely a requested flag.

    The endpoint challenge is never read or copied into evidence. The
    directory belongs to this newly created, isolated acceptance Hub.
    """
    root = Path(root)
    cache = root / 'market_cache.sqlite'
    stats = None
    for line in (root / 'market_hub.log').read_text(encoding='utf-8', errors='replace').splitlines():
        prefix = 'Market Hub WS health: '
        if not line.startswith(prefix):
            continue
        try:
            candidate = json.loads(line[len(prefix):]).get('central_demand')
        except (ValueError, AttributeError):
            continue
        if isinstance(candidate, dict):
            stats = candidate
    return dict(endpoint_observed=Path(str(cache)+'.demand.json').is_file(),
                file_writer_queue_observed=any(root.glob(cache.name+'.writer.lock*')),
                central_demand_last=stats)


def central_evidence_problems(report, *, symbols):
    """Reject legacy mode, absent receipts and incomplete parallel samples."""
    errors = []
    proof = report.get('central_transport')
    if (report.get('central_demand_requested') is not True or not isinstance(proof, dict)
            or proof.get('endpoint_observed') is not True
            or proof.get('file_writer_queue_observed') is not False):
        errors.append('Central Hub demand transport not proven')
    stats = proof.get('central_demand_last') if isinstance(proof, dict) else None
    if (not isinstance(stats, dict) or type(stats.get('committed')) is not int
            or stats['committed'] < 2 * len(symbols)
            or type(stats.get('batches')) is not int or stats['batches'] <= 0):
        errors.append('Central Hub subscription commit receipts missing')
    rounds = report.get('rounds')
    if not isinstance(rounds, list) or not rounds or any(
            not isinstance(batch, list) or not batch or any(
                not isinstance(row, dict) or row.get('parallel_pair') is not True
                for row in batch) for batch in rounds):
        errors.append('Parallel independent WS acquisition not proven')
    return errors
