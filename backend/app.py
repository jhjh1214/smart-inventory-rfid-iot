import csv
import io
import json
import os
import shutil
import tempfile
import threading
import time as _time
import urllib.request
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, jsonify, request, render_template, Response,
                   stream_with_context, session, redirect, url_for, send_file,
                   after_this_request)
from werkzeug.security import check_password_hash, generate_password_hash

from database import init_db, get_db, DB_PATH
from analytics import (get_all_analytics, get_item_analytics,
                        get_transaction_trends, get_abc_analysis,
                        get_inventory_summary, get_pipeline_summary)
from mqtt_subscriber import (start_mqtt, get_status as mqtt_status,
                              publish as mqtt_publish,
                              get_active_sessions,
                              TOPIC_FACTORY_JOB)
import events

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'inv-secret-key-change-in-prod')
app.permanent_session_lifetime = timedelta(hours=8)

# ── Login rate limiting ───────────────────────────────────────────────────────
_login_attempts: dict = {}   # ip -> {'count': int, 'reset_at': float}
_LOGIN_MAX    = 10
_LOGIN_WINDOW = 300  # 5 minutes


def _is_rate_limited(ip: str) -> bool:
    record = _login_attempts.get(ip)
    if not record:
        return False
    if _time.time() > record['reset_at']:
        _login_attempts.pop(ip, None)
        return False
    return record['count'] >= _LOGIN_MAX


def _record_failure(ip: str):
    now = _time.time()
    record = _login_attempts.get(ip)
    if record and now < record['reset_at']:
        record['count'] += 1
    else:
        _login_attempts[ip] = {'count': 1, 'reset_at': now + _LOGIN_WINDOW}


def _clear_failures(ip: str):
    _login_attempts.pop(ip, None)


# ── Webhook helper ────────────────────────────────────────────────────────────

def _webhook_post(url: str, payload: dict):
    """POST a JSON payload to a single webhook URL (blocking — call from thread)."""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'User-Agent': 'SmartInventory/1.0'},
            method='POST',
        )
        urllib.request.urlopen(req, timeout=5)
        print(f'[Webhook] OK  {url}')
    except Exception as e:
        print(f'[Webhook] ERR {url} — {e}')


def _fire_webhooks(event_type: str, data: dict):
    """Fire all active webhooks that subscribe to event_type (async, daemon threads)."""
    try:
        conn = get_db()
        c = conn.cursor()
        # Match '*' wildcard OR exact word in comma-separated events list
        c.execute(
            "SELECT url FROM webhooks WHERE active = 1 AND ("
            "  events = '*' OR events = ? OR "
            "  (',' || events || ',') LIKE ('%,' || ? || ',%')"
            ")",
            (event_type, event_type),
        )
        urls = [r['url'] for r in c.fetchall()]
        conn.close()
    except Exception:
        return

    payload = {'event': event_type, 'data': data, 'timestamp': datetime.now().isoformat()}
    for url in urls:
        threading.Thread(target=_webhook_post, args=(url, payload), daemon=True).start()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated


def manager_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('role') not in ('admin', 'manager'):
            return jsonify({'error': 'Manager access required'}), 403
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('role') != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET'])
def login_page():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    ip = request.remote_addr or 'unknown'
    if _is_rate_limited(ip):
        return jsonify({'error': 'Too many failed attempts — try again in 5 minutes'}), 429

    data = request.get_json()
    username = (data or {}).get('username', '').strip()
    password = (data or {}).get('password', '')
    if not username or not password:
        return jsonify({'error': 'Missing credentials'}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = c.fetchone()
    conn.close()

    if not user or not check_password_hash(user['password_hash'], password):
        _record_failure(ip)
        return jsonify({'error': 'Invalid username or password'}), 401

    _clear_failures(ip)
    session.permanent = True
    session['user_id']  = user['id']
    session['username'] = user['username']
    session['role']     = user['role']
    return jsonify({'status': 'ok', 'role': user['role'], 'username': user['username']})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'status': 'ok'})


@app.route('/api/me')
@login_required
def api_me():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT badge_uid, employee_id FROM users WHERE id = ?', (session['user_id'],))
    row = c.fetchone()
    conn.close()
    return jsonify({
        'id':          session['user_id'],
        'username':    session['username'],
        'role':        session['role'],
        'badge_uid':   row['badge_uid']   if row else None,
        'employee_id': row['employee_id'] if row else None,
    })


def _dashboard_actor():
    """Return 'username [badge:uid]' if badge linked, else 'username'."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT badge_uid FROM users WHERE id = ?', (session.get('user_id'),))
    row = c.fetchone()
    conn.close()
    username = session.get('username', 'admin')
    if row and row['badge_uid']:
        return f'{username} [badge:{row["badge_uid"]}]'
    return username


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')


# ── Server-Sent Events ────────────────────────────────────────────────────────

@app.route('/api/events')
@login_required
def sse():
    def stream():
        q = events.subscribe()
        try:
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"data: {json.dumps(data)}\n\n"
                except Exception:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            events.unsubscribe(q)

    return Response(
        stream_with_context(stream()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ── Status ────────────────────────────────────────────────────────────────────

@app.route('/api/status')
def api_status():
    return jsonify(mqtt_status())


@app.route('/api/dashboard')
@login_required
def api_dashboard():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) AS n FROM items')
    total_items = c.fetchone()['n']
    c.execute('SELECT COALESCE(SUM(quantity), 0) AS n FROM items')
    total_qty = c.fetchone()['n']
    c.execute('SELECT COUNT(*) AS n FROM items WHERE quantity <= low_stock_threshold')
    low_stock = c.fetchone()['n']
    c.execute('SELECT COUNT(*) AS n FROM alerts WHERE is_read = 0')
    unread_alerts = c.fetchone()['n']
    conn.close()
    return jsonify({
        'total_items':     total_items,
        'total_quantity':  total_qty,
        'low_stock_count': low_stock,
        'unread_alerts':   unread_alerts,
    })


# ── Items ─────────────────────────────────────────────────────────────────────

@app.route('/api/items', methods=['GET'])
@login_required
def get_items():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT *, (quantity - reserved_qty) AS available_qty FROM items ORDER BY name')
    items = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(items)


@app.route('/api/items', methods=['POST'])
@manager_required
def add_item():
    data = request.get_json()
    item_id = (data.get('id') or '').strip()
    name    = (data.get('name') or '').strip()
    if not item_id or not name:
        return jsonify({'error': 'id and name are required'}), 400
    qty = max(0, int(data.get('quantity', 0)))
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(
            'INSERT INTO items (id, name, quantity, unit, low_stock_threshold) VALUES (?, ?, ?, ?, ?)',
            (item_id, name, qty, data.get('unit', 'pcs'), data.get('low_stock_threshold', 5))
        )
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400
    c.execute('''INSERT INTO transactions
               (item_id, action, quantity_change, previous_quantity, new_quantity,
                performed_by, note, device_id)
               VALUES (?, 'item_added', ?, 0, ?, ?, ?, 'dashboard')''',
              (item_id, qty, qty, _dashboard_actor(), f"Item created: {name}"))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'}), 201


@app.route('/api/items/<item_id>', methods=['PUT'])
@login_required
def update_item(item_id):
    data = request.get_json()
    conn = get_db()
    c = conn.cursor()

    if 'quantity' in data:
        new_qty = int(data['quantity'])
        if new_qty < 0:
            conn.close()
            return jsonify({'error': 'Quantity cannot be negative'}), 400
        c.execute('SELECT quantity FROM items WHERE id = ?', (item_id,))
        row = c.fetchone()
        if row:
            prev_qty = row['quantity']
            c.execute('''INSERT INTO transactions
                       (item_id, action, quantity_change, previous_quantity, new_quantity,
                        performed_by, note, device_id)
                       VALUES (?, 'manual_adjust', ?, ?, ?, ?, ?, 'dashboard')''',
                      (item_id, new_qty - prev_qty, prev_qty, new_qty,
                       _dashboard_actor(), 'Manual adjustment'))

    allowed = ['name', 'quantity', 'unit', 'low_stock_threshold']
    fields  = [f for f in allowed if f in data]
    if fields:
        set_clause = ', '.join(f'{f} = ?' for f in fields)
        values     = [data[f] for f in fields] + [item_id]
        c.execute(
            f'UPDATE items SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            values
        )

    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/items/<item_id>', methods=['DELETE'])
@admin_required
def delete_item(item_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT name, quantity FROM items WHERE id = ?', (item_id,))
    item = c.fetchone()
    if item:
        who = _dashboard_actor()
        c.execute('SELECT uid, state FROM rfid_tags WHERE item_id = ?', (item_id,))
        for tag in c.fetchall():
            c.execute('''INSERT INTO transactions
                       (item_id, action, quantity_change, previous_quantity, new_quantity,
                        tag_uid, performed_by, note, device_id)
                       VALUES (?, 'tag_removed', 0, ?, ?, ?, ?, ?, 'dashboard')''',
                      (item_id, item['quantity'], item['quantity'], tag['uid'], who,
                       f"Tag removed: item '{item['name']}' deleted (state was {tag['state']})"))
        c.execute('''INSERT INTO transactions
                   (item_id, action, quantity_change, previous_quantity, new_quantity,
                    performed_by, note, device_id)
                   VALUES (?, 'item_deleted', 0, ?, 0, ?, ?, 'dashboard')''',
                  (item_id, item['quantity'], who, f"Item deleted: {item['name']}"))
    # Commit audit records first so the subsequent PRAGMA change takes effect
    # (SQLite ignores PRAGMA foreign_keys inside an active transaction)
    conn.commit()
    conn.execute('PRAGMA foreign_keys = OFF')
    c.execute('DELETE FROM rfid_tags WHERE item_id = ?', (item_id,))
    c.execute('DELETE FROM purchase_orders WHERE item_id = ?', (item_id,))
    c.execute('DELETE FROM write_jobs WHERE item_id = ?', (item_id,))
    c.execute('DELETE FROM items WHERE id = ?', (item_id,))
    conn.commit()
    conn.execute('PRAGMA foreign_keys = ON')
    conn.close()
    return jsonify({'status': 'ok'})


# ── Item stock reservation ─────────────────────────────────────────────────────

@app.route('/api/items/<item_id>/reserve', methods=['POST'])
@manager_required
def reserve_stock(item_id):
    data = request.get_json() or {}
    qty  = int(data.get('qty', 1))
    if qty < 1:
        return jsonify({'error': 'qty must be >= 1'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT quantity, reserved_qty FROM items WHERE id = ?', (item_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Item not found'}), 404
    available = row['quantity'] - row['reserved_qty']
    if qty > available:
        conn.close()
        return jsonify({'error': f'Only {available} units available (unreserved)'}), 400
    c.execute('UPDATE items SET reserved_qty = reserved_qty + ? WHERE id = ?', (qty, item_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/items/<item_id>/reserve', methods=['DELETE'])
@manager_required
def unreserve_stock(item_id):
    data = request.get_json() or {}
    qty  = int(data.get('qty', 1))
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT reserved_qty FROM items WHERE id = ?', (item_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Item not found'}), 404
    new_reserved = max(0, row['reserved_qty'] - qty)
    c.execute('UPDATE items SET reserved_qty = ? WHERE id = ?', (new_reserved, item_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# ── Transactions ──────────────────────────────────────────────────────────────

@app.route('/api/transactions')
@login_required
def get_transactions():
    limit = request.args.get('limit', 50, type=int)
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT t.*, i.name AS item_name
        FROM transactions t
        LEFT JOIN items i ON t.item_id = i.id
        ORDER BY t.timestamp DESC, t.id DESC
        LIMIT ?
    ''', (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


# ── Alerts ────────────────────────────────────────────────────────────────────

@app.route('/api/alerts')
@login_required
def get_alerts():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT a.*, i.name AS item_name
        FROM alerts a
        LEFT JOIN items i ON a.item_id = i.id
        ORDER BY a.timestamp DESC, a.id DESC
        LIMIT 50
    ''')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/alerts/<int:alert_id>/read', methods=['POST'])
@login_required
def mark_alert_read(alert_id):
    conn = get_db()
    conn.execute('UPDATE alerts SET is_read = 1 WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/alerts/read-all', methods=['POST'])
@login_required
def mark_all_read():
    conn = get_db()
    conn.execute('UPDATE alerts SET is_read = 1')
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/alerts/read', methods=['DELETE'])
@login_required
def delete_read_alerts():
    conn = get_db()
    conn.execute('DELETE FROM alerts WHERE is_read = 1')
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# ── Analytics ─────────────────────────────────────────────────────────────────

@app.route('/api/analytics')
@login_required
def get_analytics():
    return jsonify(get_all_analytics())


@app.route('/api/analytics/summary')
@login_required
def api_analytics_summary():
    return jsonify(get_inventory_summary())


@app.route('/api/analytics/trends')
@login_required
def api_analytics_trends():
    days = request.args.get('days', 7, type=int)
    return jsonify(get_transaction_trends(days))


@app.route('/api/analytics/abc')
@login_required
def api_analytics_abc():
    return jsonify(get_abc_analysis())


# ── RFID Tags ─────────────────────────────────────────────────────────────────

@app.route('/api/tags', methods=['GET'])
@login_required
def get_tags():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT t.*, i.name AS item_name
        FROM rfid_tags t
        LEFT JOIN items i ON t.item_id = i.id
        ORDER BY t.registered_at DESC
    ''')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/tags', methods=['POST'])
@login_required
def register_tag():
    data = request.get_json()
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO rfid_tags (uid, item_id, state) VALUES (?, ?, ?)',
        (data['uid'], data['item_id'], data.get('state', 'out'))
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'}), 201


@app.route('/api/tags/<uid>/return', methods=['POST'])
@admin_required
def return_tag(uid):
    data = request.get_json() or {}
    note = data.get('note', 'Admin return request')

    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT t.*, i.name AS item_name, i.quantity
                 FROM rfid_tags t JOIN items i ON t.item_id = i.id
                 WHERE t.uid = ?''', (uid,))
    tag = c.fetchone()
    if not tag:
        conn.close()
        return jsonify({'error': 'Tag not found'}), 404
    if tag['state'] not in ('consumed', 'dispatched'):
        conn.close()
        return jsonify({'error': f'Tag is in state "{tag["state"]}" — only dispatched/consumed tags can be returned'}), 400

    c.execute('UPDATE rfid_tags SET state = ?, last_scan = CURRENT_TIMESTAMP WHERE uid = ?',
              ('return_pending', uid))
    c.execute('''INSERT INTO transactions
               (item_id, action, quantity_change, previous_quantity, new_quantity,
                tag_uid, performed_by, note, device_id)
               VALUES (?, 'return_requested', 0, ?, ?, ?, ?, ?, 'dashboard')''',
              (tag['item_id'], tag['quantity'], tag['quantity'],
               uid, _dashboard_actor(), note))
    conn.commit()
    conn.close()
    events.push({'type': 'return_pending', 'item_id': tag['item_id'],
                 'item_name': tag['item_name'], 'tag_uid': uid})
    return jsonify({'status': 'ok'})


@app.route('/api/tags/<uid>/reassign', methods=['POST'])
@admin_required
def reassign_tag(uid):
    """Transfer tag state/item to a new physical UID (e.g. damaged label replacement)."""
    data    = request.get_json() or {}
    new_uid = (data.get('new_uid') or '').strip().upper()
    reason  = data.get('reason', 'Label replaced')
    if not new_uid:
        return jsonify({'error': 'new_uid is required'}), 400
    if new_uid == uid.upper():
        return jsonify({'error': 'new_uid must differ from current uid'}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT t.*, i.name AS item_name FROM rfid_tags t JOIN items i ON t.item_id = i.id WHERE t.uid = ?', (uid,))
    tag = c.fetchone()
    if not tag:
        conn.close()
        return jsonify({'error': 'Tag not found'}), 404
    c.execute('SELECT uid FROM rfid_tags WHERE uid = ?', (new_uid,))
    if c.fetchone():
        conn.close()
        return jsonify({'error': f'Tag {new_uid} already registered'}), 409

    c.execute('''INSERT INTO rfid_tags (uid, item_id, state, rack_location, previous_uid)
               VALUES (?, ?, ?, ?, ?)''',
              (new_uid, tag['item_id'], tag['state'], tag['rack_location'], uid))
    c.execute('''INSERT INTO transactions
               (item_id, action, quantity_change, previous_quantity, new_quantity,
                tag_uid, performed_by, note, device_id)
               VALUES (?, 'tag_reassigned', 0, ?, ?, ?, ?, ?, 'dashboard')''',
              (tag['item_id'], 0, 0, new_uid, _dashboard_actor(),
               f'Reassigned from {uid} → {new_uid}: {reason}'))
    c.execute('DELETE FROM rfid_tags WHERE uid = ?', (uid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'new_uid': new_uid})


@app.route('/api/tags/<uid>', methods=['DELETE'])
@admin_required
def delete_tag(uid):
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT t.*, i.name AS item_name, i.quantity
                 FROM rfid_tags t JOIN items i ON t.item_id = i.id
                 WHERE t.uid = ?''', (uid,))
    tag = c.fetchone()
    if tag:
        c.execute('''INSERT INTO transactions
                   (item_id, action, quantity_change, previous_quantity, new_quantity,
                    tag_uid, performed_by, note, device_id)
                   VALUES (?, 'tag_removed', 0, ?, ?, ?, ?, ?, 'dashboard')''',
                  (tag['item_id'], tag['quantity'], tag['quantity'], uid,
                   _dashboard_actor(),
                   f"Tag removed: state was '{tag['state']}' for '{tag['item_name']}'"))
    c.execute('DELETE FROM rfid_tags WHERE uid = ?', (uid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# ── User management ───────────────────────────────────────────────────────────

@app.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, username, role, created_at, badge_uid, employee_id FROM users ORDER BY created_at')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/users', methods=['POST'])
@admin_required
def create_user():
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'error': 'username and password required'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
            (username, generate_password_hash(password), data.get('role', 'viewer'))
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400
    conn.close()
    return jsonify({'status': 'ok'}), 201


@app.route('/api/users/<int:user_id>', methods=['PUT'])
@admin_required
def update_user(user_id):
    data    = request.get_json() or {}
    allowed = ['role', 'badge_uid', 'employee_id']
    fields  = [f for f in allowed if f in data]
    if not fields:
        return jsonify({'error': 'Nothing to update'}), 400
    set_clause = ', '.join(f'{f} = ?' for f in fields)
    values     = [data[f] for f in fields] + [user_id]
    conn = get_db()
    conn.execute(f'UPDATE users SET {set_clause} WHERE id = ?', values)
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    if user_id == session.get('user_id'):
        return jsonify({'error': 'Cannot delete your own account'}), 400
    conn = get_db()
    conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/users/<int:user_id>/password', methods=['PUT'])
@login_required
def change_password(user_id):
    if session.get('role') != 'admin' and session.get('user_id') != user_id:
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    new_password = data.get('password', '')
    if len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    # Non-admins must verify current password
    if session.get('role') != 'admin':
        current = data.get('current_password', '')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT password_hash FROM users WHERE id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        if not row or not check_password_hash(row['password_hash'], current):
            return jsonify({'error': 'Current password incorrect'}), 403

    conn = get_db()
    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                 (generate_password_hash(new_password), user_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# ── Audit Trail ───────────────────────────────────────────────────────────────

@app.route('/api/audit')
@login_required
def get_audit():
    limit   = request.args.get('limit', 100, type=int)
    filter_ = request.args.get('filter', 'all')
    conn = get_db()
    c = conn.cursor()
    base = '''SELECT t.*, i.name AS item_name FROM transactions t LEFT JOIN items i ON t.item_id = i.id'''
    if filter_ == 'dashboard':
        base += " WHERE t.device_id = 'dashboard'"
    elif filter_ == 'physical':
        base += " WHERE t.device_id != 'dashboard' AND t.device_id != 'system'"
    elif filter_ == 'admin':
        base += " WHERE t.action IN ('item_added','item_deleted','tag_removed','return_requested','manual_adjust','tag_reassigned')"
    base += ' ORDER BY t.timestamp DESC, t.id DESC LIMIT ?'
    c.execute(base, (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


# ── Workers ───────────────────────────────────────────────────────────────────

@app.route('/api/workers', methods=['GET'])
@login_required
def get_workers():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM workers ORDER BY name')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    sessions = get_active_sessions()
    for w in rows:
        for did, sess in sessions.items():
            if sess['employee_id'] == w['employee_id']:
                w['active_station']    = did
                w['session_expires_in'] = sess['expires_in']
                break
        else:
            w['active_station']    = None
            w['session_expires_in'] = None
    return jsonify(rows)


@app.route('/api/workers', methods=['POST'])
@manager_required
def create_worker():
    data = request.get_json() or {}
    employee_id = data.get('employee_id', '').upper().strip()
    name        = data.get('name', '').strip()
    role        = data.get('role', 'operator')
    zone        = data.get('zone', 'general')
    if not employee_id or not name:
        return jsonify({'error': 'employee_id and name required'}), 400
    if not employee_id.startswith('EMP-'):
        return jsonify({'error': 'employee_id must start with EMP-'}), 400
    conn = get_db()
    try:
        conn.execute('INSERT INTO workers (employee_id, name, role, zone) VALUES (?, ?, ?, ?)',
                     (employee_id, name, role, zone))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400
    conn.close()
    return jsonify({'status': 'ok'}), 201


@app.route('/api/workers/<int:worker_id>', methods=['PUT'])
@manager_required
def update_worker(worker_id):
    data    = request.get_json() or {}
    allowed = ['name', 'role', 'active', 'zone']
    fields  = [f for f in allowed if f in data]
    if not fields:
        return jsonify({'error': 'Nothing to update'}), 400
    set_clause = ', '.join(f'{f} = ?' for f in fields)
    values     = [data[f] for f in fields] + [worker_id]
    conn = get_db()
    conn.execute(f'UPDATE workers SET {set_clause} WHERE id = ?', values)
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/workers/<int:worker_id>', methods=['DELETE'])
@admin_required
def delete_worker(worker_id):
    conn = get_db()
    conn.execute('DELETE FROM workers WHERE id = ?', (worker_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/workers/sessions')
@login_required
def worker_sessions():
    return jsonify(get_active_sessions())


# ── Pipeline ──────────────────────────────────────────────────────────────────

@app.route('/api/pipeline')
@login_required
def api_pipeline():
    return jsonify(get_pipeline_summary())


# ── Factory write jobs ────────────────────────────────────────────────────────

@app.route('/api/factory/jobs', methods=['GET'])
@login_required
def get_write_jobs():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT j.*, i.name AS item_name
        FROM write_jobs j LEFT JOIN items i ON j.item_id = i.id
        ORDER BY j.created_at DESC LIMIT 30
    ''')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/factory/jobs', methods=['POST'])
@manager_required
def create_write_job():
    data     = request.get_json() or {}
    item_id  = data.get('item_id')
    quantity = int(data.get('quantity', 1))
    if not item_id or quantity < 1:
        return jsonify({'error': 'item_id and quantity required'}), 400

    batch_id = f"batch-{datetime.now().strftime('%Y%m%d%H%M%S')}-{item_id}"
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM items WHERE id = ?', (item_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': f'Item {item_id} not found'}), 404

    c.execute('''INSERT INTO write_jobs (batch_id, item_id, quantity, status, created_by)
               VALUES (?, ?, ?, 'pending', ?)''',
              (batch_id, item_id, quantity, session.get('username', 'admin')))
    conn.commit()
    conn.close()
    mqtt_publish(TOPIC_FACTORY_JOB, json.dumps({
        'batch_id': batch_id, 'item_id': item_id, 'quantity': quantity,
    }))
    events.push({'type': 'job_created', 'batch_id': batch_id,
                 'item_id': item_id, 'quantity': quantity})
    return jsonify({'status': 'ok', 'batch_id': batch_id}), 201


# ── Purchase Orders ───────────────────────────────────────────────────────────

@app.route('/api/purchase-orders', methods=['GET'])
@login_required
def get_purchase_orders():
    conn = get_db()
    c = conn.cursor()
    status_filter = request.args.get('status', '')
    if status_filter:
        c.execute('''SELECT p.*, i.name AS item_name FROM purchase_orders p
                     LEFT JOIN items i ON p.item_id = i.id
                     WHERE p.status = ? ORDER BY p.created_at DESC''', (status_filter,))
    else:
        c.execute('''SELECT p.*, i.name AS item_name FROM purchase_orders p
                     LEFT JOIN items i ON p.item_id = i.id
                     ORDER BY p.created_at DESC LIMIT 50''')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/purchase-orders', methods=['POST'])
@manager_required
def create_purchase_order():
    data         = request.get_json() or {}
    item_id      = data.get('item_id')
    expected_qty = int(data.get('expected_qty', 0))
    if not item_id or expected_qty < 1:
        return jsonify({'error': 'item_id and expected_qty required'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM items WHERE id = ?', (item_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Item not found'}), 404
    c.execute('''INSERT INTO purchase_orders (item_id, expected_qty, note, created_by)
               VALUES (?, ?, ?, ?)''',
              (item_id, expected_qty, data.get('note', ''),
               session.get('username', 'admin')))
    conn.commit()
    po_id = c.lastrowid
    conn.close()
    return jsonify({'status': 'ok', 'id': po_id}), 201


@app.route('/api/purchase-orders/<int:po_id>', methods=['PUT'])
@manager_required
def update_purchase_order(po_id):
    data    = request.get_json() or {}
    allowed = ['received_qty', 'status', 'note']
    fields  = [f for f in allowed if f in data]
    if not fields:
        return jsonify({'error': 'Nothing to update'}), 400

    # Auto-set status based on received_qty
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT expected_qty, received_qty FROM purchase_orders WHERE id = ?', (po_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'PO not found'}), 404

    set_clause = ', '.join(f'{f} = ?' for f in fields)
    values     = [data[f] for f in fields] + [po_id]
    c.execute(f'UPDATE purchase_orders SET {set_clause} WHERE id = ?', values)

    if 'received_qty' in data:
        new_recv = int(data['received_qty'])
        exp      = row['expected_qty']
        if new_recv >= exp:
            c.execute("UPDATE purchase_orders SET status = 'complete' WHERE id = ?", (po_id,))
        elif new_recv > 0:
            c.execute("UPDATE purchase_orders SET status = 'partial' WHERE id = ?", (po_id,))

    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/purchase-orders/<int:po_id>', methods=['DELETE'])
@manager_required
def delete_purchase_order(po_id):
    conn = get_db()
    conn.execute('DELETE FROM purchase_orders WHERE id = ?', (po_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# ── Webhooks ──────────────────────────────────────────────────────────────────

@app.route('/api/webhooks', methods=['GET'])
@admin_required
def get_webhooks():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM webhooks ORDER BY created_at DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/webhooks', methods=['POST'])
@admin_required
def create_webhook():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    url  = (data.get('url') or '').strip()
    if not name or not url:
        return jsonify({'error': 'name and url required'}), 400
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'url must start with http:// or https://'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO webhooks (name, url, events) VALUES (?, ?, ?)',
              (name, url, data.get('events', 'low_stock,security')))
    conn.commit()
    wh_id = c.lastrowid
    conn.close()
    return jsonify({'status': 'ok', 'id': wh_id}), 201


@app.route('/api/webhooks/<int:wh_id>', methods=['PUT'])
@admin_required
def update_webhook(wh_id):
    data    = request.get_json() or {}
    allowed = ['name', 'url', 'events', 'active']
    fields  = [f for f in allowed if f in data]
    if not fields:
        return jsonify({'error': 'Nothing to update'}), 400
    set_clause = ', '.join(f'{f} = ?' for f in fields)
    values     = [data[f] for f in fields] + [wh_id]
    conn = get_db()
    conn.execute(f'UPDATE webhooks SET {set_clause} WHERE id = ?', values)
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/webhooks/<int:wh_id>', methods=['DELETE'])
@admin_required
def delete_webhook(wh_id):
    conn = get_db()
    conn.execute('DELETE FROM webhooks WHERE id = ?', (wh_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/webhooks/<int:wh_id>/test', methods=['POST'])
@admin_required
def test_webhook(wh_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT url, name FROM webhooks WHERE id = ?', (wh_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Webhook not found'}), 404

    payload = {
        'event': 'test',
        'data': {'message': 'Test ping from Smart Inventory System', 'webhook_name': row['name']},
        'timestamp': datetime.now().isoformat(),
    }
    ok = True
    try:
        req = urllib.request.Request(
            row['url'],
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'User-Agent': 'SmartInventory/1.0'},
            method='POST',
        )
        urllib.request.urlopen(req, timeout=5)
        print(f'[Webhook] Test OK  {row["url"]}')
    except Exception as e:
        print(f'[Webhook] Test ERR {row["url"]} — {e}')
        ok = False

    if ok:
        return jsonify({'status': 'ok', 'message': f'Test payload delivered to {row["url"]}'})
    return jsonify({'error': f'Delivery failed — check the endpoint URL and ensure it is reachable'}), 502


# ── Cartons ───────────────────────────────────────────────────────────────────

@app.route('/api/cartons', methods=['GET'])
@login_required
def get_cartons():
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT ca.*, i.name AS item_name
                 FROM cartons ca LEFT JOIN items i ON i.id = ca.item_id
                 ORDER BY ca.created_at DESC''')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/cartons', methods=['POST'])
@manager_required
def create_carton():
    data       = request.get_json() or {}
    item_id    = (data.get('item_id') or '').strip()
    unit_count = int(data.get('unit_count') or 0)
    note       = (data.get('note') or '').strip()
    if not item_id or unit_count < 1:
        return jsonify({'error': 'item_id and unit_count (>=1) required'}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM items WHERE id = ?', (item_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': f'Item {item_id} does not exist'}), 404

    # Auto-generate CTN id: CTN-NNNN
    c.execute("SELECT COUNT(*) FROM cartons")
    n = c.fetchone()[0] + 1
    carton_id = f'CTN-{n:04d}'
    # Ensure uniqueness
    while True:
        c.execute('SELECT id FROM cartons WHERE id = ?', (carton_id,))
        if not c.fetchone():
            break
        n += 1
        carton_id = f'CTN-{n:04d}'

    c.execute('''INSERT INTO cartons (id, item_id, unit_count, note, created_by)
               VALUES (?, ?, ?, ?, ?)''',
              (carton_id, item_id, unit_count, note or None, session.get('username', 'system')))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'carton_id': carton_id}), 201


@app.route('/api/cartons/<carton_id>/tag', methods=['PUT'])
@manager_required
def assign_carton_tag(carton_id):
    """Manually link a scanned tag UID to a carton (dashboard override)."""
    data    = request.get_json() or {}
    tag_uid = (data.get('tag_uid') or '').strip().upper()
    if not tag_uid:
        return jsonify({'error': 'tag_uid required'}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM cartons WHERE id = ?', (carton_id.upper(),))
    carton = c.fetchone()
    if not carton:
        conn.close()
        return jsonify({'error': 'Carton not found'}), 404

    c.execute('UPDATE cartons SET tag_uid = ? WHERE id = ?', (tag_uid, carton_id.upper()))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/cartons/<carton_id>', methods=['DELETE'])
@manager_required
def delete_carton(carton_id):
    conn = get_db()
    conn.execute('DELETE FROM pallet_cartons WHERE carton_id = ?', (carton_id.upper(),))
    conn.execute('DELETE FROM cartons WHERE id = ?', (carton_id.upper(),))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# ── Pallets ───────────────────────────────────────────────────────────────────

@app.route('/api/pallets', methods=['GET'])
@login_required
def get_pallets():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM pallets ORDER BY created_at DESC')
    pallets = [dict(r) for r in c.fetchall()]

    # Attach carton summary to each pallet
    for p in pallets:
        c.execute('''SELECT ca.*, i.name AS item_name
                     FROM cartons ca
                     JOIN pallet_cartons pc ON pc.carton_id = ca.id
                     LEFT JOIN items i ON i.id = ca.item_id
                     WHERE pc.pallet_id = ?
                     ORDER BY ca.id''', (p['id'],))
        p['cartons']    = [dict(r) for r in c.fetchall()]
        p['total_units'] = sum(ca['unit_count'] for ca in p['cartons'])

    conn.close()
    return jsonify(pallets)


@app.route('/api/pallets', methods=['POST'])
@manager_required
def create_pallet():
    data = request.get_json() or {}
    note = (data.get('note') or '').strip()

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM pallets")
    n = c.fetchone()[0] + 1
    pallet_id = f'PLT-{n:04d}'
    while True:
        c.execute('SELECT id FROM pallets WHERE id = ?', (pallet_id,))
        if not c.fetchone():
            break
        n += 1
        pallet_id = f'PLT-{n:04d}'

    c.execute('INSERT INTO pallets (id, note, created_by) VALUES (?, ?, ?)',
              (pallet_id, note or None, session.get('username', 'system')))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'pallet_id': pallet_id}), 201


@app.route('/api/pallets/<pallet_id>/cartons', methods=['POST'])
@manager_required
def add_carton_to_pallet(pallet_id):
    data      = request.get_json() or {}
    carton_id = (data.get('carton_id') or '').strip().upper()
    if not carton_id:
        return jsonify({'error': 'carton_id required'}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM pallets WHERE id = ?', (pallet_id.upper(),))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Pallet not found'}), 404
    c.execute('SELECT id FROM cartons WHERE id = ?', (carton_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Carton not found'}), 404

    try:
        c.execute('INSERT INTO pallet_cartons (pallet_id, carton_id) VALUES (?, ?)',
                  (pallet_id.upper(), carton_id))
        conn.commit()
    except Exception:
        conn.close()
        return jsonify({'error': 'Carton already on this pallet'}), 409
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/pallets/<pallet_id>/cartons/<carton_id>', methods=['DELETE'])
@manager_required
def remove_carton_from_pallet(pallet_id, carton_id):
    conn = get_db()
    conn.execute('DELETE FROM pallet_cartons WHERE pallet_id=? AND carton_id=?',
                 (pallet_id.upper(), carton_id.upper()))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/pallets/<pallet_id>/tag', methods=['PUT'])
@manager_required
def assign_pallet_tag(pallet_id):
    data    = request.get_json() or {}
    tag_uid = (data.get('tag_uid') or '').strip().upper()
    if not tag_uid:
        return jsonify({'error': 'tag_uid required'}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM pallets WHERE id = ?', (pallet_id.upper(),))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Pallet not found'}), 404
    c.execute('UPDATE pallets SET tag_uid = ? WHERE id = ?', (tag_uid, pallet_id.upper()))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/pallets/<pallet_id>', methods=['DELETE'])
@manager_required
def delete_pallet(pallet_id):
    conn = get_db()
    conn.execute('DELETE FROM pallet_cartons WHERE pallet_id = ?', (pallet_id.upper(),))
    conn.execute('DELETE FROM pallets WHERE id = ?', (pallet_id.upper(),))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# ── Export / Import ───────────────────────────────────────────────────────────

@app.route('/api/export/backup')
@admin_required
def export_backup():
    """Download a copy of the live SQLite database."""
    fd, tmp_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    shutil.copy2(DB_PATH, tmp_path)

    @after_this_request
    def _cleanup(response):
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return response

    filename = f'inventory_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
    return send_file(tmp_path, as_attachment=True, download_name=filename,
                     mimetype='application/octet-stream')


@app.route('/api/export/items')
@login_required
def export_items_csv():
    """Export items table as CSV."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, name, quantity, reserved_qty, unit, low_stock_threshold FROM items ORDER BY name')
    rows = c.fetchall()
    conn.close()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['id', 'name', 'quantity', 'reserved_qty', 'unit', 'low_stock_threshold'])
    for r in rows:
        w.writerow([r['id'], r['name'], r['quantity'], r['reserved_qty'],
                    r['unit'], r['low_stock_threshold']])
    out.seek(0)
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=items.csv'})


@app.route('/api/import/items', methods=['POST'])
@manager_required
def import_items_csv():
    """Import items from uploaded CSV. Columns: id, name, quantity, unit, low_stock_threshold."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename.endswith('.csv'):
        return jsonify({'error': 'File must be a .csv'}), 400

    text    = f.stream.read().decode('utf-8-sig')
    reader  = csv.DictReader(io.StringIO(text))
    who     = _dashboard_actor()
    created = 0
    updated = 0
    errors  = []

    conn = get_db()
    c = conn.cursor()
    for i, row in enumerate(reader, start=2):
        item_id = (row.get('id') or '').strip()
        name    = (row.get('name') or '').strip()
        if not item_id or not name:
            errors.append(f'Row {i}: id and name are required')
            continue
        try:
            qty       = max(0, int(row.get('quantity', 0)))
            unit      = row.get('unit', 'pcs') or 'pcs'
            threshold = max(0, int(row.get('low_stock_threshold', 5)))
        except ValueError:
            errors.append(f'Row {i}: invalid numeric value')
            continue

        c.execute('SELECT id, quantity FROM items WHERE id = ?', (item_id,))
        existing = c.fetchone()
        if existing:
            c.execute('''UPDATE items SET name=?, quantity=?, unit=?, low_stock_threshold=?,
                         updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                      (name, qty, unit, threshold, item_id))
            if qty != existing['quantity']:
                c.execute('''INSERT INTO transactions
                           (item_id, action, quantity_change, previous_quantity, new_quantity,
                            performed_by, note, device_id)
                           VALUES (?, 'manual_adjust', ?, ?, ?, ?, ?, 'dashboard')''',
                          (item_id, qty - existing['quantity'], existing['quantity'], qty,
                           who, 'CSV import update'))
            updated += 1
        else:
            c.execute('INSERT INTO items (id, name, quantity, unit, low_stock_threshold) VALUES (?, ?, ?, ?, ?)',
                      (item_id, name, qty, unit, threshold))
            c.execute('''INSERT INTO transactions
                       (item_id, action, quantity_change, previous_quantity, new_quantity,
                        performed_by, note, device_id)
                       VALUES (?, 'item_added', ?, 0, ?, ?, ?, 'dashboard')''',
                      (item_id, qty, qty, who, 'CSV import'))
            created += 1

    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'created': created, 'updated': updated, 'errors': errors})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    start_mqtt()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False, threaded=True)
