# Smart Inventory Management System — RFID & IoT

**Final Year Project — TAI KE YING DOROTHY**

A full-stack, industrial-grade inventory management system built around RFID tags, ESP32 microcontrollers, MQTT messaging, and a real-time web dashboard. Products are tagged at the manufacturing line and tracked automatically through every stage — factory floor, warehouse gate, shelf placement, dispatch, and customer returns — with a complete, tamper-evident audit trail of who handled what, where, and when.

---

## System Architecture

```
 ESP32 #1          ESP32 #2          ESP32 #3          ESP32 #4
 esp32-01          esp32-02          esp32-03          esp32-04
[factory_writer]  [factory_exit]  [warehouse_gate]  [warehouse_rack]
      │                 │                 │                 │
      └─────────────────┴─────────────────┴─────────────────┘
                                  │
                        MQTT (Mosquitto) — LAN port 1883
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
                       │   http://server:5000 │
                       └─────────────────────┘
```

### On-Premise Deployment

The default setup runs **entirely on a local company network** — no cloud, no public URL required:

- A mini-PC (or Raspberry Pi 4) runs Mosquitto + Flask + SQLite on the LAN.
- All ESP32 boards connect to the same Wi-Fi network and publish to the LAN broker.
- Dashboard staff access `http://inventory.company.local` via an internal DNS A record.
- Remote management can be added via VPN (no port forwarding to the public internet).

### Cloud Deployment

The decoupled architecture (ESP32 → MQTT broker → Flask backend → database) supports full cloud deployment with minimal code changes — directly addressing the FYP objective of *real-time data synchronisation between RFID devices and cloud storage*:

| Component | Cloud equivalent |
|-----------|-----------------|
| SQLite (`inventory.db`) | PostgreSQL on any managed provider (Supabase, Railway, Neon, AWS RDS) — swap `sqlite3` for `psycopg2`, change `?` placeholders to `%s`, `AUTOINCREMENT` → `SERIAL` |
| Flask app + MQTT subscriber | Deploy to Railway, Render, Fly.io, or any Linux VM |
| Mosquitto broker | Run on the same cloud server, or use a managed broker (HiveMQ Cloud / EMQX Cloud free tier) |
| ESP32 boards | Change `broker` in `config.py` to the cloud server hostname — no other firmware change required |

Once the broker hostname is set in `config.py`, each ESP32 connects over any Wi-Fi network to the cloud backend — no fixed LAN IP, no VPN.

---

## Hardware

### Components

| Qty | Part | Purpose |
|-----|------|---------|
| 4 | ESP32 Dev Board | RFID pipeline nodes (one per station) |
| 4 | RC522 RFID Reader (MFRC522) | One per board |
| N | MIFARE Classic 1K tags | Product stickers + worker badges |
| 1 | PC / Laptop | Flask server + Mosquitto broker |

### ESP32 Pin Wiring (identical on every board)

```
RC522 Pin   →   ESP32 GPIO
─────────────────────────
SDA (CS)    →   GPIO 22
SCK         →   GPIO 19
MOSI        →   GPIO 23
MISO        →   GPIO 25
GND         →   GND
3.3V        →   3.3V
RST         →   3.3V  (hardwired HIGH — no software reset needed)
```

> **Important:** RC522 runs on **3.3V only** — do NOT connect to 5V.

### 4-Board Reference Layout

```
ESP32 #1 (esp32-01) — Factory Writer
  CS=22  →  factory_writer   writes item_id to blank sticker tags (auto-cycles demo items)

ESP32 #2 (esp32-02) — Factory Exit
  CS=22  →  factory_exit     scans products leaving the manufacturing floor

ESP32 #3 (esp32-03) — Warehouse Gate
  CS=22  →  warehouse_gate   smart gate: receives in-transit stock OR dispatches racked stock

ESP32 #4 (esp32-04) — Warehouse Rack
  CS=22  →  warehouse_rack   shelf placement (qty +1), shelf removal (qty -1),
                              and return finalisation when tag is return_pending (qty +1)
```

Boards 1–3 run a shared multi-role `main.py`. Board 4 runs a simplified `main.py` (rack-only, no worker auth). All boards share `rfid_reader.py`, `mfrc522.py`, and `boot.py`. Only `config.py` differs per board.

> **Worker authentication** is implemented in the backend and ESP32 firmware but is currently disabled on board 4 (`REQUIRE_WORKER_AUTH = False`) due to hardware constraints. Re-enable it per board by setting `REQUIRE_WORKER_AUTH = True` in `config.py` when additional reader capacity is available.

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
                    [warehouse_rack]          │  (picked off shelf)
                                              ▼
                                           picked
                                              │
                    [warehouse_gate]          │  (exits building — qty confirmed out)
                                              ▼
                                          dispatched ──► (qty -1)  TERMINAL
                                              │
                    [dashboard admin]         │  (mark for return)
                                              ▼
                                       return_pending
                                              │
                    [warehouse_rack]          │  (worker places back on shelf)
                                              ▼
                                           returned ──► (qty +1)
                                              │
                    [warehouse_rack]          │  (same scan, two audit rows)
                                              ▼
                                           racked
                                              │  (cycle repeats)
```

**Return flow (4-board setup):** Because board 4 (warehouse rack) is the only available station after dispatch, returns are handled in two steps:
1. Admin or supervisor marks the tag as `return_pending` from the dashboard (Tags tab → Return button).
2. A warehouse worker physically places the item back on the shelf and scans it at board 4. The rack reader detects the `return_pending` state and finalises the return — qty +1, state `racked`, action `rack_return`.

A dedicated return-desk board can be added later; the `return_gate` MQTT topic and backend handler are already implemented for when that hardware is available.

**Legacy mode** (single-reader `inventory/scan` topic):

```
out → in → consumed → return_pending → in → ...
```

**Security:** Any tag in `dispatched` or `consumed` state that is re-scanned at a gate triggers a **security alert** in the dashboard and an MQTT alert broadcast.

---

## Worker RFID Authentication

Workers carry RFID badge tags (written with their employee ID, e.g. `EMP-001`). When a worker taps their badge on any station reader, the backend:

1. Detects the `EMP-` prefix and routes to the worker auth handler (not the product pipeline).
2. Creates a **5-minute session** on that device.
3. All product transactions from that device during the session record `performed_by = "Alice Tan (EMP-001)"`.
4. Session expiry or badge re-tap renews the timer.

**Supervisor dispatch enforcement:** Warehouse dispatch (goods leaving) requires an active supervisor session on the gate device. If no supervisor is authenticated, an alert is raised in the dashboard with timestamp and device ID. The dispatch still proceeds physically to avoid deadlocking warehouse operations, but the event is flagged for investigation.

### Worker Zones

Workers can be assigned a `zone` (e.g., `warehouse`, `factory`, `returns`) to scope their access geographically within the facility.

### Pre-seeded Workers

| Employee ID | Name | Role |
|-------------|------|------|
| EMP-001 | Alice Tan | supervisor |
| EMP-002 | Bob Lim | operator |
| EMP-003 | Carol Wong | operator |
| EMP-004 | David Ng | operator |

Write these to physical RFID tags using `tag_writer.py` (see Setup below).

---

## Accountability & Audit Trail

Every transaction in the system — whether from a physical scanner or a dashboard action — records **who** did it, **where**, and **what device** was involved.

### device_id Column

All `transactions` rows carry a `device_id` field:

| Value | Meaning |
|-------|---------|
| `'dashboard'` | Action performed through the web UI |
| `'esp32-factory'` | Physical scan at manufacturing ESP32 |
| `'esp32-warehouse'` | Physical scan at warehouse ESP32 |
| `'esp32-returns'` | Physical scan at returns desk |
| `'system'` | Auto-generated by migration / seeding |

### Dashboard Actor Tracking

Dashboard-triggered transactions record the logged-in username. If the account has a linked RFID badge (via `badge_uid`), the record shows `"alice [badge:A1B2C3D4]"`, tying the digital action to a physical badge holder.

### Audit Trail Tab

The dashboard Audit Trail tab is visible to **all roles** (viewer, manager, admin). No administrator can hide their own actions. Filters available:

- **All** — full transaction log
- **Dashboard Actions** — web UI actions only (`device_id = 'dashboard'`)
- **Physical Scans** — ESP32 scanner actions only
- **Admin Actions** — item adds, deletes, manual adjustments

The log can be exported as CSV.

---

## Dashboard

Access at `http://<server>:5000` — login required.

### Tabs

| Tab | Contents |
|-----|----------|
| **Overview** | KPI cards (total items, total quantity, low stock count, active alerts), live transaction feed, recent alerts |
| **Inventory** | Full item table with search, add / edit / delete items, manual quantity adjustment |
| **Analytics** | Transaction trends (7-day bar chart), ABC classification, inventory health score, demand forecast, EOQ, risk scores |
| **RFID Tags** | All registered tags with UID, current state, rack location, last scan time |
| **Workers** | Active station sessions (live), register / edit workers with zone and badge UID |
| **Manufacturing** | Pipeline stage counts, per-item stage breakdown, rack utilisation, write job queue |
| **Alerts** | Security alerts, low stock and out-of-stock notifications, filterable |
| **Audit Trail** | Full tamper-evident transaction log with device, actor, and note columns |

### Role-Based Access Control

| Role | Access |
|------|--------|
| `admin` | All tabs + user account management + delete items/tags/workers |
| `manager` | All tabs + add/edit items + create write jobs + manage workers |
| `viewer` | Overview, Inventory (read-only), Alerts, Audit Trail (read-only) |

### Dashboard Account Management (Admin)

Admins can create, edit, and delete dashboard login accounts from within the interface:

- Assign role (`admin`, `manager`, `viewer`)
- Link account to a physical RFID badge (`badge_uid`) for hardware-tied identity
- Link to an internal employee record (`employee_id`)

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

All ESP32 payloads include a `device_id` field identifying the originating board.

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

### Payload Format (ESP32 → backend)

```json
{
  "device_id": "esp32-warehouse",
  "tag_uid": "A1B2C3D4",
  "timestamp": 1720000000
}
```

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
│   │   └── dashboard.html      # Main dashboard (sidebar + 8 tabs)
│   └── static/
│       ├── css/style.css       # GitHub-style sidebar, skeleton loaders, toasts, modals
│       └── js/dashboard.js     # Tab logic, Chart.js, SSE handler, RBAC, audit/user mgmt
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
| GET | `/api/me` | any | Current user info (includes badge_uid, employee_id) |

### Items
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/items` | viewer+ | List all items |
| POST | `/api/items` | manager+ | Create item (logs `item_added` transaction) |
| PUT | `/api/items/<id>` | viewer+ | Update item (quantity change logs `manual_adjust`) |
| DELETE | `/api/items/<id>` | admin | Delete item and its tags (logs `item_deleted`) |

### RFID Tags
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/tags` | viewer+ | All tags with state and rack location |
| POST | `/api/tags` | any | Register tag manually |
| POST | `/api/tags/<uid>/return` | admin | Admin force-return (dispatched → returned) |
| DELETE | `/api/tags/<uid>` | admin | Delete tag record (logs `tag_removed`) |

### Workers
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/workers` | viewer+ | All workers + active station sessions |
| POST | `/api/workers` | manager+ | Register worker |
| PUT | `/api/workers/<id>` | manager+ | Update name/role/zone/active |
| DELETE | `/api/workers/<id>` | admin | Delete worker |
| GET | `/api/workers/sessions` | any | Currently authenticated stations |

### Users (Dashboard Accounts)
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/users` | admin | List all dashboard accounts |
| POST | `/api/users` | admin | Create account |
| PUT | `/api/users/<id>` | admin | Update role / badge_uid / employee_id |
| DELETE | `/api/users/<id>` | admin | Delete account (cannot delete own) |

### Audit Trail
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/audit` | viewer+ | Transaction log; `?filter=dashboard\|physical\|admin&limit=N` |

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

## Database Schema

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `items` | id, name, quantity, unit, low_stock_threshold | Product catalogue |
| `rfid_tags` | uid, item_id, state, rack_location, last_scan | Tag registry with pipeline state |
| `transactions` | action, quantity_change, tag_uid, performed_by, **device_id**, note, timestamp | Full audit log |
| `alerts` | item_id, alert_type, message, acknowledged | Low stock + security events |
| `users` | username, password_hash, role, **badge_uid**, **employee_id** | Dashboard login accounts |
| `workers` | employee_id, name, uid, role, **zone**, active, last_seen | Worker registry |
| `write_jobs` | batch_id, item_id, quantity, written, status | Factory write job queue |

**Bold** = columns added in the accountability refactor.

### Transaction `action` Values

| Action | Trigger |
|--------|---------|
| `scan_in` | Legacy reader — item received |
| `scan_out` | Legacy reader — item dispatched |
| `tagged` | factory_writer — new tag written |
| `in_transit` | factory_exit — product left factory |
| `received` | warehouse_gate — product arrived |
| `racked` | warehouse_rack — shelf confirmed |
| `dispatched` | warehouse_gate — product sent out |
| `returned` | return_gate — customer return |
| `item_added` | Dashboard — new item created |
| `item_deleted` | Dashboard — item deleted |
| `tag_removed` | Dashboard — tag deregistered |
| `return_requested` | Dashboard — admin force-return |
| `manual_adjust` | Dashboard — quantity edited |

---

## Setup

### 1. Backend

**Requirements:** Python 3.9+, Mosquitto MQTT broker

```bash
pip install flask werkzeug paho-mqtt
```

Start Mosquitto on the LAN server (default port 1883), then:

```bash
cd backend
python app.py
```

The database (`inventory.db`) is created automatically on first run with demo items and default accounts.

Update the broker IP in `backend/mqtt_subscriber.py` if not using `192.168.0.115`.

#### Production Deployment (Linux)

For a persistent on-premise service, use gunicorn + systemd:

```bash
pip install gunicorn
```

`/etc/systemd/system/inventory.service`:

```ini
[Unit]
Description=Smart Inventory Backend
After=network.target mosquitto.service

[Service]
User=inventory
WorkingDirectory=/opt/smart-inventory-rfid-iot/backend
ExecStart=/usr/local/bin/gunicorn -w 2 -b 0.0.0.0:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable inventory
sudo systemctl start inventory
```

Set an internal DNS A record:

```
inventory.company.local  →  <server LAN IP>
```

### 2. ESP32 Firmware

**Requirements:** MicroPython v1.24+ flashed on each ESP32, `mpremote`, `esptool`

#### Step 1 — Flash MicroPython firmware (do this once per board)

```
python -m esptool --chip esp32 --port COM<N> erase-flash
python -m esptool --chip esp32 --port COM<N> --baud 460800 write_flash -z 0x1000 ESP32_GENERIC-20260406-v1.28.0.bin
```

#### Step 2 — Set `config.py` for each board, then upload

Boards 1–3 use the shared multi-role `main.py`. Board 4 uses its own simplified `main.py` (rack reader only). All boards share `rfid_reader.py`, `mfrc522.py`, and `boot.py`. Only `config.py` changes per board.

**Board 1 — esp32-01 — Factory Writer**
```python
DEVICE_ID = 'esp32-01'
READERS = [
    {'role': 'factory_writer', 'cs': 22, 'rack_location': None},
]
# DEMO_ITEMS list auto-cycles item-001..item-008 when no dashboard job is active
DEMO_ITEMS = ['item-001','item-002','item-003','item-004',
              'item-005','item-006','item-007','item-008']
```

**Board 2 — esp32-02 — Factory Exit**
```python
DEVICE_ID = 'esp32-02'
READERS = [
    {'role': 'factory_exit', 'cs': 22, 'rack_location': None},
]
```

**Board 3 — esp32-03 — Warehouse Gate**
```python
DEVICE_ID = 'esp32-03'
READERS = [
    {'role': 'warehouse_gate', 'cs': 22, 'rack_location': None},
]
```

**Board 4 — esp32-04 — Warehouse Rack**
```python
DEVICE_ID = 'esp32-04'
READERS = [
    {'role': 'warehouse_rack', 'cs': 22, 'rack_location': 'A1'},
]
```

All boards share the same WiFi credentials in `WIFI_NETWORKS`. Set the broker IP to the laptop's LAN IP (`ipconfig` to find it).

#### Step 3 — Upload files (run from the `esp32/` folder)

```
mpremote connect COM<N> cp config.py :config.py + cp mfrc522.py :mfrc522.py + cp rfid_reader.py :rfid_reader.py + cp main.py :main.py + reset
```

After upload, power the board via any USB charger — `main.py` runs automatically on boot.

### 3. Write Worker Badges

```bash
mpremote connect COM<N>
# Press Ctrl+C to stop main.py
>>> import tag_writer
```

Follow the interactive prompts to write `EMP-001` through `EMP-004` to four RFID tags. These become worker authentication badges.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Microcontroller | ESP32 (MicroPython 1.23) |
| RFID | MFRC522 / RC522, MIFARE Classic 1K |
| Messaging | MQTT via Mosquitto; paho-mqtt (backend), umqtt.simple (ESP32) |
| Backend | Python 3, Flask, SQLite (on-premise) / PostgreSQL (cloud) |
| Real-time push | Server-Sent Events (SSE) |
| Frontend | Tailwind CSS (CDN), Chart.js v4, vanilla JS |
| Auth | Flask sessions, Werkzeug PBKDF2-SHA256 password hashing |
| Deployment | On-premise: Gunicorn + systemd + LAN; Cloud: Railway / Render + managed PostgreSQL + cloud MQTT broker |
