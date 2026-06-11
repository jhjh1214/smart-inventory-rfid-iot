# ══════════════════════════════════════════════════════════════════════════════
#  DEVICE CONFIGURATION — edit these two sections per ESP32 board
# ══════════════════════════════════════════════════════════════════════════════

DEVICE_ID = 'esp32-04'

# ── WiFi networks (tried in order until one connects) ─────────────────────────
# broker: the IP of the laptop running Mosquitto on that network.
# Find it by running  ipconfig  in Command Prompt on the laptop.
WIFI_NETWORKS = [
    {
        'ssid':     'Tong@unifi',
        'password': '***REMOVED***',
        'broker':   '192.168.0.115',
    },
    {
        'ssid':     'CAMPUS_WIFI_NAME',    # <-- fill in before campus demo
        'password': 'CAMPUS_PASSWORD',     # <-- fill in
        'broker':   'CAMPUS_LAPTOP_IP',    # <-- run ipconfig at campus, fill in
    },
]

# Fallback broker — overwritten at boot by whichever WiFi connects above
MQTT_BROKER = '192.168.0.115'
MQTT_PORT   = 1883

# ── Multi-reader layout (used when running full pipeline firmware) ────────────
#
#  ESP32 #1  Manufacturing (2 readers):
#    CS=22  factory_writer    CS=5   factory_exit
#
#  ESP32 #2  Warehouse (2 readers):
#    CS=22  warehouse_gate    CS=5   warehouse_rack  rack_location='A1'
#
#  ESP32 #3  Returns desk (1 reader):
#    CS=22  return_gate
#
READERS = [
    {'role': 'warehouse_rack', 'cs': 22, 'rack_location': 'A1'},
]

# ══════════════════════════════════════════════════════════════════════════════
#  Shared settings — identical on every board
# ══════════════════════════════════════════════════════════════════════════════

# RFID GPIO — shared SPI bus, one CS per reader
RFID_SCK  = 19
RFID_MOSI = 23
RFID_MISO = 25
RFID_CS   = 22        # default CS for single-reader / standalone mode
RFID_BLOCK = 8        # MIFARE block that stores the item_id string
RFID_KEY   = b'\xff\xff\xff\xff\xff\xff'

SCAN_COOLDOWN   = 2   # seconds — suppress re-scan of same UID per reader
STATUS_INTERVAL = 30  # seconds between MQTT heartbeats
DEBUG           = True

# ── Worker authentication ──────────────────────────────────────────────────────
# Workers tap their RFID badge (EMP-XXX) at a station to authenticate.
# Item/carton/pallet scans are attributed to that worker for the next N seconds.
LED_PIN             = 2    # GPIO2 — onboard LED (active HIGH on most ESP32 dev boards)
LED_ACTIVE_LOW      = False  # set True if your board's LED is active LOW
WORKER_AUTH_TIMEOUT = 300   # seconds — sliding-window session TTL per reader
REQUIRE_WORKER_AUTH = False  # True: item scans silently blocked until badge tapped
                              # False: item scans proceed but logged as 'system'

# ── MQTT authentication (leave blank if broker has no auth) ───────────────────
MQTT_USER     = ''
MQTT_PASSWORD = ''

FIRMWARE_VERSION = '1.0.0'

# ── MQTT topics ───────────────────────────────────────────────────────────────

# Legacy / simple demo (single reader, in-out)
MQTT_TOPIC_SCAN   = 'inventory/scan'
MQTT_TOPIC_STATUS = 'inventory/status'

# Full pipeline (multi-reader firmware)
TOPIC_FACTORY_JOB     = 'inventory/factory/job'
TOPIC_FACTORY_WRITTEN = 'inventory/factory/written'
TOPIC_FACTORY_EXIT    = 'inventory/factory/exit'
TOPIC_WAREHOUSE_GATE  = 'inventory/warehouse/gate'
TOPIC_WAREHOUSE_RACK  = 'inventory/warehouse/rack'
TOPIC_RETURNS_GATE    = 'inventory/returns/gate'
TOPIC_ALERT           = 'inventory/alert'
TOPIC_STATUS          = 'inventory/status'
TOPIC_SCAN            = 'inventory/scan'
