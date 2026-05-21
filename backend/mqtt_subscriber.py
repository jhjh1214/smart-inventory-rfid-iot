import json
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt

from database import get_db
import events

BROKER = '192.168.0.115'
PORT   = 1883

TOPIC_SCAN   = 'inventory/scan'
TOPIC_ALERT  = 'inventory/alert'
TOPIC_STATUS = 'inventory/status'

status = {'connected': False, 'last_message': None, 'device_last_seen': None}


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        status['connected'] = True
        client.subscribe(TOPIC_SCAN)
        client.subscribe(TOPIC_STATUS)
        print('[MQTT] Connected to broker')
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
    except Exception:
        return

    if msg.topic == TOPIC_SCAN:
        _handle_scan(client, payload)
    elif msg.topic == TOPIC_STATUS:
        status['device_last_seen'] = datetime.now().isoformat()


def _handle_scan(client, payload):
    tag_uid = payload.get('tag_uid')
    item_id = payload.get('item_id')

    if not tag_uid or not item_id:
        return

    conn = get_db()
    c = conn.cursor()

    # Auto-create item if new
    c.execute('SELECT * FROM items WHERE id = ?', (item_id,))
    item = c.fetchone()
    if not item:
        c.execute(
            'INSERT INTO items (id, name, quantity, unit, low_stock_threshold) VALUES (?, ?, 0, ?, 5)',
            (item_id, item_id, 'pcs')
        )
        c.execute('SELECT * FROM items WHERE id = ?', (item_id,))
        item = c.fetchone()
        print(f'[MQTT] Auto-created new item: {item_id}')

    # Auto-register tag on first scan
    c.execute('SELECT state FROM rfid_tags WHERE uid = ?', (tag_uid,))
    tag = c.fetchone()
    if not tag:
        c.execute(
            'INSERT INTO rfid_tags (uid, item_id, state) VALUES (?, ?, ?)',
            (tag_uid, item_id, 'out')
        )
        current_state = 'out'
    else:
        current_state = tag['state']

    # ── Consumed tag — reject and raise security alert ────────────────────
    if current_state == 'consumed':
        msg_text = (f'SECURITY: Consumed tag {tag_uid} re-scanned for '
                    f'{item["name"]} — possible reuse attempt')
        c.execute(
            'INSERT INTO alerts (item_id, alert_type, message) VALUES (?, ?, ?)',
            (item_id, 'security', msg_text)
        )
        conn.commit()
        conn.close()
        print(f'[MQTT] REJECTED consumed tag: {tag_uid}')
        events.push({'type': 'rejected_scan', 'tag_uid': tag_uid,
                     'item_id': item_id, 'item_name': item['name']})
        return

    prev_qty = item['quantity']

    if current_state == 'out':
        action, change, new_state = 'scan_in', +1, 'in'
    else:
        # state == 'in' — item leaving inventory; mark tag consumed
        action, change, new_state = 'scan_out', -1, 'consumed'

    new_qty = max(0, prev_qty + change)

    c.execute(
        'UPDATE items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        (new_qty, item_id)
    )
    c.execute(
        'UPDATE rfid_tags SET state = ?, last_scan = CURRENT_TIMESTAMP WHERE uid = ?',
        (new_state, tag_uid)
    )
    c.execute(
        '''INSERT INTO transactions
           (item_id, action, quantity_change, previous_quantity, new_quantity, tag_uid)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (item_id, action, change, prev_qty, new_qty, tag_uid)
    )

    # Low-stock / out-of-stock alert on dispatch
    if action == 'scan_out' and new_qty <= item['low_stock_threshold']:
        alert_type = 'out_of_stock' if new_qty == 0 else 'low_stock'
        alert_msg  = (
            f"{item['name']} is {'out of stock' if new_qty == 0 else 'low on stock'}: "
            f"{new_qty} {item['unit']} remaining"
        )
        c.execute(
            'INSERT INTO alerts (item_id, alert_type, message) VALUES (?, ?, ?)',
            (item_id, alert_type, alert_msg)
        )
        client.publish(TOPIC_ALERT, json.dumps({
            'item_id':   item_id,
            'item_name': item['name'],
            'quantity':  new_qty,
            'alert_type': alert_type,
            'message':   alert_msg,
        }))

    conn.commit()
    conn.close()
    print(f'[MQTT] {tag_uid} -> {item["name"]} {action} ({prev_qty} -> {new_qty})')

    events.push({
        'type':      'scan',
        'item_id':   item_id,
        'item_name': item['name'],
        'action':    action,
        'quantity':  new_qty,
        'tag_uid':   tag_uid,
        'tag_state': new_state,
    })


def get_status():
    return dict(status)


def start_mqtt():
    client = mqtt.Client()
    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message    = _on_message

    def _run():
        while True:
            try:
                client.connect(BROKER, PORT, keepalive=60)
                client.loop_forever()
            except Exception as e:
                print(f'[MQTT] Reconnecting in 5s ({e})')
                status['connected'] = False
                time.sleep(5)

    threading.Thread(target=_run, daemon=True).start()
    return client
