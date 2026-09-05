#!/usr/bin/env python
"""Populate the database with a coherent demo world, so every tab shows real work.

WHY THIS EXISTS
    A fresh install has 10 catalogue items, one RFID tag and no cartons, pallets,
    purchase orders or recent movement. Most of the dashboard is therefore empty,
    and the analytics engine - which forecasts from a 30-day `warehouse_dispatch`
    series - correctly reports zero demand and infinite cover for everything.
    That is honest but demonstrates nothing. This backfills a plausible world.

WHAT IT WRITES
    1. Ledger      warehouse_receive / warehouse_dispatch transactions across the
                   last N days, arranged to land EXACTLY on each item's current
                   quantity so `items.quantity` is never modified.
    2. Tags        unit tags spread across the pipeline states, with rack
                   locations and last-scan times.
    3. Cartons     cartons of a single SKU, some loaded onto pallets.
    4. Pallets     pallets holding those cartons.
    5. Orders      purchase orders in open / partial / closed states.
    6. Workers     one active station session, plus last-seen times.
    7. Jobs        tag-writing batches, completed and pending.
    8. Alerts      a spread of low-stock, out-of-stock and security alerts.

TRACEABILITY
    Every seeded transaction carries device_id='demo-seed'. Seeded RFID tags,
    cartons and pallets use the DEMO_PREFIX below, so synthetic records stay
    distinguishable from real scans at a glance and `--status` can count them.

SAFETY
    Never invoked by app.py, database.py or start.ps1 - it runs only when you run
    it. It requires --yes, refuses to seed twice without --force, never edits or
    deletes an existing row, and refuses outright to write a ledger that does not
    reconcile.

USAGE
    .venv\\Scripts\\python tools\\seed_demo.py --status
    .venv\\Scripts\\python tools\\seed_demo.py --dry-run
    .venv\\Scripts\\python tools\\seed_demo.py --yes
"""
import argparse
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

DEVICE_ID   = 'demo-seed'
NOTE        = 'demo seed - synthetic history for demonstration'
DEMO_PREFIX = 'DE'          # seeded tag UIDs start with this
DEFAULT_DB  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '..', 'backend', 'inventory.db')

RACK_ROWS = ['A', 'B', 'C', 'D']
STATIONS  = ['esp32-01', 'esp32-02', 'esp32-03', 'esp32-04']


def _utcnow():
    """Naive UTC now.

    SQLite's CURRENT_TIMESTAMP is UTC and the dashboard renders stored
    timestamps as UTC, so everything seeded here must match or it appears
    hours in the future.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── 1. Ledger ─────────────────────────────────────────────────────────────────

def _plan_item(rng, current_qty, days, active_ratio=0.6):
    """Day-by-day (receive, dispatch) plan that ends exactly on `current_qty`.

    Built newest-first: `after` is the balance at the close of the day being
    planned, so the balance at its start is `after - receive + dispatch`. A
    receipt that would drive the opening balance negative is trimmed, which is
    always sufficient - with receive at 0 the opening balance is
    `after + dispatch`, and both terms are non-negative.
    """
    plan  = []
    after = max(int(current_qty or 0), 0)

    for day in range(days - 1, -1, -1):
        dispatch = rng.randint(1, 4) if rng.random() < active_ratio else 0
        receive  = rng.randint(4, 12) if rng.random() < 0.20 else 0

        before = after - receive + dispatch
        if before < 0:
            receive += before               # `before` is negative, so this shrinks it
            receive = max(receive, 0)
            before = after - receive + dispatch

        if receive or dispatch:
            plan.append((day, receive, dispatch))
        after = before

    plan.reverse()
    return after, plan


def build_ledger(conn, days, rng):
    """Transaction rows to insert, oldest-first. Touches nothing."""
    c = conn.cursor()
    c.execute('SELECT id, quantity FROM items ORDER BY id')
    items = c.fetchall()
    if not items:
        return []

    # UTC, to match SQLite's CURRENT_TIMESTAMP. The dashboard renders stored
    # timestamps as UTC, so local times would appear hours in the future.
    midnight  = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    start_day = midnight - timedelta(days=days - 1)

    now = _utcnow()

    def slots(stamp):
        """Receive and dispatch times for one day, always in the past.

        Past days use a fixed 08:00 dock slot and 15:00 despatch slot. Today's
        would otherwise sit in the future whenever the script runs before 15:00
        UTC, which shows up as tomorrow's scans in the dashboard, so today is
        placed relative to now instead - receive first, dispatch after.
        """
        if stamp.date() < now.date():
            return (stamp + timedelta(hours=8, minutes=rng.randint(0, 59)),
                    stamp + timedelta(hours=15, minutes=rng.randint(0, 59)))
        recv = now - timedelta(minutes=rng.randint(150, 240))
        disp = now - timedelta(minutes=rng.randint(10, 100))
        if recv >= disp:
            recv = disp - timedelta(minutes=5)
        return recv, disp

    rows = []
    for item_id, current_qty in items:
        running, plan = _plan_item(rng, current_qty, days)
        for day, receive, dispatch in plan:
            recv_at, disp_at = slots(start_day + timedelta(days=day))
            if receive:
                rows.append((item_id, 'warehouse_receive', receive, running,
                             running + receive,
                             recv_at.strftime('%Y-%m-%d %H:%M:%S')))
                running += receive
            if dispatch:
                rows.append((item_id, 'warehouse_dispatch', -dispatch, running,
                             running - dispatch,
                             disp_at.strftime('%Y-%m-%d %H:%M:%S')))
                running -= dispatch
    return rows


def check_ledger(conn, rows):
    """Assert the generated ledger is internally consistent. Returns problems."""
    c = conn.cursor()
    c.execute('SELECT id, quantity FROM items')
    catalogue = dict(c.fetchall())

    problems, last_new = [], {}
    for item_id, action, change, prev, new, _ts in rows:
        if prev + change != new:
            problems.append('%s: %s %+d does not carry %d -> %d'
                            % (item_id, action, change, prev, new))
        if new < 0 or prev < 0:
            problems.append('%s: negative balance (%d -> %d)' % (item_id, prev, new))
        if item_id in last_new and last_new[item_id] != prev:
            problems.append('%s: ledger gap, %d then opens at %d'
                            % (item_id, last_new[item_id], prev))
        last_new[item_id] = new

    for item_id, end in last_new.items():
        want = catalogue.get(item_id, 0) or 0
        if end != want:
            problems.append('%s: ledger ends at %d but catalogue says %d'
                            % (item_id, end, want))
    return problems


# ── 2-8. The physical world ───────────────────────────────────────────────────

def _uid(rng):
    return DEMO_PREFIX + ''.join(rng.choice('0123456789ABCDEF') for _ in range(6))


def _recent(rng, max_days=6):
    """A recent UTC timestamp, matching SQLite's CURRENT_TIMESTAMP."""
    return (_utcnow() - timedelta(days=rng.randint(0, max_days),
                                  hours=rng.randint(0, 23),
                                  minutes=rng.randint(0, 59))
            ).strftime('%Y-%m-%d %H:%M:%S')


def build_world(conn, rng):
    """Everything that is not a transaction. Returns a dict of row lists."""
    c = conn.cursor()
    c.execute('SELECT id, quantity FROM items ORDER BY id')
    items = [(r[0], r[1] or 0) for r in c.fetchall()]
    c.execute('SELECT employee_id, name, role, zone FROM workers ORDER BY employee_id')
    workers = c.fetchall()

    tags, cartons, pallets, pallet_cartons, orders, jobs, alerts = [], [], [], [], [], [], []

    # 2. Unit tags spread across the pipeline. Racked tags never exceed the
    #    item's quantity, so the shelf never claims more stock than exists.
    shape = [('racked', 3), ('received', 1), ('in_transit', 1),
             ('picked', 1), ('tagged', 1), ('dispatched', 2)]
    for item_id, qty in items:
        racked_budget = max(0, min(3, qty))
        for state, count in shape:
            n = racked_budget if state == 'racked' else count
            for _ in range(n):
                rack = ('%s%d' % (rng.choice(RACK_ROWS), rng.randint(1, 6))
                        if state == 'racked' else None)
                tags.append((_uid(rng), item_id, state, rack, _recent(rng)))

    # 3-4. Cartons, some loaded onto pallets.
    for n, (item_id, _qty) in enumerate(items[:6], start=1):
        cid = 'CTN-%04d' % n
        state = ['racked', 'received', 'in_transit', 'created'][n % 4]
        cartons.append((cid, item_id, rng.choice([6, 12, 24]), _uid(rng), state))
    for n in range(1, 3):
        pid = 'PLT-%04d' % n
        pallets.append((pid, _uid(rng), ['sealed', 'in_transit'][n % 2]))
        for cid, *_ in cartons[(n - 1) * 3:(n - 1) * 3 + 3]:
            pallet_cartons.append((pid, cid))

    # 5. Purchase orders in each state.
    for n, (item_id, _qty) in enumerate(items[:5]):
        expected = rng.choice([24, 48, 60, 100])
        received, status = [(0, 'open'), (expected // 2, 'partial'),
                            (expected, 'closed')][n % 3]
        orders.append((item_id, expected, received, status,
                       'Supplier PO for %s' % item_id))

    # 6. One live station session. WORKER_AUTH_TIMEOUT is 300 s, so this is a
    #    genuinely short-lived record - re-run before demoing the Workers tab.
    if workers:
        w = workers[0]
        sessions = [(STATIONS[1], w[0], w[1], w[2], w[3] or 'general',
                     int(time.time()) + 300)]
    else:
        sessions = []

    # 7. Tag-writing batches.
    for n, (item_id, _qty) in enumerate(items[:3], start=1):
        qty = rng.choice([10, 20, 25])
        done, status = [(qty, 'completed'), (qty, 'completed'),
                        (qty // 2, 'pending')][n - 1]
        jobs.append(('BATCH-%03d' % n, item_id, qty, done, status))

    # 8. Alerts across every type the dashboard filters on.
    c.execute('SELECT id, name, quantity, low_stock_threshold, unit FROM items')
    for row in c.fetchall():
        iid, name, qty, thr, unit = row
        if qty is not None and thr is not None and qty <= thr:
            kind = 'out_of_stock' if qty == 0 else 'low_stock'
            msg = ('%s is %s: %d %s remaining'
                   % (name, 'out of stock' if qty == 0 else 'low on stock', qty, unit))
            alerts.append((iid, kind, msg, 0, _recent(rng, 3)))
    for item_id, _qty in items[:3]:
        alerts.append((item_id, 'security',
                       'SECURITY: dispatched tag %s (%s) re-scanned at warehouse gate'
                       % (_uid(rng), item_id), rng.choice([0, 0, 1]), _recent(rng, 4)))

    return {'tags': tags, 'cartons': cartons, 'pallets': pallets,
            'pallet_cartons': pallet_cartons, 'orders': orders,
            'sessions': sessions, 'jobs': jobs, 'alerts': alerts}


# ── Write ─────────────────────────────────────────────────────────────────────

def seeded_counts(conn):
    c = conn.cursor()
    like = DEMO_PREFIX + '%'
    return {
        'transactions': c.execute('SELECT COUNT(*) FROM transactions WHERE device_id = ?',
                                  (DEVICE_ID,)).fetchone()[0],
        'tags':    c.execute('SELECT COUNT(*) FROM rfid_tags WHERE uid LIKE ?', (like,)).fetchone()[0],
        'cartons': c.execute('SELECT COUNT(*) FROM cartons').fetchone()[0],
        'pallets': c.execute('SELECT COUNT(*) FROM pallets').fetchone()[0],
        'orders':  c.execute('SELECT COUNT(*) FROM purchase_orders').fetchone()[0],
    }


def write_all(conn, ledger, world):
    c = conn.cursor()
    c.executemany(
        """INSERT INTO transactions
             (item_id, action, quantity_change, previous_quantity, new_quantity,
              performed_by, device_id, note, timestamp)
           VALUES (?, ?, ?, ?, ?, 'demo-seed', ?, ?, ?)""",
        [(r[0], r[1], r[2], r[3], r[4], DEVICE_ID, NOTE, r[5]) for r in ledger])

    c.executemany(
        """INSERT OR IGNORE INTO rfid_tags (uid, item_id, state, rack_location, last_scan)
           VALUES (?, ?, ?, ?, ?)""", world['tags'])
    c.executemany(
        """INSERT OR IGNORE INTO cartons (id, item_id, unit_count, tag_uid, state, created_by)
           VALUES (?, ?, ?, ?, ?, 'demo-seed')""", world['cartons'])
    c.executemany(
        """INSERT OR IGNORE INTO pallets (id, tag_uid, state, created_by)
           VALUES (?, ?, ?, 'demo-seed')""", world['pallets'])
    c.executemany(
        'INSERT OR IGNORE INTO pallet_cartons (pallet_id, carton_id) VALUES (?, ?)',
        world['pallet_cartons'])
    c.executemany(
        """INSERT INTO purchase_orders (item_id, expected_qty, received_qty, status, note, created_by)
           VALUES (?, ?, ?, ?, ?, 'demo-seed')""", world['orders'])
    c.executemany(
        """INSERT OR REPLACE INTO worker_sessions
             (device_id, employee_id, name, role, zone, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""", world['sessions'])
    c.executemany(
        """INSERT OR IGNORE INTO write_jobs (batch_id, item_id, quantity, written, status, created_by)
           VALUES (?, ?, ?, ?, ?, 'demo-seed')""", world['jobs'])
    c.executemany(
        'INSERT INTO alerts (item_id, alert_type, message, is_read, timestamp) VALUES (?, ?, ?, ?, ?)',
        world['alerts'])
    conn.commit()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=DEFAULT_DB, help='SQLite file (default: backend/inventory.db)')
    ap.add_argument('--days', type=int, default=30, help='days of ledger history (default: 30)')
    ap.add_argument('--seed', type=int, default=20260904, help='RNG seed, for reproducible output')
    ap.add_argument('--status', action='store_true', help='report what is already seeded and exit')
    ap.add_argument('--dry-run', action='store_true', help='print a summary and write nothing')
    ap.add_argument('--yes', action='store_true', help='confirm the write')
    ap.add_argument('--force', action='store_true', help='seed again even if seeded rows exist')
    args = ap.parse_args(argv)

    db = os.path.abspath(args.db)
    if not os.path.exists(db):
        print('No database at %s - start the backend once to create it.' % db)
        return 2

    conn = sqlite3.connect(db)
    try:
        existing = seeded_counts(conn)
        if args.status:
            print('Database : %s' % db)
            for k, v in existing.items():
                print('  %-14s %d' % (k, v))
            return 0

        if existing['transactions'] and not args.force:
            print('This database already holds %d demo-seed transactions.'
                  % existing['transactions'])
            print('Seeding again would double the history. Re-run with --force if intended.')
            return 1

        rng = random.Random(args.seed)
        ledger = build_ledger(conn, args.days, rng)
        if not ledger:
            print('No items in the catalogue - nothing to seed.')
            return 1
        world = build_world(conn, rng)

        problems = check_ledger(conn, ledger)
        dispatches = [r for r in ledger if r[1] == 'warehouse_dispatch']
        receipts   = [r for r in ledger if r[1] == 'warehouse_receive']

        print('Database : %s' % db)
        print('Window   : %d days ending today' % args.days)
        print('Ledger   : %d dispatch (%d units), %d receive (%d units) - %s' % (
            len(dispatches), sum(-r[2] for r in dispatches),
            len(receipts), sum(r[2] for r in receipts),
            'consistent' if not problems else '%d PROBLEMS' % len(problems)))
        print('Tags     : %d across the pipeline' % len(world['tags']))
        print('Cartons  : %d   Pallets: %d   Pallet links: %d'
              % (len(world['cartons']), len(world['pallets']), len(world['pallet_cartons'])))
        print('Orders   : %d   Write jobs: %d   Sessions: %d   Alerts: %d'
              % (len(world['orders']), len(world['jobs']),
                 len(world['sessions']), len(world['alerts'])))

        if problems:
            for p in problems[:10]:
                print('   !! %s' % p)
            print('\nRefusing to write an inconsistent ledger.')
            return 3
        if args.dry_run:
            print('\n--dry-run: nothing written.')
            return 0
        if not args.yes:
            print('\nRefusing to write without --yes.')
            return 1

        write_all(conn, ledger, world)
        print('\nSeeded. items.quantity was not modified; no existing row was changed.')
        print('Station sessions last %d s by design, so the Workers tab shows one only'
              ' briefly after seeding.' % 300)
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
