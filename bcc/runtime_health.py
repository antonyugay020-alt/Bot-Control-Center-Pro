"""Authoritative process-verified runtime health for launcher and GUI.

Health is an observation, never permission to mutate PAPER accounting state.
The runtime's OS host lease remains the exclusive controller lock.
"""
import json
import math
import os
import time
import uuid
from pathlib import Path

from . import process_os as psutil

from .storage import atomic_json


HEALTH_FILENAME = '.runtime_health.json'


def publish(root, *, run_id, state):
    if state not in ('ready', 'degraded'):
        raise ValueError('unknown runtime health state')
    from .process_identity import current_creation_time
    pid, created = os.getpid(), current_creation_time()
    atomic_json(Path(root) / HEALTH_FILENAME, {
        'schema': 1, 'run_id': run_id, 'state': state,
        'pid': pid, 'created': created,
        'heartbeat': time.time(),
        'data_root': str(Path(root).resolve()),
    })


def observe(root, *, max_age=15.0):
    """Read advisory health only after independent OS identity verification.

    A stale JSON file or reused PID cannot be accepted as a running service.
    """
    root = Path(root).resolve()
    try:
        value = json.loads((root / HEALTH_FILENAME).read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or value.get('schema') != 1
                or value.get('data_root') != str(root)
                or value.get('state') != 'ready'
                or not isinstance(value.get('run_id'), str)
                or not value['run_id']
                or type(value.get('pid')) is not int
                or type(value.get('created')) not in (int, float)
                or type(value.get('heartbeat')) not in (int, float)
                or not math.isfinite(value['created']) or value['created'] <= 0
                or not math.isfinite(value['heartbeat'])):
            return None
        age = time.time() - value['heartbeat']
        if not 0 <= age <= max_age:
            return None
        process = psutil.Process(value['pid'])
        if abs(process.create_time() - value['created']) > .05:
            return None
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        args = process.cmdline()
        # Explicitly verify the service entrypoint and canonical data root.
        from .runtime_process import service_command_matches
        if not service_command_matches(args, root):
            return None
        return value
    except (OSError, ValueError, TypeError, IndexError, LookupError,
            json.JSONDecodeError, psutil.Error):
        return None


def wait_ready(root, *, timeout=15.0, poll=0.1, clock=time.monotonic, sleep=time.sleep):
    """Bounded startup gate; never infer readiness from guardian creation."""
    if timeout <= 0 or poll <= 0:
        raise ValueError('invalid readiness timeout')
    until = clock() + timeout
    while True:
        value = observe(root)
        if value is not None:
            return value
        remaining = until - clock()
        if remaining <= 0:
            raise RuntimeError('Runtime service did not become ready')
        sleep(min(poll, remaining))


def new_run_id():
    return uuid.uuid4().hex
