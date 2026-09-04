# CLAUDE.md — Working Context for This Repository

Smart Inventory Management System (RFID + IoT) — Final Year Project, TAI KE YING DOROTHY.
This file is loaded into every session. It records the **design spec, invariants, and known
quirks** so a fresh session on any machine can work here without re-deriving them.

---

## 1. What this system is

An end-to-end RFID supply-chain tracker. Physical goods carry MIFARE Classic 1K tags written
with an item/carton/pallet identifier. ESP32 boards with RC522 readers sit at four stations and
publish every scan over MQTT. A Flask backend consumes those scans, drives a **tag state
machine**, mutates inventory quantities, raises alerts, and pushes live updates to a browser
dashboard over Server-Sent Events.

Everything runs on a LAN by default: Mosquitto + Flask + SQLite on one PC, ESP32 boards on the
same Wi-Fi.

---

## 2. Repository layout

```
smart-inventory-rfid-iot/
├── backend/
│   ├── app.py               Flask app — ~90 REST endpoints, RBAC decorators, SSE, webhooks
│   ├── database.py          SQLite schema, versioned migrations, demo seeds
│   ├── mqtt_subscriber.py   MQTT client + the pipeline state machine (the system's core)
│   ├── analytics.py         Forecasting, EOQ, ABC, anomaly detection, pipeline summary
│   ├── events.py            In-process SSE fan-out bus (queue per browser client)
│   ├── inventory.db         Runtime DB — gitignored, created on first run
│   ├── templates/           login.html, dashboard.html (8-tab SPA shell)
│   ├── static/css|js/       style.css, dashboard.js (all frontend logic, no build step)
│   └── tests/               pytest suite, 387 tests
├── esp32/
│   ├── config.py            PER-BOARD config: DEVICE_ID, READERS, Wi-Fi, broker, topics
│   ├── boot.py              Wi-Fi bring-up, selects broker IP from the matched network
│   ├── main.py              Firmware loop (currently the rack-reader variant, esp32-04)
│   ├── rfid_reader.py       RFIDReader.read_tag() / write_item_id()
│   ├── mfrc522.py           Low-level MFRC522 driver (MicroPython)
│   └── tag_writer.py        Interactive REPL utility for writing item IDs / worker badges
├── firmware/                MicroPython image for reflashing (gitignored, zip-only)
├── tools/esptoolenv/        venv for esptool + mpremote — MUST be rebuilt per machine
├── docs/                    Academic deliverables, ~830 MB, all gitignored (zip-only)
│   ├── uec/                 UEC abstract v1/v2 + poster
│   ├── prism/               PRISM 2026 poster
│   ├── ieee/                IEEE technical paper
│   ├── report/              FYP2 report, presentation, submission forms
│   ├── media/               Demo video, use-case diagram
│   └── figures/             Figure exports
├── mosquitto.conf           listener 1883, allow_anonymous true
├── start.ps1 / start.bat    Windows one-click: broker + deps + backend + browser
├── requirements-pc.txt      flask, paho-mqtt, scikit-learn, numpy
├── requirements-test.txt    pytest, pytest-cov, flask, paho-mqtt, werkzeug
├── pytest.ini               testpaths=backend/tests, pythonpath=backend
├── README.md                User-facing setup + onboarding + feature documentation
└── reference.md             Report-oriented reference (maps to FYP thesis chapters)
```

**Git tracks only source, tests, and docs (~1.4 MB).** `docs/`, `firmware/*.bin`,
`tools/esptoolenv/`, and `backend/inventory.db` are gitignored and travel by zip. Never
`git add -f` them — GitHub rejects files over 100 MB and `docs/report/` contains a 349 MB pptx.

---

## 3. Running it on a new device

```powershell
# 1. Python 3.9+ and Mosquitto installed (default path C:\Program Files (x86)\Mosquitto)
pip install -r requirements-pc.txt

# 2. One-click: starts broker, installs deps, launches backend, opens dashboard
.\start.bat

# — or manually —
mosquitto -c mosquitto.conf
cd backend; python app.py          # http://localhost:5000

# 3. Tests
pip install -r requirements-test.txt
python -m pytest -q                # takes ~3.5 min
```

**Environment variables** (all optional, all read at import time):

| Variable | Default | Used by |
|----------|---------|---------|
| `SECRET_KEY` | `inv-secret-key-change-in-prod` | Flask session signing |
| `MQTT_BROKER` | `127.0.0.1` | `mqtt_subscriber.py` |
| `MQTT_PORT` | `1883` | `mqtt_subscriber.py` |
| `MQTT_USER` / `MQTT_PASSWORD` | empty | broker auth (unused by default) |

**ESP32 side:** edit `esp32/config.py` (`DEVICE_ID`, `READERS`, `WIFI_NETWORKS[].broker` = the
new PC's LAN IP from `ipconfig`), then upload with `mpremote`. `start.ps1` prints the host's IPs
at the end for exactly this reason.

Default logins: `admin/admin123`, `manager/manager123`, `viewer/viewer123`.

---

## 4. Architecture invariants — do not break these

1. **The MQTT subscriber runs in a daemon thread inside the Flask process.** `init_db()` and
   `start_mqtt()` are called from `app.py`'s `__main__` block. Any alternative launcher
   (gunicorn, waitress, WSGI) **must call both explicitly**, or scans are silently ignored.
2. **`mqtt_subscriber.py` is the only writer of pipeline state.** REST endpoints mutate
   catalogue/config data; physical movement flows exclusively through MQTT handlers.
3. **Every quantity change writes a `transactions` row** with `previous_quantity`,
   `new_quantity`, `device_id`, and `performed_by`. The audit trail is append-only — nothing in
   the codebase updates or deletes transaction rows, and it must stay that way.
4. **Quantity is only ever incremented at receive/return and decremented at dispatch.** Racking
   and picking are state-only (`quantity_change = 0`). The one exception is `rack_add`: a
   brand-new tag seen first at the rack is `+1`.
5. **RBAC is enforced server-side** via `@login_required` / `@manager_required` /
   `@admin_required`. The frontend's role gating is UX only and must never be the only check.
6. **Escape all interpolated values in `dashboard.js`** with the `esc()` helper before putting
   them in `innerHTML`. This is currently consistent across all ~70 sites — keep it that way.
7. **Migrations are append-only.** Add a new `(version, sql)` tuple to `_MIGRATIONS` in
   `database.py`; never renumber or edit an existing one. Currently 14 migrations.
8. **`get_db()` returns a fresh connection per call** with WAL, `busy_timeout=3000`, and
   `foreign_keys=ON`. Handlers open, use, commit, close. There is no connection pool and no ORM.

---

## 5. The tag state machine (the heart of the system)

States live in `rfid_tags.state`. Handlers in `mqtt_subscriber.py` are keyed by MQTT topic;
the tag's **current state** decides what the scan means.

```
blank ──factory_writer──► tagged ──factory_exit──► in_transit
                                                        │
                                     warehouse_gate ─────┤ (+1)
                                                        ▼
                                                    received
                                                        │
                                     warehouse_rack ─────┤ (0)
                                                        ▼
                                                     racked
                                                        │
                                     warehouse_rack ─────┤ (0)  pick off shelf
                                                        ▼
                                                     picked
                                                        │
                                     warehouse_gate ─────┤ (−1)
                                                        ▼
                                                   dispatched  ← TERMINAL
                                                        │
                              dashboard admin  ─────────┤ mark for return
                                                        ▼
                                                  return_pending
                                                        │
                                     warehouse_rack ─────┤ (+1 then racked, 2 audit rows)
                                                        ▼
                                                    returned → racked → (cycle repeats)
```

- **Same reader, opposite meanings.** `warehouse_gate` receives when the tag is
  `in_transit`/`out`, and dispatches when it is `received`/`racked`/`picked`/`returned`/`in`.
  `warehouse_rack` racks, picks, finalises returns, or standalone-adds depending on state.
- **Security states.** A `dispatched` or `consumed` tag scanned anywhere raises a `security`
  alert row and an SSE `security_alert` event. An **unregistered** tag at factory exit or the
  warehouse gate also raises a security alert and is *not* auto-created.
- **Supervisor enforcement is detective, not preventive.** Dispatch without an active
  supervisor session raises `UNVERIFIED DISPATCH` — **but the dispatch still completes.** This
  is deliberate (warehouse operations must never deadlock on a badge). Do not "fix" it into a
  hard block without asking.
- **Legacy mode** (`inventory/scan` topic) is a separate, simpler toggle machine:
  `out → in → consumed`. It is kept for the single-reader demo and must not be deleted.

### Tag hierarchy

`rfid_tags.tag_level` is `unit` | `carton` | `pallet`. Routing is by **ID prefix on the tag's
written payload**: `CTN-*` → carton handlers, `PLT-*` → pallet handlers, `EMP-*` → worker badge
handler (intercepted in `_on_message` before pipeline routing), anything else → unit handlers.
A carton scan moves `unit_count` units; a pallet scan moves every carton on it, grouped by SKU.

---

## 6. Conventions used throughout

- **Python:** stdlib + Flask only in the request path; sklearn/numpy are optional and guarded by
  a `_SKLEARN` flag with an exponential-smoothing fallback. Parameterised SQL everywhere —
  never f-string a value into SQL (column *names* in whitelisted `UPDATE` builders are fine).
- **Comments:** section dividers use `# ── Title ─────` box-drawing rules. Match that style.
- **Frontend:** no framework, no build step. Tailwind/Chart.js/jsPDF come from CDNs. Data
  loading is `fetch` → render function → `innerHTML` with `esc()`. Live updates arrive via the
  `/api/events` SSE stream and trigger targeted re-renders.
- **Naming:** MQTT handlers `_handle_<station>`, carton/pallet variants `_carton_*` / `_pallet_*`.
- **Errors:** endpoints return `{'error': '...'}` with a real HTTP status. Success is
  `{'status': 'ok'}`.

---

## 7. Known state of the test suite

`python -m pytest` → **377 passed, 10 failed** as of the last audit. All ten failures are
**stale tests or environment dependence, not product regressions**:

| Failing test(s) | Cause |
|---|---|
| `test_database.py::TestIdempotency::test_migrations_*` (2) | Assert `schema_version` count is 10; there are now 14 migrations. |
| `test_pipeline.py::TestHandleWarehouseRack::*` (5) | Call `_handle_warehouse_rack` without `item_id`; the handler now requires it and returns early. |
| `test_pipeline.py::test_unknown_tag_with_item_id_auto_creates` | Asserts an unregistered tag at factory exit auto-creates. Behaviour intentionally changed to "raise a security alert, create nothing". |
| `test_webhooks.py::TestTestWebhook::*` (2) | Hit `http://example.com/hook` over the real internet; the endpoint returns 405. Needs mocking. |

Do not "fix the code" to make these pass — fix the tests, and only when asked.

---

## 8. Deliberate behaviours that look like bugs

Flag these if relevant, but do not silently change them:

- Dispatch proceeds without a supervisor (alert only) — see §5.
- `delete_item` turns `PRAGMA foreign_keys = OFF` and leaves `transactions` rows pointing at the
  deleted item. This is intentional: the audit trail outlives the catalogue record.
- Legacy `inventory/scan` handling coexists with the pipeline handlers.
- `quantity` is clamped with `MAX(0, quantity - N)` so it can never go negative.
- `main.py` on `esp32-04` deliberately has no worker auth (`REQUIRE_WORKER_AUTH = False`) — a
  hardware-availability constraint, documented in the README.

---

## 9. Open gaps (audited, not yet addressed)

Short list so a fresh session doesn't rediscover them. Details and severity are in the audit
notes; the ones most likely to matter first:

- `SECRET_KEY` and `debug=True` defaults are unsafe for anything but a demo LAN.
- No CSRF tokens, no cookie hardening (`Secure`/`HttpOnly`/`SameSite`), no security headers.
- MQTT broker is anonymous and unencrypted; any LAN host can forge scan events.
- MIFARE Classic with the default key `FFFFFFFFFFFF` and plaintext payloads — tags are clonable.
- `esp32/config.py` with **real Wi-Fi passwords is committed to git**.
- Analytics query `action = 'scan_out'` / `'scan_in'`, which the current pipeline never emits —
  forecasting, trends, and ABC are effectively blind to pipeline traffic.
- `reserved_qty` is written by `/api/items/<id>/reserve` but never consumed by dispatch, and no
  UI calls it.
- `PUT /api/items/<id>` and `POST /api/tags` are `@login_required` only — a viewer can edit
  quantities and re-map tags.
- Pallets routed to `_carton_factory_exit` at factory exit, which looks them up in `cartons`
  and aborts.
- `_check_purchase_order` increments PO receipts by 1 even for an N-unit carton.
- No idempotency on MQTT messages (QoS 0); a replayed packet double-counts.
- Dashboard depends on four internet CDNs, contradicting the offline-LAN deployment story.

---

## 10. Academic context — what the paper claims

The UEC v2 abstract (`docs/uec/UEC_Abstract_FYP_v2.docx`) is the current submission and makes
specific, checkable claims about this codebase. Verified: the GBR hyperparameters
(`n_estimators=50, max_depth=3`), Isolation Forest settings (`n_estimators=100,
contamination=0.05`, 500 transactions, 4 features), EOQ constants (S=10, H=0.5), WAL mode, SSE,
and the five-stage state machine **all match the code exactly**. Keep them in sync — changing a
hyperparameter in `analytics.py` invalidates a published equation.

Two claims do not hold, and are the priority work items:

1. **"LLM Assistant"** appears in the Figure 1 architecture caption. No such code exists.
   Build it or remove it from the figure.
2. **Gradient Boosting "trained on 30-day dispatch history"** — `_get_daily_usage` filters
   `action = 'scan_out'`, which the pipeline never emits (it writes `warehouse_dispatch`), so
   the model trains on an empty series and always falls back to exponential smoothing. Same
   flaw in `get_transaction_trends` and `get_abc_analysis`.

Full deltas between abstract v1 and v2, and the complete claim-vs-code table, are in the
README's *Academic Deliverables* and *Specification Gaps to Close* sections.

---

## 11. Working preferences for this repo

- Commit and push after every completed task (repo: `origin` → `jhjh1214/smart-inventory-rfid-iot`,
  branch `main`).
- `backend/inventory.db` is gitignored — never commit runtime data.
- Keep `README.md` (setup/features) and `reference.md` (thesis chapters) in sync when behaviour
  changes; they are both graded artefacts.
