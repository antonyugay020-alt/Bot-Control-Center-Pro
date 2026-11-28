"""Small offline contract checks for the Windows V39 acceptance harness."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools import verify_windows
from tools import windows_v39_acceptance as runner

SYMBOLS = tuple(f'{index}USDT' for index in range(10))


class AcceptanceHarnessTests(unittest.TestCase):
    def test_240_on_boundary_is_complete(self):
        rows = [{'symbol': symbol, 'status': 'PASS', 'received_in_seconds': 1.5}
                for symbol in SYMBOLS]
        report = {'rounds': [rows] * 24}
        self.assertEqual(runner.delivery_stats(report, symbols=SYMBOLS),
                         {'exact_inventory': True, 'measurements': 240,
                          'successful_within_budget': 240, 'latency_exceeded': 0})

    def test_first_round_is_subject_to_same_deadline(self):
        rows = [{'symbol': symbol, 'status': 'PASS', 'received_in_seconds': 1.1}
                for symbol in SYMBOLS]
        first = [dict(rows[0], received_in_seconds=1.501), *rows[1:]]
        delivery = runner.delivery_stats({'rounds': [first] + [rows] * 23}, symbols=SYMBOLS)
        self.assertEqual(delivery['successful_within_budget'], 239)
        self.assertEqual(delivery['latency_exceeded'], 1)

    def test_duplicates_cannot_certify_inventory(self):
        rows = [{'symbol': symbol, 'status': 'PASS', 'received_in_seconds': 1.0}
                for symbol in SYMBOLS]
        rows[-1] = rows[0]
        delivery = runner.delivery_stats({'rounds': [rows] * 24}, symbols=SYMBOLS)
        self.assertFalse(delivery['exact_inventory'])

    def test_rest_and_received_lane_checks(self):
        lane = dict(connection='connected', received_symbols=1, subscribed=1)
        sample = dict(rest_lanes={'enabled': False}, streams={'bybit_quotes': {
            'shards': [dict(index=0, primary=dict(lane), recovery=dict(lane))],
        }})
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'market_hub.log'
            def replay():
                log.write_text('Market Hub WS health: ' + json.dumps(sample) + '\n', encoding='utf-8')
                return runner.observed_ws_transport(log, [1])

            self.assertTrue(replay()['market_rest_disabled'])
            sample['rest_lanes']['enabled'] = True
            self.assertFalse(replay()['market_rest_disabled'])
            sample['rest_lanes']['enabled'] = False
            sample['streams']['bybit_quotes']['shards'][0]['recovery']['received_symbols'] = 0
            self.assertEqual(replay()['all_lanes_received_samples'], 0)

    def test_failed_ws_stops_before_preliminary(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'report'
            names = []
            def run_stage(path, _args, **_kwargs):
                names.append(path.name)
                if path.name == 'WS_PRECHECK_001':
                    return 1
                return 0

            with (patch.object(verify_windows, 'is_native_windows', return_value=True),
                  patch.object(runner, 'verify', return_value={'manifest_sha256': 'fixture'}),
                  patch.object(runner, 'run_stage', side_effect=run_stage),
                  patch.object(runner, 'ws_evidence', return_value={'evidence_errors': ['late']})):
                self.assertEqual(runner.accept(output, selftest=False), 1)
            self.assertEqual(names, ['WS_PRECHECK_001'])
            report = json.loads((output / 'ACCEPTANCE_SUMMARY.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'FAIL_OR_BLOCKED')
            self.assertEqual(report['stages'][0]['status'], 'FAIL')

    def test_archive_contains_reports_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reports = root / 'REPORTS'
            reports.mkdir()
            for relative in ('ACCEPTANCE_SUMMARY.json', 'checks.xml', 'market_cache.sqlite',
                             'market_cache.sqlite.demand.json', '.env', 'transfer.zip'):
                (reports / relative).touch()
            (root / 'outside.log').touch()
            destination = root / 'report.zip'
            runner.archive_reports(reports, destination)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.namelist(), ['ACCEPTANCE_SUMMARY.json', 'checks.xml'])
                self.assertIsNone(archive.testzip())


if __name__ == '__main__':
    unittest.main()
