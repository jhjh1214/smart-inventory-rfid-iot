import json
from functools import wraps

from flask import (Flask, jsonify, request, render_template, Response,
                   stream_with_context, session, redirect, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from database import init_db, get_db
from analytics import (get_all_analytics, get_item_analytics,
                        get_transaction_trends, get_abc_analysis,
                        get_inventory_summary)
from mqtt_subscriber import start_mqtt, get_status as mqtt_status
import events

app = Flask(__name__)
app.secret_key = 'inv-secret-key-change-in-prod'


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
        return jsonify({'error': 'Invalid username or password'}), 401

    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    return jsonify({'status': 'ok', 'role': user['role'], 'username': user['username']})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'status': 'ok'})


@app.route('/api/me')
@login_required
def api_me():
    return jsonify({'username': session['username'], 'role': session['role']})


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
    c.execute('SELECT * FROM items ORDER BY name')
    items = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(items)


@app.route('/api/items', methods=['POST'])
@login_required
def add_item():
    data = request.get_json()
    conn = get_db()
    c = conn.cursor()
    c.execute(
        'INSERT INTO items (id, name, quantity, unit, low_stock_threshold) VALUES (?, ?, ?, ?, ?)',
        (data['id'], data['name'], data.get('quantity', 0),
         data.get('unit', 'pcs'), data.get('low_stock_threshold', 5))
    )
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
        c.execute('SELECT quantity FROM items WHERE id = ?', (item_id,))
        row = c.fetchone()
        if row:
            prev_qty = row['quantity']
            new_qty  = data['quantity']
            c.execute(
                '''INSERT INTO transactions
                   (item_id, action, quantity_change, previous_quantity, new_quantity,
                    performed_by, note)
                   VALUES (?, 'manual_adjust', ?, ?, ?, ?, ?)''',
                (item_id, new_qty - prev_qty, prev_qty, new_qty,
                 session.get('username', 'admin'), 'Manual adjustment')
            )

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
    c.execute('DELETE FROM rfid_tags WHERE item_id = ?', (item_id,))
    c.execute('DELETE FROM items WHERE id = ?', (item_id,))
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
        ORDER BY t.timestamp DESC
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
        ORDER BY a.timestamp DESC
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
    note = data.get('note', 'Admin return')

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT t.*, i.name AS item_name, i.quantity FROM rfid_tags t JOIN items i ON t.item_id = i.id WHERE t.uid = ?', (uid,))
    tag = c.fetchone()
    if not tag:
        conn.close()
        return jsonify({'error': 'Tag not found'}), 404

    if tag['state'] != 'consumed':
        conn.close()
        return jsonify({'error': 'Tag is not in consumed state'}), 400

    prev_qty = tag['quantity']
    new_qty  = prev_qty + 1

    c.execute('UPDATE rfid_tags SET state = ?, last_scan = CURRENT_TIMESTAMP WHERE uid = ?', ('out', uid))
    c.execute('UPDATE items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (new_qty, tag['item_id']))
    c.execute(
        '''INSERT INTO transactions (item_id, action, quantity_change, previous_quantity,
           new_quantity, tag_uid, performed_by, note)
           VALUES (?, 'admin_return', 1, ?, ?, ?, ?, ?)''',
        (tag['item_id'], prev_qty, new_qty, uid, session.get('username', 'admin'), note)
    )
    conn.commit()
    conn.close()

    events.push({
        'type':      'scan',
        'item_id':   tag['item_id'],
        'item_name': tag['item_name'],
        'action':    'admin_return',
        'quantity':  new_qty,
        'tag_uid':   uid,
        'tag_state': 'out',
    })
    return jsonify({'status': 'ok', 'new_quantity': new_qty})


@app.route('/api/tags/<uid>', methods=['DELETE'])
@admin_required
def delete_tag(uid):
    conn = get_db()
    conn.execute('DELETE FROM rfid_tags WHERE uid = ?', (uid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# ── User management (admin only) ──────────────────────────────────────────────

@app.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, username, role, created_at FROM users ORDER BY created_at')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/users', methods=['POST'])
@admin_required
def create_user():
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
            (data['username'], generate_password_hash(data['password']),
             data.get('role', 'viewer'))
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400
    conn.close()
    return jsonify({'status': 'ok'}), 201


@app.route('/api/users/<int:user_id>/password', methods=['PUT'])
@login_required
def change_password(user_id):
    if session.get('role') != 'admin' and session.get('user_id') != user_id:
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json()
    conn = get_db()
    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                 (generate_password_hash(data['password']), user_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    start_mqtt()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False, threaded=True)
