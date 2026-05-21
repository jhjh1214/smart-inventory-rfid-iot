# ── WiFi ──────────────────────────────────────────────────────────────────
WIFI_SSID     = "Tong@unifi"
WIFI_PASSWORD = "***REMOVED***"

# ── MQTT ──────────────────────────────────────────────────────────────────
MQTT_BROKER = "192.168.0.115"
MQTT_PORT   = 1883
DEVICE_ID   = "inventory-esp32-01"

MQTT_TOPIC_SCAN   = "inventory/scan"
MQTT_TOPIC_STATUS = "inventory/status"

# ── RFID GPIO pins ────────────────────────────────────────────────────────
RFID_SCK  = 19
RFID_MOSI = 23
RFID_MISO = 25
RFID_CS   = 22

# ── RFID tag data ─────────────────────────────────────────────────────────
RFID_BLOCK = 8
RFID_KEY   = b'\xff\xff\xff\xff\xff\xff'

# ── System ────────────────────────────────────────────────────────────────
SCAN_COOLDOWN   = 2    # seconds between repeated scans of same tag
STATUS_INTERVAL = 30   # seconds between heartbeat publishes
DEBUG           = True