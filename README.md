# Smart Inventory Management System — RFID & IoT

**Final Year Project — TAI KE YING DOROTHY**

A full-stack inventory management system built around RFID tags, ESP32 microcontrollers, MQTT
messaging, and a real-time web dashboard. Products are tagged at the manufacturing line and
tracked automatically through every stage — factory floor, warehouse gate, shelf placement,
dispatch, and customer returns — with an append-only audit trail of who handled what, where,
and when.

Goods can be tracked at three levels of granularity: **individual units**, **cartons**
(N units under one tag), and **pallets** (M cartons under one tag) — one pallet scan at the gate
moves the entire load.

---

## Table of Contents

- [System Architecture](#system-architecture)
- [Hardware](#hardware)
- [Tag Lifecycle — State Machine](#tag-lifecycle--state-machine)
- [Tag Hierarchy — Units, Cartons, Pallets](#tag-hierarchy--units-cartons-pallets)
- [Worker RFID Authentication](#worker-rfid-authentication)
- [Accountability & Audit Trail](#accountability--audit-trail)
- [Dashboard](#dashboard)
- [Analytics Engine](#analytics-engine)
- [Integrations — Webhooks, CSV, Backup](#integrations--webhooks-csv-backup)
- [MQTT Topics](#mqtt-topics)
- [Project Structure](#project-structure)
- [REST API](#rest-api)
- [Database Schema](#database-schema)
- [Setup](#setup)
- [Moving the Project to a New Device](#moving-the-project-to-a-new-device)
- [Testing](#testing)
- [Security Posture & Known Limitations](#security-posture--known-limitations)
- [Tech Stack](#tech-stack)

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
                       ┌──────────▼───────────┐
                       │   Flask Backend      │
                       │   app.py  (REST/SSE) │
                       │   mqtt_subscriber.py │  ← pipeline state machine
                       │   analytics.py       │
                       │   SQLite inventory.db│
                       └──────────┬───────────┘
                                  │ SSE (live push)   ──► outbound webhooks
                       ┌──────────▼───────────┐
                       │   Web Dashboard      │
                       │   Tailwind + Chart.js│
                       │   http://server:5000 │
                       └──────────────────────┘
```

The backend process runs three concurrent concerns:

1. **Flask HTTP server** — REST API, dashboard templates, and the SSE endpoint.
2. **MQTT subscriber thread** (daemon) — consumes every ESP32 scan and drives the state machine.
3. **SSE event bus** (`events.py`) — a queue per connected browser; the MQTT thread pushes,
   Flask streams.

> **Important:** `init_db()` and `start_mqtt()` are invoked from `app.py`'s `__main__` block.
> Any WSGI launcher must call them explicitly — see [Production Deployment](#production-deployment-linux).

### On-Premise Deployment

The default setup runs entirely on a local company network — no cloud, no public URL:

- A mini-PC (or Raspberry Pi 4) runs Mosquitto + Flask + SQLite on the LAN.
- All ESP32 boards join the same Wi-Fi and publish to the LAN broker.
- Staff reach the dashboard at `http://inventory.company.local` via an internal DNS A record.
- Remote management via VPN — no port forwarding to the public internet.

> The dashboard currently loads Tailwind, Chart.js, and jsPDF from public CDNs. A genuinely
> air-gapped install must vendor these four files locally first — see
> [Known Limitations](#security-posture--known-limitations).

### Cloud Deployment

The decoupled architecture (ESP32 → MQTT broker → Flask backend → database) supports cloud
deployment with minimal code changes:

| Component | Cloud equivalent |
|-----------|-----------------|
| SQLite (`inventory.db`) | PostgreSQL on any managed provider (Supabase, Railway, Neon, AWS RDS) — swap `sqlite3` for `psycopg2`, change `?` placeholders to `%s`, `AUTOINCREMENT` → `SERIAL` |
| Flask app + MQTT subscriber | Railway, Render, Fly.io, or any Linux VM |
| Mosquitto broker | Same cloud server, or a managed broker (HiveMQ Cloud / EMQX Cloud) |
| ESP32 boards | Set `broker` in `config.py` to the cloud hostname — no other firmware change |

---

## Hardware

### Components

| Qty | Part | Purpose |
|-----|------|---------|
| 4 | ESP32 Dev Board | RFID pipeline nodes (one per station) |
| 4 | RC522 RFID Reader (MFRC522) | One per board |
| N | MIFARE Classic 1K tags | Product stickers, carton/pallet labels, worker badges |
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

> **Important:** RC522 runs on **3.3 V only** — do NOT connect to 5 V.

Item identifiers are stored in **MIFARE block 8**, 16 bytes, zero-padded, authenticated with
key A `FF FF FF FF FF FF` (`RFID_BLOCK` / `RFID_KEY` in `config.py`).

### 4-Board Reference Layout

```
ESP32 #1 (esp32-01) — Factory Writer
  CS=22  →  factory_writer   writes item_id to blank tags (auto-cycles demo items)

ESP32 #2 (esp32-02) — Factory Exit
  CS=22  →  factory_exit     scans products leaving the manufacturing floor

ESP32 #3 (esp32-03) — Warehouse Gate
  CS=22  →  warehouse_gate   smart gate: receives in-transit stock OR dispatches racked stock

ESP32 #4 (esp32-04) — Warehouse Rack
  CS=22  →  warehouse_rack   shelf placement, shelf removal (pick), and return finalisation
```

`config.py` supports **multiple readers per board** via the `READERS` list (shared SPI bus, one
CS line each). The `main.py` currently in the repository is the **single-reader rack variant**
for `esp32-04`; the multi-role firmware for boards 1–3 uses the same `config.py` contract.
All boards share `rfid_reader.py`, `mfrc522.py`, and `boot.py` — only `config.py` differs.

> **Worker authentication** is implemented in both backend and firmware but is disabled on
> board 4 (`REQUIRE_WORKER_AUTH = False`) due to reader-capacity constraints. Re-enable per
> board when additional readers are available.

---

## Tag Lifecycle — State Machine

Every tag follows a one-way state machine. Once dispatched, a tag can only re-enter through an
explicit return path — preventing duplicate registration and re-use fraud.

```
                    [factory_writer]
  blank tag  ──────────────────────────►  tagged
                                              │
                    [factory_exit]            ▼
                                          in_transit
                                              │
                    [warehouse_gate]          ▼
                                          received  ──► (qty +1)
                                              │
                    [warehouse_rack]          ▼
                                           racked   ──► (qty  0)
                                              │
                    [warehouse_rack]          ▼  (picked off shelf)
                                           picked   ──► (qty  0)
                                              │
                    [warehouse_gate]          ▼  (exits building)
                                          dispatched ──► (qty −1)  TERMINAL
                                              │
                    [dashboard admin]         ▼  (mark for return)
                                       return_pending
                                              │
                    [warehouse_rack]          ▼  (worker re-shelves)
                                           returned ──► (qty +1)
                                              │
                    [warehouse_rack]          ▼  (same scan, two audit rows)
                                           racked
```

**The reader's meaning depends on the tag's state, not the topic:**

| Station | Tag state on arrival | Action | Qty |
|---------|---------------------|--------|-----|
| `warehouse_gate` | `in_transit`, `out` | receive | **+N** |
| `warehouse_gate` | `received`, `racked`, `picked`, `returned`, `in` | dispatch | **−N** |
| `warehouse_gate` | `dispatched`, `consumed` | **security alert** | 0 |
| `warehouse_rack` | `return_pending` | return + rack (2 audit rows) | **+1** |
| `warehouse_rack` | `racked` | pick off shelf | 0 |
| `warehouse_rack` | unknown tag | standalone rack add | **+1** |
| `warehouse_rack` | `dispatched`, `consumed` | **security alert** | 0 |
| `factory_exit` | `tagged`, `out` | leave factory | 0 |
| `factory_exit` | unregistered tag | **security alert** (tag not created) | 0 |
| `returns/gate` | `dispatched`, `consumed`, `return_pending` | customer return | **+1** |

**Return flow on the 4-board setup.** Because board 4 is the only station after dispatch,
returns are two steps:

1. An admin marks the tag `return_pending` from the dashboard (Tags tab → Return).
2. A worker physically re-shelves the item and scans it at board 4. The rack reader detects
   `return_pending` and finalises: qty +1, state `racked`, two transactions (`returned`, then
   `racked`).

A dedicated return desk can be added later — the `inventory/returns/gate` topic and its handler
are already implemented.

**Legacy mode** (single-reader `inventory/scan` topic), retained for the simple demo:

```
out → in → consumed → return_pending → in → ...
```

**Quantity is clamped at zero** (`MAX(0, quantity − N)`) so stock can never go negative.

---

## Tag Hierarchy — Units, Cartons, Pallets

Routing is decided by the **prefix of the identifier written to the tag**, intercepted in the
MQTT message handler:

| Prefix | Level | Handler | Effect of one gate scan |
|--------|-------|---------|------------------------|
| `EMP-` | worker badge | `_handle_worker_badge` | opens a 5-minute session on that device |
| `CTN-` | carton | `_carton_*` | moves `unit_count` units of one SKU |
| `PLT-` | pallet | `_pallet_*` | moves every carton on the pallet, grouped by SKU |
| anything else | unit | `_handle_*` | moves 1 unit |

- **Cartons** (`cartons` table) are created from the dashboard with an item and a unit count,
  then bound to a physical tag either by scanning at the factory writer or via
  `PUT /api/cartons/<id>/tag`. IDs auto-generate as `CTN-0001`, `CTN-0002`, …
- **Pallets** (`pallets` + `pallet_cartons`) group cartons. IDs auto-generate as `PLT-0001`, …
  A single pallet scan at the gate produces one `warehouse_receive` / `warehouse_dispatch`
  transaction *per SKU* on the pallet and cascades the state to every carton.
- `rfid_tags.tag_level` records `unit` | `carton` | `pallet`; `unit_count` records the multiplier.

---

## Worker RFID Authentication

Workers carry badge tags written with their employee ID (`EMP-001`, …). When a worker taps a
badge on any station reader, the backend:

1. Detects the `EMP-` prefix and routes to the worker handler, bypassing the product pipeline.
2. Creates a **5-minute session keyed on `device_id`** (`WORKER_SESSION_TTL = 300`).
3. Attributes every subsequent transaction from that device to
   `performed_by = "Alice Tan (EMP-001)"` and `worker_id = "EMP-001"`.
4. Renews the timer on any re-tap. Inactive workers (`active = 0`) are denied and a
   `worker_denied` event is pushed to the dashboard.

Sessions are mirrored to the `worker_sessions` table so they **survive a backend restart**.

**Supervisor dispatch enforcement.** Dispatch at the warehouse gate — unit, carton, or pallet —
requires an active session whose role is `supervisor`. If none exists, a `security` alert
(`UNVERIFIED DISPATCH`) is raised with the device ID and timestamp. **The dispatch still
completes**: this is a deliberate choice so a missing badge cannot deadlock warehouse
operations. The control is detective, not preventive.

### Worker Zones

Workers carry a `zone` (`warehouse`, `factory`, `general`, …) to scope them geographically.
Zone is recorded on the session but is not currently used to reject scans.

### Pre-seeded Workers

| Employee ID | Name | Role | Zone |
|-------------|------|------|------|
| EMP-001 | Alice Tan | supervisor | warehouse |
| EMP-002 | Bob Lim | operator | warehouse |
| EMP-003 | Carol Wong | operator | factory |
| EMP-004 | David Ng | operator | factory |

Write these to physical tags with `tag_writer.py` (see [Setup](#3-write-worker-badges)).
An unknown `EMP-` badge is **auto-provisioned** as a new `operator` on first tap.

---

## Accountability & Audit Trail

Every transaction — physical scan or dashboard action — records **who**, **where**, and
**on what device**. Nothing in the codebase updates or deletes a `transactions` row.

### device_id Column

| Value | Meaning |
|-------|---------|
| `dashboard` | Action performed through the web UI |
| `esp32-01` … `esp32-04` | Physical scan at the named station |
| `system` | Auto-generated by migration / seeding |

### Dashboard Actor Tracking

Dashboard actions record the logged-in username. If the account has a linked badge
(`users.badge_uid`), the record reads `alice [badge:A1B2C3D4]`, tying the digital action to a
physical badge holder.

### Audit Trail Tab

Visible to **all roles** — no administrator can hide their own actions. Filters:

- **All** — full transaction log
- **Dashboard Actions** — `device_id = 'dashboard'`
- **Physical Scans** — everything except `dashboard` and `system`
- **Admin Actions** — `item_added`, `item_deleted`, `tag_removed`, `return_requested`,
  `manual_adjust`, `tag_reassigned`

Exportable as CSV and PDF (jsPDF + autoTable) from the dashboard.

---

## Dashboard

Access at `http://<server>:5000` — login required.

### Tabs

| Tab | Contents |
|-----|----------|
| **Overview** | KPI cards, 7-day transaction trend chart, inventory status and tag-state doughnuts, live scan feed, quick stats |
| **Inventory** | Item table with search, add / edit / delete, manual quantity adjustment, CSV import & export |
| **Analytics** | Current stock levels, days-remaining risk chart, per-item forecast / EOQ / risk / anomaly table |
| **RFID Tags** | Rack inventory by location, tag-state reference, all active tags with state, level, rack, last scan; return request and tag reassignment |
| **Workers** | Live station sessions, worker registry with zone and role, role-access summary, dashboard account management, webhook configuration |
| **Manufacturing** | Pipeline stage counts, per-item stage breakdown, rack utilisation, carton management, pallet management, purchase orders |
| **Alerts** | Security, low-stock, and out-of-stock notifications, filterable, mark-read / clear |
| **Audit Trail** | Full append-only transaction log with device, actor, note; CSV / PDF export |

Live updates arrive over `/api/events` (SSE) — scans, security alerts, worker
authentication, and job creation all repaint the relevant panel without a page reload.

### Role-Based Access Control

Enforced server-side by `@login_required`, `@manager_required`, `@admin_required`. Frontend
role gating is UX only.

| Role | Access |
|------|--------|
| `admin` | Everything: user accounts, webhooks, DB backup, delete items / tags / workers, tag return & reassign |
| `manager` | Items, workers, write jobs, cartons, pallets, purchase orders, CSV import, stock reservation |
| `viewer` | Read access to all tabs; see the RBAC gap note in [Security Posture](#security-posture--known-limitations) |

### Default Accounts

| Username | Password | Role |
|----------|----------|------|
| admin | admin123 | admin |
| manager | manager123 | manager |
| viewer | viewer123 | viewer |

**Change these before any deployment.** Note that `database.py` re-creates the `manager` and
`viewer` accounts on every startup if they are missing — deleting them is not permanent.

---

## Analytics Engine

`backend/analytics.py`. scikit-learn and numpy are **optional**: if either is missing, the
module degrades gracefully to the statistical fallbacks.

| Feature | Method |
|---------|--------|
| **Demand forecast** | Gradient Boosting Regressor over lag-1/2/3 + 3-day and 7-day rolling means (needs ≥ 7 days of history); falls back to exponential smoothing (α = 0.3) |
| **EOQ** | `√(2DS/H)` — D = annual demand, S = 10 (reorder cost), H = 0.5 (holding cost) |
| **Risk score** | Days of stock remaining → 90 (≤ 3 d), 60 (≤ 7 d), 20 (otherwise) |
| **ABC analysis** | Items ranked by transaction volume — top 20 % = A, next 30 % = B, rest = C |
| **Anomaly detection** | Isolation Forest (contamination 0.05) over the last 500 transactions, featurised as hour-of-day, weekday, |Δqty|, inter-scan gap |
| **Transaction trends** | Daily received / dispatched counts, zero-filled for missing days |
| **Inventory summary** | Health score, low-stock / out-of-stock / dead-stock counts, today's scans, today's security events |
| **Pipeline summary** | Tag counts per stage, per-item stage breakdown, rack utilisation, write-job history |

> **Known gap:** the demand, trend, and ABC queries filter on the legacy `scan_in` / `scan_out`
> actions, which the current pipeline does not emit (it writes `warehouse_receive` /
> `warehouse_dispatch`). Until those queries are widened, forecasting and ABC see no pipeline
> traffic. Anomaly detection and the pipeline/inventory summaries are unaffected.

---

## Integrations — Webhooks, CSV, Backup

**Outbound webhooks** (`webhooks` table, admin-managed). Each webhook subscribes to a
comma-separated event list, or `*` for all. Deliveries are fired on daemon threads with a 5 s
timeout, so a slow endpoint never blocks a scan.

Event types currently fired: `low_stock`, `security`. Payload:

```json
{
  "event": "low_stock",
  "data": { "item_id": "item-003", "item_name": "AA Batteries (pack)",
            "quantity": 2, "alert_type": "low_stock" },
  "timestamp": "2026-09-04T14:22:05.123456"
}
```

`POST /api/webhooks/<id>/test` sends a synchronous `test` ping and reports success or failure.

**CSV import / export.** `GET /api/export/items` downloads the catalogue; `POST /api/import/items`
accepts a CSV with columns `id, name, quantity, unit, low_stock_threshold`, upserting rows and
writing an `item_added` or `manual_adjust` transaction for each change. Per-row errors are
returned in the response without aborting the import.

**Database backup.** `GET /api/export/backup` (admin) downloads a timestamped copy of
`inventory.db`.

---

## MQTT Topics

Broker address is read from the `MQTT_BROKER` / `MQTT_PORT` environment variables
(defaults `127.0.0.1:1883`); optional `MQTT_USER` / `MQTT_PASSWORD` enable broker auth.
The subscriber reconnects automatically with exponential backoff (5 s → 120 s cap).

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `inventory/factory/job` | backend → ESP32 | Dispatch a write job to factory_writer |
| `inventory/factory/written` | ESP32 → backend | Tag written; registers tag, increments `write_jobs.written` |
| `inventory/factory/exit` | ESP32 → backend | Product leaving the manufacturing floor |
| `inventory/warehouse/gate` | ESP32 → backend | Smart gate: receive or dispatch by state |
| `inventory/warehouse/rack` | ESP32 → backend | Shelf placement, pick, or return finalisation |
| `inventory/returns/gate` | ESP32 → backend | Customer return re-admission |
| `inventory/alert` | backend → all | Low-stock / out-of-stock broadcast |
| `inventory/status` | ESP32 → backend | Heartbeat (every 30 s) |
| `inventory/scan` | ESP32 → backend | Legacy single-reader compatibility |

### Payload Format (ESP32 → backend)

```json
{
  "device_id": "esp32-04",
  "tag_uid": "A1B2C3D4",
  "item_id": "item-001",
  "rack_location": "A1",
  "tag_type": "unit",
  "worker_id": "EMP-002"
}
```

`item_id` carries whatever was read from tag block 8 — a SKU, `CTN-0001`, `PLT-0001`, or
`EMP-002`. `rack_location` is sent only by rack readers; `worker_id` only by firmware that
tracks badge sessions locally.

---

## Project Structure

```
smart-inventory-rfid-iot/
│
├── backend/
│   ├── app.py                  Flask app, ~65 REST endpoints, RBAC, SSE, webhooks
│   ├── database.py             SQLite schema, 14 versioned migrations, demo seeds
│   ├── mqtt_subscriber.py      MQTT client, pipeline state machine, worker sessions,
│   │                             carton/pallet handlers
│   ├── analytics.py            Forecasting, EOQ, ABC, Isolation-Forest anomalies, summaries
│   ├── events.py               SSE event bus (thread-safe queue per client)
│   ├── inventory.db            Runtime database (gitignored, auto-created)
│   ├── templates/
│   │   ├── login.html
│   │   └── dashboard.html      Sidebar + 8 tabs + 13 modals
│   ├── static/
│   │   ├── css/style.css       Sidebar, skeleton loaders, toasts, modals
│   │   └── js/dashboard.js     Tabs, Chart.js, SSE, RBAC gating, all CRUD
│   └── tests/                  pytest suite — 387 tests across 13 modules
│
├── esp32/
│   ├── config.py               Per-board: DEVICE_ID, READERS, Wi-Fi list, broker, topics
│   ├── boot.py                 Wi-Fi bring-up, selects broker IP per matched network
│   ├── main.py                 Rack-reader firmware (esp32-04 variant)
│   ├── rfid_reader.py          RFIDReader.read_tag() / write_item_id()
│   ├── mfrc522.py              Low-level MFRC522 driver (MicroPython)
│   └── tag_writer.py           Interactive tag-writing utility (setup / demo)
│
├── mosquitto.conf              listener 1883, allow_anonymous true
├── start.bat / start.ps1       Windows one-click launcher
├── requirements-pc.txt         Backend runtime dependencies
├── requirements-test.txt       Test dependencies
├── pytest.ini                  testpaths=backend/tests, pythonpath=backend
├── CLAUDE.md                   Design spec + invariants (session context)
├── README.md                   This file
└── reference.md                Report-oriented reference (maps to FYP chapters)
```

---

## REST API

Auth column: **—** public · **viewer+** any logged-in user · **manager+** manager or admin ·
**admin** admin only.

### Auth & Session
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/login` | — | Login page |
| POST | `/api/login` | — | Authenticate (rate-limited: 10 failures / 5 min per IP) |
| POST | `/api/logout` | — | End session |
| GET | `/api/me` | viewer+ | Current user, badge_uid, employee_id |
| PUT | `/api/users/<id>/password` | viewer+ | Change password (self requires current password; admin does not) |

### Core
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/` | viewer+ | Dashboard |
| GET | `/api/events` | viewer+ | SSE stream (25 s keepalive) |
| GET | `/api/status` | — | MQTT connection + last-message timestamps |
| GET | `/api/dashboard` | viewer+ | KPI counters |
| GET | `/api/transactions?limit=N` | viewer+ | Recent transactions with item names |

### Items
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/items` | viewer+ | All items with computed `available_qty` |
| POST | `/api/items` | manager+ | Create item (logs `item_added`) |
| PUT | `/api/items/<id>` | viewer+ ⚠ | Update; quantity change logs `manual_adjust` |
| DELETE | `/api/items/<id>` | admin | Delete item, its tags, POs and write jobs (logs `item_deleted`) |
| POST | `/api/items/<id>/reserve` | manager+ | Reserve N units against available stock |
| DELETE | `/api/items/<id>/reserve` | manager+ | Release N reserved units |

⚠ `PUT /api/items/<id>` is `@login_required`, so viewers can edit items — see
[Security Posture](#security-posture--known-limitations).

### RFID Tags & Rack
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/tags` | viewer+ | All tags with state, level, rack, last scan |
| POST | `/api/tags` | viewer+ ⚠ | Register/replace a tag→item mapping |
| POST | `/api/tags/<uid>/return` | admin | Mark dispatched/consumed tag `return_pending` |
| POST | `/api/tags/<uid>/reassign` | admin | Transfer state to a new physical UID (damaged label) |
| DELETE | `/api/tags/<uid>` | admin | Deregister tag (logs `tag_removed`) |
| GET | `/api/rack` | viewer+ | Racked tags grouped by rack location |

### Cartons & Pallets
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/cartons` | viewer+ | All cartons with item names |
| POST | `/api/cartons` | manager+ | Create carton (auto ID `CTN-NNNN`) |
| PUT | `/api/cartons/<id>/tag` | manager+ | Bind a tag UID to a carton |
| DELETE | `/api/cartons/<id>` | manager+ | Delete carton and its pallet links |
| GET | `/api/pallets` | viewer+ | Pallets with nested cartons and unit totals |
| POST | `/api/pallets` | manager+ | Create pallet (auto ID `PLT-NNNN`) |
| POST | `/api/pallets/<id>/cartons` | manager+ | Add a carton to a pallet |
| DELETE | `/api/pallets/<id>/cartons/<cid>` | manager+ | Remove a carton from a pallet |
| PUT | `/api/pallets/<id>/tag` | manager+ | Bind a tag UID to a pallet |
| DELETE | `/api/pallets/<id>` | manager+ | Delete pallet and its links |

### Alerts
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/alerts` | viewer+ | 50 most recent alerts |
| POST | `/api/alerts/<id>/read` | viewer+ | Mark one read |
| POST | `/api/alerts/read-all` | viewer+ | Mark all read |
| DELETE | `/api/alerts/read` | viewer+ | Delete all read alerts |

### Workers
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/workers` | viewer+ | Workers + their live station sessions |
| POST | `/api/workers` | manager+ | Register worker (`EMP-` prefix enforced) |
| PUT | `/api/workers/<id>` | manager+ | Update name / role / zone / active |
| DELETE | `/api/workers/<id>` | admin | Delete worker |
| GET | `/api/workers/sessions` | viewer+ | Currently authenticated stations |

### Dashboard Accounts
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/users` | admin | List accounts |
| POST | `/api/users` | admin | Create account (password ≥ 6 chars) |
| PUT | `/api/users/<id>` | admin | Update role / badge_uid / employee_id |
| DELETE | `/api/users/<id>` | admin | Delete account (cannot delete own) |

### Manufacturing & Procurement
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/pipeline` | viewer+ | Stage counts, per-item breakdown, rack stats, jobs |
| GET | `/api/factory/jobs` | viewer+ | Write job history (30 most recent) |
| POST | `/api/factory/jobs` | manager+ | Create write job and publish it to the writer board |
| GET | `/api/purchase-orders?status=` | viewer+ | Purchase orders |
| POST | `/api/purchase-orders` | manager+ | Create PO |
| PUT | `/api/purchase-orders/<id>` | manager+ | Update; status auto-advances open → partial → complete |
| DELETE | `/api/purchase-orders/<id>` | manager+ | Delete PO |

### Analytics
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/analytics` | viewer+ | Per-item forecast, EOQ, risk, anomaly flag |
| GET | `/api/analytics/summary` | viewer+ | Inventory health metrics |
| GET | `/api/analytics/trends?days=7` | viewer+ | Daily trend data |
| GET | `/api/analytics/abc` | viewer+ | ABC classification |
| GET | `/api/analytics/anomalies` | viewer+ | Isolation-Forest anomalous transactions |

### Audit, Webhooks, Data
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/audit?filter=&limit=` | viewer+ | Transaction log; filter `all\|dashboard\|physical\|admin` |
| GET | `/api/webhooks` | admin | List webhooks |
| POST | `/api/webhooks` | admin | Create webhook |
| PUT | `/api/webhooks/<id>` | admin | Update name / url / events / active |
| DELETE | `/api/webhooks/<id>` | admin | Delete webhook |
| POST | `/api/webhooks/<id>/test` | admin | Send a test ping |
| GET | `/api/export/items` | viewer+ | Items as CSV |
| POST | `/api/import/items` | manager+ | Upsert items from CSV upload |
| GET | `/api/export/backup` | admin | Download a copy of `inventory.db` |

---

## Database Schema

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `items` | id, name, quantity, reserved_qty, unit, low_stock_threshold | Product catalogue |
| `rfid_tags` | uid, item_id, state, tag_level, unit_count, parent_uid, rack_location, previous_uid, last_scan | Tag registry + pipeline state |
| `transactions` | action, quantity_change, previous/new_quantity, tag_uid, performed_by, worker_id, device_id, note, timestamp | Append-only audit log |
| `alerts` | item_id, alert_type, message, is_read, timestamp | Low-stock, out-of-stock, security events |
| `users` | username, password_hash, role, badge_uid, employee_id | Dashboard login accounts |
| `workers` | employee_id, name, uid, role, zone, active, last_seen | Physical worker registry |
| `worker_sessions` | device_id, employee_id, name, role, zone, expires_at | Restart-durable badge sessions |
| `write_jobs` | batch_id, item_id, quantity, written, status | Factory write-job queue |
| `purchase_orders` | item_id, expected_qty, received_qty, status, note, created_by | Inbound procurement |
| `webhooks` | name, url, events, active | Outbound integrations |
| `cartons` | id, item_id, unit_count, tag_uid, state | Carton / inner-pack grouping |
| `pallets` | id, tag_uid, state | Pallet / trolley grouping |
| `pallet_cartons` | pallet_id, carton_id | Pallet ↔ carton join |
| `schema_version` | version, applied_at, description | Migration ledger (14 applied) |

Connections open with `journal_mode=WAL`, `busy_timeout=3000`, `foreign_keys=ON`.

### Transaction `action` Values

| Action | Emitted by |
|--------|-----------|
| `tag_write` | factory_writer — tag registered (unit or carton) |
| `factory_exit` | factory_exit — product left the factory |
| `warehouse_receive` | warehouse_gate — stock arrived (+N) |
| `warehouse_rack` | warehouse_rack — shelf placement (0) |
| `racked` | warehouse_rack — shelf placement following a return (0) |
| `rack_add` | warehouse_rack — brand-new tag added directly at the shelf (+1) |
| `rack_remove` | warehouse_rack — picked off the shelf (0) |
| `warehouse_dispatch` | warehouse_gate — stock left the building (−N) |
| `returned` | warehouse_rack — return finalised (+1) |
| `customer_return` | returns/gate — return via the return desk (+1) |
| `return_confirmed` | returns/gate or legacy scan — pending return confirmed (+1) |
| `return_requested` | Dashboard — admin marked a tag for return |
| `scan_in` / `scan_out` | Legacy single-reader mode |
| `item_added` / `item_deleted` | Dashboard — catalogue changes |
| `manual_adjust` | Dashboard — quantity edited or CSV import change |
| `tag_removed` | Dashboard — tag deregistered or its item deleted |
| `tag_reassigned` | Dashboard — tag moved to a replacement UID |

---

## Setup

### 1. Backend

**Requirements:** Python 3.9+, Mosquitto MQTT broker.

```bash
pip install -r requirements-pc.txt
```

Start Mosquitto (port 1883), then:

```bash
cd backend
python app.py                       # http://localhost:5000
```

`inventory.db` is created automatically on first run with demo items, default accounts, and
demo workers. Point the backend at a non-local broker with environment variables:

```bash
set MQTT_BROKER=192.168.0.115       # Windows
export MQTT_BROKER=192.168.0.115    # Linux/macOS
```

**Windows one-click:** double-click `start.bat`. It stops any stale Mosquitto, starts the
broker, installs dependencies, launches the backend, opens the dashboard, and prints the host's
LAN IPs for ESP32 configuration.

#### Production Deployment (Linux)

`app.py` starts the database and MQTT thread only under `__main__`, so a WSGI server needs a
small entry module:

`backend/wsgi.py`
```python
from app import app
from database import init_db
from mqtt_subscriber import start_mqtt

init_db()
start_mqtt()
```

`/etc/systemd/system/inventory.service`:
```ini
[Unit]
Description=Smart Inventory Backend
After=network.target mosquitto.service

[Service]
User=inventory
Environment=SECRET_KEY=<a-long-random-value>
Environment=MQTT_BROKER=127.0.0.1
WorkingDirectory=/opt/smart-inventory-rfid-iot/backend
ExecStart=/usr/local/bin/gunicorn -w 1 --threads 8 -b 0.0.0.0:5000 wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

> Use **one worker with threads**, not multiple processes: each worker would open its own MQTT
> subscription and process every scan again. Threads are also required because SSE holds a
> connection open per browser.

```bash
sudo systemctl enable --now inventory
```

Internal DNS: `inventory.company.local → <server LAN IP>`.

### 2. ESP32 Firmware

**Requirements:** MicroPython v1.24+, `mpremote`, `esptool`.

**Step 1 — flash MicroPython (once per board)**
```
python -m esptool --chip esp32 --port COM<N> erase-flash
python -m esptool --chip esp32 --port COM<N> --baud 460800 write_flash -z 0x1000 ESP32_GENERIC-20260406-v1.28.0.bin
```

**Step 2 — set `config.py` per board**

```python
# Board 1 — Factory Writer
DEVICE_ID = 'esp32-01'
READERS = [{'role': 'factory_writer', 'cs': 22, 'rack_location': None}]

# Board 2 — Factory Exit
DEVICE_ID = 'esp32-02'
READERS = [{'role': 'factory_exit', 'cs': 22, 'rack_location': None}]

# Board 3 — Warehouse Gate
DEVICE_ID = 'esp32-03'
READERS = [{'role': 'warehouse_gate', 'cs': 22, 'rack_location': None}]

# Board 4 — Warehouse Rack
DEVICE_ID = 'esp32-04'
READERS = [{'role': 'warehouse_rack', 'cs': 22, 'rack_location': 'A1'}]
```

Fill `WIFI_NETWORKS` with each network's SSID, password, and the **broker IP of the server on
that network** (`ipconfig` / `ip addr` on the host). `boot.py` tries each entry in order and
sets `MQTT_BROKER` to whichever one connects — this is what makes the boards work unchanged
across home Wi-Fi, campus Wi-Fi, and a phone hotspot.

`DEMO_OVERWRITE_TAGS = True` lets the factory writer relabel already-written tags so a small
set of physical tags can be reused across demo runs. Leave it `False` in production.

**Step 3 — upload (from the `esp32/` folder)**
```
mpremote connect COM<N> cp config.py :config.py + cp mfrc522.py :mfrc522.py + cp rfid_reader.py :rfid_reader.py + cp boot.py :boot.py + cp main.py :main.py + reset
```

After upload, any USB power source boots the board straight into `main.py`.

### 3. Write Worker Badges

```bash
mpremote connect COM<N>
# Ctrl+C to stop main.py
>>> import tag_writer
```

Follow the prompts to write `EMP-001` … `EMP-004` (and item IDs) to physical tags.

---

## Moving the Project to a New Device

1. **Clone and install**
   ```powershell
   git clone https://github.com/jhjh1214/smart-inventory-rfid-iot.git
   cd smart-inventory-rfid-iot
   pip install -r requirements-pc.txt
   ```
2. **Install Mosquitto.** `start.ps1` expects `C:\Program Files (x86)\Mosquitto\mosquitto.exe`
   — edit `$MOSQUITTO` in `start.ps1` if it lands elsewhere.
3. **Carry the data over (optional).** `backend/inventory.db` is gitignored. To keep existing
   inventory, copy it manually, or download a backup from the old machine via
   `GET /api/export/backup` and drop it in as `backend/inventory.db`. Skipping this step gives
   you a fresh seeded database.
4. **Find the new LAN IP** — `ipconfig`, or read the list `start.ps1` prints on startup.
5. **Update `esp32/config.py`** so the matching `WIFI_NETWORKS` entry's `broker` is the new IP,
   then re-upload `config.py` to every board:
   ```
   mpremote connect COM<N> cp config.py :config.py + reset
   ```
6. **Open Windows Firewall** for inbound TCP 1883 (Mosquitto) and 5000 (Flask) on the private
   network profile, or the boards and other machines cannot reach the server.
7. **Set a real `SECRET_KEY`** — otherwise every install shares the same signing key:
   ```powershell
   $env:SECRET_KEY = "<a-long-random-value>"
   ```
8. **Run `.\start.bat`** and confirm the dashboard's MQTT pill reads *MQTT Live*.
9. **Verify** by scanning one tag at any station and watching the live feed update.

---

## Testing

```bash
pip install -r requirements-test.txt
python -m pytest -q                 # ~3.5 minutes
```

387 tests across 13 modules cover auth and RBAC, item CRUD, tags, workers and sessions, alerts,
audit filters, purchase orders, webhooks, CSV export/import, analytics, database migrations and
seeding, and every pipeline handler (factory write/exit, warehouse gate/rack, returns, legacy
scan, worker badge routing). Each test runs against an isolated temporary SQLite file.

**Current status: 377 passed, 10 failed.** All ten failures are stale assertions or
environment dependence rather than product regressions:

| Failing test(s) | Cause |
|---|---|
| `TestIdempotency::test_migrations_*` (2) | Assert 10 rows in `schema_version`; there are now 14 migrations. |
| `TestHandleWarehouseRack::*` (5) | Call the handler without `item_id`, which it now requires. |
| `test_unknown_tag_with_item_id_auto_creates` | Asserts auto-creation at factory exit; behaviour intentionally changed to raise a security alert instead. |
| `TestTestWebhook::*` (2) | Post to `http://example.com/hook` over the real internet (returns 405). Needs mocking. |

---

## Security Posture & Known Limitations

Documented honestly so the deployment risk is visible. Nothing here blocks the LAN demo; all of
it matters before the system touches real stock or an untrusted network.

### Implemented controls

- Passwords hashed with Werkzeug PBKDF2-SHA256; login rate-limited to 10 failures / 5 min per IP.
- RBAC enforced server-side on every endpoint; frontend gating is UX only.
- Audit trail is append-only and visible to every role — no role can hide its own actions.
- All SQL is parameterised; all dashboard output is HTML-escaped through `esc()`.
- State machine rejects re-scanned `dispatched` / `consumed` tags and unregistered tags at
  gates, raising security alerts and SSE notifications.
- Worker sessions expire after 5 minutes and survive restarts.

### Open gaps

| Area | Gap | Impact |
|------|-----|--------|
| Session security | `SECRET_KEY` falls back to a hardcoded default; `debug=True` in `app.run`; no `Secure`/`HttpOnly`/`SameSite` cookie flags; no CSRF tokens; no security headers | Session forgery and the Werkzeug debugger are both reachable if the port is exposed |
| RBAC | `PUT /api/items/<id>` and `POST /api/tags` are `@login_required`, so a **viewer can edit quantities and re-map tags**; alert mark-read/delete likewise | The documented read-only role is not read-only |
| Session lifecycle | Sessions are stateless cookies — deleting a user or changing their role does not invalidate their live session | Revocation is delayed until expiry (8 h) |
| Default accounts | `manager` / `viewer` are re-created with known passwords on every startup if missing | Deleting them is not permanent |
| MQTT | `allow_anonymous true`, no TLS, no ACLs | Any LAN host can publish forged scans and move stock arbitrarily |
| RFID | MIFARE Classic 1K, default key `FFFFFFFFFFFF`, plaintext payload, no signature | Tags are trivially cloneable — a cloned supervisor badge grants dispatch authority |
| Secrets in git | `esp32/config.py` contains **real Wi-Fi passwords** and is tracked | Credential disclosure to anyone with repo access |
| Message integrity | MQTT QoS 0, no message IDs, no server-side dedupe (only a 2 s per-UID cooldown on the board) | A replayed or duplicated packet double-counts stock |
| Concurrency | Read-modify-write sequences (`reserve_stock`, PO receipt) are not transactional; `previous_quantity` on dispatch is reconstructed and is wrong when the clamp at zero fires | Small accounting drift under concurrent scans |
| Webhooks | No URL allowlist, no HMAC signing, no redirect limits | SSRF reachable by an admin; receivers cannot verify payloads |
| Uploads | No `MAX_CONTENT_LENGTH`; CSV import commits partial results | Memory exhaustion, partial imports |
| Error handling | Several endpoints return `str(e)` directly | Leaks SQL internals to the client |
| Offline claim | Dashboard loads Tailwind, Chart.js, and jsPDF from public CDNs | A truly air-gapped LAN renders unstyled and loses charts/PDF export |
| Backup | `shutil.copy2` on a live WAL database | Copy can be inconsistent; use `VACUUM INTO` or the SQLite backup API |
| Timezone | SQLite writes UTC via `CURRENT_TIMESTAMP`, analytics filters use local `datetime.now()` alongside `DATE('now')` (UTC) | Day-boundary metrics are off by the UTC offset |

### Functional gaps

| Area | Gap |
|------|-----|
| Analytics | Demand forecast, trends, and ABC query `scan_in`/`scan_out`, which the pipeline never emits — see [Analytics Engine](#analytics-engine) |
| Reservations | `reserved_qty` is set by the reserve endpoints but never consumed or released by dispatch, and no dashboard control calls it |
| Pallets | Pallet scans at `factory_exit` are routed to the carton handler, which looks pallets up in the `cartons` table and aborts — pallets never reach `in_transit` via that station |
| Purchase orders | `_check_purchase_order` increments receipts by 1 even when an N-unit carton arrives; POs carry no supplier, unit cost, or expected date, and are not linked to the tags that fulfil them |
| Referential cleanup | Deleting an item removes its tags, POs and write jobs but leaves `cartons` orphaned |
| ERP readiness | No cost/price/supplier/category fields, no stock valuation, no multi-warehouse or bin hierarchy, no lot/expiry tracking, no UoM conversion |
| Integration surface | Cookie sessions only — no API keys or tokens for machine-to-machine use, no `/api/v1` versioning, no OpenAPI spec, no pagination convention, no inbound webhooks |
| Operations | `print()`-based logging with no log file, no structured logs, no metrics, no DB health check (`/api/status` reports MQTT only) |

### Suggested remediation order

1. `SECRET_KEY` from environment (fail closed), `debug=False`, cookie flags, security headers.
2. Fix the RBAC decorators on item update and tag registration.
3. Broker credentials + per-topic ACLs; TLS if the network is not fully trusted.
4. Purge Wi-Fi credentials from `config.py` (move to an untracked `config_local.py` — already
   gitignored) and rotate the exposed passwords.
5. Widen the analytics action filters to the pipeline actions.
6. Wrap the read-modify-write paths in explicit transactions; add MQTT message IDs and dedupe.
7. Vendor the CDN assets locally.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Microcontroller | ESP32 (MicroPython 1.24+) |
| RFID | MFRC522 / RC522, MIFARE Classic 1K, block 8, key A |
| Messaging | MQTT via Mosquitto; paho-mqtt (backend), umqtt.simple (ESP32) |
| Backend | Python 3.9+, Flask, SQLite (WAL) — PostgreSQL-ready |
| Machine learning | scikit-learn (Gradient Boosting, Isolation Forest), numpy — optional, with fallbacks |
| Real-time push | Server-Sent Events (SSE) |
| Frontend | Tailwind CSS (CDN), Chart.js v4, jsPDF + autoTable, vanilla JS — no build step |
| Auth | Flask sessions, Werkzeug PBKDF2-SHA256 |
| Testing | pytest, pytest-cov — 387 tests |
| Deployment | On-premise: Gunicorn (1 worker, threaded) + systemd + LAN; Cloud: Railway / Render + managed PostgreSQL + cloud MQTT |
