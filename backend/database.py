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
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_scan     TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT DEFAULT 'viewer',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS workers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL DEFAULT 'Unknown',
            uid         TEXT UNIQUE,
            role        TEXT DEFAULT 'operator',
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
    ''')

    # ── Schema migrations (safe to run on existing databases) ────────────────
    migrations = [
        "ALTER TABLE rfid_tags ADD COLUMN last_scan TIMESTAMP",
        "ALTER TABLE rfid_tags ADD COLUMN rack_location TEXT",
        "ALTER TABLE transactions ADD COLUMN performed_by TEXT DEFAULT 'system'",
        "ALTER TABLE transactions ADD COLUMN note TEXT",
        # Accountability layer
        "ALTER TABLE transactions ADD COLUMN device_id TEXT DEFAULT 'dashboard'",
        "ALTER TABLE users ADD COLUMN badge_uid TEXT",
        "ALTER TABLE users ADD COLUMN employee_id TEXT",
        "ALTER TABLE workers ADD COLUMN zone TEXT DEFAULT 'general'",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except Exception:
            pass  # column already exists

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

    # ── Seed default admin and manager on fresh database ────────────────────
    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        from werkzeug.security import generate_password_hash as _h
        c.executemany('INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)', [
            ('admin',   _h('admin123'),   'admin'),
            ('manager', _h('manager123'), 'manager'),
            ('viewer',  _h('viewer123'),  'viewer'),
        ])
        print('[DB] Default users: admin/admin123  manager/manager123  viewer/viewer123')

    # ── Ensure manager + viewer demo accounts exist ──────────────────────────
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
            ('EMP-001', 'Alice Tan',   'supervisor'),
            ('EMP-002', 'Bob Lim',     'operator'),
            ('EMP-003', 'Carol Wong',  'operator'),
            ('EMP-004', 'David Ng',    'operator'),
        ]
        c.executemany(
            'INSERT INTO workers (employee_id, name, role) VALUES (?, ?, ?)',
            demo_workers
        )
        print('[DB] Demo workers seeded — write EMP-001..EMP-004 to RFID badges via tag_writer')

    conn.commit()
    conn.close()
