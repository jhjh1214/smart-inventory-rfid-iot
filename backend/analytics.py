import math
from datetime import datetime, timedelta
from database import get_db

ALPHA        = 0.3
REORDER_COST = 10.0
HOLDING_COST = 0.5


# ── Per-item helpers ──────────────────────────────────────────────────────────

def _get_daily_usage(item_id, days=30):
    conn = get_db()
    c = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    c.execute('''
        SELECT DATE(timestamp) AS day, SUM(-quantity_change) AS used
        FROM transactions
        WHERE item_id = ? AND action = 'scan_out' AND DATE(timestamp) >= ?
        GROUP BY DATE(timestamp)
        ORDER BY day
    ''', (item_id, since))
    rows = c.fetchall()
    conn.close()
    return [{'date': r['day'], 'used': r['used'] or 0} for r in rows]


def _exponential_smoothing(values):
    if not values:
        return 1.0
    s = float(values[0])
    for v in values[1:]:
        s = ALPHA * v + (1 - ALPHA) * s
    return round(s, 2)


def _eoq(avg_daily_demand):
    annual = avg_daily_demand * 365
    if annual <= 0:
        return 0
    return round(math.sqrt((2 * annual * REORDER_COST) / HOLDING_COST), 1)


def _risk_score(days_remaining):
    if days_remaining <= 3:
        return 90
    if days_remaining <= 7:
        return 60
    return 20


def get_item_analytics(item_id, current_quantity):
    daily  = _get_daily_usage(item_id)
    usages = [d['used'] for d in daily]

    avg_daily    = sum(usages) / len(usages) if usages else 1.0
    forecast     = _exponential_smoothing(usages)
    eoq          = _eoq(avg_daily)
    days_left    = current_quantity / avg_daily if avg_daily > 0 else 999.0
    risk         = _risk_score(days_left)

    return {
        'item_id':        item_id,
        'avg_daily_usage': round(avg_daily, 2),
        'forecast_demand': forecast,
        'eoq':             eoq,
        'days_remaining':  round(min(days_left, 999), 1),
        'risk_score':      risk,
        'daily_history':   daily[-7:],
    }


def get_all_analytics():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, quantity FROM items')
    items = c.fetchall()
    conn.close()
    return [get_item_analytics(r['id'], r['quantity']) for r in items]


# ── Aggregate analytics ───────────────────────────────────────────────────────

def get_transaction_trends(days=7):
    """Daily scan_in / scan_out counts for the last N days."""
    conn = get_db()
    c = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    c.execute('''
        SELECT
            DATE(timestamp) AS day,
            SUM(CASE WHEN action = 'scan_in'  THEN 1 ELSE 0 END) AS received,
            SUM(CASE WHEN action = 'scan_out' THEN 1 ELSE 0 END) AS dispatched
        FROM transactions
        WHERE DATE(timestamp) >= ?
        GROUP BY DATE(timestamp)
        ORDER BY day
    ''', (since,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    # Fill in any missing dates with zeros
    result = []
    for i in range(days):
        day = (datetime.now() - timedelta(days=days - 1 - i)).strftime('%Y-%m-%d')
        match = next((r for r in rows if r['day'] == day), None)
        result.append(match if match else {'day': day, 'received': 0, 'dispatched': 0})
    return result


def get_abc_analysis():
    """Classify items as A/B/C by transaction volume."""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT item_id, COUNT(*) AS txn_count
        FROM transactions
        WHERE action IN ('scan_in', 'scan_out')
        GROUP BY item_id
        ORDER BY txn_count DESC
    ''')
    rows = c.fetchall()
    conn.close()

    total = len(rows)
    result = {}
    for i, row in enumerate(rows):
        pct = (i + 1) / total if total > 0 else 1.0
        cls = 'A' if pct <= 0.2 else ('B' if pct <= 0.5 else 'C')
        result[row['item_id']] = {'class': cls, 'txn_count': row['txn_count']}
    return result


def get_pipeline_summary():
    """Tag counts at each pipeline stage + per-item breakdown + rack utilisation."""
    conn = get_db()
    c = conn.cursor()

    stages = ['tagged', 'in_transit', 'received', 'racked', 'picked',
              'dispatched', 'returned', 'out', 'in', 'consumed']

    totals = {}
    for stage in stages:
        c.execute('SELECT COUNT(*) FROM rfid_tags WHERE state = ?', (stage,))
        n = c.fetchone()[0]
        if n:
            totals[stage] = n

    c.execute('''
        SELECT r.item_id, i.name AS item_name, r.state, COUNT(*) AS cnt
        FROM rfid_tags r LEFT JOIN items i ON r.item_id = i.id
        GROUP BY r.item_id, r.state ORDER BY r.item_id, r.state
    ''')
    items_map = {}
    for row in c.fetchall():
        iid = row['item_id']
        if iid not in items_map:
            items_map[iid] = {'item_id': iid, 'item_name': row['item_name'],
                              **{s: 0 for s in stages}}
        if row['state'] in stages:
            items_map[iid][row['state']] = row['cnt']

    c.execute('''
        SELECT rack_location, COUNT(*) AS cnt FROM rfid_tags
        WHERE state = 'racked' AND rack_location IS NOT NULL
        GROUP BY rack_location ORDER BY rack_location
    ''')
    rack_stats = [dict(r) for r in c.fetchall()]

    c.execute('''
        SELECT j.*, i.name AS item_name FROM write_jobs j
        LEFT JOIN items i ON j.item_id = i.id
        ORDER BY j.created_at DESC LIMIT 20
    ''')
    jobs = [dict(r) for r in c.fetchall()]

    conn.close()
    return {
        'totals':     totals,
        'per_item':   list(items_map.values()),
        'rack_stats': rack_stats,
        'jobs':       jobs,
    }


def get_inventory_summary():
    """Overall inventory health metrics."""
    conn = get_db()
    c = conn.cursor()

    c.execute('SELECT COUNT(*) AS n FROM items')
    total_items = c.fetchone()['n']

    c.execute('SELECT COUNT(*) AS n FROM items WHERE quantity > low_stock_threshold')
    healthy = c.fetchone()['n']

    c.execute('SELECT COUNT(*) AS n FROM items WHERE quantity = 0')
    out_of_stock = c.fetchone()['n']

    c.execute('SELECT COUNT(*) AS n FROM items WHERE quantity > 0 AND quantity <= low_stock_threshold')
    low_stock = c.fetchone()['n']

    c.execute("SELECT COUNT(*) AS n FROM rfid_tags WHERE state = 'out'")
    tags_out = c.fetchone()['n']

    c.execute("SELECT COUNT(*) AS n FROM rfid_tags WHERE state = 'in'")
    tags_in = c.fetchone()['n']

    c.execute("SELECT COUNT(*) AS n FROM rfid_tags WHERE state = 'consumed'")
    tags_consumed = c.fetchone()['n']

    c.execute('''
        SELECT COUNT(DISTINCT item_id) AS n FROM transactions
        WHERE DATE(timestamp) < DATE('now', '-30 days')
        AND item_id NOT IN (
            SELECT DISTINCT item_id FROM transactions
            WHERE DATE(timestamp) >= DATE('now', '-30 days')
        )
    ''')
    dead_stock = c.fetchone()['n']

    c.execute('''
        SELECT COUNT(*) AS n FROM transactions
        WHERE DATE(timestamp) = DATE('now')
    ''')
    today_scans = c.fetchone()['n']

    c.execute('''
        SELECT COUNT(*) AS n FROM alerts
        WHERE alert_type = 'security'
        AND DATE(timestamp) = DATE('now')
    ''')
    security_today = c.fetchone()['n']

    conn.close()

    health_score = round((healthy / total_items * 100) if total_items > 0 else 100)

    return {
        'total_items':      total_items,
        'healthy':          healthy,
        'low_stock':        low_stock,
        'out_of_stock':     out_of_stock,
        'dead_stock':       dead_stock,
        'health_score':     health_score,
        'today_scans':      today_scans,
        'security_today':   security_today,
        'tags': {
            'out':      tags_out,
            'in':       tags_in,
            'consumed': tags_consumed,
            'total':    tags_out + tags_in + tags_consumed,
        },
    }
