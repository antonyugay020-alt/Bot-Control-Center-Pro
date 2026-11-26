"""Prove existing public demand coverage without acquiring the WAL writer.

A receipt confirms a currently active transport lease, never market freshness.
The read snapshot includes the elected leader generation. Mutating requests
recheck everything under BEGIN IMMEDIATE; completed one-shot work is rearmed.
"""
import time


class DemandReceipt(list):
    def __init__(self, keys, *, written_rows, write_batches):
        super().__init__(keys)
        self.written_rows = written_rows
        self.write_batches = write_batches


def _covered(db, row, now):
    key, url, params, lifetime, priority, force_due = row
    current = db.execute(
        'SELECT url,params,active_until,priority_until,next_due,subscription_token '
        'FROM hub_subscriptions WHERE k=?', (key,)).fetchone()
    # A 75% remaining lifetime covers the client's normal renewal cadence
    # (30/20/4s for 90/60/12s), including urgent 5s renewals. Do not renew
    # priority from a non-priority demand; never acknowledge parked one-shots.
    return bool(current and current[:2] == (url, params) and current[5]
                and current[2] >= now + .75*lifetime
                and (not priority or current[3] >= now + .75*min(lifetime*.75,90))
                and (not force_due or current[4] <= now))


def commit_demand_batch(rows, *, connect, write_connect, leader_check, write):
    keys = [row[0] for row in rows]
    # A consistent read transaction proves all receipts at one observation.
    # No process-local lease cache survives a leader change or database reset.
    with connect() as db:
        db.execute('BEGIN')
        now = time.time()
        leader_check(db, now)
        if all(_covered(db, row, now) for row in rows):
            return DemandReceipt(keys, written_rows=0, write_batches=0)
    written = 0
    with write_connect() as db:
        now = time.time()
        leader_check(db, now)
        for row in rows:
            # Preserve order for distinct requests of the same key. Recheck
            # inside the write transaction, including earlier writes here.
            if not _covered(db, row, now):
                write(db, row, now)
                written += 1
    return DemandReceipt(keys, written_rows=written, write_batches=1)
