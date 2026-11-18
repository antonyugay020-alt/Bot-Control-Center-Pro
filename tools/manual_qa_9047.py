"""Manual BCC 9047 acceptance: fresh QA-owned account, existing sealed source."""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_MANIFEST = 'c33fce8df18741a7409dfb6fdf18f85fb4f7678096c9174e78ad6337c81ae9b0'
EXPECTED_NAME = 'BCC_NATIVE_QA_9047b10'
CHECKS = [
 ('windows_display', 'Окно, разворачивание и масштаб 125%'),
 ('constructors', 'Создание/редактирование/повторное открытие всех восьми стратегий'),
 ('coin_settings', 'Монеты, секции, VELES: процент и фиксированные USDT, отмена'),
 ('monitor_transfer', 'Мониторинг, сортировка, копии, импорт/экспорт и ограничения работающего бота'),
 ('veles_live', 'VELES: публичные данные, RSI, переходы, DCA и выходы'),
 ('grid_live', 'GRID: реальные PAPER-входы/выходы, уровни, LONG/SHORT'),
 ('pump_live', 'Pump & Dump: реальные PAPER-импульсы, LONG/SHORT и выходы'),
 ('polymarket_live', 'Polymarket: публичный рынок, вход и официальный исход'),
 ('other_live', 'EMA, MACD, Bollinger, EMA+RSI+Volume: реальные входы и выходы'),
 ('restart', 'С открытой позицией: Stop/Start, закрытие/открытие окна, восстановление'),
 ('stop_modes', 'Stop при запуске, после сделок, ожидающая сетка и отмена завершения'),
 ('outage', 'Обрыв и восстановление только тестового потока, без устаревших исполнений'),
 ('accounting', 'Баланс, резервы, комиссии, PnL и funding сверены'),
]


def validate_root(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('QA folder is missing or is a symbolic link')
    root = root.resolve(strict=True)
    if root.name.casefold() != EXPECTED_NAME.casefold():
        raise ValueError('Choose the existing BCC_NATIVE_QA_9047b10 folder')
    manifest = root / 'SHA256_MANIFEST.json'
    if manifest.is_symlink() or hashlib.sha256(manifest.read_bytes()).hexdigest() != EXPECTED_MANIFEST:
        raise ValueError('Source manifest does not match 9047b10; refusing to launch')
    for name in ('check_workspaces', 'verification_reports'):
        candidate = root / name
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
            raise ValueError('Unsafe QA workspace directory: ' + name)
    return root


def copy_existing_automated_evidence(root, output):
    """Copy only a source-matched previous QA receipt; never rerun QA."""
    reports = root / 'verification_reports'
    matches = []
    if reports.is_dir() and not reports.is_symlink():
        for folder in reports.iterdir():
            if folder.is_symlink() or not folder.is_dir() or not folder.name.startswith('check_'):
                continue
            summary = folder / 'check_summary.json'
            automated = folder / 'automated' / 'windows_report.json'
            if (summary.is_symlink() or automated.is_symlink()
                    or (folder / 'automated').is_symlink()):
                continue
            try:
                if summary.stat().st_size > 1000000 or automated.stat().st_size > 1000000:
                    continue
                one = json.loads(summary.read_text(encoding='utf-8'))
                two = json.loads(automated.read_text(encoding='utf-8'))
                if (one.get('source_integrity', {}).get('manifest_sha256') != EXPECTED_MANIFEST
                        or one.get('source_integrity_final', {}).get('manifest_sha256') != EXPECTED_MANIFEST
                        or two.get('source_integrity', {}).get('manifest_sha256') != EXPECTED_MANIFEST
                        or two.get('source_integrity_final', {}).get('manifest_sha256') != EXPECTED_MANIFEST):
                    continue
                matches.append((summary.stat().st_mtime_ns, folder, one, two))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    if not matches:
        return {'status': 'NOT_FOUND', 'meaning': 'Prior Windows QA has not been independently verified by this collector'}
    _, folder, summary, report = max(matches, key=lambda item: item[0])
    dest = output / 'automated_qa'
    dest.mkdir()
    copied = []
    for rel in ('check_summary.json', 'automated/windows_report.json',
                'automated/collected_tests.json', 'automated/tests.xml'):
        src = folder / rel
        if src.is_file() and not src.is_symlink():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            copied.append(rel)
    return {'status': 'COPIED', 'files': copied, 'reported_status': summary.get('status'),
            'reported_test_totals': report.get('test_totals'),
            'meaning': 'Copied prior receipt, not an independent re-execution or acceptance'}


def main():
    if os.name != 'nt':
        raise RuntimeError('This interactive acceptance must run on native Windows')
    if len(sys.argv) != 2:
        raise ValueError('Pass the path to BCC_NATIVE_QA_9047b10')
    root = validate_root(sys.argv[1])
    sys.path.insert(0, str(root))
    os.chdir(root)
    from tools.verify_package import verify, assert_same_snapshot
    from tools.acceptance_session import AcceptanceSession
    from bcc.support import export_current_report
    from bcc.storage import atomic_json
    import PySide6
    seal = verify(root)
    if seal['manifest_sha256'] != EXPECTED_MANIFEST:
        raise ValueError('Unexpected verified manifest')
    ident = datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
    workspace = root / 'check_workspaces' / ('manual_' + ident)
    output = root / 'verification_reports' / ('manual_' + ident)
    # AcceptanceSession exclusively creates both paths and authenticates its
    # own guardian, account root and Hub on cleanup. Existing roots are refused.
    child = None
    results = {'source_commit': '9047b10df730273b05eb4f2e0513cbf9ffeee3f1',
               'source_integrity': seal, 'platform': platform.platform(),
               'python': sys.version, 'pyside6': PySide6.__version__,
               'started_utc': datetime.now(timezone.utc).isoformat(),
               'manual_acceptance': 'NOT_CONFIRMED', 'release_ready': False,
               'workspace': str(workspace), 'observations': [], 'captures': [], 'errors': []}
    def save():
        atomic_json(output / 'manual_results.json', results)
    def capture():
        assert_same_snapshot(seal, verify(root))
        name = 'snapshot_' + datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6] + '.zip'
        report = export_current_report(workspace, output / name, allow_empty=True,
                                      session_info={'manual_qa': True, 'read_only': True})
        results['captures'].append({'file': name, 'bot_count': report['bot_count'],
                                    'errors': report['errors'], 'at_utc': datetime.now(timezone.utc).isoformat()})
        save()
        print('Сохранено:', output / name, flush=True)
    def launch():
        assert_same_snapshot(seal, verify(root))
        log = (output / 'qt_application.log').open('ab')
        try:
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            for k in ('BCC_DATA_ROOT', 'BCC_PUBLIC_CACHE', 'BCC_MARKET_HUB_MODE',
                      'BCC_BYBIT_WS_QUOTES', 'BCC_BYBIT_WS_BOOKS'):
                env.pop(k, None)
            return subprocess.Popen([sys.executable, str(root / 'app.py'), '--data-root', str(workspace)],
                                    cwd=str(root), env=env, stdout=log, stderr=subprocess.STDOUT)
        finally:
            log.close()
    with AcceptanceSession(workspace, output, python=sys.executable):
        results['prior_automated_qa'] = copy_existing_automated_evidence(root, output)
        print('\nТолько новая QA-папка счётов:', workspace)
        print('Все результаты сохраняются в:', output)
        print('Откройте ACCEPTANCE_WINDOWS_RU.md в папке программы. Только PAPER, без ключей и реальных ордеров.')
        print('Скриншоты важных состояний сохраняйте вручную в папку результатов.')
        child = launch()
        save()
        while True:
            cmd = input('\nS — сохранить снимок; R — открыть окно снова; Q — закончить: ').strip().upper()
            if cmd == 'S':
                try:
                    capture()
                except Exception as exc:
                    results['errors'].append({'stage': 'capture', 'error': str(exc)})
                    save()
                    print('Ошибка снимка:', exc)
            elif cmd == 'R':
                if child.poll() is None:
                    print('Сначала закройте текущее окно штатно.')
                else:
                    results.setdefault('qt_exits', []).append(child.returncode)
                    child = launch()
            elif cmd == 'Q':
                if child.poll() is None:
                    print('Сначала закройте окно штатно. Для сохранения ботов выберите соответствующее действие приложения.')
                    continue
                results.setdefault('qt_exits', []).append(child.returncode)
                try:
                    capture()
                except Exception as exc:
                    results['errors'].append({'stage': 'final_capture', 'error': str(exc)})
                break
        print('\nОтметьте только фактически выполненное: P — прошло, F — ошибка, Enter — не проверено.')
        for key, title in CHECKS:
            while True:
                answer = input(title + ' [P/F/Enter]: ').strip().upper()
                if answer in ('P', 'F', ''):
                    break
            note = input('Примечание (необязательно): ').strip() if answer else ''
            results['observations'].append({'id': key, 'operator_result': {'P':'PASS','F':'FAIL','':'NOT_RUN'}[answer], 'note': note})
            save()
    # Exit of AcceptanceSession gracefully stops only session-owned PAPER
    # workers and identity-verified Hub; it never touches another workspace.
    results['source_integrity_final'] = assert_same_snapshot(seal, verify(root))
    results['finished_utc'] = datetime.now(timezone.utc).isoformat()
    results['session_cleanup'] = json.loads((output / 'session.json').read_text(encoding='utf-8'))
    try:
        capture()  # Capture final ledger after owned QA workers have stopped.
    except Exception as exc:
        results['errors'].append({'stage': 'post_stop_capture', 'error': str(exc)})
    save()
    archive = output / 'BCC_MANUAL_QA_RESULTS.zip'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as package:
        for file in sorted(output.rglob('*')):
            if file.is_symlink():
                raise RuntimeError('Linked evidence path rejected: ' + str(file))
            if file.is_file() and file != archive:
                package.write(file, file.relative_to(output))
    print('\nГОТОВО. Отправьте папку целиком:', output)
    print('Или отправьте готовый архив:', archive)
    print('Ручные отметки требуют проверки; этот сценарий не объявляет релиз готовым.')


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        traceback.print_exc()
        print('Проверка не завершена. Существующие папки ботов не изменялись этим сценарием.')
        raise
