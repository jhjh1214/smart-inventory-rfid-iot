"""
main.py — Simple warehouse rack reader (esp32-04)

Scan tag   → publish to inventory/warehouse/rack  (+1 on backend)
Scan again → publish again                        (-1 on backend, toggle)
No worker auth. No carton/pallet logic.
"""

import json
import time
import gc
import network
from machine import Pin, SPI
from umqtt.simple import MQTTClient

import config
from rfid_reader import RFIDReader

_led = None


def _led_flash(n=1, on_ms=80, off_ms=80):
    if _led is None:
        return
    active_low = getattr(config, 'LED_ACTIVE_LOW', False)
    for _ in range(n):
        _led.value(0 if active_low else 1)
        time.sleep_ms(on_ms)
        _led.value(1 if active_low else 0)
        time.sleep_ms(off_ms)


def connect_wifi():
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if sta.isconnected():
        return
    for net in config.WIFI_NETWORKS:
        if net['ssid'].startswith('CAMPUS_') or net['broker'].startswith('CAMPUS_'):
            continue
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


def connect_mqtt():
    client = MQTTClient(config.DEVICE_ID, config.MQTT_BROKER,
                        config.MQTT_PORT, keepalive=60)
    client.connect()
    print('[MQTT] Connected to', config.MQTT_BROKER)
    return client


def run():
    global _led

    try:
        _led = Pin(getattr(config, 'LED_PIN', 2), Pin.OUT)
        _led.value(1 if getattr(config, 'LED_ACTIVE_LOW', False) else 0)
    except Exception as e:
        print('[LED] Init failed:', e)
        _led = None

    connect_wifi()

    # Single reader on the shared SPI bus
    reader_cfg   = config.READERS[0]
    spi          = SPI(1, baudrate=1000000, polarity=0, phase=0,
                       sck=Pin(config.RFID_SCK),
                       mosi=Pin(config.RFID_MOSI),
                       miso=Pin(config.RFID_MISO))
    reader       = RFIDReader(spi=spi, cs=Pin(reader_cfg['cs'], Pin.OUT))
    rack_loc     = reader_cfg.get('rack_location', 'A1')

    cooldowns        = {}
    status_t         = 0
    reconnect_delay  = 5

    while True:
        try:
            mqtt = connect_mqtt()
            reconnect_delay = 5

            while True:
                now = time.time()
                gc.collect()
                mqtt.check_msg()

                # Heartbeat
                if now - status_t >= config.STATUS_INTERVAL:
                    status_t = now
                    mqtt.publish(config.TOPIC_STATUS, json.dumps({
                        'device_id': config.DEVICE_ID,
                        'roles':     ['warehouse_rack'],
                        'firmware':  getattr(config, 'FIRMWARE_VERSION', '1.0.0'),
                        'timestamp': now,
                    }))

                uid, item_id = reader.read_tag()
                if not uid:
                    time.sleep_ms(50)
                    continue

                # Cooldown — suppress duplicate reads of same tag
                if now - cooldowns.get(uid, 0) < config.SCAN_COOLDOWN:
                    time.sleep_ms(50)
                    continue
                cooldowns[uid] = now

                # Ignore blank tags and worker badges
                if not item_id or item_id.upper().startswith('EMP-'):
                    print('[RACK] Ignored tag:', uid, '(blank or badge)')
                    time.sleep_ms(50)
                    continue

                payload = json.dumps({
                    'tag_uid':      uid,
                    'item_id':      item_id,
                    'device_id':    config.DEVICE_ID,
                    'rack_location': rack_loc,
                    'tag_type':     'unit',
                })
                mqtt.publish(config.TOPIC_WAREHOUSE_RACK, payload)
                _led_flash(1, on_ms=80)
                print('[RACK] Published', uid, '->', item_id, '@ rack', rack_loc)

                time.sleep_ms(50)

        except OSError as e:
            print('[MQTT] Lost connection:', e, '— retry in', reconnect_delay, 's')
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 120)


run()
