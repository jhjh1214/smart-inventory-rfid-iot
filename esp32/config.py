# ═══════════════════════════════════════════════════════════════════════════════
#  DEVICE CONFIGURATION — edit these two sections per ESP32
# ═══════════════════════════════════════════════════════════════════════════════

# Unique ID for this board (shows in logs and MQTT status)
DEVICE_ID = 'esp32-01'

# One entry per RFID reader wired to this board.
# Role options:
#   factory_writer  — writes item_id to blank sticker tags (job queue from dashboard)
#   factory_exit    — scans products leaving the manufacturing floor
#   warehouse_gate  — smart gate: receives in-transit stock OR dispatches racked stock
#   warehouse_rack  — confirms shelf placement, records rack location
#   return_gate     — accepts customer returns: re-admits dispatched items to stock
#
# cs            : GPIO for that reader's Chip-Select line (each reader needs its own)
# rack_location : shelf label for warehouse_rack readers (e.g. 'A1', 'B2')
#
# ─── Optimal 3-ESP32 reference wiring ────────────────────────────────────────
#
#  ESP32 #1  Manufacturing floor (2 readers):
#    CS=22  factory_writer  — tag sticker + auto-write item_id on production line
#    CS=5   factory_exit    — final scan as products leave factory
READERS = [
    {'role': 'factory_writer', 'cs': 22, 'rack_location': None},
    {'role': 'factory_exit',   'cs':  5, 'rack_location': None},
]

#  ESP32 #2  Warehouse (2 readers):
#    CS=22  warehouse_gate  — smart receive/dispatch gate at warehouse door
#    CS=5   warehouse_rack  — rack scanner at shelf A1
# READERS = [
#     {'role': 'warehouse_gate', 'cs': 22, 'rack_location': None},
#     {'role': 'warehouse_rack', 'cs':  5, 'rack_location': 'A1'},
# ]

#  ESP32 #3  Admin / Returns desk (1 reader, optionally 2):
#    CS=22  return_gate  — scans customer returns, re-admits to stock (admin only)
#    CS=5   warehouse_rack for second shelf (optional B1 rack)
# READERS = [
#     {'role': 'return_gate',   'cs': 22, 'rack_location': None},
#     # {'role': 'warehouse_rack', 'cs': 5,  'rack_location': 'B1'},
# ]
# ─────────────────────────────────────────────────────────────────────────────
# Total readers used: 5 (writer + exit + gate + rack + return)
# Spare port on ESP32 #3 can add a second rack shelf at any time.
# ─────────────────────────────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
#  Shared settings — identical on every device
# ═══════════════════════════════════════════════════════════════════════════════

WIFI_SSID     = 'Tong@unifi'
WIFI_PASSWORD = '***REMOVED***'

MQTT_BROKER = '192.168.0.115'
MQTT_PORT   = 1883

# Shared SPI bus — all readers use the same SCK/MOSI/MISO
RFID_SCK  = 19
RFID_MOSI = 23
RFID_MISO = 25
RFID_BLOCK = 8                         # MIFARE block storing the item_id string
RFID_KEY   = b'\xff\xff\xff\xff\xff\xff'

SCAN_COOLDOWN   = 2    # seconds — suppresses same UID within this window per reader
STATUS_INTERVAL = 30   # seconds between MQTT heartbeats
DEBUG = True

# MQTT topics
TOPIC_FACTORY_JOB     = 'inventory/factory/job'       # backend  → factory_writer
TOPIC_FACTORY_WRITTEN = 'inventory/factory/written'   # factory_writer → backend
TOPIC_FACTORY_EXIT    = 'inventory/factory/exit'      # factory_exit   → backend
TOPIC_WAREHOUSE_GATE  = 'inventory/warehouse/gate'    # warehouse_gate → backend
TOPIC_WAREHOUSE_RACK  = 'inventory/warehouse/rack'    # warehouse_rack → backend
TOPIC_RETURNS_GATE    = 'inventory/returns/gate'      # return_gate    → backend
TOPIC_ALERT           = 'inventory/alert'
TOPIC_STATUS          = 'inventory/status'
TOPIC_SCAN            = 'inventory/scan'              # legacy single-reader compat
