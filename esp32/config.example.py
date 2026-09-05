# ══════════════════════════════════════════════════════════════════════════════
#  DEVICE CONFIGURATION — edit these two sections per ESP32 board
#
#  TEMPLATE. Copy to  esp32/config.py  and fill in your own Wi-Fi credentials.
#  config.py is gitignored because it holds real network passwords in plaintext
#  (MicroPython has no keystore) — never commit it.
# ══════════════════════════════════════════════════════════════════════════════

DEVICE_ID = 'esp32-04'

# ── WiFi networks (tried in order until one connects) ─────────────────────────
# broker: the IP of the laptop running Mosquitto on that network.
# Find it by running  ipconfig  in Command Prompt on the laptop.
WIFI_NETWORKS = [
    {
        'ssid':     'YOUR-WIFI-SSID',
        'password': 'YOUR-WIFI-PASSWORD',
        'broker':   '192.168.0.100',      # the PC running Mosquitto, from ipconfig
    },
    # Add one entry per network the boards may meet; boot.py tries them in order
    # and takes the broker IP from whichever connects.
]

# Fallback broker — overwritten at boot by whichever WiFi connects above
MQTT_BROKER = '192.168.0.100'
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
REQUIRE_WORKER_AUTH = False  # Rack reader: no badge required
                              # False: item scans proceed but logged as 'system'

# ── Factory writer demo mode ────────────────────────────────────────────────────
# When True, factory_writer overwrites any existing data on a scanned tag instead
# of skipping it — lets a small set of physical tags be reused across demo runs.
# Leave False in production: a real deployment should never silently relabel a
# tag that's already been issued to a different item.
DEMO_OVERWRITE_TAGS = False

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
