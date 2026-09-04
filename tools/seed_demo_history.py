#!/usr/bin/env python
"""Seed demo warehouse movement history so the analytics engine has something to model.

WHY THIS EXISTS
    analytics._get_daily_usage forecasts from a 30-day `warehouse_dispatch`
    series. A database whose newest dispatch is months old produces a correct
    but empty forecast: 0.0 demand, infinite days of cover, and exponential
    smoothing instead of the Gradient Boosting path. That is honest, but it
    demonstrates nothing. This script backfills a plausible recent history.

WHAT IT WRITES
    One `warehouse_receive` (+qty) and/or `warehouse_dispatch` (-qty) transaction
    per item per active day, with `previous_quantity` / `new_quantity` forming a
    consistent running ledger. Every row is stamped device_id='demo-seed' and
    carries a 'demo seed' note, so seeded rows are always distinguishable from
    real scans.

    History is generated BACKWARDS from each item's present quantity, so the
    ledger lands exactly on `items.quantity` and the running balance never goes
    negative. `items.quantity` itself is never modified. Both invariants are
    asserted before anything is written.

WHAT IT DOES NOT DO
    It is never invoked by app.py, database.py, or start.ps1 - it only runs when
    you run it. It never deletes or edits an existing row; the audit trail stays
    append-only.

USAGE
    .venv\\Scripts\\python tools\\seed_demo_history.py --dry-run
    .venv\\Scripts\\python tools\\seed_demo_history.py --yes
    .venv\\Scripts\\python tools\\seed_demo_history.py --yes --days 45 --db path\\to.db
"""
import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

DEVICE_ID  = 'demo-seed'
NOTE       = 'demo seed - synthetic history for analytics demonstration'
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          '..', 'backend', 'inventory.db')


def _plan_item(rng, current_qty, days, active_ratio=0.6):
    """Day-by-day (receive, dispatch) plan that ends exactly on `current_qty`.

    Built newest-first: `after` is the balance at the close of the day being
    planned, so the balance at its start is `after - receive + dispatch`. A
    receipt that would drive the opening balance negative is trimmed, which is
    always sufficient - with receive at 0 the opening balance is `after +
    dispatch`, and both terms are non-negative.

    Returns (opening_balance, [(day_index, receive, dispatch), ...]) ordered
    oldest-first.
    """
    plan  = []
    after = max(int(current_qty or 0), 0)

    for day in range(days - 1, -1, -1):
        dispatch = rng.randint(1, 4) if rng.random() < active_ratio else 0
        receive  = rng.randint(4, 12) if rng.random() < 0.20 else 0

        # Opening balance for this day; trim the receipt until it is >= 0.
        before = after - receive + dispatch
        if before < 0:
            receive += before      # `before` is negative, so this shrinks it
            receive = max(receive, 0)
            before = after - receive + dispatch

        if receive or dispatch:
            plan.append((day, receive, dispatch))
        after = before

    plan.reverse()
    return after, plan


def build_rows(conn, days, seed):
    """Transaction rows to insert, oldest-first. Touches nothing."""
    rng = random.Random(seed)
    c = conn.cursor()
    c.execute('SELECT id, quantity FROM items ORDER BY id')
    items = c.fetchall()
    if not items:
        return []

    midnight  = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_day = midnight - timedelta(days=days - 1)

    rows = []
    for item_id, current_qty in items:
        running, plan = _plan_item(rng, current_qty, days)
        for day, receive, dispatch in plan:
            stamp = start_day + timedelta(days=day)
            if receive:
                ts = (stamp + timedelta(hours=8, minutes=rng.randint(0, 59))
                      ).strftime('%Y-%m-%d %H:%M:%S')
                rows.append((item_id, 'warehouse_receive', receive,
                             running, running + receive, ts))
                running += receive
            if dispatch:
                ts = (stamp + timedelta(hours=15, minutes=rng.randint(0, 59))
                      ).strftime('%Y-%m-%d %H:%M:%S')
                rows.append((item_id, 'warehouse_dispatch', -dispatch,
                             running, running - dispatch, ts))
                running -= dispatch
    return rows


def check_rows(conn, rows):
    """Assert the generated ledger is internally consistent. Returns a problem list."""
    c = conn.cursor()
    c.execute('SELECT id, quantity FROM items')
    catalogue = dict(c.fetchall())

    problems, last_new, seen_start = [], {}, {}
    for item_id, action, change, prev, new, _ts in rows:
        if prev + change != new:
            problems.append('%s: %s %+d does not carry %d -> %d'
                            % (item_id, action, change, prev, new))
        if new < 0 or prev < 0:
            problems.append('%s: negative balance (%d -> %d)' % (item_id, prev, new))
        if item_id in last_new and last_new[item_id] != prev:
            problems.append('%s: ledger gap, %d then opens at %d'
                            % (item_id, last_new[item_id], prev))
        seen_start.setdefault(item_id, prev)
        last_new[item_id] = new

    for item_id, end in last_new.items():
        want = catalogue.get(item_id, 0) or 0
        if end != want:
            problems.append('%s: ledger ends at %d but catalogue says %d'
                            % (item_id, end, want))
    return problems


def existing_seed_count(conn):
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM transactions WHERE device_id = ?', (DEVICE_ID,))
    return c.fetchone()[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=DEFAULT_DB, help='SQLite file (default: backend/inventory.db)')
    ap.add_argument('--days', type=int, default=30, help='days of history to generate (default: 30)')
    ap.add_argument('--seed', type=int, default=20260904, help='RNG seed, for reproducible output')
    ap.add_argument('--dry-run', action='store_true', help='print a summary and write nothing')
    ap.add_argument('--yes', action='store_true', help='confirm the write (required without --dry-run)')
    ap.add_argument('--force', action='store_true', help='seed again even if seeded rows already exist')
    args = ap.parse_args(argv)

    db = os.path.abspath(args.db)
    if not os.path.exists(db):
        print('No database at %s - start the backend once to create it.' % db)
        return 2

    conn = sqlite3.connect(db)
    try:
        already = existing_seed_count(conn)
        if already and not args.force:
            print('This database already holds %d demo-seed rows.' % already)
            print('Seeding again would double the history. Re-run with --force if that is intended.')
            return 1

        rows = build_rows(conn, args.days, args.seed)
        if not rows:
            print('No items in the catalogue - nothing to seed.')
            return 1

        problems = check_rows(conn, rows)
        dispatches = [r for r in rows if r[1] == 'warehouse_dispatch']
        receipts   = [r for r in rows if r[1] == 'warehouse_receive']
        print('Database : %s' % db)
        print('Window   : %d days ending today' % args.days)
        print('Items    : %d' % len({r[0] for r in rows}))
        print('Rows     : %d dispatch (%d units), %d receive (%d units)' % (
            len(dispatches), sum(-r[2] for r in dispatches),
            len(receipts), sum(r[2] for r in receipts)))
        print('Ledger   : %s' % ('consistent' if not problems else '%d PROBLEMS' % len(problems)))

        if problems:
            for p in problems[:10]:
                print('   !! %s' % p)
            print('\nRefusing to write an inconsistent ledger.')
            return 3

        if args.dry_run:
            print('\n--dry-run: nothing written.')
            return 0
        if not args.yes:
            print('\nRefusing to write without --yes. Re-run with --yes to insert these rows.')
            return 1

        conn.executemany(
            """INSERT INTO transactions
                 (item_id, action, quantity_change, previous_quantity, new_quantity,
                  performed_by, device_id, note, timestamp)
               VALUES (?, ?, ?, ?, ?, 'demo-seed', ?, ?, ?)""",
            [(r[0], r[1], r[2], r[3], r[4], DEVICE_ID, NOTE, r[5]) for r in rows])
        conn.commit()
        print('\nInserted %d transactions. items.quantity was not modified.' % len(rows))
        print('Reload the dashboard Analytics tab to see the Gradient Boosting forecast.')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
