# Smart Inventory Management System — RFID & IoT

**Thesis Project — TAI KE YING DOROTHY**

A full-stack, industrial-grade inventory management system built around RFID tags, ESP32 microcontrollers, MQTT messaging, and a real-time web dashboard. Products are tagged at the manufacturing line and tracked automatically through every stage — factory floor, warehouse gate, shelf placement, dispatch, and customer returns — with a complete audit trail of who handled what and when.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Manufacturing Floor       │  Warehouse              │  Returns  │
│                            │                         │           │
│  [factory_writer]          │  [warehouse_gate]       │[return_gate]
│  ESP32 #1 (CS=22)          │  ESP32 #2 (CS=22)       │ESP32 #3   │
│                            │                         │(CS=22)    │
│  [factory_exit]            │  [warehouse_rack]       │           │
│  ESP32 #1 (CS=5)           │  ESP32 #2 (CS=5)        │           │
└────────────────────────────┴─────────────────────────┴───────────┘
              │                        │                      │
              └──────────── MQTT (Mosquitto) ─────────────────┘
                                       │
                            ┌──────────▼──────────┐
                            │   Flask Backend      │
                            │   (Python + SQLite)  │
                            │   analytics.py       │
                            │   mqtt_subscriber.py │
                            └──────────┬──────────┘
                                       │ SSE (live push)
                            ┌──────────▼──────────┐
                            │   Web Dashboard      │
                            │   Tailwind + Chart.js│
                            └─────────────────────┘
```

---

## Hardware

### Components

| Qty | Part | Purpose |
|-----|------|---------|
| 3 | ESP32 Dev Board | RFID pipeline nodes |
| 5 | RC522 RFID Reader (MFRC522) | One per station |
| N | MIFARE Classic 1K tags | Product stickers + worker badges |
| 1 | PC / Raspberry Pi | Flask server + Mosquitto broker |

### ESP32 Pin Wiring (all boards identical except CS pins)

```
RC522 Pin   →   ESP32 GPIO
─────────────────────────
SDA (CS)    →   GPIO 22  (Reader 1)
            →   GPIO 5   (Reader 2, if board has 2 readers)
SCK         →   GPIO 19
MOSI        →   GPIO 23
MISO        →   GPIO 25
GND         →   GND
3.3V        →   3.3V
RST         →   not required (hardwired high)
```

All readers on the same board share **one SPI bus** (SCK/MOSI/MISO). Only the CS line is unique per reader.

### 3-ESP32 Reference Layout

```
ESP32 #1 — Manufacturing Floor
  CS=22  →  factory_writer   (writes item_id to blank sticker tags)
  CS=5   →  factory_exit     (scans products leaving the factory)

ESP32 #2 — Warehouse
  CS=22  →  warehouse_gate   (smart receive / dispatch gate)
  CS=5   →  warehouse_rack   (confirms shelf placement, records location)

ESP32 #3 — Admin / Returns Desk
  CS=22  →  return_gate      (customer returns — re-admits stock)
  CS=5   →  (spare — add a second rack shelf at any time)
```

---

## Tag Lifecycle — State Machine

Every RFID tag follows a strict one-way state machine. Once dispatched, a tag can only re-enter via the **return gate** — preventing fraud and duplicate registration.

```
                    [factory_writer]
  blank tag  ──────────────────────────►  tagged
                                              │
                    [factory_exit]            │
                                              ▼
                                          in_transit
                                              │
                    [warehouse_gate]          │
                                              ▼
                                          received  ──► (qty +1)
                                              │
                    [warehouse_rack]          │
                                              ▼
                                           racked
                                              │
                    [warehouse_gate]          │
                                              ▼
                                          dispatched ──► (qty -1)  TERMINAL
                                              │
                    [return_gate]             │  (customer return)
                                              ▼
                                           returned  ──► (qty +1)
                                              │
                    [warehouse_rack]          │
                                              ▼
                                           racked
                                              │  (cycle repeats)
```

**Security:** Any tag in `dispatched` or `consumed` state that is re-scanned at a gate triggers a **security alert** in the dashboard and an MQTT alert message.

---

## Worker RFID Authentication

Workers carry RFID badge tags (written with their employee ID, e.g. `EMP-001`). When a worker taps their badge on any station reader, the backend:

1. Detects the `EMP-` prefix and routes to the worker auth handler (not the product pipeline)
2. Creates a **5-minute session** on that device
3. All product transactions from that device during the session record `performed_by = "Alice Tan (EMP-001)"`
4. Session expiry or badge re-tap renews the timer

This gives a full, tamper-evident audit trail of who handled each item at every stage.

### Pre-seeded Workers

| Employee ID | Name | Role |
|-------------|------|------|
| EMP-001 | Alice Tan | supervisor |
| EMP-002 | Bob Lim | operator |
| EMP-003 | Carol Wong | operator |
| EMP-004 | David Ng | operator |

Write these to physical RFID tags using `tag_writer.py` (see Setup below).

---

## Dashboard

Access at `http://<server>:5000` — login required.

### Tabs

| Tab | Contents |
|-----|----------|
| **Overview** | KPI cards (total items, qty, low stock, alerts), live transaction feed, recent alerts |
| **Inventory** | Full item table, add / edit / delete, manual quantity adjustment |
| **Analytics** | Transaction trends (7-day bar chart), ABC classification, inventory health score, demand forecast, EOQ, risk scores |
| **RFID Tags** | All registered tags with UID, current state, rack location, last scan time |
| **Workers** | Active station sessions (live), register/edit workers, badge UID association |
| **Manufacturing** | Pipeline stage counts, per-item stage breakdown, rack utilisation, write job queue |
| **Alerts** | Security alerts, low stock and out-of-stock notifications |

### Role-Based Access Control

| Role | Access |
|------|--------|
| `admin` | All tabs + user management + delete items/tags/workers |
| `manager` | All tabs + add/edit items + create write jobs + manage workers |
| `viewer` | Overview, Inventory (read-only), Alerts only |

### Default Accounts

| Username | Password | Role |
|----------|----------|------|
| admin | admin123 | admin |
| manager | manager123 | manager |
| viewer | viewer123 | viewer |

**Change default passwords before any production deployment.**

---

## Analytics Engine

`backend/analytics.py` provides:

- **Transaction trends** — daily scan_in / scan_out counts for the last N days
- **ABC analysis** — classifies items by transaction volume (top 20% = A, next 30% = B, rest = C)
- **Demand forecasting** — exponential smoothing (α = 0.3) over last 30 days of daily usage
- **EOQ (Economic Order Quantity)** — `√(2DS/H)` where D = annual demand, S = 10 (reorder cost), H = 0.5 (holding cost)
- **Risk scoring** — days of stock remaining bucketed into low / medium / high risk
- **Pipeline summary** — tag counts per stage, per-item breakdown, rack utilisation, write job history

---

## MQTT Topics

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `inventory/factory/job` | backend → ESP32 | Dispatch a write job to factory_writer |
| `inventory/factory/written` | ESP32 → backend | Confirm tag written (updates write_jobs table) |
| `inventory/factory/exit` | ESP32 → backend | Product leaving manufacturing floor |
| `inventory/warehouse/gate` | ESP32 → backend | Smart gate: receive or dispatch |
| `inventory/warehouse/rack` | ESP32 → backend | Shelf placement confirmation |
| `inventory/returns/gate` | ESP32 → backend | Customer return re-admission |
| `inventory/alert` | backend → all | Low stock / security alert broadcast |
| `inventory/status` | ESP32 → backend | Heartbeat (every 30 s) |
| `inventory/scan` | ESP32 → backend | Legacy single-reader compat |

---

## Project Structure

```
smart-inventory-rfid-iot/
│
├── backend/
│   ├── app.py                  # Flask app, all REST endpoints, RBAC decorators
│   ├── database.py             # SQLite schema, migrations, demo seeds
│   ├── mqtt_subscriber.py      # MQTT client, pipeline state machine, worker sessions
│   ├── analytics.py            # ABC analysis, forecasting, EOQ, pipeline summary
│   ├── events.py               # SSE event bus (thread-safe queue per client)
│   ├── templates/
│   │   ├── login.html          # Login page
│   │   └── dashboard.html      # Main dashboard (sidebar + 7 tabs)
│   └── static/
│       ├── css/style.css       # Sidebar layout, cards, badges, animations
│       └── js/dashboard.js     # Tab logic, Chart.js charts, SSE handler, RBAC
│
└── esp32/
    ├── config.py               # Per-board config: DEVICE_ID, READERS list, WiFi, MQTT
    ├── main.py                 # Firmware: multi-reader poll loop, role dispatch, MQTT
    ├── rfid_reader.py          # RFIDReader class: read_tag(), write_item_id()
    ├── mfrc522.py              # Low-level MFRC522 driver (MicroPython)
    ├── tag_writer.py           # Interactive tag writing utility (setup / demo)
    └── boot.py                 # MicroPython boot stub
```

---

## REST API

### Auth
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/login` | — | Login page |
| POST | `/api/login` | — | Authenticate, returns role |
| POST | `/api/logout` | any | End session |
| GET | `/api/me` | any | Current user info |

### Items
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/items` | viewer+ | List all items |
| POST | `/api/items` | manager+ | Create item |
| PUT | `/api/items/<id>` | viewer+ | Update item (quantity logs a transaction) |
| DELETE | `/api/items/<id>` | admin | Delete item and its tags |

### RFID Tags
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/tags` | viewer+ | All tags with state and rack location |
| POST | `/api/tags` | any | Register tag manually |
| POST | `/api/tags/<uid>/return` | admin | Admin force-return (dispatched/consumed → returned) |
| DELETE | `/api/tags/<uid>` | admin | Delete tag record |

### Workers
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/workers` | viewer+ | All workers + active station sessions |
| POST | `/api/workers` | manager+ | Register worker |
| PUT | `/api/workers/<id>` | manager+ | Update name/role/active |
| DELETE | `/api/workers/<id>` | admin | Delete worker |
| GET | `/api/workers/sessions` | any | Currently authenticated stations |

### Manufacturing
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/factory/jobs` | viewer+ | Write job history |
| POST | `/api/factory/jobs` | manager+ | Create write job + dispatch to ESP32 |
| GET | `/api/pipeline` | viewer+ | Pipeline stage counts, rack stats, jobs |

### Analytics
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/analytics/summary` | viewer+ | Inventory health metrics |
| GET | `/api/analytics/trends?days=7` | viewer+ | Daily transaction trend data |
| GET | `/api/analytics/abc` | viewer+ | ABC item classification |
| GET | `/api/events` | any | SSE stream (real-time push) |

---

## Setup

### 1. Backend

**Requirements:** Python 3.9+, Mosquitto MQTT broker

```bash
pip install flask werkzeug paho-mqtt
```

Start Mosquitto on the server (default port 1883), then:

```bash
cd backend
python app.py
```

The database (`inventory.db`) is created automatically on first run with demo items and default user accounts.

Update the broker IP in `backend/mqtt_subscriber.py` line 31 if not using `192.168.0.115`.

### 2. ESP32 Firmware

**Requirements:** MicroPython flashed on each ESP32, `mpremote` or Thonny IDE

1. Edit `esp32/config.py` — set `DEVICE_ID`, `WIFI_SSID`, `WIFI_PASSWORD`, `MQTT_BROKER`, and the `READERS` list for each board:

```python
# ESP32 #1 — Manufacturing
DEVICE_ID = 'esp32-factory'
READERS = [
    {'role': 'factory_writer', 'cs': 22, 'rack_location': None},
    {'role': 'factory_exit',   'cs':  5, 'rack_location': None},
]

# ESP32 #2 — Warehouse
DEVICE_ID = 'esp32-warehouse'
READERS = [
    {'role': 'warehouse_gate', 'cs': 22, 'rack_location': None},
    {'role': 'warehouse_rack', 'cs':  5, 'rack_location': 'A1'},
]

# ESP32 #3 — Returns
DEVICE_ID = 'esp32-returns'
READERS = [
    {'role': 'return_gate', 'cs': 22, 'rack_location': None},
]
```

2. Upload all files in `esp32/` to each board (same firmware, only `config.py` differs):

```bash
mpremote connect COM<N> cp esp32/config.py :config.py
mpremote connect COM<N> cp esp32/main.py :main.py
mpremote connect COM<N> cp esp32/rfid_reader.py :rfid_reader.py
mpremote connect COM<N> cp esp32/mfrc522.py :mfrc522.py
mpremote connect COM<N> cp esp32/boot.py :boot.py
```

3. Reset the board — `main.py` runs automatically on boot.

### 3. Write Worker Badges

```bash
mpremote connect COM<N>
# Press Ctrl+C to stop main.py
>>> import tag_writer
```

Follow the interactive prompts to write `EMP-001` through `EMP-004` to four RFID tags. These become worker authentication badges.

---

## Database Schema

| Table | Purpose |
|-------|---------|
| `items` | Product catalogue — id, name, quantity, unit, low_stock_threshold |
| `rfid_tags` | Tag registry — uid, item_id, state, rack_location, last_scan |
| `transactions` | Full audit log — action, quantity_change, tag_uid, performed_by, note |
| `alerts` | Low stock + security events |
| `users` | Dashboard login accounts — username, password_hash, role |
| `workers` | Worker registry — employee_id, name, uid, role, active, last_seen |
| `write_jobs` | Factory write job queue — batch_id, item_id, quantity, written, status |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Microcontroller | ESP32 (MicroPython) |
| RFID | MFRC522 / RC522, MIFARE Classic 1K |
| Messaging | MQTT (paho-mqtt on backend, umqtt.simple on ESP32) |
| Backend | Python 3, Flask, SQLite |
| Real-time push | Server-Sent Events (SSE) |
| Frontend | Tailwind CSS (CDN), Chart.js v4, vanilla JS |
| Auth | Flask sessions, Werkzeug password hashing (PBKDF2-SHA256) |
