"""Windows entrypoint with a persistent, continuously written AI development log.

Called by AI_SETUP_WINDOWS.bat / AI_CYCLE_WINDOWS.bat. Deliberately does not
read, overwrite, or send PAPER account databases, and never certifies a release.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.ai_work'


def _log_path(action: str) -> Path:
    configured = os.getenv('BCC_AI_LAUNCH_LOG')
    if configured:
        candidate = Path(configured).resolve()
        if not candidate.is_relative_to(WORK.resolve()):
            raise ValueError('AI launcher log must be inside the BCC .ai_work directory')
        return candidate
    return WORK / ('AI_' + action.upper() + '_WINDOWS_LAST.log')


def run_step(action: str, log, *, runner=None, extra=()) -> int:
    """Stream tool output to console AND disk. Preserve child exit code."""
    command = [sys.executable, '-u', '-m', 'tools.ai_dev_pipeline', action, *extra]
    header = f'[BCC AI] Running {action}: {dt.datetime.now(dt.timezone.utc).isoformat()}\n'
    sys.stdout.write(header)
    sys.stdout.flush()
    log.write(header)
    log.flush()
    popen = runner or subprocess.Popen
    child = popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  text=True, encoding='utf-8', errors='replace', bufsize=1)
    try:
        for line in child.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
        return child.wait()
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    finally:
        if child.stdout:
            child.stdout.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='BCC: Windows AI setup/cycle with visible diagnostics')
    parser.add_argument('action', choices=('setup', 'cycle'))
    args = parser.parse_args(argv)
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        log_path = _log_path(args.action)
        # The BAT script creates a header; keep it and append each stage live.
        with log_path.open('a', encoding='utf-8') as log:
            log.write('BEGIN ' + dt.datetime.now(dt.timezone.utc).isoformat() + '\n')
            log.flush()
            if os.name != 'nt':
                message = '[BCC AI] BLOCKED: This launcher requires native Windows.\n'
                print(message, end='', flush=True)
                log.write(message)
                return 2
            # An explicitly supplied, local, one-time API credential is
            # consumed before doctor. Running this launcher is the user's
            # opt-in; it never runs model inference at this bootstrap stage.
            steps = (('doctor', 'init') if args.action == 'setup' else
                     ('doctor', 'init', 'cycle'))
            if (WORK / '.codex_api_key_once').is_file():
                steps = ('api-login', *steps)
            for step in steps:
                code = run_step(step, log, extra=('--qa', 'windows') if step == 'cycle' else ())
                if code != 0:
                    message = (f'[BCC AI] BLOCKED at {step}: exit={code}. '
                               f'Log: {log_path}\n')
                    print(message, end='', flush=True)
                    log.write(message)
                    log.flush()
                    return code
            message = '[BCC AI] Workflow finished; this is NOT a certified BCC release.\n'
            print(message, end='', flush=True)
            log.write(message)
            return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        message = '[BCC AI] BLOCKED: ' + type(exc).__name__ + ': ' + str(exc)
        print(message, file=sys.stderr, flush=True)
        try:
            with _log_path(args.action).open('a', encoding='utf-8') as log:
                log.write(message + '\n')
        except (OSError, ValueError):
            pass
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
