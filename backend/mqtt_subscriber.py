"""
mqtt_subscriber.py — MQTT broker client + full pipeline state machine

Pipeline state machine:
  blank tag
    -> [factory_writer] writes item_id  -> state: tagged
    -> [factory_exit]   leaves factory  -> state: in_transit
    -> [warehouse_gate] arrives WH      -> state: received   qty +1
    -> [warehouse_rack] placed on shelf -> state: racked     (no qty change)
    -> [warehouse_gate] dispatched out  -> state: dispatched qty -1  (TERMINAL)
    -> [return_gate]    customer return -> state: returned   qty +1  (re-admitted)
    -> [warehouse_rack] re-shelved      -> state: racked
    -> [warehouse_gate] dispatched out  -> state: dispatched qty -1  (TERMINAL again)

Security:
  dispatched / consumed tag at any gate -> SECURITY ALERT
  warehouse dispatch without active supervisor session -> SECURITY ALERT (dispatch still proceeds)
"""

import json
import os
import threading
import time as _time
from datetime import datetime

import paho.mqtt.client as mqtt

from database import get_db
import events

BROKER        = os.environ.get('MQTT_BROKER', '192.168.0.115')
PORT          = int(os.environ.get('MQTT_PORT', 1883))
MQTT_USER     = os.environ.get('MQTT_USER', '')
MQTT_PASSWORD = os.environ.get('MQTT_PASSWORD', '')

TOPIC_SCAN            = 'inventory/scan'
TOPIC_ALERT           = 'inventory/alert'
TOPIC_STATUS          = 'inventory/status'
TOPIC_FACTORY_JOB     = 'inventory/factory/job'
TOPIC_FACTORY_WRITTEN = 'inventory/factory/written'
TOPIC_FACTORY_EXIT    = 'inventory/factory/exit'
TOPIC_WAREHOUSE_GATE  = 'inventory/warehouse/gate'
TOPIC_WAREHOUSE_RACK  = 'inventory/warehouse/rack'
TOPIC_RETURNS_GATE    = 'inventory/returns/gate'

status  = {'connected': False, 'last_message': None, 'device_last_seen': None}
_client = None

# ── Worker session state ──────────────────────────────────────────────────────
# In-memory cache; persisted to worker_sessions table for restart recovery.
_worker_sessions: dict = {}   # device_id -> {employee_id, name, role, zone, expires}
WORKER_SESSION_TTL = 300      # 5 minutes


# ── Session persistence helpers ───────────────────────────────────────────────

def _save_session(device_id: str, sess: dict):
    """Persist a worker session to DB so it survives restarts."""
    try:
        conn = get_db()
        conn.execute('''
            INSERT OR REPLACE INTO worker_sessions
            (device_id, employee_id, name, role, zone, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (device_id, sess['employee_id'], sess['name'], sess['role'],
              sess.get('zone', 'general'), int(sess['expires'])))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[MQTT] Session persist error: {e}')


def _delete_session(device_id: str):
    """Remove a session from DB when it expires."""
    try:
        conn = get_db()
        conn.execute('DELETE FROM worker_sessions WHERE device_id = ?', (device_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _load_sessions():
    """Load unexpired sessions from DB into memory on startup."""
    now = _time.time()
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM worker_sessions WHERE expires_at > ?', (int(now),))
        rows = c.fetchall()
        conn.close()
        for row in rows:
            _worker_sessions[row['device_id']] = {
                'employee_id': row['employee_id'],
                'name':        row['name'],
                'role':        row['role'],
                'zone':        row.get('zone', 'general'),
                'expires':     row['expires_at'],
            }
        if rows:
            print(f'[MQTT] Restored {len(rows)} worker session(s) from DB')
    except Exception as e:
        print(f'[MQTT] Session load error: {e}')


# ── MQTT callbacks ────────────────────────────────────────────────────────────

def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        status['connected'] = True
        for t in (TOPIC_SCAN, TOPIC_STATUS,
                  TOPIC_FACTORY_WRITTEN, TOPIC_FACTORY_EXIT,
                  TOPIC_WAREHOUSE_GATE, TOPIC_WAREHOUSE_RACK,
                  TOPIC_RETURNS_GATE):
            client.subscribe(t)
        print('[MQTT] Connected to broker, subscribed to all pipeline topics')
    else:
        status['connected'] = False
        print(f'[MQTT] Connection refused (rc={rc})')


def _on_disconnect(client, userdata, rc):
    status['connected'] = False
    print('[MQTT] Disconnected')


def _on_message(client, userdata, msg):
    status['last_message'] = datetime.now().isoformat()
    try:
        payload = json.loads(msg.payload.decode())
    except Exception as e:
        print(f'[MQTT] Bad payload on {msg.topic}: {e}')
        return

    # Worker badge interception — must happen before pipeline routing
    item_id = payload.get('item_id', '') or ''
    if item_id.upper().startswith('EMP-'):
        _handle_worker_badge(msg.topic, payload)
        return

    t = msg.topic
    if   t == TOPIC_FACTORY_WRITTEN: _handle_factory_written(client, payload)
    elif t == TOPIC_FACTORY_EXIT:    _handle_factory_exit(client, payload)
    elif t == TOPIC_WAREHOUSE_GATE:  _handle_warehouse_gate(client, payload)
    elif t == TOPIC_WAREHOUSE_RACK:  _handle_warehouse_rack(client, payload)
    elif t == TOPIC_RETURNS_GATE:    _handle_return_gate(client, payload)
    elif t == TOPIC_STATUS:          status['device_last_seen'] = datetime.now().isoformat()
    elif t == TOPIC_SCAN:            _handle_legacy_scan(client, payload)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_tag_with_item(c, tag_uid):
    c.execute('''
        SELECT t.*, i.name AS item_name, i.quantity,
               i.low_stock_threshold, i.unit
        FROM rfid_tags t JOIN items i ON t.item_id = i.id
        WHERE t.uid = ?
    ''', (tag_uid,))
    return c.fetchone()


def _ensure_item(c, item_id):
    c.execute('SELECT * FROM items WHERE id = ?', (item_id,))
    item = c.fetchone()
    if not item:
        c.execute(
            'INSERT INTO items (id, name, quantity, unit, low_stock_threshold) VALUES (?, ?, 0, ?, 5)',
            (item_id, item_id, 'pcs')
        )
        c.execute('SELECT * FROM items WHERE id = ?', (item_id,))
        item = c.fetchone()
    return item


def _security_alert(c, conn, client, item_id, item_name, tag_uid, message):
    c.execute('INSERT INTO alerts (item_id, alert_type, message) VALUES (?, ?, ?)',
              (item_id, 'security', message))
    conn.commit()
    conn.close()
    events.push({'type': 'security_alert', 'tag_uid': tag_uid,
                 'item_id': item_id, 'item_name': item_name, 'message': message})
    print(f'[MQTT] SECURITY: {message}')
    # Fire outbound webhooks for security events
    try:
        from app import _fire_webhooks
        _fire_webhooks('security', {'item_id': item_id, 'item_name': item_name,
                                     'tag_uid': tag_uid, 'message': message})
    except Exception:
        pass


def _low_stock_check(c, client, item_id, item_name, new_qty, threshold, unit):
    if new_qty <= threshold:
        alert_type = 'out_of_stock' if new_qty == 0 else 'low_stock'
        msg = (f"{item_name} is {'out of stock' if new_qty == 0 else 'low on stock'}: "
               f"{new_qty} {unit} remaining")
        c.execute('INSERT INTO alerts (item_id, alert_type, message) VALUES (?, ?, ?)',
                  (item_id, alert_type, msg))
        if client:
            client.publish(TOPIC_ALERT, json.dumps({
                'item_id': item_id, 'item_name': item_name,
                'quantity': new_qty, 'alert_type': alert_type, 'message': msg,
            }))
        try:
            from app import _fire_webhooks
            _fire_webhooks('low_stock', {'item_id': item_id, 'item_name': item_name,
                                          'quantity': new_qty, 'alert_type': alert_type})
        except Exception:
            pass


# ── Worker session helpers ────────────────────────────────────────────────────

def _get_current_worker(device_id):
    """Return active worker session for device, or None if expired/absent."""
    if not device_id:
        return None
    sess = _worker_sessions.get(device_id)
    if not sess:
        return None
    if _time.time() > sess['expires']:
        del _worker_sessions[device_id]
        _delete_session(device_id)
        return None
    return sess


def _performed_by(device_id):
    """Return 'Name (EMP-XXX)' string or 'system'."""
    w = _get_current_worker(device_id)
    return f'{w["name"]} ({w["employee_id"]})' if w else 'system'


def _attach_worker(c, txn_id, device_id):
    """Update performed_by on just-inserted transaction row."""
    label = _performed_by(device_id)
    if label != 'system':
        c.execute('UPDATE transactions SET performed_by = ? WHERE id = ?', (label, txn_id))


def _handle_worker_badge(source_topic, payload):
    """Worker taps RFID badge at any station — creates/renews a 5-min session."""
    tag_uid     = payload.get('tag_uid')
    employee_id = (payload.get('item_id') or '').upper()
    device_id   = payload.get('device_id', 'unknown')
    if not employee_id:
        return

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM workers WHERE employee_id = ?', (employee_id,))
    worker = c.fetchone()

    if not worker:
        c.execute('INSERT INTO workers (employee_id, name, uid) VALUES (?, ?, ?)',
                  (employee_id, employee_id, tag_uid))
        conn.commit()
        c.execute('SELECT * FROM workers WHERE employee_id = ?', (employee_id,))
        worker = c.fetchone()
    elif tag_uid and not worker['uid']:
        c.execute('UPDATE workers SET uid = ? WHERE employee_id = ?', (tag_uid, employee_id))

    c.execute('UPDATE workers SET last_seen = CURRENT_TIMESTAMP WHERE employee_id = ?',
              (employee_id,))
    conn.commit()
    conn.close()

    if not worker['active']:
        print(f'[MQTT] Worker {employee_id} is inactive — access denied at {device_id}')
        events.push({'type': 'worker_denied', 'employee_id': employee_id,
                     'name': worker['name'], 'device_id': device_id})
        return

    expires = _time.time() + WORKER_SESSION_TTL
    sess = {
        'employee_id': employee_id,
        'name':        worker['name'],
        'role':        worker['role'],
        'zone':        worker.get('zone', 'general'),
        'expires':     expires,
    }
    _worker_sessions[device_id] = sess
    _save_session(device_id, sess)

    print(f'[MQTT] Worker authenticated: {worker["name"]} ({employee_id}) @ {device_id}')
    events.push({'type': 'worker_auth', 'employee_id': employee_id,
                 'name': worker['name'], 'role': worker['role'], 'device_id': device_id})


def get_worker_sessions():
    """Return active sessions (for dashboard display)."""
    now = _time.time()
    expired = [did for did, sess in list(_worker_sessions.items()) if now >= sess['expires']]
    for did in expired:
        del _worker_sessions[did]
        _delete_session(did)
    return {
        did: {**sess, 'expires_in': int(sess['expires'] - now)}
        for did, sess in _worker_sessions.items()
    }


# ── Pipeline handlers ─────────────────────────────────────────────────────────

def _handle_factory_written(client, payload):
    tag_uid  = payload.get('tag_uid')
    item_id  = payload.get('item_id')
    batch_id = payload.get('batch_id', '')
    if not tag_uid or not item_id:
        return

    conn = get_db()
    c = conn.cursor()
    item = _ensure_item(c, item_id)

    c.execute('SELECT state FROM rfid_tags WHERE uid = ?', (tag_uid,))
    if c.fetchone():
        conn.close()
        return  # already registered

    c.execute('INSERT INTO rfid_tags (uid, item_id, state) VALUES (?, ?, ?)',
              (tag_uid, item_id, 'tagged'))
    c.execute('''INSERT INTO transactions
               (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, note, device_id)
               VALUES (?, 'tag_write', 0, ?, ?, ?, ?, ?)''',
              (item_id, item['quantity'], item['quantity'], tag_uid, f'batch:{batch_id}',
               payload.get('device_id', 'unknown')))
    _attach_worker(c, c.lastrowid, payload.get('device_id'))
    if batch_id:
        c.execute('UPDATE write_jobs SET written = written + 1 WHERE batch_id = ?', (batch_id,))
        c.execute('''UPDATE write_jobs
                   SET status = 'complete', completed_at = CURRENT_TIMESTAMP
                   WHERE batch_id = ? AND written >= quantity''', (batch_id,))

    conn.commit()
    conn.close()
    print(f'[MQTT] TAGGED    {tag_uid} -> {item["name"]}  (batch={batch_id})')
    events.push({'type': 'pipeline', 'stage': 'tagged',
                 'tag_uid': tag_uid, 'item_id': item_id, 'item_name': item['name']})


def _handle_factory_exit(client, payload):
    tag_uid = payload.get('tag_uid')
    if not tag_uid:
        return

    conn = get_db()
    c = conn.cursor()
    tag = _get_tag_with_item(c, tag_uid)

    if not tag:
        item_id = payload.get('item_id')
        if item_id:
            item = _ensure_item(c, item_id)
            c.execute('INSERT INTO rfid_tags (uid, item_id, state) VALUES (?, ?, ?)',
                      (tag_uid, item_id, 'in_transit'))
            c.execute('''INSERT INTO transactions
                       (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, device_id)
                       VALUES (?, 'factory_exit', 0, ?, ?, ?, ?)''',
                      (item_id, item['quantity'], item['quantity'], tag_uid,
                       payload.get('device_id', 'unknown')))
            _attach_worker(c, c.lastrowid, payload.get('device_id'))
            conn.commit()
            conn.close()
            events.push({'type': 'pipeline', 'stage': 'in_transit',
                         'tag_uid': tag_uid, 'item_id': item_id, 'item_name': item_id})
        else:
            conn.close()
        return

    state = tag['state']
    if state in ('tagged', 'out'):
        c.execute('UPDATE rfid_tags SET state = ?, last_scan = CURRENT_TIMESTAMP WHERE uid = ?',
                  ('in_transit', tag_uid))
        c.execute('''INSERT INTO transactions
                   (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, device_id)
                   VALUES (?, 'factory_exit', 0, ?, ?, ?, ?)''',
                  (tag['item_id'], tag['quantity'], tag['quantity'], tag_uid,
                   payload.get('device_id', 'unknown')))
        _attach_worker(c, c.lastrowid, payload.get('device_id'))
        conn.commit()
        conn.close()
        print(f'[MQTT] IN_TRANSIT {tag_uid} -> {tag["item_name"]}')
        events.push({'type': 'pipeline', 'stage': 'in_transit',
                     'tag_uid': tag_uid, 'item_id': tag['item_id'], 'item_name': tag['item_name']})
    elif state in ('dispatched', 'consumed'):
        _security_alert(c, conn, client, tag['item_id'], tag['item_name'], tag_uid,
                        f'SECURITY: {state} tag {tag_uid} ({tag["item_name"]}) at factory exit')
    else:
        print(f'[MQTT] factory_exit: {tag_uid} state={state}, skip')
        conn.close()


def _handle_warehouse_gate(client, payload):
    """
    Smart gate — state determines action:
      in_transit / out            -> receive  (qty +1)
      received / racked / returned / in -> dispatch (qty -1, TERMINAL)
      dispatched / consumed       -> SECURITY ALERT
    """
    tag_uid = payload.get('tag_uid')
    if not tag_uid:
        return

    conn = get_db()
    c = conn.cursor()
    tag = _get_tag_with_item(c, tag_uid)

    if not tag:
        item_id = payload.get('item_id')
        if item_id:
            item = _ensure_item(c, item_id)
            # Use atomic SQL update to avoid race conditions
            c.execute('''UPDATE items SET quantity = quantity + 1, updated_at = CURRENT_TIMESTAMP
                         WHERE id = ?''', (item_id,))
            c.execute('SELECT quantity FROM items WHERE id = ?', (item_id,))
            new_qty = c.fetchone()['quantity']
            prev = new_qty - 1
            c.execute('INSERT INTO rfid_tags (uid, item_id, state) VALUES (?, ?, ?)',
                      (tag_uid, item_id, 'received'))
            c.execute('''INSERT INTO transactions
                       (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, device_id)
                       VALUES (?, 'warehouse_receive', 1, ?, ?, ?, ?)''',
                      (item_id, prev, new_qty, tag_uid, payload.get('device_id', 'unknown')))
            _attach_worker(c, c.lastrowid, payload.get('device_id'))
            conn.commit()
            conn.close()
            events.push({'type': 'pipeline', 'stage': 'received',
                         'tag_uid': tag_uid, 'item_id': item_id,
                         'item_name': item_id, 'quantity': new_qty})
        else:
            conn.close()
        return

    state   = tag['state']
    item_id = tag['item_id']

    if state in ('in_transit', 'out'):
        # Atomic increment
        c.execute('''UPDATE items SET quantity = quantity + 1, updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?''', (item_id,))
        c.execute('SELECT quantity FROM items WHERE id = ?', (item_id,))
        new_qty = c.fetchone()['quantity']
        prev = new_qty - 1
        c.execute('UPDATE rfid_tags SET state = ?, last_scan = CURRENT_TIMESTAMP WHERE uid = ?',
                  ('received', tag_uid))
        c.execute('''INSERT INTO transactions
                   (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, device_id)
                   VALUES (?, 'warehouse_receive', 1, ?, ?, ?, ?)''',
                  (item_id, prev, new_qty, tag_uid, payload.get('device_id', 'unknown')))
        _attach_worker(c, c.lastrowid, payload.get('device_id'))
        # Check against any open purchase orders
        _check_purchase_order(c, item_id)
        conn.commit()
        conn.close()
        print(f'[MQTT] RECEIVED   {tag_uid} -> {tag["item_name"]}  qty {prev}->{new_qty}')
        events.push({'type': 'pipeline', 'stage': 'received',
                     'tag_uid': tag_uid, 'item_id': item_id,
                     'item_name': tag['item_name'], 'quantity': new_qty})

    elif state in ('received', 'racked', 'returned', 'in'):
        device_id = payload.get('device_id', 'unknown')
        worker    = _get_current_worker(device_id)
        if not worker or worker['role'] != 'supervisor':
            c.execute('INSERT INTO alerts (item_id, alert_type, message) VALUES (?, ?, ?)',
                      (item_id, 'security',
                       f'UNVERIFIED DISPATCH: tag {tag_uid} ({tag["item_name"]}) dispatched '
                       f'from {device_id} — no active supervisor session'))
            events.push({'type': 'security_alert', 'tag_uid': tag_uid, 'item_id': item_id,
                         'item_name': tag['item_name'],
                         'message': f'Dispatch from {device_id} without supervisor'})
        # Atomic decrement, never below 0
        c.execute('''UPDATE items SET quantity = MAX(0, quantity - 1),
                     updated_at = CURRENT_TIMESTAMP WHERE id = ?''', (item_id,))
        c.execute('SELECT quantity FROM items WHERE id = ?', (item_id,))
        new_qty = c.fetchone()['quantity']
        prev    = new_qty + 1  # approximate; could be 0 if was 0
        c.execute('UPDATE rfid_tags SET state = ?, last_scan = CURRENT_TIMESTAMP WHERE uid = ?',
                  ('dispatched', tag_uid))
        c.execute('''INSERT INTO transactions
                   (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, device_id)
                   VALUES (?, 'warehouse_dispatch', -1, ?, ?, ?, ?)''',
                  (item_id, prev, new_qty, tag_uid, device_id))
        _attach_worker(c, c.lastrowid, device_id)
        _low_stock_check(c, client, item_id, tag['item_name'],
                         new_qty, tag['low_stock_threshold'], tag['unit'])
        conn.commit()
        conn.close()
        print(f'[MQTT] DISPATCHED  {tag_uid} -> {tag["item_name"]}  qty ->{new_qty}')
        events.push({'type': 'pipeline', 'stage': 'dispatched',
                     'tag_uid': tag_uid, 'item_id': item_id,
                     'item_name': tag['item_name'], 'quantity': new_qty})

    elif state in ('dispatched', 'consumed'):
        _security_alert(c, conn, client, item_id, tag['item_name'], tag_uid,
                        f'SECURITY: {state} tag {tag_uid} ({tag["item_name"]}) '
                        f're-scanned at warehouse gate — possible fraud')
    else:
        print(f'[MQTT] warehouse_gate: {tag_uid} state={state}, skip')
        conn.close()


def _handle_warehouse_rack(client, payload):
    """received / returned -> racked, records shelf location.
    Invalid states are logged and rejected — no silent corruption."""
    tag_uid       = payload.get('tag_uid')
    rack_location = payload.get('rack_location', 'unknown')
    if not tag_uid:
        return

    conn = get_db()
    c = conn.cursor()
    tag = _get_tag_with_item(c, tag_uid)

    if not tag:
        conn.close()
        return

    state = tag['state']
    if state in ('received', 'returned', 'in'):
        c.execute('''UPDATE rfid_tags
                   SET state = ?, rack_location = ?, last_scan = CURRENT_TIMESTAMP
                   WHERE uid = ?''', ('racked', rack_location, tag_uid))
        c.execute('''INSERT INTO transactions
                   (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, note, device_id)
                   VALUES (?, 'warehouse_rack', 0, ?, ?, ?, ?, ?)''',
                  (tag['item_id'], tag['quantity'], tag['quantity'],
                   tag_uid, f'rack:{rack_location}', payload.get('device_id', 'unknown')))
        _attach_worker(c, c.lastrowid, payload.get('device_id'))
        conn.commit()
        conn.close()
        print(f'[MQTT] RACKED     {tag_uid} -> {tag["item_name"]}  @ {rack_location}')
        events.push({'type': 'pipeline', 'stage': 'racked',
                     'tag_uid': tag_uid, 'item_id': tag['item_id'],
                     'item_name': tag['item_name'], 'rack_location': rack_location})
    else:
        # Invalid state — log an alert instead of silently ignoring
        print(f'[MQTT] warehouse_rack: {tag_uid} REJECTED — state={state}, expected received/returned')
        c.execute('INSERT INTO alerts (item_id, alert_type, message) VALUES (?, ?, ?)',
                  (tag['item_id'], 'security',
                   f'RACK REJECTED: tag {tag_uid} ({tag["item_name"]}) cannot be racked '
                   f'from state "{state}" — expected received or returned'))
        conn.commit()
        conn.close()
        events.push({'type': 'security_alert', 'tag_uid': tag_uid,
                     'item_id': tag['item_id'], 'item_name': tag['item_name'],
                     'message': f'Rack attempt on {state} tag — possible mis-scan'})


def _handle_return_gate(client, payload):
    tag_uid = payload.get('tag_uid')
    if not tag_uid:
        return

    conn = get_db()
    c = conn.cursor()
    tag = _get_tag_with_item(c, tag_uid)

    if not tag:
        conn.close()
        return

    state = tag['state']
    if state in ('dispatched', 'consumed', 'return_pending'):
        # Atomic increment
        c.execute('''UPDATE items SET quantity = quantity + 1, updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?''', (tag['item_id'],))
        c.execute('SELECT quantity FROM items WHERE id = ?', (tag['item_id'],))
        new_qty = c.fetchone()['quantity']
        prev    = new_qty - 1
        action  = 'return_confirmed' if state == 'return_pending' else 'customer_return'
        note    = ('Return confirmed via physical scan'
                   if state == 'return_pending' else 'Customer return via return gate')
        c.execute('UPDATE rfid_tags SET state = ?, last_scan = CURRENT_TIMESTAMP WHERE uid = ?',
                  ('returned', tag_uid))
        c.execute('''INSERT INTO transactions
                   (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, note, device_id)
                   VALUES (?, ?, 1, ?, ?, ?, ?, ?)''',
                  (tag['item_id'], action, prev, new_qty, tag_uid, note,
                   payload.get('device_id', 'unknown')))
        _attach_worker(c, c.lastrowid, payload.get('device_id'))
        conn.commit()
        conn.close()
        print(f'[MQTT] RETURNED   {tag_uid} -> {tag["item_name"]}  qty ->{new_qty}')
        events.push({'type': 'pipeline', 'stage': 'returned',
                     'tag_uid': tag_uid, 'item_id': tag['item_id'],
                     'item_name': tag['item_name'], 'quantity': new_qty})
    else:
        print(f'[MQTT] return_gate: {tag_uid} state={state}, not returnable — ignore')
        conn.close()


def _check_purchase_order(c, item_id):
    """Increment received_qty on the oldest open PO for this item."""
    c.execute('''SELECT id, expected_qty, received_qty FROM purchase_orders
                 WHERE item_id = ? AND status IN ('open', 'partial')
                 ORDER BY created_at ASC LIMIT 1''', (item_id,))
    po = c.fetchone()
    if not po:
        return
    new_recv = po['received_qty'] + 1
    status   = 'complete' if new_recv >= po['expected_qty'] else 'partial'
    c.execute('UPDATE purchase_orders SET received_qty = ?, status = ? WHERE id = ?',
              (new_recv, status, po['id']))


def _handle_legacy_scan(client, payload):
    """Legacy inventory/scan topic — only acts on old states (out/in/consumed)."""
    tag_uid = payload.get('tag_uid')
    item_id = payload.get('item_id')
    if not tag_uid or not item_id:
        return

    conn = get_db()
    c = conn.cursor()
    item = _ensure_item(c, item_id)

    c.execute('SELECT state FROM rfid_tags WHERE uid = ?', (tag_uid,))
    row = c.fetchone()
    if not row:
        c.execute('INSERT INTO rfid_tags (uid, item_id, state) VALUES (?, ?, ?)',
                  (tag_uid, item_id, 'out'))
        current_state = 'out'
    else:
        current_state = row['state']

    if current_state not in ('out', 'in', 'consumed', 'return_pending'):
        conn.close()
        return

    if current_state == 'consumed':
        msg = (f'SECURITY: Consumed tag {tag_uid} re-scanned for '
               f'{item["name"]} — possible reuse attempt')
        c.execute('INSERT INTO alerts (item_id, alert_type, message) VALUES (?, ?, ?)',
                  (item_id, 'security', msg))
        conn.commit()
        conn.close()
        events.push({'type': 'rejected_scan', 'tag_uid': tag_uid,
                     'item_id': item_id, 'item_name': item['name']})
        return

    if current_state == 'return_pending':
        c.execute('''UPDATE items SET quantity = quantity + 1, updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?''', (item_id,))
        c.execute('SELECT quantity FROM items WHERE id = ?', (item_id,))
        new_qty  = c.fetchone()['quantity']
        prev_qty = new_qty - 1
        c.execute('UPDATE rfid_tags SET state = ?, last_scan = CURRENT_TIMESTAMP WHERE uid = ?',
                  ('in', tag_uid))
        c.execute('''INSERT INTO transactions
                   (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, note, device_id)
                   VALUES (?, 'return_confirmed', 1, ?, ?, ?, ?, ?)''',
                  (item_id, prev_qty, new_qty, tag_uid, 'Return confirmed — item back in stock',
                   payload.get('device_id', 'unknown')))
        _attach_worker(c, c.lastrowid, payload.get('device_id'))
        conn.commit()
        conn.close()
        events.push({'type': 'scan', 'item_id': item_id, 'item_name': item['name'],
                     'action': 'return_confirmed', 'quantity': new_qty,
                     'tag_uid': tag_uid, 'tag_state': 'in'})
        return

    if current_state == 'out':
        action, change, new_state = 'scan_in',  +1, 'in'
        c.execute('''UPDATE items SET quantity = quantity + 1, updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?''', (item_id,))
    else:
        action, change, new_state = 'scan_out', -1, 'consumed'
        c.execute('''UPDATE items SET quantity = MAX(0, quantity - 1),
                     updated_at = CURRENT_TIMESTAMP WHERE id = ?''', (item_id,))

    c.execute('SELECT quantity FROM items WHERE id = ?', (item_id,))
    new_qty  = c.fetchone()['quantity']
    prev_qty = new_qty - change  # reconstruct
    c.execute('UPDATE rfid_tags SET state = ?, last_scan = CURRENT_TIMESTAMP WHERE uid = ?',
              (new_state, tag_uid))
    c.execute('''INSERT INTO transactions
               (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid, device_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (item_id, action, change, prev_qty, new_qty, tag_uid,
               payload.get('device_id', 'unknown')))
    _attach_worker(c, c.lastrowid, payload.get('device_id'))
    if action == 'scan_out' and new_qty <= item['low_stock_threshold']:
        alert_type = 'out_of_stock' if new_qty == 0 else 'low_stock'
        alert_msg  = (f"{item['name']} is {'out of stock' if new_qty == 0 else 'low on stock'}: "
                      f"{new_qty} {item['unit']} remaining")
        c.execute('INSERT INTO alerts (item_id, alert_type, message) VALUES (?, ?, ?)',
                  (item_id, alert_type, alert_msg))
    conn.commit()
    conn.close()
    events.push({'type': 'scan', 'item_id': item_id, 'item_name': item['name'],
                 'action': action, 'quantity': new_qty,
                 'tag_uid': tag_uid, 'tag_state': new_state})


# ── Public API ────────────────────────────────────────────────────────────────

def get_status():
    return dict(status)


def get_active_sessions():
    return get_worker_sessions()


def publish(topic, payload_str):
    if _client and status['connected']:
        try:
            _client.publish(topic, payload_str)
        except Exception as e:
            print(f'[MQTT] publish failed: {e}')


def start_mqtt():
    global _client
    _load_sessions()  # Restore sessions from DB on startup

    _client = mqtt.Client()
    if MQTT_USER and MQTT_PASSWORD:
        _client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    _client.on_connect    = _on_connect
    _client.on_disconnect = _on_disconnect
    _client.on_message    = _on_message

    def _run():
        delay = 5
        while True:
            try:
                _client.connect(BROKER, PORT, keepalive=60)
                delay = 5  # reset backoff on successful connect
                _client.loop_forever()
            except Exception as e:
                status['connected'] = False
                print(f'[MQTT] Reconnecting in {delay}s ({e})')
                _time.sleep(delay)
                delay = min(delay * 2, 120)  # exponential backoff, cap at 2 minutes

    threading.Thread(target=_run, daemon=True).start()
    return _client
