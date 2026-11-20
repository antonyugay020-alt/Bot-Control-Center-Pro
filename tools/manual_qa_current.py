"""Manual acceptance for an explicitly pinned fresh Git checkout."""
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

EXPECTED_MANIFEST = None
CHECKS = [
 ('display', 'Окно, разворачивание и масштаб Windows 125%'),
 ('constructors', 'Все восемь стратегий: создание, переименование, сохранение, повторное открытие и отмена'),
 ('veles_controls', 'VELES: мышью и клавиатурой плечо, маржа и ордера/DCA на обоих шагах'),
 ('coin_overrides', 'Выбор монет, индивидуальные настройки и сброс одной к общим'),
 ('remove_coin_guard', 'Запрет удаления монеты с позицией или ожидающей сеткой'),
 ('sorting', 'Сортировка, фильтрация, дерево и действия по выбранному боту'),
 ('active_config', 'Просмотр работающей конфигурации; запреты редактирования, копирования и переноса'),
 ('monitor_snapshot', 'Монитор: обновление снимка, время, экспорт и неизменная активная конфигурация'),
 ('transfer_full', 'Полный перенос остановленного бота: счёт, позиции, статистика и журналы'),
 ('transfer_clean', 'Перенос только параметров: новый независимый PAPER-счёт'),
 ('delete_guard', 'Удаление остановленного бота без позиций и запрет удаления открытых позиций'),
 ('veles_rsi', 'VELES: одиночная и последовательная RSI-секция, вход 2m, выход 1m и другие интервалы'),
 ('veles_candles', 'VELES: периоды, направления, закрытая свеча и внутри свечи'),
 ('veles_transition', 'VELES: при общем сигнале выход перед новым входом; разные интервалы'),
 ('veles_indicators', 'VELES: другой индикатор, сравнение индикаторов и группа И/ИЛИ'),
 ('veles_dca', 'VELES: три режима DCA, частичное размещение, отступ и подтяжка'),
 ('veles_exits', 'VELES: мульти-тейк, безубыток, трейлинг и оба SL'),
 ('liquidity_risk', 'Тонкая ликвидность: фактический объём и остаток; резервы и убыток ограничивают входы'),
 ('poly_markets', 'Polymarket: рынки 5/15/60 минут, независимые параметры, окно, ASK, комиссия, запрет повтора'),
 ('poly_settlement', 'Polymarket: официальный исход; до исхода нет предполагаемой выплаты'),
 ('poly_limits', 'Polymarket: после сделки, дневной лимит и запрет ручного закрытия'),
 ('pump_both', 'Pump & Dump: реальный импульс и объём, LONG/SHORT, быстрый и подтверждённый отскок'),
 ('pump_exit', 'Pump & Dump: независимые позиции, TP/SL, удержание и cooldown'),
 ('grid_levels', 'GRID: несколько независимых уровней и закрытия, LONG/SHORT'),
 ('grid_risk', 'GRID: свободная маржа, TP/SL и сохранение незакрытых уровней'),
 ('other_live', 'EMA, MACD, Bollinger, EMA+RSI+Volume: реальные входы, выходы, параметры и учёт'),
 ('stop_restart', 'С позицией: Stop/Start и восстановление управления после закрытия/открытия окна'),
 ('running_identity', 'Подтверждённая идентичность worker, переход запуска в работу и доступные команды'),
 ('after_deals', 'После сделок: запрет новых входов, завершение и отмена завершения'),
 ('early_stop', 'Стоп сразу после запуска и отсутствие старой команды при повторном запуске'),
 ('pending_grid', 'Ожидающая сетка при Stop/Start и после сделок; параметры, исполнение/отмена'),
 ('forced_worker', 'Завершение только выбранного worker, восстановление, комиссии и отсутствие повторного входа'),
 ('fleet_isolation', 'Несколько стратегий: независимые счета, сортировка, выбор и история'),
 ('fleet_monitor', 'Общий монитор VELES/GRID при фильтре, действия, закрытие и остановленные боты'),
 ('outage', 'Обрыв только тестового потока: нет устаревших исполнений, выход после восстановления'),
 ('accounting', 'Баланс и события, резервы, нереализованный PnL, funding и завершённые циклы'),
]



def validate_root(root, expected_commit):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('QA folder is missing or is a symbolic link')
    root = root.resolve(strict=True)
    if not root.name.startswith('BCC_NATIVE_QA_CURRENT_'):
        raise ValueError('Choose a freshly created BCC_NATIVE_QA_CURRENT_ folder')
    if len(expected_commit) != 40 or any(c not in '0123456789abcdef' for c in expected_commit):
        raise ValueError('A full lowercase Git commit SHA is required')
    actual = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != expected_commit:
        raise ValueError('Checkout commit differs from the explicitly pinned commit')
    manifest = root / 'SHA256_MANIFEST.json'
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError('Source manifest is missing or linked')
    for name in ('check_workspaces', 'verification_reports'):
        candidate = root / name
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
            raise ValueError('Unsafe QA workspace directory: ' + name)
    return root


def evidence_file(output, name):
    """Accept only a regular file directly in the session evidence folder."""
    if not name or Path(name).name != name or name in ('.', '..'):
        return False
    path = Path(output) / name
    return not path.is_symlink() and path.is_file()


def copy_existing_automated_evidence(root, output):
    """Copy only a source-matched previous QA receipt; never rerun QA."""
    if not EXPECTED_MANIFEST:
        raise ValueError('Verify the source manifest before selecting evidence')
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
                'automated/collected_tests.json', 'automated/tests.xml',
                'automated/public_sources.json', 'automated/screenshots/gui_report.json'):
        src = folder / rel
        if src.is_file() and not src.is_symlink():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            copied.append(rel)
    screenshots = folder / 'automated' / 'screenshots'
    if screenshots.is_dir() and not screenshots.is_symlink():
        for src in sorted(screenshots.glob('*.png')):
            if src.is_file() and not src.is_symlink() and src.stat().st_size <= 20_000_000:
                rel = 'automated/screenshots/' + src.name
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
    global EXPECTED_MANIFEST
    if len(sys.argv) != 3:
        raise ValueError('Pass the fresh QA root and its full pinned commit')
    root = validate_root(sys.argv[1], sys.argv[2])
    sys.path.insert(0, str(root))
    os.chdir(root)
    from tools.verify_package import verify, assert_same_snapshot
    from tools.acceptance_session import AcceptanceSession
    from bcc.support import export_current_report
    from bcc.storage import atomic_json
    import PySide6
    seal = verify(root)
    EXPECTED_MANIFEST = seal['manifest_sha256']
    ident = datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
    workspace = root / 'check_workspaces' / ('manual_' + ident)
    output = root / 'verification_reports' / ('manual_' + ident)
    # AcceptanceSession exclusively creates both paths and authenticates its
    # own guardian, account root and Hub on cleanup. Existing roots are refused.
    child = None
    results = {'source_commit': sys.argv[2],
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
            evidence = ''
            if answer == 'P':
                evidence = input('Имя файла доказательства в папке результатов (Enter = не проверено): ').strip()
                if not evidence_file(output, evidence):
                    print('Доказательство не найдено; критерий остаётся непроверенным.')
                    answer, evidence = '', ''
            note = input('Примечание (необязательно): ').strip() if answer else ''
            results['observations'].append({'id': key, 'operator_result': {'P':'PASS','F':'FAIL','':'NOT_RUN'}[answer], 'note': note, 'evidence': evidence})
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
