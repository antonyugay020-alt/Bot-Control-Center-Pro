"""Execute the entire native Windows pytest inventory in isolated bounded shards.

One slow integration case must not erase the JUnit for 900+ other tests.
All inventory cases must appear exactly once in the merged JUnit; no partial
regression can ever return PASS. This runner has no trading capability.
"""
import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.process_guard import run_logged_command
from tools.verify_windows import read_test_totals, verify_complete_test_inventory
from bcc.runtime_paths import python_environment


# Qt belongs to the mandatory Windows inventory. Linux can explicitly
# request an independent non-Qt regression, but the native default must still
# reject a report that omits even one Qt module.
HEADLESS_QT_FILES = frozenset({
    'test_qt.py', 'test_poly_indicator_qt.py', 'test_veles_hierarchy_qt.py',
    'test_wizard_visual_qt.py', 'test_settings_tree_contrast.py',
    'test_native_dashboard_workflow_qt.py',
})


def partitions(inventory, groups=6, *, excluded=()):
    data = json.loads(Path(inventory).read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not isinstance(data.get('cases'), list):
        raise ValueError('Invalid test inventory')
    modules = Counter()
    for entry in data['cases']:
        if (not isinstance(entry, list) or len(entry) != 2
                or not all(isinstance(x, str) for x in entry)
                or not entry[0].startswith('tests.test_')):
            raise ValueError('Unexpected test identity: ' + repr(entry))
        module = entry[0].split('.')[1]
        if not module.startswith('test_'):
            raise ValueError('Test file name invalid')
        modules[module + '.py'] += 1
    tests = sorted((ROOT / 'tests').rglob('test_*.py'))
    actual = {str(p.relative_to(ROOT / 'tests')).replace('\\', '/') for p in tests}
    excluded = set(excluded)
    if excluded - actual:
        raise ValueError('Unknown excluded test modules: ' + repr(excluded - actual))
    # The native collection includes Qt even when a Linux-only headless run
    # explicitly excludes it. Compare *after* applying that same exclusion
    # to both sides: otherwise --headless cannot run from a full inventory.
    # With excluded=() (the Windows default), no omissions are permitted.
    actual -= excluded
    for omitted in excluded:
        modules.pop(omitted, None)
    if set(modules) != actual:
        raise ValueError('Test file inventory differs: ' + repr((set(modules)-actual, actual-set(modules))))
    # One high-volume SQLite replay has a separate wall clock budget. Keep it
    # in the inventory and actually execute it; never silently exclude it.
    costly = 'test_veles_resource_scaled_accounts_replay.py'
    # OS-backed replay is sensitive to inherited pytest fixtures and process
    # handles. Run this whole module in its own disposable pytest process,
    # never in the same shard as unrelated tests. The exact test inventory is
    # still mandatory and the isolation cannot turn a timeout into PASS.
    process_module = 'test_process.py'
    buckets = [[] for _ in range(groups)]
    weights = [0] * groups
    for name, weight in sorted(modules.items(), key=lambda kv: (-kv[1], kv[0])):
        if name in (costly, process_module):
            continue
        idx = weights.index(min(weights))
        buckets[idx].append(name)
        weights[idx] += weight
    result = [sorted(bucket) for bucket in buckets if bucket]
    if process_module in modules:
        # Exercise the OS lifecycle before unrelated modules can accumulate
        # shared-state or orphaned worker side effects.
        result.insert(0, [process_module])
    if costly in modules:
        result.append([costly])
    if sorted(x for bucket in result for x in bucket) != sorted(modules):
        raise ValueError('Some pytest files were not scheduled')
    return result


def read_shard_junit(path, inventory, module_files):
    """Read complete assigned evidence, retaining failures for diagnostics.

    Incomplete, duplicated and foreign cases still cannot enter the merged
    inventory. Execution success is checked separately by validate_shard_junit.
    """
    collected = json.loads(Path(inventory).read_text(encoding='utf-8'))
    module_names = {'tests.' + filename[:-3] for filename in module_files}
    expected = Counter(tuple(case) for case in collected['cases'] if case[0].split('.')[0] == 'tests'
                       and case[0].split('.')[1] in {filename[:-3] for filename in module_files})
    if not expected or any(n != 1 for n in expected.values()):
        raise ValueError('Shard assignment has empty or duplicated collected test identities')
    totals = read_test_totals(path)
    parsed = ET.parse(path).getroot()
    suites = [parsed] if parsed.tag == 'testsuite' else parsed.findall('testsuite')
    actual = Counter((case.get('classname'), case.get('name'))
                     for suite in suites for case in suite.findall('testcase'))
    if expected != actual:
        missing, extra = expected - actual, actual - expected
        raise ValueError(f'Shard JUnit inventory mismatch: missing={sum(missing.values())}, '
                         f'unexpected_or_duplicate={sum(extra.values())}')
    return totals, suites


def validate_shard_junit(path, inventory, module_files, *, headless=False):
    totals, suites = read_shard_junit(path, inventory, module_files)
    if totals['failures'] or totals['errors']:
        raise ValueError('Shard JUnit contains failed or erroneous tests')
    if totals['skipped']:
        if headless:
            raise ValueError('Skipped headless regression cases cannot pass')
        from tools.verify_windows import ALLOWED_WINDOWS_SKIPS
        for suite in suites:
            for case in suite.findall('testcase'):
                if case.findall('skipped'):
                    module = (case.get('classname') or '').rsplit('.', 1)[-1]
                    testname = (case.get('name') or '').split('[', 1)[0]
                    if (module, testname) not in ALLOWED_WINDOWS_SKIPS:
                        raise ValueError('Unexpected skipped native regression case')
    return totals


def combine_junit(paths, output):
    root = ET.Element('testsuites')
    for path in paths:
        parsed = ET.parse(path).getroot()
        suites = ([parsed] if parsed.tag == 'testsuite' else
                  parsed.findall('testsuite') if parsed.tag == 'testsuites' else [])
        if not suites:
            raise ValueError('Shard JUnit missing suites: ' + str(path))
        for suite in suites:
            # Reuse original testcases and actual suite counts.
            root.append(suite)
    temp = output.with_suffix('.xml.tmp')
    ET.ElementTree(root).write(temp, encoding='utf-8', xml_declaration=True)
    temp.replace(output)
    return read_test_totals(output)


def run(inventory, output, *, shard_timeout=360, heavy_timeout=600, headless=False):
    if headless and os.name == 'nt':
        raise ValueError('Headless test exclusions are forbidden in native Windows QA')
    output = Path(output).resolve()
    inventory = Path(inventory).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Refusing to reuse QA shards from a prior run')
    output.mkdir(parents=True, exist_ok=True)
    schedule = partitions(inventory, excluded=HEADLESS_QT_FILES if headless else ())
    outcome = {'schema': 1, 'status': 'FAIL', 'shards': [], 'inventory': str(inventory)}
    xmls = []
    all_good = True
    for idx, files in enumerate(schedule):
        name = f'shard_{idx:02d}'
        budget = heavy_timeout if 'test_veles_resource_scaled_accounts_replay.py' in files else shard_timeout
        xml = output / (name + '.xml')
        log = output / (name + '.log')
        args = [sys.executable, '-X', 'faulthandler', '-m', 'pytest', '-vv',
                '-o', 'faulthandler_timeout=60', '--durations=15',
                *('tests/' + f for f in files), '--junitxml=' + str(xml)]
        env = python_environment()
        env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        env['BCC_TEST_ARTIFACTS'] = str(output / (name + '_process_checks'))
        if env.get('BCC_QA_TEXT_ENCODING'):
            args.extend(['-p', 'tools.qa_locale'])
        started = time.monotonic()
        print(f'{name}: {len(files)} modules, timeout {budget}s', flush=True)
        with log.open('w', encoding='utf-8') as stream:
            try:
                code = run_logged_command(args, cwd=ROOT, stdout=stream, timeout=budget, env=env)
                error = None
            except Exception as exc:
                code = -1
                error = type(exc).__name__ + ': ' + str(exc)
        elapsed = round(time.monotonic() - started, 3)
        totals = None
        try:
            if xml.is_file():
                totals, _ = read_shard_junit(xml, inventory, files)
                xmls.append(xml)
                # Preserve complete failed evidence for the merged report.
                # Structural/inventory validity does not imply test success.
                validate_shard_junit(xml, inventory, files, headless=headless)
        except (ValueError, ET.ParseError) as exc:
            error = (error or '') + ' invalid JUnit ' + str(exc)
        passed = code == 0 and error is None and totals and not (totals['failures'] or totals['errors'])
        all_good &= bool(passed)
        outcome['shards'].append({'name': name, 'files': files, 'seconds': elapsed,
                                  'timeout': budget, 'exit_code': code, 'error': error,
                                  'totals': totals, 'status': 'PASS' if passed else 'FAIL'})
        (output / 'status.json').write_text(json.dumps(outcome, indent=2), encoding='utf-8')
    try:
        if len(xmls) == len(schedule):
            junit = output.parent / 'tests.xml'
            totals = combine_junit(xmls, junit)
            count = verify_complete_test_inventory(junit, inventory)
            outcome['totals'] = totals
            outcome['inventory_count'] = count
            all_good &= totals['tests'] == count and totals['failures'] == 0 and totals['errors'] == 0
        else:
            all_good = False
            outcome['error'] = 'One or more shards did not produce a valid JUnit file'
    except (OSError, ValueError, ET.ParseError) as exc:
        all_good = False
        outcome['error'] = type(exc).__name__ + ': ' + str(exc)
    outcome['status'] = 'PASS' if all_good else 'FAIL'
    (output / 'status.json').write_text(json.dumps(outcome, indent=2), encoding='utf-8')
    print(f"Sharded QA: {outcome['status']}", flush=True)
    return 0 if all_good else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--headless', action='store_true',
                        help='Linux-only: explicitly exclude native Qt modules')
    args = parser.parse_args()
    return run(args.inventory, args.output, headless=args.headless)


if __name__ == '__main__':
    raise SystemExit(main())
