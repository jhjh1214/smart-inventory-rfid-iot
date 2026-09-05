import math
from datetime import datetime, timedelta
from database import get_db

try:
    from sklearn.ensemble import GradientBoostingRegressor, IsolationForest
    import numpy as np
    _SKLEARN = True
except ImportError:
    _SKLEARN = False

ALPHA        = 0.3
REORDER_COST = 10.0
HOLDING_COST = 0.5

# Pipeline actions that move stock. The four-station pipeline writes these;
# 'scan_in' / 'scan_out' come only from the legacy single-reader handler, so
# analytics keyed on those never observe real traffic.
DEMAND_ACTION   = 'warehouse_dispatch'
RECEIPT_ACTION  = 'warehouse_receive'

# Days that must show actual dispatch before a Gradient Boosting forecast is
# claimed. A lag-3 model fitted to an almost-empty series is ML in name only.
MIN_ACTIVE_DAYS = 3

# Isolation Forest settings. CONTAMINATION is the share of rows the detector
# flags by construction, which is what makes the per-item rule below
# necessary - see _anomaly_item_ids.
ANOMALY_WINDOW  = 500
CONTAMINATION   = 0.05
MIN_ANOMALIES   = 2


# ── Per-item helpers ──────────────────────────────────────────────────────────

def _get_daily_usage(item_id, days=30):
    """Units dispatched per day over the last `days` days, zero-filled.

    Dispatch is the demand signal: warehouse_dispatch is the only action that
    records goods physically leaving the building, and it stores a negative
    quantity_change, so -quantity_change is units out.

    Days with no dispatch are returned as 0 rather than omitted. _gb_forecast
    builds lag and rolling-mean features by position, so it needs a contiguous
    daily series - a gappy one silently treats last month's scan as yesterday's.
    """
    conn = get_db()
    c = conn.cursor()
    start = datetime.now() - timedelta(days=days - 1)
    c.execute('''
        SELECT DATE(timestamp) AS day, SUM(-quantity_change) AS used
        FROM transactions
        WHERE item_id = ? AND action = ? AND DATE(timestamp) >= ?
        GROUP BY DATE(timestamp)
    ''', (item_id, DEMAND_ACTION, start.strftime('%Y-%m-%d')))
    by_day = {r['day']: (r['used'] or 0) for r in c.fetchall()}
    conn.close()

    series = []
    for i in range(days):
        day = (start + timedelta(days=i)).strftime('%Y-%m-%d')
        series.append({'date': day, 'used': by_day.get(day, 0)})
    return series


def _exponential_smoothing(values):
    if not values:
        return 1.0
    s = float(values[0])
    for v in values[1:]:
        s = ALPHA * v + (1 - ALPHA) * s
    return round(s, 2)


def _gb_forecast(usages):
    """Gradient Boosting demand forecast using lag + rolling-mean features.

    Falls back to exponential smoothing when sklearn is unavailable, the series
    is shorter than 7 days, or fewer than MIN_ACTIVE_DAYS days saw any dispatch.
    The series is zero-filled, so length alone no longer implies real history."""
    active_days = sum(1 for v in usages if v)
    if not _SKLEARN or len(usages) < 7 or active_days < MIN_ACTIVE_DAYS:
        return _exponential_smoothing(usages), 'exponential_smoothing'

    vals = np.array(usages, dtype=float)
    n = len(vals)
    X, y = [], []
    for i in range(3, n):
        ma3 = vals[max(0, i - 3):i].mean()
        ma7 = vals[max(0, i - 7):i].mean()
        X.append([i, vals[i - 1], vals[i - 2], vals[i - 3], ma3, ma7])
        y.append(vals[i])

    if len(X) < 4:
        return _exponential_smoothing(usages), 'exponential_smoothing'

    model = GradientBoostingRegressor(n_estimators=50, max_depth=3, random_state=42)
    model.fit(X, y)
    ma3_last = vals[-3:].mean()
    ma7_last = vals[-7:].mean()
    pred = model.predict([[n, vals[-1], vals[-2], vals[-3], ma3_last, ma7_last]])[0]
    return round(float(max(0.0, pred)), 2), 'gradient_boosting'


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

    avg_daily              = sum(usages) / len(usages) if usages else 1.0
    forecast, forecast_method = _gb_forecast(usages)
    eoq                    = _eoq(forecast if forecast > 0 else avg_daily)
    days_left              = current_quantity / forecast if forecast > 0 else 999.0
    risk                   = _risk_score(days_left)

    return {
        'item_id':         item_id,
        'avg_daily_usage': round(avg_daily, 2),
        'forecast_demand': forecast,
        'forecast_method': forecast_method,
        'eoq':             eoq,
        'days_remaining':  round(min(days_left, 999), 1),
        'risk_score':      risk,
        'daily_history':   daily[-7:],
    }


def _recent_transactions(limit=500):
    """The most recent `limit` transactions, oldest-first.

    Chronological order matters: the anomaly detector derives an inter-scan
    gap feature by walking the list forward in time.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, item_id, action, quantity_change, timestamp, device_id
        FROM transactions
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    rows.reverse()   # newest-N selected above, returned oldest-first
    return rows


def detect_scan_anomalies():
    """Isolation Forest anomaly detection on recent scan transactions.
    Returns list of anomalous transaction dicts (most-recent last)."""
    rows = _recent_transactions(limit=ANOMALY_WINDOW)

    if not _SKLEARN or len(rows) < 10:
        return []

    features, valid_rows = [], []
    prev_ts = None
    for r in rows:
        raw = (r['timestamp'] or '')[:19]
        try:
            ts = datetime.strptime(raw, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        gap = (ts - prev_ts).total_seconds() if prev_ts else 0.0
        features.append([
            ts.hour,
            ts.weekday(),
            abs(r['quantity_change'] or 0),
            min(gap, 86400),
        ])
        valid_rows.append(r)
        prev_ts = ts

    if len(features) < 10:
        return []

    X = np.array(features, dtype=float)
    model = IsolationForest(n_estimators=100, contamination=CONTAMINATION,
                            random_state=42)
    labels = model.fit_predict(X)

    return [
        {
            'txn_id':          r['id'],
            'item_id':         r['item_id'],
            'action':          r['action'],
            'quantity_change': r['quantity_change'],
            'device_id':       r['device_id'],
            'timestamp':       r['timestamp'],
        }
        for r, label in zip(valid_rows, labels)
        if label == -1
    ]


def _anomaly_item_ids():
    """Items whose anomalous scans exceed what the detector flags by chance.

    Isolation Forest flags CONTAMINATION of every window by construction, so
    for a busy item a single flagged scan is the expected background rate
    rather than evidence of anything. Rolling the raw flags up per item
    therefore marks every item as anomalous once there is enough traffic,
    which tells a reader nothing.

    An item is surfaced only when its anomaly count exceeds the rate chance
    would produce over its own scans, with a floor of MIN_ANOMALIES so a
    one-off never trips it.
    """
    anomalies = detect_scan_anomalies()
    if not anomalies:
        return set()

    scans = {}
    for row in _recent_transactions(limit=ANOMALY_WINDOW):
        scans[row['item_id']] = scans.get(row['item_id'], 0) + 1

    flagged = {}
    for a in anomalies:
        flagged[a['item_id']] = flagged.get(a['item_id'], 0) + 1

    out = set()
    for item_id, count in flagged.items():
        expected = CONTAMINATION * scans.get(item_id, 0)
        if count >= max(MIN_ANOMALIES, math.ceil(expected)):
            out.add(item_id)
    return out


def get_all_analytics():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, quantity FROM items')
    items = c.fetchall()
    conn.close()
    flagged = _anomaly_item_ids()
    result = []
    for r in items:
        a = get_item_analytics(r['id'], r['quantity'])
        a['anomaly'] = r['id'] in flagged
        result.append(a)
    return result


# ── Aggregate analytics ───────────────────────────────────────────────────────

def get_transaction_trends(days=7):
    """Daily warehouse receive / dispatch counts for the last N days."""
    conn = get_db()
    c = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    c.execute('''
        SELECT
            DATE(timestamp) AS day,
            SUM(CASE WHEN action = ? THEN 1 ELSE 0 END) AS received,
            SUM(CASE WHEN action = ? THEN 1 ELSE 0 END) AS dispatched
        FROM transactions
        WHERE DATE(timestamp) >= ?
        GROUP BY DATE(timestamp)
        ORDER BY day
    ''', (RECEIPT_ACTION, DEMAND_ACTION, since))
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
    """Classify items A/B/C by cumulative share of stock movement.

    Standard Pareto banding: ranked by volume descending, the items making up
    the first 80% of movement are A, those up to 95% are B, the tail is C.
    Bands are decided on the cumulative share *before* each item, so the
    largest item is always A even when it alone exceeds 95%.

    Volume is units moved (ABS(quantity_change)) across the two warehouse gate
    actions, not row count - one 50-unit carton outweighs fifty single scans.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT item_id,
               COUNT(*) AS txn_count,
               SUM(ABS(quantity_change)) AS volume
        FROM transactions
        WHERE action IN (?, ?)
        GROUP BY item_id
        ORDER BY volume DESC, txn_count DESC
    ''', (RECEIPT_ACTION, DEMAND_ACTION))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    total = sum(r['volume'] or 0 for r in rows)
    result, cumulative = {}, 0
    for row in rows:
        share_before = (cumulative / total) if total else 0.0
        cls = 'A' if share_before < 0.80 else ('B' if share_before < 0.95 else 'C')
        cumulative += (row['volume'] or 0)
        result[row['item_id']] = {
            'class':          cls,
            'txn_count':      row['txn_count'],
            'volume':         row['volume'] or 0,
            'cumulative_pct': round((cumulative / total * 100) if total else 100.0, 1),
        }
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

    c.execute('''
        SELECT
            COUNT(*) AS total_items,
            SUM(CASE WHEN quantity > low_stock_threshold THEN 1 ELSE 0 END) AS healthy,
            SUM(CASE WHEN quantity = 0 THEN 1 ELSE 0 END) AS out_of_stock,
            SUM(CASE WHEN quantity > 0 AND quantity <= low_stock_threshold THEN 1 ELSE 0 END) AS low_stock
        FROM items
    ''')
    r = c.fetchone()
    total_items  = r['total_items']  or 0
    healthy      = r['healthy']      or 0
    out_of_stock = r['out_of_stock'] or 0
    low_stock    = r['low_stock']    or 0

    # The dashboard doughnut buckets tags as In Warehouse / With Product /
    # Consumed. Those three names predate the pipeline, so the query used to
    # count only the legacy 'in' / 'out' / 'consumed' states and reported zero
    # for every pipeline tag. Each pipeline state now maps to the bucket that
    # matches where the goods physically are.
    c.execute('''
        SELECT
            SUM(CASE WHEN state IN ('out', 'tagged', 'in_transit', 'picked',
                                    'return_pending') THEN 1 ELSE 0 END) AS tags_out,
            SUM(CASE WHEN state IN ('in', 'received', 'racked', 'returned')
                     THEN 1 ELSE 0 END) AS tags_in,
            SUM(CASE WHEN state IN ('consumed', 'dispatched')
                     THEN 1 ELSE 0 END) AS tags_consumed,
            COUNT(*) AS tags_total
        FROM rfid_tags
    ''')
    t = c.fetchone()
    tags_out      = t['tags_out']      or 0
    tags_in       = t['tags_in']       or 0
    tags_consumed = t['tags_consumed'] or 0
    tags_total    = t['tags_total']    or 0

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
        SELECT
            SUM(CASE WHEN src = 'txn'   THEN 1 ELSE 0 END) AS today_scans,
            SUM(CASE WHEN src = 'alert' THEN 1 ELSE 0 END) AS security_today
        FROM (
            SELECT 'txn'   AS src FROM transactions WHERE DATE(timestamp) = DATE('now')
            UNION ALL
            SELECT 'alert' AS src FROM alerts WHERE alert_type = 'security' AND DATE(timestamp) = DATE('now')
        )
    ''')
    d = c.fetchone()
    today_scans    = d['today_scans']    or 0
    security_today = d['security_today'] or 0

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
            # Every tag row, so the total still reconciles if a future state
            # is added without being bucketed above.
            'total':    tags_total,
        },
    }
