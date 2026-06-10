"""Tests for database.py — schema creation, migrations, seeding, helpers."""
import pytest
from database import get_db, init_db


class TestSchema:
    EXPECTED_TABLES = {
        'items', 'transactions', 'alerts', 'rfid_tags',
        'users', 'workers', 'write_jobs', 'worker_sessions',
        'schema_version', 'webhooks', 'purchase_orders',
    }

    def test_all_tables_created(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in c.fetchall()}
        conn.close()
        assert self.EXPECTED_TABLES.issubset(tables)

    def test_items_columns(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA table_info(items)')
        cols = {r['name'] for r in c.fetchall()}
        conn.close()
        assert {'id', 'name', 'quantity', 'reserved_qty', 'unit',
                'low_stock_threshold', 'created_at', 'updated_at'}.issubset(cols)

    def test_transactions_columns(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA table_info(transactions)')
        cols = {r['name'] for r in c.fetchall()}
        conn.close()
        assert {'id', 'item_id', 'action', 'quantity_change', 'previous_quantity',
                'new_quantity', 'tag_uid', 'performed_by', 'note', 'device_id',
                'timestamp'}.issubset(cols)

    def test_rfid_tags_columns(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA table_info(rfid_tags)')
        cols = {r['name'] for r in c.fetchall()}
        conn.close()
        assert {'uid', 'item_id', 'state', 'rack_location',
                'previous_uid', 'last_scan', 'registered_at'}.issubset(cols)

    def test_users_columns(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA table_info(users)')
        cols = {r['name'] for r in c.fetchall()}
        conn.close()
        assert {'id', 'username', 'password_hash', 'role',
                'badge_uid', 'employee_id', 'created_at'}.issubset(cols)

    def test_workers_columns(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA table_info(workers)')
        cols = {r['name'] for r in c.fetchall()}
        conn.close()
        assert {'id', 'employee_id', 'name', 'uid', 'role',
                'zone', 'active', 'last_seen', 'created_at'}.issubset(cols)

    def test_worker_sessions_columns(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA table_info(worker_sessions)')
        cols = {r['name'] for r in c.fetchall()}
        conn.close()
        assert {'device_id', 'employee_id', 'name', 'role', 'zone', 'expires_at'}.issubset(cols)

    def test_purchase_orders_columns(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA table_info(purchase_orders)')
        cols = {r['name'] for r in c.fetchall()}
        conn.close()
        assert {'id', 'item_id', 'expected_qty', 'received_qty', 'status',
                'note', 'created_by', 'created_at'}.issubset(cols)

    def test_webhooks_columns(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA table_info(webhooks)')
        cols = {r['name'] for r in c.fetchall()}
        conn.close()
        assert {'id', 'name', 'url', 'events', 'active', 'created_at'}.issubset(cols)

    def test_foreign_keys_enabled(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA foreign_keys')
        result = c.fetchone()[0]
        conn.close()
        assert result == 1

    def test_row_factory_enables_column_access(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id, name FROM items LIMIT 1')
        row = c.fetchone()
        conn.close()
        assert row['name'] is not None


class TestIdempotency:
    def test_init_db_twice_does_not_duplicate_items(self, test_db):
        init_db()
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM items')
        count = c.fetchone()[0]
        conn.close()
        assert count == 8

    def test_init_db_twice_does_not_duplicate_users(self, test_db):
        init_db()
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM users')
        count = c.fetchone()[0]
        conn.close()
        assert count == 3

    def test_migrations_tracked_in_schema_version(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM schema_version')
        count = c.fetchone()[0]
        conn.close()
        assert count == 10

    def test_migrations_not_rerun(self, test_db):
        init_db()
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM schema_version')
        count = c.fetchone()[0]
        conn.close()
        assert count == 10  # still 10, not doubled


class TestSeeding:
    def test_eight_demo_items(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM items')
        assert c.fetchone()[0] == 8
        conn.close()

    def test_demo_users_roles(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT username, role FROM users ORDER BY username')
        users = {r['username']: r['role'] for r in c.fetchall()}
        conn.close()
        assert users == {'admin': 'admin', 'manager': 'manager', 'viewer': 'viewer'}

    def test_four_demo_workers(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT employee_id FROM workers ORDER BY employee_id')
        ids = [r['employee_id'] for r in c.fetchall()]
        conn.close()
        assert ids == ['EMP-001', 'EMP-002', 'EMP-003', 'EMP-004']

    def test_item_reserved_qty_defaults_to_zero(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT reserved_qty FROM items LIMIT 1')
        row = c.fetchone()
        conn.close()
        assert row['reserved_qty'] == 0

    def test_worker_active_by_default(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT active FROM workers LIMIT 1')
        row = c.fetchone()
        conn.close()
        assert row['active'] == 1

    def test_passwords_are_hashed(self, test_db):
        from werkzeug.security import check_password_hash
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT password_hash FROM users WHERE username = 'admin'")
        row = c.fetchone()
        conn.close()
        assert check_password_hash(row['password_hash'], 'admin123')
        assert row['password_hash'] != 'admin123'
