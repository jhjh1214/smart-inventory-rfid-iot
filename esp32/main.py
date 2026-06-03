"""
main.py — Multi-reader RFID pipeline node

One binary runs on all ESP32 boards.  Only config.py differs per device:
  DEVICE_ID   — unique name shown in logs
  READERS     — list of {role, cs, rack_location} dicts (one per RFID module wired)

Roles handled here:
  factory_writer  — auto-writes item_id to blank tags from a backend job queue
  factory_exit    — reads tagged products leaving the manufacturing floor
  warehouse_gate  — receives in-transit stock OR dispatches racked stock
  warehouse_rack  — confirms rack placement, records shelf location
"""

import json
import time
import gc
import network
from machine import Pin, SPI
from umqtt.simple import MQTTClient

import config
from rfid_reader import RFIDReader

# ── Write-job state (factory_writer role) ─────────────────────────────────────
_job        = None   # current active job dict {batch_id, item_id, quantity}
_job_written = 0     # tags written so far in this job

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
    print('Connecting to WiFi...')
    sta.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    for _ in range(20):
        if sta.isconnected():
            break
        time.sleep(1)
    if sta.isconnected():
        print('WiFi OK:', sta.ifconfig()[0])
    else:
        print('WiFi FAILED — continuing without network')

# ── MQTT ──────────────────────────────────────────────────────────────────────
def connect_mqtt():
    client = MQTTClient(config.DEVICE_ID, config.MQTT_BROKER,
                        config.MQTT_PORT, keepalive=60)
    client.set_callback(_on_mqtt_msg)
    client.connect()
    # Subscribe only if a factory_writer reader is present
    if any(r['role'] == 'factory_writer' for r in config.READERS):
        client.subscribe(config.TOPIC_FACTORY_JOB)
    print('[MQTT] Connected to', config.MQTT_BROKER)
    return client

# ── Reader initialisation ─────────────────────────────────────────────────────
def init_readers():
    """
    All readers share one SPI bus (SCK/MOSI/MISO).
    Each reader gets its own CS pin from config.READERS[n]['cs'].
    """
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
    global _job, _job_written

    connect_wifi()
    readers   = init_readers()
    # Per-reader last-scan time: {idx: {uid: timestamp}}
    cooldowns = [{} for _ in config.READERS]
    status_t  = 0

    while True:
        try:
            mqtt = connect_mqtt()

            while True:
                now = time.time()
                gc.collect()

                # Pull incoming MQTT messages (non-blocking)
                mqtt.check_msg()

                # Heartbeat
                if now - status_t >= config.STATUS_INTERVAL:
                    status_t = now
                    mqtt.publish(config.TOPIC_STATUS, json.dumps({
                        'device_id': config.DEVICE_ID,
                        'roles':     [r['role'] for r in config.READERS],
                        'timestamp': now,
                    }))

                # ── Poll each reader ──────────────────────────────────────────
                for idx, reader_cfg in enumerate(config.READERS):
                    role = reader_cfg['role']
                    cd   = cooldowns[idx]

                    # ── factory_writer ────────────────────────────────────────
                    if role == 'factory_writer':
                        write_id = None
                        if _job and _job_written < _job.get('quantity', 0):
                            write_id = _job['item_id']

                        uid, existing_id, wrote_ok = readers[idx].write_item_id(write_id)
                        if not uid:
                            continue

                        # Cooldown check
                        if now - cd.get(uid, 0) < config.SCAN_COOLDOWN:
                            continue
                        cd[uid] = now

                        if wrote_ok:
                            _job_written += 1
                            remaining = _job['quantity'] - _job_written
                            mqtt.publish(config.TOPIC_FACTORY_WRITTEN, json.dumps({
                                'tag_uid':   uid,
                                'item_id':   write_id,
                                'batch_id':  _job.get('batch_id', ''),
                                'device_id': config.DEVICE_ID,
                                'written':   _job_written,
                                'remaining': remaining,
                            }))
                            print('[WRITER] Wrote', write_id, '->', uid,
                                  '| remaining:', remaining)
                            if remaining == 0:
                                print('[WRITER] Job complete:', _job.get('batch_id', ''))
                                _job = None
                        elif existing_id:
                            print('[WRITER] Skip — tag', uid, 'already has:', existing_id)
                        elif write_id is None:
                            print('[WRITER] Blank tag detected —',
                                  'waiting for write job from dashboard')

                    # ── factory_exit / warehouse_gate / warehouse_rack ─────────
                    else:
                        uid, item_id = readers[idx].read_tag()
                        if not uid:
                            continue

                        if now - cd.get(uid, 0) < config.SCAN_COOLDOWN:
                            continue
                        cd[uid] = now

                        payload = {
                            'tag_uid':   uid,
                            'item_id':   item_id,
                            'device_id': config.DEVICE_ID,
                        }

                        if role == 'factory_exit':
                            mqtt.publish(config.TOPIC_FACTORY_EXIT,
                                         json.dumps(payload))
                            print('[EXIT]', uid, '->', item_id)

                        elif role == 'warehouse_gate':
                            mqtt.publish(config.TOPIC_WAREHOUSE_GATE,
                                         json.dumps(payload))
                            print('[GATE]', uid, '->', item_id)

                        elif role == 'warehouse_rack':
                            payload['rack_location'] = reader_cfg.get('rack_location', 'unknown')
                            mqtt.publish(config.TOPIC_WAREHOUSE_RACK,
                                         json.dumps(payload))
                            print('[RACK]', uid, '->', item_id,
                                  '@ rack', payload['rack_location'])

                        elif role == 'return_gate':
                            mqtt.publish(config.TOPIC_RETURNS_GATE,
                                         json.dumps(payload))
                            print('[RETURN]', uid, '->', item_id)

                time.sleep_ms(50)

        except OSError as e:
            print('[MQTT] Lost connection, reconnecting in 5 s:', e)
            time.sleep(5)

run()
