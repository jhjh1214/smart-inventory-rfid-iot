import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), 'inventory.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS items (
            id                  TEXT PRIMARY KEY,
            name                TEXT NOT NULL,
            quantity            INTEGER DEFAULT 0,
            reserved_qty        INTEGER DEFAULT 0,
            unit                TEXT DEFAULT 'pcs',
            low_stock_threshold INTEGER DEFAULT 5,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id           TEXT NOT NULL,
            action            TEXT NOT NULL,
            quantity_change   INTEGER NOT NULL,
            previous_quantity INTEGER NOT NULL,
            new_quantity      INTEGER NOT NULL,
            tag_uid           TEXT,
            performed_by      TEXT DEFAULT 'system',
            note              TEXT,
            device_id         TEXT DEFAULT 'dashboard',
            timestamp         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    TEXT,
            alert_type TEXT NOT NULL,
            message    TEXT NOT NULL,
            is_read    INTEGER DEFAULT 0,
            timestamp  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS rfid_tags (
            uid           TEXT PRIMARY KEY,
            item_id       TEXT NOT NULL,
            state         TEXT DEFAULT 'tagged',
            rack_location TEXT,
            previous_uid  TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_scan     TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT DEFAULT 'viewer',
            badge_uid     TEXT,
            employee_id   TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS workers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL DEFAULT 'Unknown',
            uid         TEXT UNIQUE,
            role        TEXT DEFAULT 'operator',
            zone        TEXT DEFAULT 'general',
            active      INTEGER DEFAULT 1,
            last_seen   TIMESTAMP,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS write_jobs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id     TEXT UNIQUE NOT NULL,
            item_id      TEXT NOT NULL,
            quantity     INTEGER NOT NULL,
            written      INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'pending',
            created_by   TEXT DEFAULT 'admin',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );

        CREATE TABLE IF NOT EXISTS worker_sessions (
            device_id   TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL,
            name        TEXT NOT NULL,
            role        TEXT NOT NULL,
            zone        TEXT DEFAULT 'general',
            expires_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS webhooks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            url        TEXT NOT NULL,
            events     TEXT DEFAULT 'low_stock,security',
            active     INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS purchase_orders (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id      TEXT NOT NULL,
            expected_qty INTEGER NOT NULL,
            received_qty INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'open',
            note         TEXT,
            created_by   TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );

        -- ── Tag hierarchy: cartons and pallets ──────────────────────────────
        --  A carton groups N unit-level items of one SKU under one RFID tag.
        --  A pallet groups M cartons under one RFID tag.
        --  One pallet scan at the warehouse gate moves the entire load.

        CREATE TABLE IF NOT EXISTS cartons (
            id          TEXT PRIMARY KEY,         -- e.g. CTN-0001
            item_id     TEXT NOT NULL,             -- which SKU is inside
            unit_count  INTEGER NOT NULL DEFAULT 1,-- how many units this carton holds
            tag_uid     TEXT UNIQUE,               -- RFID tag UID once written
            state       TEXT DEFAULT 'created',   -- created|in_transit|received|racked|dispatched
            note        TEXT,
            created_by  TEXT DEFAULT 'system',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );

        CREATE TABLE IF NOT EXISTS pallets (
            id         TEXT PRIMARY KEY,           -- e.g. PLT-0001
            tag_uid    TEXT UNIQUE,
            state      TEXT DEFAULT 'loading',     -- loading|sealed|in_transit|received|dispatched
            note       TEXT,
            created_by TEXT DEFAULT 'system',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pallet_cartons (
            pallet_id  TEXT NOT NULL,
            carton_id  TEXT NOT NULL,
            added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (pallet_id, carton_id),
            FOREIGN KEY (pallet_id) REFERENCES pallets(id),
            FOREIGN KEY (carton_id) REFERENCES cartons(id)
        );
    ''')

    # ── Schema migrations (tracked via schema_version to avoid re-runs) ──────
    _MIGRATIONS = [
        (1,  "ALTER TABLE rfid_tags ADD COLUMN last_scan TIMESTAMP"),
        (2,  "ALTER TABLE rfid_tags ADD COLUMN rack_location TEXT"),
        (3,  "ALTER TABLE transactions ADD COLUMN performed_by TEXT DEFAULT 'system'"),
        (4,  "ALTER TABLE transactions ADD COLUMN note TEXT"),
        (5,  "ALTER TABLE transactions ADD COLUMN device_id TEXT DEFAULT 'dashboard'"),
        (6,  "ALTER TABLE users ADD COLUMN badge_uid TEXT"),
        (7,  "ALTER TABLE users ADD COLUMN employee_id TEXT"),
        (8,  "ALTER TABLE workers ADD COLUMN zone TEXT DEFAULT 'general'"),
        (9,  "ALTER TABLE items ADD COLUMN reserved_qty INTEGER DEFAULT 0"),
        (10, "ALTER TABLE rfid_tags ADD COLUMN previous_uid TEXT"),
        # Tag hierarchy columns
        (11, "ALTER TABLE rfid_tags ADD COLUMN tag_level TEXT DEFAULT 'unit'"),
        (12, "ALTER TABLE rfid_tags ADD COLUMN parent_uid TEXT"),
        (13, "ALTER TABLE rfid_tags ADD COLUMN unit_count INTEGER DEFAULT 1"),
        # Direct worker attribution on transactions (complements performed_by string)
        (14, "ALTER TABLE transactions ADD COLUMN worker_id TEXT"),
    ]
    for version, sql in _MIGRATIONS:
        c.execute('SELECT 1 FROM schema_version WHERE version = ?', (version,))
        if c.fetchone():
            continue
        try:
            c.execute(sql)
        except Exception as e:
            if 'duplicate column' not in str(e).lower():
                print(f'[DB] Migration {version} note: {e}')
        c.execute('INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)',
                  (version, sql[:80]))

    # ── Seed demo items on fresh database ────────────────────────────────────
    c.execute('SELECT COUNT(*) FROM items')
    if c.fetchone()[0] == 0:
        demo_items = [
            ('item-001', 'USB Cable Type-C',    15, 'pcs',   5),
            ('item-002', 'HDMI Cable',            8, 'pcs',   3),
            ('item-003', 'AA Batteries (pack)',   4, 'packs', 5),
            ('item-004', 'Ethernet Cable 2m',    12, 'pcs',   4),
            ('item-005', 'Mouse Pad',             3, 'pcs',   5),
            ('item-006', 'RFID Reader Module',    6, 'pcs',   2),
            ('item-007', 'ESP32 Dev Board',       2, 'pcs',   3),
            ('item-008', 'Jumper Wires (set)',   20, 'sets',  5),
        ]
        c.executemany(
            'INSERT INTO items (id, name, quantity, unit, low_stock_threshold) VALUES (?, ?, ?, ?, ?)',
            demo_items
        )

    # ── Seed default users on fresh database ─────────────────────────────────
    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        from werkzeug.security import generate_password_hash as _h
        c.executemany('INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)', [
            ('admin',   _h('admin123'),   'admin'),
            ('manager', _h('manager123'), 'manager'),
            ('viewer',  _h('viewer123'),  'viewer'),
        ])
        print('[DB] Default users: admin/admin123  manager/manager123  viewer/viewer123')

    # ── Ensure demo accounts exist on upgraded databases ─────────────────────
    for username, plain, role in [('manager', 'manager123', 'manager'),
                                   ('viewer',  'viewer123',  'viewer')]:
        c.execute('SELECT id FROM users WHERE username = ?', (username,))
        if not c.fetchone():
            c.execute('INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                      (username, generate_password_hash(plain), role))

    # ── Seed demo workers ────────────────────────────────────────────────────
    c.execute('SELECT COUNT(*) FROM workers')
    if c.fetchone()[0] == 0:
        demo_workers = [
            ('EMP-001', 'Alice Tan',   'supervisor', 'warehouse'),
            ('EMP-002', 'Bob Lim',     'operator',   'warehouse'),
            ('EMP-003', 'Carol Wong',  'operator',   'factory'),
            ('EMP-004', 'David Ng',    'operator',   'factory'),
        ]
        c.executemany(
            'INSERT INTO workers (employee_id, name, role, zone) VALUES (?, ?, ?, ?)',
            demo_workers
        )
        print('[DB] Demo workers seeded — write EMP-001..EMP-004 to RFID badges via tag_writer')

    conn.commit()
    conn.close()
