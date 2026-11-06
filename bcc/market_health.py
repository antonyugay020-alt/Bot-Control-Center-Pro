"""Pure market-availability projection for the UI; never confuses PID with market health."""
import time


def display_market_status(status, process, now=None):
    if status != "Работает" or not isinstance(process, dict):
        return status
    health = process.get("market_health")
    if not isinstance(health, dict):
        return status
    pressure = health.get('scheduler_pressure')
    if (isinstance(pressure, dict) and pressure.get('entry_admission_paused', pressure.get('scan_paused'))
            and pressure.get('unmarked_priority', 0) > 0):
        return 'Работает · нет свежих цен открытых позиций; новые входы приостановлены'
    if not health.get('market_errors'):
        return status
    recent = health.get("last_completed_utc")
    if type(recent) not in (int, float) or (now if now is not None else time.time()) - recent > 15:
        return "Работает · нет свежих рыночных данных"
    return "Работает · ошибки рыночных данных"
