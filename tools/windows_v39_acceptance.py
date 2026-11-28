"""Run the V39 Windows gate from a sealed source archive."""
import argparse
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
from datetime import datetime
import zipfile

from tools.verify_package import ROOT, assert_same_snapshot, verify

BASE_COMMIT = '51bb3c8d4640e68004e3c6126abbb5595a8f51b9'
WS_RUNS = 3
WS_ROUNDS = 24
WS_LIMIT = 1.5
PRELIMINARY_COUNT = 543
REGRESSION_COUNT = 4512
INTERPROCESS = (
    'test_v28_interprocess_writer.py', 'test_hub_multiprocess_delivery.py',
    'test_shared_singleflight_multiprocess.py', 'test_process.py',
)
REPORT_SUFFIXES = {'.json', '.log', '.txt', '.xml', '.png', '.csv'}


def preliminary_files():
    tests = ROOT / 'tests'
    bybit = (p for p in tests.glob('test_ws_*.py')
             if 'poly' not in p.name and 'binance' not in p.name)
    versions = (p for p in tests.glob('test_v[23][0-9]_*.py')
                if p.name not in INTERPROCESS)
    paths = [*bybit, *versions, *tests.glob('test_bybit_ws_*.py'),
             tests / 'test_wal_reader_isolation.py']
    return sorted(p.relative_to(ROOT).as_posix() for p in paths)


def delivery_stats(report, *, symbols):
    rounds = report.get('rounds')
    if not isinstance(rounds, list) or any(not isinstance(batch, list) for batch in rounds):
        raise ValueError('Invalid WS round inventory')
    rows = [row for batch in rounds for row in batch]
    exact = (len(rounds) == WS_ROUNDS
             and all(all(isinstance(row, dict) for row in batch)
                     and sorted(row.get('symbol') or '' for row in batch) == sorted(symbols)
                     for batch in rounds))
    def within_budget(row):
        seconds = row.get('received_in_seconds')
        return type(seconds) in (int, float) and math.isfinite(seconds) and 0 <= seconds <= WS_LIMIT

    return {
        'exact_inventory': exact,
        'measurements': len(rows),
        'successful_within_budget': sum(
            isinstance(row, dict) and row.get('status') == 'PASS' and within_budget(row)
            for row in rows),
        'latency_exceeded': sum(isinstance(row, dict) and not within_budget(row) for row in rows),
    }


def observed_ws_transport(log, counts):
    """Require an observed frame on every lane and no market REST lane."""
    from tools.ws_native_profile import PREFIX

    proven = 0
    ws_only = True
    for line in log.read_text(encoding='utf-8').splitlines():
        if not line.startswith(PREFIX):
            continue
        sample = json.loads(line[len(PREFIX):])
        ws_only &= sample.get('rest_lanes', {}).get('enabled') is False
        quotes = sample.get('streams', {}).get('bybit_quotes', {})
        shards = quotes.get('shards', [])
        if (len(shards) == len(counts)
                and all(shard.get('index') == index
                        and all(shard.get(lane, {}).get('connection') == 'connected'
                                and shard[lane].get('subscribed') == counts[index]
                                and shard[lane].get('received_symbols') == counts[index]
                                for lane in ('primary', 'recovery'))
                        for index, shard in enumerate(shards))):
            proven += 1
    return {'all_lanes_received_samples': proven, 'market_rest_disabled': ws_only and proven > 0}


def ws_evidence(path, manifest):
    from tools.bybit_ws_live_acceptance import SYMBOLS, validate_report
    from tools.ws_native_profile import profile_evidence

    report = json.loads((path / 'live_ws_report.json').read_text(encoding='utf-8'))
    log = path / 'isolated_hub' / 'market_hub.log'
    profile = profile_evidence(log, SYMBOLS)
    stats = delivery_stats(report, symbols=SYMBOLS)
    transport = observed_ws_transport(log, profile['expected_shard_counts'])
    errors = validate_report(report, manifest['manifest_sha256'], require_central=True)
    errors.extend(profile['errors'])
    if not stats['exact_inventory'] or stats['successful_within_budget'] != WS_ROUNDS * len(SYMBOLS):
        errors.append('All 240 measurements, including round one, must succeed within 1.5 s')
    if not transport['market_rest_disabled']:
        errors.append('WS frames on all eight lanes and disabled market REST were not proven')
    result = dict(profile, **transport)
    (path / 'native_ws_profile.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return dict(**stats, profile=result, evidence_errors=errors, ws_status=report.get('status'))


def run_stage(output, args, *, timeout, env):
    from tools.process_guard import run_logged_command

    print(output.name, flush=True)
    output.mkdir()
    with (output / 'console.log').open('w', encoding='utf-8') as log:
        return run_logged_command([sys.executable, *args], cwd=ROOT, stdout=log,
                                  timeout=timeout, env=env)


def accept(output, *, selftest=True):
    from bcc.storage import atomic_json
    from tools import verify_windows
    from tools.ws_native_profile import step_environment

    output.mkdir()
    target = output / 'ACCEPTANCE_SUMMARY.json'
    report = dict(host=platform.platform(), mode='windows', status='RUNNING',
                  stages=[], release_ready=False, manual_acceptance='NOT_CONFIRMED')
    env = dict(os.environ)
    evidence = None

    def stage(name, args, *, timeout, stage_env=env):
        path = output / name
        row = dict(kind=name, status='FAIL', path=name)
        report['stages'].append(row)
        row['exit_code'] = run_stage(path, args, timeout=timeout, env=stage_env)
        return row

    def tests(name, files, *, count=None):
        path = output / name
        junit = path / 'tests.xml'
        row = stage(name, ['-m', 'pytest', '-q', *files, '--junitxml=' + str(junit)],
                    timeout=360, stage_env=dict(env, BCC_TEST_ARTIFACTS=str(path / 'process_checks')))
        row['tests'] = totals = verify_windows.read_test_totals(junit)
        if (row['exit_code'] != 0 or totals['tests'] == 0 or totals['failures']
                or totals['errors'] or totals['skipped']
                or (count is not None and totals['tests'] != count)):
            raise RuntimeError(name + ' failed or has incomplete JUnit evidence')
        row['status'] = 'PASS'
        atomic_json(target, report)

    try:
        if not verify_windows.is_native_windows():
            raise RuntimeError('Native Windows with Qt windows is required')
        report['source_integrity'] = manifest = verify(ROOT)
        if selftest:
            row = stage('HARNESS_SELFTEST_001',
                        ['-m', 'tools.windows_v39_acceptance_selftest'], timeout=60)
            if row['exit_code'] != 0:
                raise RuntimeError('Acceptance harness self-test failed')
            row['status'] = 'PASS'
            atomic_json(target, report)
        for index in range(1, WS_RUNS + 1):
            assert_same_snapshot(manifest, verify(ROOT))
            name = f'WS_PRECHECK_{index:03}'
            evidence = output / name / 'EVIDENCE'
            args = ['-m', 'tools.bybit_ws_live_acceptance', '--output', str(evidence),
                    '--rounds', str(WS_ROUNDS), '--require-mark', '--central-demand']
            row = stage(name, args, timeout=180,
                        stage_env=step_environment(dict(env, PYTHONHASHSEED=str(390100 + index)),
                                                   'bybit_ws', args))
            row.update(ws_evidence(evidence, manifest))
            if row['exit_code'] != 0 or row['evidence_errors']:
                raise RuntimeError(name + ' failed the strict WS gate')
            row['status'] = 'PASS'
            atomic_json(target, report)

        tests('PRELIMINARY', preliminary_files(), count=PRELIMINARY_COUNT)
        tests('INTERPROCESS', ['tests/' + file for file in INTERPROCESS])
        assert_same_snapshot(manifest, verify(ROOT))

        name = 'NATIVE_WINDOWS'
        row = dict(kind=name, path=name, status='FAIL')
        report['stages'].append(row)
        atomic_json(target, report)
        row['exit_code'] = verify_windows.main(
            ['--venue', 'bybit', '--central-demand', '--output', str(output / name)],
            prechecked_ws=evidence,
        )
        native = json.loads((output / name / 'windows_report.json').read_text(encoding='utf-8'))
        row['tests'] = totals = native.get('test_totals')
        if (row['exit_code'] != 0 or not totals or totals['tests'] != REGRESSION_COUNT):
            raise RuntimeError('Full native Windows verification failed or inventory differed')
        report['source_integrity_final'] = assert_same_snapshot(manifest, verify(ROOT))
        row['status'] = 'PASS'
        report['status'] = 'BYBIT_PHASE_AUTOMATED_PASS'
        return 0
    except Exception as exc:
        report.update(status='FAIL_OR_BLOCKED', error=f'{type(exc).__name__}: {exc}')
        print(report['error'], flush=True)
        return 1
    finally:
        atomic_json(target, report)


def isolated_environment(work):
    excluded = ('BCC_', 'PYTEST_', 'QT_')
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(excluded)
           and key.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'PIP_CONFIG_FILE')}
    for key, folder in (('BCC_DATA_ROOT', 'paper'), ('APPDATA', 'roaming'),
                        ('LOCALAPPDATA', 'local'), ('TEMP', 'tmp'), ('TMP', 'tmp')):
        path = work / folder
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update(QT_QPA_PLATFORM='windows', PYTHONIOENCODING='utf-8', PYTHONNOUSERSITE='1',
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PIP_NO_INPUT='1')
    return env


def archive_reports(reports, archive):
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(reports.rglob('*')):
            if (path.is_file() and not path.is_symlink() and path.suffix in REPORT_SUFFIXES
                    and not path.name.endswith('.demand.json')):
                zipped.write(path, path.relative_to(reports).as_posix())


def bootstrap(report_root):
    root = report_root.resolve()
    if root == ROOT or ROOT in root.parents:
        raise ValueError('Place acceptance reports outside the sealed source tree')
    run = root / ('V39_ACCEPTANCE_WINDOWS_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    reports = run / 'REPORTS'
    reports.mkdir(parents=True)
    summary = dict(base_commit=BASE_COMMIT, mode='Windows', source_integrity_pass=False,
                   acceptance_exit=1, release_ready=False, production_deployed=False)
    archive = run.with_suffix('.zip')
    try:
        summary['source_integrity'] = verify(ROOT)
        summary['source_integrity_pass'] = True
        if os.name != 'nt':
            raise RuntimeError('Native Windows is required')
        work = run / 'WORK'
        env = isolated_environment(work)
        venv = work / 'venv'
        python = venv / 'Scripts' / 'python.exe'
        for name, command, timeout in (
            ('environment', [sys.executable, '-m', 'venv', str(venv)], 180),
            ('dependencies', [str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                              '-r', str(ROOT / 'requirements-dev.txt')], 900),
            ('acceptance', [str(python), '-m', 'tools.windows_v39_acceptance',
                            '--prepared-output', str(reports / 'ACCEPTANCE')], None),
        ):
            print(name + '...', flush=True)
            with (reports / (name + '.log')).open('w', encoding='utf-8') as log:
                if name == 'acceptance':
                    with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, encoding='utf-8',
                                          errors='replace') as process:
                        for line in process.stdout:
                            log.write(line)
                            log.flush()
                            print(line, end='', flush=True)
                        code = process.wait()
                else:
                    code = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                          stderr=subprocess.STDOUT, timeout=timeout).returncode
            if code:
                raise RuntimeError(f'{name} exited {code}. See {name}.log')
        summary['acceptance_exit'] = 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        summary['error'] = f'{type(exc).__name__}: {exc}'
        print(summary['error'], flush=True)
    finally:
        (reports / 'BOOTSTRAP_SUMMARY.json').write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
        archive_reports(reports, archive)
        print('Report:', archive, flush=True)
    return summary['acceptance_exit']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--report-root', type=Path)
    mode.add_argument('--prepared-output', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')
    return (bootstrap(args.report_root) if args.report_root is not None
            else accept(args.prepared_output))


if __name__ == '__main__':
    raise SystemExit(main())
