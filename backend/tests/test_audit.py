"""Tests for audit trail (/api/audit) and transactions (/api/transactions)."""
import pytest
from database import get_db


def _seed_txn(item_id='item-001', action='manual_adjust', device_id='dashboard',
              qty_change=0):
    conn = get_db()
    conn.execute(
        '''INSERT INTO transactions (item_id, action, quantity_change, previous_quantity,
           new_quantity, performed_by, device_id) VALUES (?, ?, ?, 10, 10, 'test', ?)''',
        (item_id, action, qty_change, device_id),
    )
    conn.commit()
    conn.close()


class TestGetTransactions:
    def test_returns_200(self, viewer_client):
        r = viewer_client.get('/api/transactions')
        assert r.status_code == 200

    def test_returns_list(self, viewer_client):
        r = viewer_client.get('/api/transactions')
        assert isinstance(r.get_json(), list)

    def test_returns_seeded_transaction(self, viewer_client, test_db):
        _seed_txn(action='item_added')
        txns = viewer_client.get('/api/transactions').get_json()
        assert any(t['action'] == 'item_added' for t in txns)

    def test_includes_item_name(self, viewer_client, test_db):
        _seed_txn(item_id='item-001', action='manual_adjust')
        txns = viewer_client.get('/api/transactions').get_json()
        t = next((t for t in txns if t['action'] == 'manual_adjust'), None)
        assert t is not None
        assert t['item_name'] is not None

    def test_ordered_newest_first(self, viewer_client, test_db):
        _seed_txn(action='scan_in')
        _seed_txn(action='scan_out')
        txns = viewer_client.get('/api/transactions').get_json()
        ids = [t['id'] for t in txns]
        assert ids == sorted(ids, reverse=True)

    def test_default_limit_is_50(self, viewer_client, test_db):
        for _ in range(60):
            _seed_txn()
        txns = viewer_client.get('/api/transactions').get_json()
        assert len(txns) <= 50

    def test_custom_limit_param(self, viewer_client, test_db):
        for _ in range(10):
            _seed_txn()
        txns = viewer_client.get('/api/transactions?limit=5').get_json()
        assert len(txns) <= 5

    def test_unauthenticated_returns_401(self, client):
        r = client.get('/api/transactions')
        assert r.status_code == 401


class TestGetAudit:
    def test_returns_200(self, viewer_client):
        r = viewer_client.get('/api/audit')
        assert r.status_code == 200

    def test_returns_list(self, viewer_client):
        assert isinstance(viewer_client.get('/api/audit').get_json(), list)

    def test_filter_dashboard(self, viewer_client, test_db):
        _seed_txn(action='manual_adjust', device_id='dashboard')
        _seed_txn(action='scan_in', device_id='device-001')
        txns = viewer_client.get('/api/audit?filter=dashboard').get_json()
        assert all(t['device_id'] == 'dashboard' for t in txns)

    def test_filter_physical(self, viewer_client, test_db):
        _seed_txn(action='scan_in', device_id='device-001')
        txns = viewer_client.get('/api/audit?filter=physical').get_json()
        assert all(t['device_id'] not in ('dashboard', 'system') for t in txns)

    def test_filter_admin(self, viewer_client, test_db):
        _seed_txn(action='item_added', device_id='dashboard')
        _seed_txn(action='scan_in', device_id='device-001')
        txns = viewer_client.get('/api/audit?filter=admin').get_json()
        admin_actions = {'item_added', 'item_deleted', 'tag_removed',
                         'return_requested', 'manual_adjust', 'tag_reassigned'}
        assert all(t['action'] in admin_actions for t in txns)

    def test_filter_all_returns_all(self, viewer_client, test_db):
        _seed_txn(action='scan_in', device_id='device-001')
        _seed_txn(action='manual_adjust', device_id='dashboard')
        txns = viewer_client.get('/api/audit?filter=all').get_json()
        assert len(txns) >= 2

    def test_default_limit_is_100(self, viewer_client, test_db):
        for _ in range(110):
            _seed_txn()
        txns = viewer_client.get('/api/audit').get_json()
        assert len(txns) <= 100

    def test_custom_limit_param(self, viewer_client, test_db):
        for _ in range(20):
            _seed_txn()
        txns = viewer_client.get('/api/audit?limit=5').get_json()
        assert len(txns) <= 5

    def test_unauthenticated_returns_401(self, client):
        r = client.get('/api/audit')
        assert r.status_code == 401

    def test_includes_item_name(self, viewer_client, test_db):
        _seed_txn(item_id='item-001', action='scan_in')
        txns = viewer_client.get('/api/audit').get_json()
        t = next((t for t in txns if t['item_id'] == 'item-001'), None)
        assert t is not None
        assert t['item_name'] is not None


class TestTransactionCreatedByItem:
    """Ensure item CRUD actions create correct audit entries."""

    def test_add_item_creates_item_added_txn(self, admin_client):
        admin_client.post('/api/items', json={'id': 'audit-item', 'name': 'Audit Widget', 'quantity': 3})
        txns = admin_client.get('/api/transactions').get_json()
        assert any(t['action'] == 'item_added' and t['item_id'] == 'audit-item' for t in txns)

    def test_update_qty_creates_manual_adjust_txn(self, admin_client):
        admin_client.put('/api/items/item-001', json={'quantity': 77})
        txns = admin_client.get('/api/transactions').get_json()
        assert any(t['action'] == 'manual_adjust' and t['item_id'] == 'item-001' for t in txns)

    def test_delete_item_creates_item_deleted_txn(self, admin_client):
        admin_client.delete('/api/items/item-001')
        txns = admin_client.get('/api/transactions').get_json()
        assert any(t['action'] == 'item_deleted' and t['item_id'] == 'item-001' for t in txns)
