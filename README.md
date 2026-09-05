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

- [New Device Onboarding — Start Here](#new-device-onboarding--start-here)
- [System Architecture](#system-architecture)
- [Hardware](#hardware)
- [Tag Lifecycle — State Machine](#tag-lifecycle--state-machine)
- [Tag Hierarchy — Units, Cartons, Pallets](#tag-hierarchy--units-cartons-pallets)
- [Worker RFID Authentication](#worker-rfid-authentication)
- [Accountability & Audit Trail](#accountability--audit-trail)
- [Dashboard](#dashboard)
- [Analytics Engine](#analytics-engine)
- [LLM Assistant](#llm-assistant)
- [Stock profiles](#stock-profiles)
- [Integrations — Webhooks, CSV, Backup](#integrations--webhooks-csv-backup)
- [MQTT Topics](#mqtt-topics)
- [Project Structure](#project-structure)
- [REST API](#rest-api)
- [Database Schema](#database-schema)
- [Setup](#setup)
- [Testing](#testing)
- [Academic Deliverables — UEC, PRISM, IEEE, Report](#academic-deliverables--uec-prism-ieee-report)
- [Specification Gaps to Close](#specification-gaps-to-close)
- [Security Posture & Known Limitations](#security-posture--known-limitations)
- [Tech Stack](#tech-stack)

---

## New Device Onboarding — Start Here

This folder is **self-contained**: code, firmware image, tooling, and every academic document
live inside it. Zip it, copy it to the new machine, unzip, and work through this section.

### What you must install (not in the zip)

| Software | Why | Notes |
|----------|-----|-------|
| **Python 3.11+** | Backend and tooling | Tested on 3.14.2. Tick *Add Python to PATH* during install. |
| **Mosquitto** | MQTT broker | `winget install EclipseFoundation.Mosquitto`, or [mosquitto.org/download](https://mosquitto.org/download/). `start.ps1` probes `C:\Program Files\mosquitto\` (x64) and `C:\Program Files (x86)\Mosquitto\` (x86), then PATH. **The winget package also registers an auto-starting service** — see step 2b. |
| **Git** | Version control | Optional if you only ever work from the zip, but the repo remote is `github.com/jhjh1214/smart-inventory-rfid-iot`. |
| **CP210x / CH340 USB driver** | ESP32 serial | Usually auto-installs on Windows 11. If no COM port appears, install the driver for your board's USB chip. |

### Step-by-step

**1. Unzip somewhere without spaces or OneDrive sync**, e.g. `C:\dev\smart-inventory-rfid-iot`.
OneDrive can lock `inventory.db` mid-write and corrupt WAL journals.

**2. Install the Python dependencies into a project venv.** `start.bat` does this for you on
first run; do it by hand only if you want the test dependencies too. Install into `.venv`, not
the system interpreter, so a second Python project cannot break these pins.

```powershell
cd C:\dev\smart-inventory-rfid-iot
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-pc.txt
.venv\Scripts\python -m pip install -r requirements-test.txt   # optional, for the test suite
```

**2b. Stop the Mosquitto service if winget installed one.** The winget package registers
`mosquitto` as an **auto-starting Windows service** whose config file is empty. Mosquitto 2.x
with no config binds `127.0.0.1` only, so the backend works but **every ESP32 gets "connection
refused"**. This repo's `mosquitto.conf` (`listener 1883`, `allow_anonymous true`) binds all
interfaces, and `start.ps1` launches the broker with it — so the service must get out of the way.
Run elevated:

```powershell
Stop-Service mosquitto
Set-Service mosquitto -StartupType Manual
```

Leaving the service running also makes `start.ps1` fail at *"Stopping existing Mosquitto"* with
*Access denied*, because a service runs in session 0 where an unelevated `Stop-Process` cannot
touch it.

**3. Build the ESP32 toolchain venv.** `tools/esptoolenv/` is **not** shipped — a copied
virtualenv does not work, because its `pyvenv.cfg` hardcodes the absolute Python path and its own
location from the machine that built it. Build it fresh from the pinned requirements:

```powershell
python -m venv tools\esptoolenv
tools\esptoolenv\Scripts\python -m pip install -r tools\requirements-esp32.txt
```

Verify with `tools\esptoolenv\Scripts\esptool version` and `...\mpremote version`.

> `rshell` is deliberately not in that list: its `pyreadline` dependency calls
> `collections.Callable`, removed in Python 3.10, so it crashes on any modern interpreter.
> Nothing in this project uses it.

**4. Set a session signing key.** Without this, every install shares the same hardcoded
fallback key and sessions are forgeable.

```powershell
# Per session:
$env:SECRET_KEY = "<a-long-random-value>"
# Or permanently:
setx SECRET_KEY "<a-long-random-value>"
```

**5. Decide about the database.** `backend/inventory.db` is gitignored and is **not** in the
repo — but it *is* in the zip if it existed when you zipped. Options:

- **Fresh start (recommended):** delete `backend/inventory.db`. It is recreated on first run
  with 8 demo items, 3 accounts, and 4 demo workers.
- **Keep history:** leave the file in place. Migrations run automatically on startup.

**6. Find the new machine's LAN IP.**

```powershell
ipconfig            # look for IPv4 Address on your active adapter
```

`start.ps1` also prints every candidate IP after it launches.

**7. Point the ESP32 boards at the new server.** Edit `esp32/config.py` so the `broker` field of
the matching `WIFI_NETWORKS` entry is that IP, then re-upload `config.py` to **every board**:

```powershell
tools\esptoolenv\Scripts\mpremote connect COM7 cp esp32\config.py :config.py + reset
```

Boards silently fail to reach the backend if you skip this. `boot.py` tries each network in
order and adopts the broker IP of whichever one connects — that is what makes the boards work
across home Wi-Fi, campus Wi-Fi, and a phone hotspot without reflashing.

**8. Open the Windows firewall** for inbound TCP **1883** (Mosquitto) and **5000** (Flask).
Without this the boards and other machines cannot reach the server.

**Check the adapter's network category first** — a rule scoped to `Private` does nothing while
Windows classifies your Wi-Fi as `Public`, which is the default on a network you have not marked
as trusted:

```powershell
Get-NetConnectionProfile | Select-Object InterfaceAlias, NetworkCategory
```

If it says `Public`, reclassify it (elevated) before adding the rules:

```powershell
Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private
New-NetFirewallRule -DisplayName "Smart Inventory - Mosquitto MQTT (1883)" -Direction Inbound -Protocol TCP -LocalPort 1883 -Profile Private -Action Allow
New-NetFirewallRule -DisplayName "Smart Inventory - Flask dashboard (5000)" -Direction Inbound -Protocol TCP -LocalPort 5000 -Profile Private -Action Allow
```

**9. Launch.**

```powershell
.\start.bat
```

It stops any stale broker, starts Mosquitto, installs dependencies, starts the backend, opens
the dashboard, and prints the host's LAN IPs.

**10. Verify, in this order:**

- Dashboard loads at `http://localhost:5000` and you can log in as `admin` / `admin123`.
- The topbar MQTT pill reads **MQTT Live** (not *MQTT Offline*).
- Power on one ESP32; within 30 s `/api/status` shows a recent `device_last_seen`.
- Scan one tag — the Overview tab's live feed updates within a second, and a row appears in
  the Audit Trail.
- `.venv\Scripts\python -m pytest -q` → 389 passed, 0 failed ([expected](#testing)).

### Reflashing a board from scratch

Only needed if a board's MicroPython is missing or corrupt. The firmware image is bundled at
`firmware/ESP32_GENERIC-20260406-v1.28.0.bin`.

```powershell
tools\esptoolenv\Scripts\python -m esptool --chip esp32 --port COM7 erase-flash
tools\esptoolenv\Scripts\python -m esptool --chip esp32 --port COM7 --baud 460800 write_flash -z 0x1000 firmware\ESP32_GENERIC-20260406-v1.28.0.bin
```

Then upload the application files — see [ESP32 Firmware](#2-esp32-firmware).

### Finding your COM port

```powershell
Get-PnpDevice -Class Ports | Where-Object {$_.Status -eq 'OK'} | Select-Object FriendlyName
```

### Gotchas that cost time

- **`.claude/settings.local.json`** contains absolute paths from the old machine (permission
  allowlist entries). Stale entries are harmless — they just cause an extra approval prompt.
- **Don't run two backends at once.** SQLite is in WAL mode with a 3 s busy timeout, but two
  processes also mean two MQTT subscriptions and every scan is processed twice.
- **`sklearn` and `numpy` are optional.** If they fail to install, the backend still runs —
  forecasting falls back to exponential smoothing and anomaly detection returns empty.
- **The dashboard needs internet** for Tailwind, Chart.js, and jsPDF (CDN). On an offline
  network it renders unstyled with no charts. See [Open gaps](#open-gaps).

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
4. Renews the timer on any re-tap.

A badge is **refused** — no session, a `security` alert row, and a `worker_denied` event on
the dashboard — in four cases:

| Case | Alert message |
|------|---------------|
| Employee ID is not on the roster | `UNREGISTERED BADGE` |
| Worker is deactivated (`active = 0`) | `INACTIVE BADGE` |
| Employee ID presented on a different tag UID than the one bound to it | `CLONED BADGE` |
| Tag UID is already bound to a different employee | `REUSED BADGE UID` |

**UID binding.** A worker registered from the dashboard starts with `uid = NULL`; their first
badge tap binds the tag's UID to them. Every later tap must present that same physical tag.
This makes cloning a badge — trivial on MIFARE Classic with the default key — detectable rather
than silent.

Sessions are mirrored to the `worker_sessions` table so they **survive a backend restart**.

**Sessions are scoped to the station, not just the board.** A session records which reader the
badge was tapped at. One ESP32 can carry two readers — the documented warehouse layout puts
`warehouse_gate` and `warehouse_rack` on one board — so without this, badging in to rack a
pallet would silently satisfy the dispatch check at the gate for the next five minutes. Scan
*attribution* stays board-scoped, which is correct: the worker who badged in at that board is
physically the one at its readers.

**Supervisor dispatch enforcement.** Dispatch at the warehouse gate — unit, carton, or pallet —
requires an active session whose role is `supervisor` **and which was opened at the gate**. If none exists, a `security` alert
(`UNVERIFIED DISPATCH`) is raised with the device ID and timestamp. **The dispatch still
completes**: this is a deliberate choice so a missing badge cannot deadlock warehouse
operations. The control is detective, not preventive.

### Worker Zones

Workers carry a `zone` (`warehouse`, `factory`, `general`) scoping them geographically. Each
station sits in a zone — `factory_writer`/`factory_exit` in `factory`, `warehouse_gate`/
`warehouse_rack`/`returns_gate` in `warehouse`.

Badging in outside your zone raises a `ZONE VIOLATION` security alert **but still grants the
session**. This is detective, matching the supervisor rule: a roster detail must never deadlock
a warehouse. A worker whose zone is `general` is in scope everywhere and never triggers it.

To make it preventive instead, turn the alert branch in `_handle_worker_badge` into an early
`return` — but read the note in §8 of `CLAUDE.md` first.

### Pre-seeded Workers

| Employee ID | Name | Role | Zone |
|-------------|------|------|------|
| EMP-001 | Alice Tan | supervisor | warehouse |
| EMP-002 | Bob Lim | operator | warehouse |
| EMP-003 | Carol Wong | operator | factory |
| EMP-004 | David Ng | operator | factory |

Write these to physical tags with `tag_writer.py` (see [Setup](#3-write-worker-badges)).

A reader can never enrol a worker: an `EMP-` badge that is not already on the roster is refused
and raises a security alert, the same treatment an unregistered *product* tag gets at the
factory exit and the warehouse gate. Add workers from the dashboard's **Workers** tab
(`POST /api/workers`, manager or admin) before writing their badge.

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
| **Assistant** | Natural-language questions over inventory, forecasts, the audit trail, alerts and pipeline state; shows which tools answered each question |
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
| **Demand forecast** | Gradient Boosting Regressor over lag-1/2/3 + 3-day and 7-day rolling means, fitted to a zero-filled 30-day `warehouse_dispatch` series; falls back to exponential smoothing (α = 0.3) when sklearn is missing, the series is under 7 days, or fewer than 3 days saw any dispatch |
| **EOQ** | `√(2DS/H)` — D = annual demand, S = 10 (reorder cost), H = 0.5 (holding cost) |
| **Risk score** | Days of stock remaining → 90 (≤ 3 d), 60 (≤ 7 d), 20 (otherwise) |
| **ABC analysis** | Items ranked by units moved (`ABS(quantity_change)` over the two warehouse gate actions); cumulative Pareto banding — first 80 % of volume = A, up to 95 % = B, tail = C |
| **Anomaly detection** | Isolation Forest (contamination 0.05) over the 500 **most recent** transactions, read oldest-first so the inter-scan gap is computed forward in time; featurised as hour-of-day, weekday, |Δqty|, gap |
| **Per-item anomaly flag** | An item is flagged only when its anomaly count beats the rate chance would give it — `contamination × its own scans`, floor of 2. Isolation Forest flags 5 % of any window by construction, so rolling raw flags up per item marked *every* item once traffic was realistic |
| **Transaction trends** | Daily received / dispatched counts, zero-filled for missing days |
| **Inventory summary** | Health score, low-stock / out-of-stock / dead-stock counts, today's scans, today's security events, and tag counts bucketed as in-warehouse / with-product / consumed across **both** the pipeline and legacy states |
| **Pipeline summary** | Tag counts per stage, per-item stage breakdown, rack utilisation, write-job history |

**Demand signal.** `warehouse_dispatch` is what counts as demand — the only action recording
goods physically leaving the building, which is exactly the "30-day dispatch history" the UEC
abstract describes. `rack_remove` (picking) is deliberately excluded: a pick that never ships
is not demand. The legacy `scan_in` / `scan_out` actions, emitted only by the single-reader
demo handler, are excluded from every analytics query.

> **Note on empty output.** An item with no dispatch in the window reports `0.0` demand and
> `∞` days remaining, and the forecast stays on exponential smoothing. That is the honest
> result, not a failure — before the demand signal was corrected, the same items reported a
> fabricated 1.0 unit/day and an EOQ of 120.8 apiece.

---

## LLM Assistant

`POST /api/assistant`. Answers natural-language questions over the live
inventory, pipeline, and audit data - the component named in the UEC Figure 1
caption.

**Tool use, not text-to-SQL.** The model never sees or writes SQL. It chooses
from five fixed, parameterised, read-only queries in `backend/assistant_tools.py`:

| Tool | Returns |
|------|---------|
| `list_inventory(low_stock_only)` | Catalogue items and stock levels |
| `get_demand_forecast(item_id)` | Forecast, EOQ, risk score, days of cover |
| `search_transactions(item_id, action, limit)` | Audit trail rows, newest first |
| `list_alerts(unread_only, limit)` | Low-stock and security alerts |
| `get_pipeline_status()` | Tag counts per pipeline stage |

There is deliberately **no tool that writes anything**. Physical movement flows
exclusively through the MQTT handlers (architecture invariant 2), and the
assistant must not become a second writer. A test asserts that calling every
tool leaves items, transactions, tags and alerts byte-identical.

**Provider is swappable** via `ASSISTANT_PROVIDER`; both adapters share one tool
layer and one system prompt, so switching changes nothing the dashboard can see
except answer quality.

| Provider | Value | Model | Key | Cost |
|----------|-------|-------|-----|------|
| Gemini (default) | `gemini` | `gemini-3.6-flash` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Free on the AI Studio tier, rate-limited |
| Claude | `anthropic` | `claude-opus-5` | `ANTHROPIC_API_KEY` | ~$0.03-0.06 per question |

Models are pinned, not tracking `gemini-flash-latest`: a moving default would change
answers between demo runs and invalidate anything the report quotes. Override with
`GEMINI_MODEL` or `ANTHROPIC_MODEL`. Note that `gemini-2.5-flash` now returns **404 for
new API keys** — the API itself directs new keys to 3.6.

**Free-tier behaviour.** Transient `503 high demand` responses are retried through the
SDK's own backoff (3 attempts). A `429` quota hit is **not** retried — the free tier's
quota is per-minute, so a few seconds of backoff will not clear it and retrying only
spends more of it. The endpoint returns `429` with a distinct "rate limited, wait a
moment" message so a demo can tell a quota pause apart from a real failure.

Measured prompt cost: a ~1,016-token fixed prefix (system prompt + five tool
schemas) plus 133-1,356 tokens per tool result, typically ~3,000 input and ~600
output tokens per question across two round trips.

**Dashboard tab.** The **Assistant** tab hosts the chat. When no provider is configured it
shows the exact reason and the two commands that fix it, rather than failing silently.
Answers render as **escaped plain text** with `white-space: pre-wrap` — no markdown is
parsed, so nothing the model returns can inject markup (architecture invariant 6).

**Setup.** Both SDKs are optional and guarded, exactly like sklearn in
`analytics.py` - the backend boots, the dashboard works, and the ESP32 pipeline
runs with neither installed. `GET /api/assistant` reports readiness and, when
unavailable, says precisely why.

```powershell
.venv\Scripts\python -m pip install -r requirements-assistant.txt
$env:ASSISTANT_PROVIDER = "gemini"
$env:GEMINI_API_KEY     = "<key from https://aistudio.google.com/apikey>"
```

**Verified end to end.** A live Gemini round trip answers correctly and grounded — asked
which items are low, it calls `list_inventory` and returns the same four items
`/api/dashboard` reports; asked about `item-006` it calls `get_demand_forecast` and
correctly explains that 0 demand and 999 days means no dispatch in 30 days rather than
overstock.

**Security.** `@login_required` only, deliberately: every tool is read-only, so a
viewer asking a question is exactly as harmless as a viewer reading the
dashboard. Every endpoint the tools mirror (`/api/items`, `/api/transactions`,
`/api/alerts`, `/api/analytics`) is `@login_required` too, so the assistant exposes
nothing a viewer could not already fetch directly. Item names, transaction notes, and alert messages are operator- and
scanner-supplied, so the system prompt instructs the model to treat tool output
as data and never as instructions. Provider errors are logged server-side and
returned to the client as a generic 502 rather than leaking quota or project
detail.

> **Offline note.** A cloud provider needs outbound internet, which is in tension
> with the offline-LAN deployment story (see
> [Security Posture](#security-posture--known-limitations)). Running a local
> model would resolve that; the provider seam exists so a third adapter is a
> small change.

---

### Populating a demo

A fresh database has ten items, one tag and no recent movement, so most tabs are empty and the
forecast correctly reports zero demand for everything. `tools/seed_demo.py` backfills a coherent
world: 30 days of receive/dispatch history, ~90 tags spread across the pipeline states, cartons
on pallets, purchase orders, write jobs and alerts.

```powershell
.venv\Scripts\python tools\seed_demo.py --status    # what is already seeded
.venv\Scripts\python tools\seed_demo.py --dry-run   # inspect before writing
.venv\Scripts\python tools\seed_demo.py --yes       # write it
```

It never runs on its own, requires `--yes`, refuses to seed twice without `--force`, and never
edits or deletes an existing row. History is generated **backwards from each item's present
quantity**, so the ledger lands exactly on `items.quantity` — that invariant and a
never-negative running balance are both asserted before anything is written, and a ledger that
fails either is refused outright. Every seeded transaction carries `device_id='demo-seed'` and
seeded tags use a `DE` UID prefix, so synthetic rows stay distinguishable from real scans.

Timestamps are written in **UTC**, matching SQLite's `CURRENT_TIMESTAMP`; today's events are
clamped to the past so nothing appears in the future in the live feed.

Station sessions expire after 300 s by design, so the Workers tab shows a live session only
briefly after seeding.

### Screenshots

`tools/screenshot.mjs` captures every tab to `screenshots/` (gitignored) using Chrome or Edge —
useful for report figures, and how the UI is checked. Optional, and separate from the test
suite; the project itself still has no build step.

```powershell
npm install puppeteer-core
node tools/screenshot.mjs            # all tabs, light theme
node tools/screenshot.mjs --dark
node tools/screenshot.mjs --tab assistant
```

---

### Stock profiles

Every item carries a `stock_profile` that changes what dispatch *means* for it. A profile adds
**no pipeline stages** — the five-stage state machine is exactly as published; a profile changes
two decisions the warehouse gate makes when goods leave.

| Profile | Dispatch | Supervisor | For |
|---------|----------|-----------|-----|
| `consumable` *(default)* | Terminal | Alert only | Components used up when issued |
| `returnable` | Opens a return (`return_pending`) | Alert only | Tools expected back |
| `serialised` | Terminal | **Required — refused without one** | High-value, individually tracked |

- **`returnable`** removes the manual step. A tool scanned out at the gate goes to
  `return_pending`, so scanning it back at the rack returns it to stock through the existing
  handler — no admin marking it for return by hand.
- **`serialised`** is the one class where the supervisor check is *preventive*. Without an active
  supervisor session the dispatch is refused: no state change, no quantity change, no audit row,
  and a `BLOCKED DISPATCH` alert. Everything else keeps the deliberate detective behaviour, because
  warehouse operations must never deadlock on a badge reader
  ([Tag Lifecycle](#tag-lifecycle--state-machine)).
- The action stays `warehouse_dispatch` for every profile, so the demand forecast counts a
  returnable issue like any other — the goods did leave.

`consumable` reproduces the original behaviour exactly and migration 15 defaults every existing
row to it, so adding profiles changed nothing until an item was reclassified. An unrecognised
value falls back to `consumable` rather than failing, so a row written by an older build can
never stall the pipeline.

Managers set the profile from the Inventory tab's edit dialog; `GET /api/stock-profiles` serves
the catalogue so the wording lives server-side.

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
│   │   └── dashboard.html      Sidebar + 9 tabs + 13 modals
│   ├── static/
│   │   ├── css/style.css       Sidebar, skeleton loaders, toasts, modals
│   │   └── js/dashboard.js     Tabs, Chart.js, SSE, RBAC gating, all CRUD
│   └── tests/                  pytest suite — 389 tests across 13 modules
│
├── esp32/
│   ├── config.py               Per-board: DEVICE_ID, READERS, Wi-Fi list, broker, topics
│   ├── boot.py                 Wi-Fi bring-up, selects broker IP per matched network
│   ├── main.py                 Rack-reader firmware (esp32-04 variant)
│   ├── rfid_reader.py          RFIDReader.read_tag() / write_item_id()
│   ├── mfrc522.py              Low-level MFRC522 driver (MicroPython)
│   └── tag_writer.py           Interactive tag-writing utility (setup / demo)
│
├── firmware/
│   └── ESP32_GENERIC-20260406-v1.28.0.bin   MicroPython v1.28.0 image for reflashing
│
├── tools/
│   └── esptoolenv/             Python venv for esptool + mpremote — REBUILD on a new
│                                 machine, a moved venv does not work (see onboarding)
│
├── docs/                       Academic deliverables — all gitignored, zip-only
│   ├── uec/                    UEC abstract (v1, v2), UEC poster, template sample
│   ├── prism/                  PRISM 2026 poster (+ Final Version/UG077)
│   ├── ieee/                   IEEE technical paper (docx + pdf)
│   ├── report/                 Full FYP2 report, presentation, submission forms,
│   │                             Appendix C, Technical Paper, UEC PDF (~432 MB)
│   ├── media/                  Demo video (331 MB), use-case scenario diagram
│   └── figures/                Figure exports for the report/poster
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

**What git tracks vs what the zip carries.** The repository contains only source, tests, and
documentation — roughly 1.4 MB. `docs/`, `firmware/*.bin`, `tools/esptoolenv/`, and
`backend/inventory.db` are gitignored: together they are about **830 MB**, and GitHub rejects
any single file over 100 MB (`docs/report/Smart_Inventory_FYP2_Presentation.pptx` alone is
349 MB). They travel by zip, not by `git push` — never `git add -f` them.

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

**Requirements:** MicroPython v1.24+, `mpremote`, `esptool` — both provided by
`tools/esptoolenv` (rebuild it first, see [onboarding step 3](#step-by-step)).

**Step 1 — flash MicroPython (once per board).** The image is bundled at `firmware/`.
```
tools\esptoolenv\Scripts\python -m esptool --chip esp32 --port COM<N> erase-flash
tools\esptoolenv\Scripts\python -m esptool --chip esp32 --port COM<N> --baud 460800 write_flash -z 0x1000 firmware\ESP32_GENERIC-20260406-v1.28.0.bin
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

## Testing

```bash
.venv\Scripts\python -m pip install -r requirements-test.txt
.venv\Scripts\python -m pytest -q   # ~3.8 minutes
```

389 tests across 13 modules cover auth and RBAC, item CRUD, tags, workers and sessions, alerts,
audit filters, purchase orders, webhooks, CSV export/import, analytics, database migrations and
seeding, and every pipeline handler (factory write/exit, warehouse gate/rack, returns, legacy
scan, worker badge routing). Each test runs against an isolated temporary SQLite file, and the
suite makes no network calls.

**Current status: 389 passed, 0 failed.** Ten tests were previously failing on stale assertions
or network dependence; all ten were fixed **in the tests only**, with no product code changed:

| Was failing | How it was fixed |
|---|---|
| `TestIdempotency::test_migrations_*` (2) | Asserted 10 rows in `schema_version`; there are 14 migrations. Now assert the shape — versions contiguous from 1, and a second `init_db()` adds no rows — so appending a migration cannot re-break them. |
| `TestHandleWarehouseRack::*` (5) | Called the handler without `item_id`, which it requires. The payloads now include it, as the firmware does. |
| `test_unknown_tag_with_item_id_auto_creates` | Renamed `test_unregistered_tag_raises_security_alert`; asserts the current behaviour — alert raised, no tag row created. |
| `TestTestWebhook::*` (2) | Posted to `http://example.com/hook` over the real internet. A fixture now stubs `urllib.request.urlopen` and asserts the delivered URL and method. |

Two further tests were **passing for the wrong reason** — both omitted `item_id`, so the handler
returned early and the assertion never ran. `test_invalid_state_tag_unchanged` seeded a `tagged`
tag, which is a *valid* rack state, and now uses `consumed`; `test_unknown_tag_does_nothing`
split into `test_payload_without_item_id_is_ignored` and a new
`test_unknown_tag_is_standalone_rack_add` covering the `rack_add` (+1) path. Both new
load-bearing assertions were mutation-tested — removing the `item_id` guard, and restoring the
old auto-create behaviour, each turn their test red.

---

## Academic Deliverables — UEC, PRISM, IEEE, Report

All documents live under `docs/` and travel with the zip (gitignored — see
[Project Structure](#project-structure)).

| Deliverable | Location | Latest file | State |
|-------------|----------|-------------|-------|
| **UEC abstract** | `docs/uec/` | `UEC_Abstract_FYP_v2.docx` | v2 — current submission draft |
| **UEC poster** | `docs/uec/` | `UEC_Poster_FYP.pptx` | Drafted |
| **PRISM 2026 poster** | `docs/prism/` | `FIST_PRISM_2026_Poster_Final_Tai_Ke_Ying_Dorothy.pptx` / `.pdf`, plus `Final Version/UG077.pptx` | Final, submitted |
| **IEEE technical paper** | `docs/ieee/` | `ieee_paper_done.pdf`, `ieee_paper_updated.docx` | Complete |
| **FYP2 report** | `docs/report/` | `FYP2_Report_..._v2_1.docx`, `Tai_Ke_Ying_Dorothy_1211111348.pdf` | Submitted (July final submission folder included) |
| **Presentation** | `docs/report/` | `Smart_Inventory_FYP2_Presentation.pptx` (349 MB) | Delivered |
| **Demo video** | `docs/media/` | `Fyp demo video.MOV` (331 MB) | Recorded |

### UEC abstract — what changed in v2

**Title:** *AI-Driven Smart Inventory Management System using RFID and IoT*
**Authors:** Tai Ke Ying Dorothy (1211111348), Lim Jun Hong (1211110418)

| Aspect | v1 | v2 (current) |
|--------|----|----|
| Format | Long-form abstract + extended prose | Condensed 5-section short paper: Introduction, Methodology, Key Results, Model Equations, Conclusion |
| Faculty / campus | Faculty of Engineering and Technology, Cyberjaya | **Faculty of Information Science and Technology, Melaka** |
| Contact | Placeholder `author@mmu.edu.my` | Real student emails |
| ESP32 node count | **Three** nodes | **Four** nodes (factory writer, factory exit, warehouse gate, warehouse rack) |
| Anomaly result | "detection of 12 anomalous transactions" | Generalised to "detects anomalous transactions automatically" |
| Model detail | Named only | Full hyperparameters: GBR `n_estimators=50, max_depth=3`; IsolationForest `n_estimators=100, contamination=0.05` |
| Equations | — | **New:** EOQ `√(2·D̂·S/H)` with S = RM 10, H = 0.5; GB prediction `D̂ⁿ⁺¹ = Σₘ γₘhₘ(xⁿ)` over `[n, lag₁, lag₂, lag₃, MA₃, MA₇]` |
| Comparison table | — | **New:** Table 1 — latency < 1 s vs > 5 min manual; GB vs exponential smoothing; automatic vs no anomaly detection |
| References | — | **New:** Friedman (2001) Gradient Boosting; Liu et al. (2008) Isolation Forest; Want (2006) RFID |
| Figure 1 caption | Architecture only | Architecture **"+ LLM Assistant"** — see the gap below |

### Claims the abstract makes that the code must support

These are the v2 claims a reviewer could check against the repository. Verified against the
current codebase:

| Claim | Status |
|-------|--------|
| Four ESP32 nodes with RC522 across a five-stage pipeline | ✅ Backend handles all four station roles; `esp32/main.py` in the repo is the rack variant |
| MQTT → Mosquitto → Flask, SQLite in WAL mode | ✅ `get_db()` sets `journal_mode=WAL` |
| Five-stage state machine `tagged → in_transit → received → racked → dispatched` | ✅ `mqtt_subscriber.py` |
| Sub-second dashboard updates via SSE | ✅ `events.py` + `/api/events` |
| Gradient Boosting Regressor, `n_estimators=50, max_depth=3`, lag-1/2/3 + MA₃ + MA₇ | ✅ `analytics.py::_gb_forecast` — parameters match exactly |
| Falls back to exponential smoothing below 7 data points | ✅ `_gb_forecast` guard |
| Isolation Forest, `n_estimators=100, contamination=0.05`, 500 recent transactions | ✅ `analytics.py::detect_scan_anomalies` — parameters match exactly |
| Features: hour-of-day, weekday, \|Δquantity\|, inter-scan gap | ✅ Exactly these four |
| EOQ from ML-predicted demand, S = RM 10, H = 0.5 | ✅ `REORDER_COST = 10.0`, `HOLDING_COST = 0.5` |
| Hardware cost under RM 300 | ✅ 4× ESP32 + 4× RC522 + tags |
| **"LLM Assistant" in the Figure 1 architecture** | ✅ `POST /api/assistant` — tool-use over five read-only queries; see [LLM Assistant](#llm-assistant). No dashboard UI yet |

---

## Specification Gaps to Close

Ordered by what a reviewer or examiner is most likely to notice.

### 1. The LLM Assistant claimed in UEC v2 — **built**

Figure 1's caption reads *"Flask backend (AI Analytics + LLM Assistant)"*, and until now no
such code existed. `POST /api/assistant` now implements it: tool-use over five read-only
queries against `items`, `transactions`, `alerts` and the analytics engine. See
[LLM Assistant](#llm-assistant).

The provider is swappable and defaults to Gemini's free tier, so the claim holds without a
paid dependency, and the dashboard's **Assistant** tab hosts it. Verified end to end
against the live Gemini API.

### 2. Analytics are blind to pipeline traffic — **fixed**

`_get_daily_usage`, `get_transaction_trends` and `get_abc_analysis` filtered on the legacy
`scan_in` / `scan_out` actions, which the pipeline never emits. The forecast therefore trained
on an empty series and always fell back to exponential smoothing — the opposite of the
abstract's claim — while reporting a fabricated 1.0 unit/day and an identical EOQ of 120.8 for
every item. All three now read the pipeline's own actions, and the 30-day series is zero-filled
so the model's lag features line up with real calendar days.

`detect_scan_anomalies` was **not** unaffected, contrary to an earlier reading of this section:
it ordered `timestamp ASC LIMIT 500`, taking the *oldest* 500 rows while documenting itself as
"recent". Below 500 transactions that is invisible; past it, anomaly detection would have
frozen on the earliest history forever. It now takes the newest 500, returned oldest-first.

**Remaining:** the demo database holds no `warehouse_dispatch` rows inside the last 30 days
(the newest is 2026-07-01), so the Gradient Boosting path is exercised by tests but not by the
shipped demo data. A live demo needs recent dispatch scans, real or seeded.

### 3. Node count and test scope

v2 claims four ESP32 nodes; the Key Results section says testing used *"three live ESP32
boards"*. Only the rack-reader `main.py` (esp32-04) is in the repository — the multi-role
firmware for boards 1–3 is not committed. Either commit it or state the board count
consistently in both the abstract and the report.

### 4. Affiliation inconsistency in v2

The author block says *Faculty of Information Science and Technology, Melaka*; the
Acknowledgment still thanks the *Faculty of Engineering and Technology*. Pick one.

### 5. Unimplemented or half-wired features

| Feature | State |
|---------|-------|
| Stock reservation (`reserved_qty`) | Endpoints exist, never consumed by dispatch, no UI calls them |
| Pallet `factory_exit` | Routed to the carton handler, which looks pallets up in `cartons` and aborts |
| Purchase-order receipt | Increments by 1 even for an N-unit carton; no supplier, cost, or expected date |
| Return desk (`inventory/returns/gate`) | Backend handler complete; no hardware station built |
| Worker zones | Recorded on the session, never used to reject a scan |

See [Functional gaps](#functional-gaps) for the full list.

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
| Session security | **Mostly fixed.** `SECRET_KEY` is refused in production (`INVENTORY_ENV=production` fails closed) and warns loudly otherwise; `debug` is off unless `FLASK_DEBUG=1`; cookies are `HttpOnly` + `SameSite=Lax`, with `Secure` opt-in via `SESSION_COOKIE_SECURE` since the LAN demo is plain HTTP; `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` are set. **Still open:** no CSRF tokens (SameSite=Lax is the current mitigation) and no CSP, which needs the CDN assets vendored first |
| RBAC | **Fixed.** `PUT /api/items/<id>`, `POST /api/tags` and `DELETE /api/alerts/read` are now `@manager_required`, so the read-only role really is read-only. Marking a single alert read stays open to any signed-in user: it is non-destructive and acknowledging something you can already see is reasonable | — |
| Session lifecycle | Sessions are stateless cookies — deleting a user or changing their role does not invalidate their live session | Revocation is delayed until expiry (8 h) |
| Default accounts | `manager` / `viewer` are re-created with known passwords on every startup if missing | Deleting them is not permanent |
| MQTT | `allow_anonymous true`, no TLS, no ACLs | Any LAN host can publish forged scans and move stock arbitrarily |
| RFID | MIFARE Classic 1K, default key `FFFFFFFFFFFF`, plaintext payload, no signature | Tags are trivially cloneable — a cloned supervisor badge grants dispatch authority |
| Secrets in git | `esp32/config.py` contains **real Wi-Fi passwords** and is tracked | Credential disclosure to anyone with repo access |
| Message integrity | MQTT QoS 0, no message IDs, no server-side dedupe (only a 2 s per-UID cooldown on the board) | A replayed or duplicated packet double-counts stock |
| Concurrency | Read-modify-write sequences (`reserve_stock`, PO receipt) are not transactional; `previous_quantity` on dispatch is reconstructed and is wrong when the clamp at zero fires | Small accounting drift under concurrent scans |
| Webhooks | No URL allowlist, no HMAC signing, no redirect limits | SSRF reachable by an admin; receivers cannot verify payloads |
| Uploads | `MAX_CONTENT_LENGTH` is now 16 MB. **Still open:** CSV import still commits partial results rather than one transaction | Partial imports |
| Error handling | **Fixed.** The three INSERT paths that returned the raw driver message (e.g. `UNIQUE constraint failed: items.id`) now report the actionable fact and log the detail server-side | — |
| Offline claim | Dashboard loads Tailwind, Chart.js, and jsPDF from public CDNs | A truly air-gapped LAN renders unstyled and loses charts/PDF export |
| Backup | **Fixed.** `/api/export/backup` uses the SQLite backup API, so the snapshot includes commits still in the write-ahead log; a test asserts the download passes `PRAGMA integrity_check` and carries rows | — |
| Timezone | SQLite writes UTC via `CURRENT_TIMESTAMP`, analytics filters use local `datetime.now()` alongside `DATE('now')` (UTC) | Day-boundary metrics are off by the UTC offset |

### Functional gaps

| Area | Gap |
|------|-----|
| Analytics | ~~Demand forecast, trends, and ABC query `scan_in`/`scan_out`~~ — fixed; all three now read the pipeline's own actions. Demo data still has no dispatch inside the 30-day window, so the ML path needs recent scans to show in a live demo |
| Reservations | `reserved_qty` is set by the reserve endpoints but never consumed or released by dispatch, and no dashboard control calls it |
| Worker zones | Recorded on the session but never used to reject a scan, so a worker badged into one zone can authorise a scan at any station |
| Pallets | **Fixed, and the cause ran deeper than this.** `rfid_tags.item_id` is a foreign key into `items`, and a pallet id is not an item, so `_pallet_factory_written` raised `FOREIGN KEY constraint failed` — **no pallet tag could ever be registered**. Pallet lifecycle state now lives in `pallets.state` (cartons are unaffected: they carry a real `item_id`), a dedicated `_pallet_factory_exit` moves the pallet and its cartons to `in_transit`, and the gate falls back to `pallets.state` instead of assuming `in_transit`. The subsystem had no tests at all; it now has nine |
| Purchase orders | Receipt quantity **fixed**: `_check_purchase_order(c, item_id, qty)` credits what actually arrived — 1 for a unit, the carton's `unit_count`, the per-SKU total for a pallet — so a bulk receipt can now fulfil an order. **Still open:** POs carry no supplier, unit cost or expected date, and are not linked to the tags that fulfil them |
| Referential cleanup | Deleting an item removes its tags, POs and write jobs but leaves `cartons` orphaned |
| ERP readiness | No cost/price/supplier/category fields, no stock valuation, no multi-warehouse or bin hierarchy, no lot/expiry tracking, no UoM conversion |
| Integration surface | Cookie sessions only — no API keys or tokens for machine-to-machine use, no `/api/v1` versioning, no OpenAPI spec, no pagination convention, no inbound webhooks |
| Operations | `print()`-based logging with no log file, no structured logs, no metrics, no DB health check (`/api/status` reports MQTT only) |

### Suggested remediation order

1. ~~`SECRET_KEY` from environment (fail closed), `debug=False`, cookie flags, security
   headers.~~ **Done**, except CSRF tokens and a CSP.
2. ~~Fix the RBAC decorators on item update and tag registration.~~ **Done.**
3. Broker credentials + per-topic ACLs; TLS if the network is not fully trusted.
4. Purge Wi-Fi credentials from `config.py` (move to an untracked `config_local.py` — already
   gitignored) and rotate the exposed passwords.
5. ~~Widen the analytics action filters to the pipeline actions.~~ **Done.**
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
| Testing | pytest, pytest-cov — 389 tests |
| Deployment | On-premise: Gunicorn (1 worker, threaded) + systemd + LAN; Cloud: Railway / Render + managed PostgreSQL + cloud MQTT |
