"""
main.py — Multi-reader RFID pipeline node

One binary runs on all ESP32 boards.  Only config.py differs per device:
  DEVICE_ID   — unique name shown in logs
  READERS     — list of {role, cs, rack_location} dicts (one per RFID module wired)

Roles handled here:
  factory_writer  — auto-writes item/carton/pallet IDs to blank tags from a backend job queue
  factory_exit    — reads tagged products leaving the manufacturing floor
  warehouse_gate  — receives in-transit stock OR dispatches racked stock
  warehouse_rack  — confirms rack placement, records shelf location

Worker authentication (factory_exit / warehouse_gate / warehouse_rack / return_gate):
  Workers must tap their RFID badge (EMP-XXX written to tag) first.
  A 5-minute sliding-window session is created per reader role.
  If REQUIRE_WORKER_AUTH=True, item/carton/pallet scans are blocked until a badge is read.
  Badges are always forwarded to the backend so it can also track sessions.

Tag type hierarchy:
  unit   — individual item tag (item-001, item-002, …)
  carton — inner-pack aggregate tag (CTN-0001 = N units of one item)
  pallet — outer-pack aggregate tag (PLT-0001 = M cartons / P units)
  worker — employee badge tag     (EMP-001, EMP-002, …)
"""

import json
import time
import gc
import random
import network
from machine import Pin, SPI
from umqtt.simple import MQTTClient

import config
from rfid_reader import RFIDReader

# ── Write-job state (factory_writer role) ─────────────────────────────────────
_job         = None   # current active job dict {batch_id, item_id, quantity}
_job_written = 0      # tags written so far in this job

# Demo auto-cycle: item list from config, advances on each successful write
_demo_items = getattr(config, 'DEMO_ITEMS', [])
_demo_idx   = 0

# ── Worker authentication state (per reader role) ─────────────────────────────
# Keyed by role string; value: {'employee_id': str, 'expires': float}
_worker_auth = {}

# ── Onboard LED (GPIO2) ───────────────────────────────────────────────────────
_led = None   # initialised in run()


# ── Tag-type helpers ──────────────────────────────────────────────────────────

def _tag_type(item_id):
    """Classify a tag by its stored content prefix."""
    if not item_id:
        return 'blank'
    u = item_id.upper()
    if u.startswith('EMP-'): return 'worker'
    if u.startswith('CTN-'): return 'carton'
    if u.startswith('PLT-'): return 'pallet'
    return 'unit'


# ── LED helpers ───────────────────────────────────────────────────────────────

def _led_flash(n=1, on_ms=80, off_ms=80):
    """Flash onboard LED n times."""
    if _led is None:
        return
    active_low = getattr(config, 'LED_ACTIVE_LOW', False)
    for _ in range(n):
        _led.value(0 if active_low else 1)
        time.sleep_ms(on_ms)
        _led.value(1 if active_low else 0)
        time.sleep_ms(off_ms)


def _led_on_for(ms):
    """Hold LED on for ms milliseconds then off (used for error indication)."""
    if _led is None:
        return
    active_low = getattr(config, 'LED_ACTIVE_LOW', False)
    _led.value(0 if active_low else 1)
    time.sleep_ms(ms)
    _led.value(1 if active_low else 0)


# ── Worker auth helpers ───────────────────────────────────────────────────────

def _get_worker(role):
    """Return the active worker session for this role, or None if absent/expired."""
    sess = _worker_auth.get(role)
    if not sess:
        return None
    if time.time() > sess['expires']:
        del _worker_auth[role]
        return None
    return sess


def _set_worker(role, employee_id):
    """Create or refresh (sliding window) a worker session for this reader role."""
    timeout = getattr(config, 'WORKER_AUTH_TIMEOUT', 300)
    _worker_auth[role] = {
        'employee_id': employee_id,
        'expires':     time.time() + timeout,
    }


# ── Pipeline publish helper ───────────────────────────────────────────────────

def _pub(mqtt, role, payload):
    """Publish a payload dict to the correct pipeline topic for this role."""
    msg = json.dumps(payload)
    if   role == 'factory_exit':    mqtt.publish(config.TOPIC_FACTORY_EXIT,   msg)
    elif role == 'warehouse_gate':  mqtt.publish(config.TOPIC_WAREHOUSE_GATE, msg)
    elif role == 'warehouse_rack':  mqtt.publish(config.TOPIC_WAREHOUSE_RACK, msg)
    elif role == 'return_gate':     mqtt.publish(config.TOPIC_RETURNS_GATE,   msg)


# ── MQTT incoming messages (factory_writer job subscription) ──────────────────

def _on_mqtt_msg(topic, msg):
    global _job, _job_written
    if topic == config.TOPIC_FACTORY_JOB.encode():
        try:
            j = json.loads(msg.decode())
            _job        = j
            _job_written = 0
            print('[WRITER] Job received:', j['item_id'],
                  'x', j.get('quantity', 0), '  batch:', j.get('batch_id', ''))
        except Exception as e:
            print('[WRITER] Bad job payload:', e)


# ── WiFi ──────────────────────────────────────────────────────────────────────

def connect_wifi():
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if sta.isconnected():
        return
    for net in config.WIFI_NETWORKS:
        print('Trying WiFi:', net['ssid'])
        sta.connect(net['ssid'], net['password'])
        for _ in range(20):
            if sta.isconnected():
                break
            time.sleep(1)
        if sta.isconnected():
            config.MQTT_BROKER = net['broker']
            print('WiFi OK:', sta.ifconfig()[0], '  broker:', config.MQTT_BROKER)
            return
        sta.disconnect()
    print('WiFi FAILED — no networks reachable')


# ── MQTT ──────────────────────────────────────────────────────────────────────

def connect_mqtt():
    user = getattr(config, 'MQTT_USER', '')
    pw   = getattr(config, 'MQTT_PASSWORD', '')
    client = MQTTClient(config.DEVICE_ID, config.MQTT_BROKER,
                        config.MQTT_PORT, keepalive=60,
                        user=user or None, password=pw or None)
    client.set_callback(_on_mqtt_msg)
    client.connect()
    if any(r['role'] == 'factory_writer' for r in config.READERS):
        client.subscribe(config.TOPIC_FACTORY_JOB)
    print('[MQTT] Connected to', config.MQTT_BROKER)
    return client


# ── Reader initialisation ─────────────────────────────────────────────────────

def init_readers():
    spi = SPI(1, baudrate=1000000, polarity=0, phase=0,
              sck=Pin(config.RFID_SCK),
              mosi=Pin(config.RFID_MOSI),
              miso=Pin(config.RFID_MISO))
    readers = []
    for r in config.READERS:
        cs_pin = Pin(r['cs'], Pin.OUT)
        readers.append(RFIDReader(spi=spi, cs=cs_pin))
        print('[INIT] Reader role=%s  cs=GPIO%d' % (r['role'], r['cs']))
    return readers


# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    global _job, _job_written, _demo_idx, _led

    # Initialise onboard LED for feedback
    try:
        _led = Pin(getattr(config, 'LED_PIN', 2), Pin.OUT)
        _led.value(1 if getattr(config, 'LED_ACTIVE_LOW', False) else 0)
    except Exception as e:
        print('[LED] Init failed:', e)
        _led = None

    connect_wifi()
    readers          = init_readers()
    cooldowns        = [{} for _ in config.READERS]
    status_t         = 0
    _reconnect_delay = 5

    while True:
        try:
            mqtt = connect_mqtt()
            _reconnect_delay = 5

            while True:
                now = time.time()
                gc.collect()
                mqtt.check_msg()

                # Heartbeat
                if now - status_t >= config.STATUS_INTERVAL:
                    status_t = now
                    mqtt.publish(config.TOPIC_STATUS, json.dumps({
                        'device_id':    config.DEVICE_ID,
                        'roles':        [r['role'] for r in config.READERS],
                        'worker_auth':  {role: sess['employee_id']
                                         for role, sess in _worker_auth.items()
                                         if now < sess['expires']},
                        'firmware':     getattr(config, 'FIRMWARE_VERSION', 'unknown'),
                        'timestamp':    now,
                    }))

                # ── Poll each reader ──────────────────────────────────────────
                for idx, reader_cfg in enumerate(config.READERS):
                    role = reader_cfg['role']
                    cd   = cooldowns[idx]

                    # ── factory_writer ────────────────────────────────────────
                    if role == 'factory_writer':
                        # Dashboard job takes priority; fall back to demo cycle
                        if _job and _job_written < _job.get('quantity', 0):
                            write_id  = _job['item_id']
                            using_job = True
                        elif _demo_items:
                            write_id  = _demo_items[_demo_idx % len(_demo_items)]
                            using_job = False
                        else:
                            write_id  = None
                            using_job = False

                        uid, existing_id, wrote_ok = readers[idx].write_item_id(write_id)
                        if not uid:
                            continue
                        if now - cd.get(uid, 0) < config.SCAN_COOLDOWN:
                            continue
                        cd[uid] = now

                        if wrote_ok:
                            ttype = _tag_type(write_id) if write_id else 'unit'
                            if using_job:
                                _job_written += 1
                                remaining = _job['quantity'] - _job_written
                                batch_id  = _job.get('batch_id', '')
                            else:
                                _demo_idx += random.randint(1, max(1, len(_demo_items) - 1))
                                remaining = 0
                                batch_id  = 'demo'

                            _led_flash(1, on_ms=50)
                            mqtt.publish(config.TOPIC_FACTORY_WRITTEN, json.dumps({
                                'tag_uid':   uid,
                                'item_id':   write_id,
                                'tag_type':  ttype,
                                'batch_id':  batch_id,
                                'device_id': config.DEVICE_ID,
                                'written':   _job_written if using_job else _demo_idx,
                                'remaining': remaining,
                            }))
                            print('[WRITER] Wrote', write_id, '(%s)' % ttype, '->', uid)
                            if using_job and remaining == 0:
                                print('[WRITER] Job complete:', _job.get('batch_id', ''))
                                _job = None
                        elif existing_id:
                            print('[WRITER] Skip — tag', uid, 'already has:', existing_id)

                    # ── factory_exit / warehouse_gate / warehouse_rack / return_gate ──
                    else:
                        uid, item_id = readers[idx].read_tag()
                        if not uid:
                            continue
                        if now - cd.get(uid, 0) < config.SCAN_COOLDOWN:
                            continue
                        cd[uid] = now

                        ttype = _tag_type(item_id)

                        # ── Worker badge — authenticate this station ──────────
                        if ttype == 'worker':
                            _set_worker(role, item_id.upper())
                            _led_flash(3, on_ms=80, off_ms=60)  # 3 quick = auth OK
                            print('[AUTH] %s authenticated @ %s  (role=%s)'
                                  % (item_id, config.DEVICE_ID, role))
                            # Forward to backend so it can also log the session
                            _pub(mqtt, role, {
                                'tag_uid':   uid,
                                'item_id':   item_id,
                                'device_id': config.DEVICE_ID,
                                'tag_type':  'worker',
                            })
                            continue

                        # ── Worker auth gate for item/carton/pallet scans ─────
                        worker   = _get_worker(role)
                        req_auth = getattr(config, 'REQUIRE_WORKER_AUTH', True)

                        if req_auth and worker is None:
                            _led_on_for(1500)  # solid 1.5 s = access denied
                            print('[NO_AUTH] %s: scan blocked — no worker badge  uid=%s'
                                  % (role, uid))
                            continue

                        # Extend session on activity (sliding window)
                        if worker:
                            _set_worker(role, worker['employee_id'])

                        # ── Build and publish payload ─────────────────────────
                        payload = {
                            'tag_uid':   uid,
                            'item_id':   item_id,
                            'device_id': config.DEVICE_ID,
                            'worker_id': worker['employee_id'] if worker else None,
                            'tag_type':  ttype,
                        }
                        if role == 'warehouse_rack':
                            payload['rack_location'] = reader_cfg.get('rack_location', 'unknown')

                        _led_flash(1, on_ms=60)   # single brief = scan accepted
                        _pub(mqtt, role, payload)
                        print('[%s] %s -> %s  type=%s  worker=%s'
                              % (role.upper()[:6], uid, item_id, ttype,
                                 worker['employee_id'] if worker else 'none'))

                time.sleep_ms(50)

        except OSError as e:
            print('[MQTT] Lost connection:', e, '— retry in', _reconnect_delay, 's')
            time.sleep(_reconnect_delay)
            _reconnect_delay = min(_reconnect_delay * 2, 120)


run()
