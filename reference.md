# System Reference — Smart Inventory Management System (RFID/IoT)

*Use this document when updating the FYP report. Each section maps to a standard report chapter.*

---

## 0. FYP Objectives Alignment

| Objective | How the system addresses it |
|-----------|----------------------------|
| Design and implement an IoT-enabled system for automated inventory tracking using RFID technology | Four ESP32 microcontrollers with RC522 RFID readers are stationed at the factory writer, factory exit, warehouse gate, and warehouse rack. Tags are written at manufacture and scanned automatically at every pipeline stage — no manual barcode entry at any point. |
| Enable real-time data synchronisation between RFID devices and cloud storage | MQTT (Mosquitto) delivers scan events from each ESP32 to the backend within milliseconds. Server-Sent Events (SSE) push inventory updates to the dashboard in real time. The system runs on-premise by default; cloud deployment (PostgreSQL on a managed provider + cloud-hosted Flask backend + cloud MQTT broker) is supported with a database driver swap and a single hostname change in `config.py` — no architectural changes required. |
| Develop a dashboard for inventory visualisation and efficient management | A full-featured web dashboard with eight tabs: live inventory, analytics (ABC classification, demand forecasting, EOQ, risk scoring), RFID tag state tracking, worker sessions, manufacturing pipeline, alerts, and a tamper-evident audit trail. Role-based access control (admin / manager / viewer) is enforced server-side on every endpoint. |

---

## 1. System Overview

**System name:** Smart Inventory Management System (RFID/IoT)

**Purpose:** Automate product tracking across the full supply chain — from manufacturing tagging through warehouse receiving, racking, dispatch, and customer returns — using RFID tags and ESP32 microcontrollers, with a real-time web dashboard for management oversight.

**Deployment model:** On-premise LAN installation by default — the server (mini-PC or Raspberry Pi 4) runs entirely within the company network, with no cloud dependency. The decoupled architecture (ESP32 → MQTT broker → Flask backend → database) also supports full cloud deployment: replace SQLite with PostgreSQL on any managed provider, host the Flask app and Mosquitto on a cloud VM or managed MQTT service, and update the broker hostname in `config.py`. ESP32 boards then connect to the cloud backend over any Wi-Fi network without requiring a fixed LAN IP or VPN.

**Key value propositions:**
- Zero manual barcode scanning — all tracking is passive RFID
- Tamper-evident audit trail with worker identity at every step
- Real-time inventory visibility from any browser on the company LAN
- Built-in demand forecasting and ABC classification
- Supervisor authentication enforcement at dispatch points

---

## 2. System Architecture

### 2.1 Component Overview

Three physical zones each served by one ESP32, connecting back to a central Flask/SQLite backend over LAN MQTT:

| Zone | ESP32 ID | Readers | MQTT Topics Published |
|------|----------|---------|----------------------|
| Manufacturing Floor | `esp32-factory` | factory_writer (CS=22), factory_exit (CS=5) | `inventory/factory/job`, `inventory/factory/written`, `inventory/factory/exit` |
| Warehouse | `esp32-warehouse` | warehouse_gate (CS=22), warehouse_rack (CS=5) | `inventory/warehouse/gate`, `inventory/warehouse/rack` |
| Returns Desk | `esp32-returns` | return_gate (CS=22) | `inventory/returns/gate` |

### 2.2 Communication Stack

```
ESP32 (MicroPython)
    └─► umqtt.simple  ──► Mosquitto broker (LAN, port 1883)
                                └─► paho-mqtt subscriber thread
                                        └─► SQLite (inventory.db)
                                                └─► Flask SSE  ──► Browser dashboard
```

The backend operates three concurrent threads:
1. Flask HTTP server (REST API + SSE endpoint)
2. MQTT subscriber thread (processes all ESP32 events)
3. SSE broadcaster (pushes state changes to all connected browsers)

### 2.3 Multi-Reader SPI Architecture

Multiple RC522 modules on one ESP32 share a single SPI bus (SCK=GPIO19, MOSI=GPIO23, MISO=GPIO25). Each reader gets its own Chip Select (CS) line. The firmware polls readers in round-robin, activating each CS in turn while the others remain high (deselected).

---

## 3. Tag Lifecycle State Machine

### 3.1 Pipeline Mode (Primary)

Tags transition through states deterministically. Invalid transitions are rejected and an alert is raised.

| State | Entered By | Effect on Inventory |
|-------|------------|---------------------|
| `tagged` | factory_writer | — (item exists, tag registered) |
| `in_transit` | factory_exit | — |
| `received` | warehouse_gate | qty +1 |
| `racked` | warehouse_rack | qty +1 only if tag is new (not yet in pipeline); qty 0 if tag already came through warehouse gate |
| `picked` | warehouse_rack (rack_remove) | qty 0 — item taken off shelf, not yet confirmed out |
| `dispatched` | warehouse_gate | qty −1 (terminal) — the single confirmed decrement |
| `return_pending` | Dashboard admin (Return button) | — (flags tag for physical return) |
| `returned` | warehouse_rack (same scan as below) | qty +1 |
| `racked` | warehouse_rack (same scan as above) | — (location recorded) |

**Return flow with 4 boards:** There is no dedicated return-desk board in the current setup. Returns are handled in two steps: (1) admin marks the tag `return_pending` via the dashboard; (2) the warehouse worker scans the item at the rack reader (board 4), which detects the `return_pending` state and finalises the return. A `return_gate` board can replace step 2 when the hardware is available — the backend handler is already implemented.

Re-scanning a `dispatched` tag at any gate raises a security alert — this is the fraud-prevention mechanism.

### 3.2 Legacy Mode (Single-Reader Fallback)

For deployments with a single reader publishing to `inventory/scan`:

```
out → in → consumed → return_pending → in → ...
```

Legacy mode is automatically detected from the MQTT topic.

---

## 4. Worker Authentication

### 4.1 Badge Authentication Flow

1. Worker taps RFID badge at any reader.
2. ESP32 reads tag UID; if tag data begins with `EMP-`, it is routed as a worker badge (not a product tag).
3. Backend creates a 5-minute session keyed by `device_id`.
4. All subsequent product transactions on that device record `performed_by = "Name (EMP-XXX)"`.
5. Session auto-expires; re-tap renews the timer.

### 4.2 Supervisor Dispatch Enforcement

Warehouse dispatch requires a supervisor session on the gate device. If no supervisor is authenticated when a product is dispatched, the system:
- Records the dispatch (does not block — to avoid warehouse deadlock)
- Inserts a `security` alert into the `alerts` table
- Broadcasts the alert via SSE and MQTT to all connected clients

This creates a paper trail for investigation without halting operations.

### 4.3 Worker Roles

| Role | Capability |
|------|-----------|
| `supervisor` | Can authorise dispatch; visible in zone assignment |
| `operator` | Standard product handling; sessions tracked |

Workers are also assigned a `zone` field (e.g., `warehouse`, `factory`, `returns`) for geographic scoping within the facility.

---

## 5. Accountability & Audit Trail

### 5.1 Device ID Tracking

Every row in the `transactions` table carries a `device_id` column identifying the source of the action:

| Value | Source |
|-------|--------|
| `'dashboard'` | Web UI action by a logged-in dashboard user |
| `'esp32-factory'` | Physical RFID scan at the manufacturing floor |
| `'esp32-warehouse'` | Physical RFID scan at the warehouse |
| `'esp32-returns'` | Physical RFID scan at the returns desk |
| `'system'` | Database migration / auto-seeding |

### 5.2 Dashboard Actor Linking

When a dashboard user performs an action (add item, delete tag, adjust quantity, etc.), the `performed_by` field records:
- Username alone if no badge is linked: `"alice"`
- Username + badge UID if a physical badge is registered to the account: `"alice [badge:A1B2C3D4]"`

This ties digital dashboard actions to a physical identity, preventing shared-login accountability gaps.

### 5.3 Audit Trail Visibility

The Audit Trail tab is accessible to **all roles** (viewer, manager, admin). This is a deliberate design decision: no higher-privilege user can suppress or hide the record of their own actions from lower-privilege reviewers.

Filters available via `/api/audit?filter=`:
- `all` — full log
- `dashboard` — web UI actions only
- `physical` — ESP32 scanner actions only
- `admin` — item adds, deletes, manual adjustments

---

## 6. Database Design

### 6.1 Schema Summary

```sql
items (
    id INTEGER PRIMARY KEY,
    name TEXT,
    quantity INTEGER DEFAULT 0,
    unit TEXT DEFAULT 'units',
    low_stock_threshold INTEGER DEFAULT 10,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)

rfid_tags (
    uid TEXT PRIMARY KEY,
    item_id INTEGER REFERENCES items(id),
    state TEXT DEFAULT 'unknown',
    rack_location TEXT,
    last_scan TIMESTAMP
)

transactions (
    id INTEGER PRIMARY KEY,
    item_id INTEGER,
    action TEXT,
    quantity_change INTEGER DEFAULT 0,
    tag_uid TEXT,
    performed_by TEXT,
    device_id TEXT DEFAULT 'dashboard',   -- added v2
    note TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)

alerts (
    id INTEGER PRIMARY KEY,
    item_id INTEGER,
    alert_type TEXT,
    message TEXT,
    acknowledged INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)

users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE,
    password_hash TEXT,
    role TEXT DEFAULT 'viewer',
    badge_uid TEXT,           -- added v2; links to worker badge
    employee_id TEXT          -- added v2; links to HR record
)

workers (
    id INTEGER PRIMARY KEY,
    employee_id TEXT UNIQUE,
    name TEXT,
    uid TEXT,
    role TEXT DEFAULT 'operator',
    zone TEXT DEFAULT 'general',   -- added v2
    active INTEGER DEFAULT 1,
    last_seen TIMESTAMP
)

write_jobs (
    id INTEGER PRIMARY KEY,
    batch_id TEXT,
    item_id INTEGER,
    quantity INTEGER,
    written INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

### 6.2 Transaction Action Values

| Action | Triggered By |
|--------|-------------|
| `scan_in` | Legacy reader |
| `scan_out` | Legacy reader |
| `tagged` | factory_writer (new tag written) |
| `in_transit` | factory_exit |
| `received` | warehouse_gate (inbound) |
| `racked` | warehouse_rack — shelf placement |
| `dispatched` | warehouse_gate (outbound) |
| `return_requested` | Dashboard admin marks tag for return (state → return_pending, qty 0) |
| `returned` | warehouse_rack — return finalised (qty +1); immediately followed by `racked` in same scan |
| `rack_add` | warehouse_rack — new or non-racked tag placed on shelf (qty +1) |
| `rack_remove` | warehouse_rack — item picked off shelf, state → picked, qty 0 (qty-1 confirmed at gate) |
| `item_added` | Dashboard create item |
| `item_deleted` | Dashboard delete item |
| `tag_removed` | Dashboard delete tag |
| `return_requested` | Dashboard admin force-return |
| `manual_adjust` | Dashboard quantity edit |

---

## 7. REST API Reference

### Authentication
```
POST /api/login          body: {username, password}  → {role, username}
POST /api/logout
GET  /api/me             → {id, username, role, badge_uid, employee_id}
```

### Inventory Items
```
GET    /api/items
POST   /api/items        manager+   body: {name, quantity, unit, low_stock_threshold}
PUT    /api/items/<id>   viewer+    body: {name?, quantity?, unit?, low_stock_threshold?}
DELETE /api/items/<id>   admin
```

### RFID Tags
```
GET    /api/tags
POST   /api/tags                    body: {uid, item_id}
POST   /api/tags/<uid>/return       admin
DELETE /api/tags/<uid>              admin
```

### Workers
```
GET    /api/workers
POST   /api/workers      manager+   body: {employee_id, name, uid, role, zone}
PUT    /api/workers/<id> manager+   body: {name?, role?, zone?, active?}
DELETE /api/workers/<id> admin
GET    /api/workers/sessions
```

### Dashboard Users
```
GET    /api/users           admin
POST   /api/users           admin   body: {username, password, role}
PUT    /api/users/<id>      admin   body: {role?, badge_uid?, employee_id?}
DELETE /api/users/<id>      admin
```

### Audit Trail
```
GET    /api/audit           viewer+
       ?filter=all|dashboard|physical|admin
       &limit=<n>           (default 100)
```

### Analytics
```
GET    /api/analytics/summary       viewer+
GET    /api/analytics/trends?days=7 viewer+
GET    /api/analytics/abc           viewer+
GET    /api/pipeline                viewer+
GET    /api/factory/jobs            viewer+
POST   /api/factory/jobs            manager+  body: {item_id, quantity}
```

### Real-time
```
GET    /api/events          SSE stream; events: transaction, alert, security_alert,
                            pipeline_update, worker_session, mqtt_status
```

---

## 8. Analytics Engine

File: `backend/analytics.py`

### 8.1 Algorithms

**ABC Classification**
Items are ranked by outbound transaction volume over a configurable window (default: all time). Top 20% by volume = Class A (high-value, tight control). Next 30% = Class B. Remaining 50% = Class C.

**Demand Forecasting**
Exponential smoothing with α = 0.3:
```
F(t) = α × D(t−1) + (1−α) × F(t−1)
```
Applied to daily outbound quantities over the last 30 days. Produces a projected daily demand rate used for reorder point calculation.

**Economic Order Quantity (EOQ)**
```
EOQ = √(2DS / H)
```
Where D = annual demand (forecast × 365), S = 10 (fixed ordering cost), H = 0.5 (holding cost per unit per year).

**Inventory Risk Scoring**
Days of stock remaining = `current_quantity / forecast_daily_demand`.
- < 7 days → high risk
- 7–14 days → medium risk
- > 14 days → low risk

**Inventory Health Score**
Composite score (0–100) weighting:
- % items in stock
- % items above low-stock threshold
- % items with recent transaction activity

---

## 9. Frontend Architecture

### 9.1 Dashboard Structure

Single-page application (SPA) driven by vanilla JavaScript. Navigation uses tab switching (no page reloads). Real-time updates arrive via SSE and patch the DOM directly.

**Tab list:** Overview, Inventory, Analytics, RFID Tags, Workers, Manufacturing, Alerts, Audit Trail

### 9.2 RBAC Enforcement

RBAC is enforced on both server (API decorators) and client (JS `applyRBAC()` hides/disables UI elements). Client-side gating is UX only — all security checks live on the server.

| Element | admin | manager | viewer |
|---------|-------|---------|--------|
| Dashboard Accounts section | visible | hidden | hidden |
| Delete buttons (items/tags/workers) | active | hidden | hidden |
| Add/edit items | active | active | read-only |
| Register write job | active | active | hidden |
| Audit Trail | visible | visible | visible |

### 9.3 UI Components

**Skeleton loaders** — shown immediately on tab open before data arrives. CSS `@keyframes shimmer` with a gradient sweep.

**Empty states** — contextual per section and per active filter. Each includes an icon, explanatory copy, and a CTA button (where applicable). Examples:
- Inventory empty: "No items yet — add your first product to get started"
- Alerts empty (filtered to security): "No security alerts — all dispatch events have active supervisor sessions"

**Toast notifications** — dark-background toasts with left-border accent, type icons (success/error/warning/info), click-to-dismiss, CSS entrance/exit animations.

**Form loading state** — `_btnLoad(btn, true, 'Saving...')` disables the submit button and shows a spinner during async operations. Prevents double-submit.

### 9.4 Real-time SSE Events

| Event type | Dashboard effect |
|------------|-----------------|
| `transaction` | Prepends row to Overview transaction feed; updates KPI counts |
| `alert` | Prepends to Alerts tab; increments KPI alert badge |
| `security_alert` | Prepends to Alerts with red styling; browser notification |
| `pipeline_update` | Refreshes Manufacturing tab stage counts |
| `worker_session` | Updates Workers tab active sessions list |
| `mqtt_status` | Updates MQTT status indicator in sidebar footer |

---

## 10. Hardware Setup

### 10.1 SPI Wiring

```
RC522        ESP32
─────────────────────────
VCC    →    3.3V
GND    →    GND
SCK    →    GPIO 19
MOSI   →    GPIO 23
MISO   →    GPIO 25
SDA/CS →    GPIO 22  (reader 1)
SDA/CS →    GPIO 5   (reader 2)
RST    →    hardwire to 3.3V (no firmware reset needed)
```

### 10.2 RFID Tag Types

**Product tags:** MIFARE Classic 1K sticker tags. Written by factory_writer with `item_id` (e.g., `ITEM-042`). One tag per physical product unit.

**Worker badges:** Standard MIFARE Classic 1K cards or key fobs. Written with employee ID (e.g., `EMP-001`) using `tag_writer.py`.

Tags are distinguishable by the prefix in block 1: `ITEM-` routes to the product pipeline; `EMP-` routes to worker authentication.

### 10.3 Multi-WiFi Resilience

`config.py` supports a list of `WIFI_NETWORKS`. The ESP32 firmware cycles through them on connection failure, enabling deployment in facilities with both primary and backup access points.

---

## 11. Security Considerations

| Threat | Mitigation |
|--------|-----------|
| Duplicate tag fraud | State machine rejects re-scan of dispatched/consumed tags; security alert raised |
| Unauthorised dispatch | Supervisor session required; missing session generates tamper alert |
| Shared login accountability gap | Dashboard accounts linkable to physical badge UID |
| Privilege escalation | Role checked server-side on every endpoint; client RBAC is UX only |
| Password exposure | Passwords stored as PBKDF2-SHA256 hashes via Werkzeug |
| Audit manipulation | Audit trail visible to all roles including viewer — no role can hide actions |
| Network interception | Deployment on LAN only; VPN for remote; no public surface |

---

## 12. Limitations & Future Work

| Limitation | Possible Enhancement |
|------------|---------------------|
| SQLite single-writer | PostgreSQL migration is supported: swap `sqlite3` for `psycopg2`, change `?` placeholders to `%s`, and `AUTOINCREMENT` to `SERIAL`. Enables cloud deployment with no architectural changes. |
| No HTTPS | Add TLS termination via nginx reverse proxy |
| 5-minute session TTL fixed | Make TTL configurable per zone in `workers` table |
| No redundant broker | Add MQTT bridge / cluster for high availability |
| Single-facility scope | Add multi-site support with per-site MQTT namespacing |
| No native mobile app | Progressive Web App (PWA) wrapper for mobile access |
| ESP32 OTA updates | Implement MQTT-triggered OTA firmware via `ota_updater.py` |
