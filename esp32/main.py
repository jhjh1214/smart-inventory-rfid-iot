import json
import time

from config import (DEVICE_ID, MQTT_BROKER, MQTT_PORT,
                    MQTT_TOPIC_SCAN, MQTT_TOPIC_STATUS,
                    SCAN_COOLDOWN, STATUS_INTERVAL, DEBUG)
from rfid_reader import RFIDReader
from umqtt.simple import MQTTClient

print("=== Smart Inventory ESP32 ===")


def connect_mqtt():
    client = MQTTClient(DEVICE_ID, MQTT_BROKER, MQTT_PORT)
    client.connect()
    if DEBUG:
        print("MQTT connected")
    return client


def publish_status(client):
    payload = json.dumps({"device_id": DEVICE_ID, "status": "online", "timestamp": time.time()})
    try:
        client.publish(MQTT_TOPIC_STATUS, payload)
    except Exception:
        pass


rfid = RFIDReader()
mqtt = connect_mqtt()

last_uid         = None
last_scan_time   = 0
last_status_time = 0

while True:
    now = time.time()

    # Heartbeat
    if now - last_status_time >= STATUS_INTERVAL:
        publish_status(mqtt)
        last_status_time = now

    uid, item_id = rfid.read_tag()

    if uid and uid != last_uid and (now - last_scan_time) >= SCAN_COOLDOWN:
        if item_id:
            payload = json.dumps({
                "device_id": DEVICE_ID,
                "tag_uid":   uid,
                "item_id":   item_id,
                "timestamp": now
            })
            try:
                mqtt.publish(MQTT_TOPIC_SCAN, payload)
                if DEBUG:
                    print("Scanned:", uid, "->", item_id)
            except Exception as e:
                if DEBUG:
                    print("MQTT publish failed:", e)
                try:
                    mqtt = connect_mqtt()
                except Exception:
                    pass
        else:
            if DEBUG:
                print("Tag", uid, "has no item data written")

        last_uid       = uid
        last_scan_time = now

    elif not uid:
        last_uid = None   # allow re-scanning same tag after it's removed

    time.sleep(0.1)