"""Pure market-availability projection for the UI; never confuses PID with market health."""
import time


def record_market_outcome(health, symbol, *, quote=None, history=None):
    """Track recovery per symbol/channel; cumulative errors remain evidence."""
    unavailable = health.setdefault('unavailable_symbols',{})
    row = unavailable.setdefault(symbol,{})
    before = dict(row)
    for channel,error in (('quote',quote),('history',history)):
        if error is None:
            continue  # No observation of this channel, not proof of recovery.
        if error:
            row[channel] = str(error)[:500]
            # Time of the actual failed observation; a persisted error by
            # itself cannot establish when a source last failed.
            health.setdefault('last_failed_utc', {})[channel] = time.time()
        else:
            if channel in row:
                health.setdefault('last_recovered_utc', {})[channel] = time.time()
            row.pop(channel,None)
    if not row:
        unavailable.pop(symbol,None)
    return row != before


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
    current = health.get('unavailable_symbols')
    has_errors = bool(current) if isinstance(current,dict) else bool(health.get('market_errors'))
    if not has_errors:
        return status
    recent = health.get("last_completed_utc")
    if type(recent) not in (int, float) or (now if now is not None else time.time()) - recent > 15:
        return "Работает · нет свежих рыночных данных"
    return "Работает · ошибки рыночных данных"
