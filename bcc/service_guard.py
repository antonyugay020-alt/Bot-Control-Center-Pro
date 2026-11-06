"""Persistent runtime service guardian, independent of the Qt window.

The guard owns a cross-process lease. The runtime service owns its own lease;
workers own their own account fences. These are separate failure domains.
"""
import argparse
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from threading import Event

from .runtime_service import HostLease
from .runtime_paths import code_root, python_environment
from .runtime_process import stop_owned_service


def supervise(root, *, stop=None, spawn=None, base_delay=0.5, max_delay=30.0,
              healthy_after=10.0, log_path=None, on_exit=None):
    if not 0 < base_delay <= max_delay or healthy_after <= 0:
        raise ValueError('invalid guardian policy')
    root = Path(root).resolve()
    stop = stop if stop is not None else Event()
    spawn = spawn or subprocess.Popen
    log_path = Path(log_path or (root / 'runtime_service.log'))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with HostLease(root / '.runtime_guard.lock'):
        failures = 0
        while not stop.is_set():
            started = time.monotonic()
            try:
                with log_path.open('ab') as output:
                    child = spawn([sys.executable, '-m', 'bcc.runtime_service',
                                   '--data-root', str(root)],
                                  cwd=str(code_root()), stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=subprocess.STDOUT,
                                  env=python_environment(),
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    while child.poll() is None:
                        if stop.wait(0.2):
                            stop_owned_service(child, root)
                            break
                    code = child.wait()
                    if on_exit:
                        on_exit(code)
            except Exception:
                logging.exception('Managed runtime could not start or exited unexpectedly')
            if stop.is_set():
                break
            elapsed = time.monotonic() - started
            failures = 1 if elapsed >= healthy_after else failures + 1
            delay = min(max_delay, base_delay * (2 ** min(failures - 1, 10)))
            logging.error('Runtime service unavailable; retrying after %.2f seconds', delay)
            stop.wait(delay)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--log-path', type=Path)
    args = parser.parse_args(argv)
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    supervise(args.data_root, stop=stop, log_path=args.log_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
