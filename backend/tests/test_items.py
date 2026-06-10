"""Tests for item CRUD, available_qty, reservation, and quantity guards."""
import pytest
from database import get_db


class TestGetItems:
    def test_returns_seeded_items(self, viewer_client):
        r = viewer_client.get('/api/items')
        assert r.status_code == 200
        items = r.get_json()
        assert len(items) == 8

    def test_includes_available_qty(self, viewer_client):
        r = viewer_client.get('/api/items')
        item = r.get_json()[0]
        assert 'available_qty' in item

    def test_available_qty_equals_qty_when_no_reservation(self, viewer_client):
        items = viewer_client.get('/api/items').get_json()
        for item in items:
            assert item['available_qty'] == item['quantity'] - item['reserved_qty']

    def test_returns_empty_list_when_no_items(self, admin_client, test_db):
        conn = get_db()
        conn.execute('DELETE FROM items')
        conn.commit()
        conn.close()
        r = admin_client.get('/api/items')
        assert r.get_json() == []


class TestAddItem:
    def test_admin_can_create_item(self, admin_client):
        r = admin_client.post('/api/items', json={
            'id': 'item-new', 'name': 'New Widget', 'quantity': 10,
        })
        assert r.status_code == 201

    def test_manager_can_create_item(self, manager_client):
        r = manager_client.post('/api/items', json={
            'id': 'item-mgr', 'name': 'Manager Widget',
        })
        assert r.status_code == 201

    def test_viewer_cannot_create_item(self, viewer_client):
        r = viewer_client.post('/api/items', json={'id': 'x', 'name': 'X'})
        assert r.status_code == 403

    def test_missing_id_returns_400(self, admin_client):
        r = admin_client.post('/api/items', json={'name': 'No ID'})
        assert r.status_code == 400

    def test_missing_name_returns_400(self, admin_client):
        r = admin_client.post('/api/items', json={'id': 'no-name'})
        assert r.status_code == 400

    def test_duplicate_id_returns_400(self, admin_client):
        admin_client.post('/api/items', json={'id': 'dup-item', 'name': 'First'})
        r = admin_client.post('/api/items', json={'id': 'dup-item', 'name': 'Second'})
        assert r.status_code == 400

    def test_negative_quantity_clamped_to_zero(self, admin_client):
        admin_client.post('/api/items', json={'id': 'neg-item', 'name': 'Neg', 'quantity': -5})
        items = admin_client.get('/api/items').get_json()
        item = next(i for i in items if i['id'] == 'neg-item')
        assert item['quantity'] == 0

    def test_default_unit_is_pcs(self, admin_client):
        admin_client.post('/api/items', json={'id': 'unit-item', 'name': 'Unit Test'})
        items = admin_client.get('/api/items').get_json()
        item = next(i for i in items if i['id'] == 'unit-item')
        assert item['unit'] == 'pcs'

    def test_custom_unit_stored(self, admin_client):
        admin_client.post('/api/items', json={'id': 'set-item', 'name': 'Sets', 'unit': 'sets'})
        items = admin_client.get('/api/items').get_json()
        item = next(i for i in items if i['id'] == 'set-item')
        assert item['unit'] == 'sets'

    def test_creates_item_added_transaction(self, admin_client):
        admin_client.post('/api/items', json={'id': 'txn-item', 'name': 'TXN Widget', 'quantity': 5})
        r = admin_client.get('/api/transactions')
        txns = r.get_json()
        assert any(t['action'] == 'item_added' and t['item_id'] == 'txn-item' for t in txns)


class TestUpdateItem:
    def test_viewer_can_update_quantity(self, viewer_client):
        r = viewer_client.put('/api/items/item-001', json={'quantity': 20})
        assert r.status_code == 200

    def test_quantity_updated(self, admin_client):
        admin_client.put('/api/items/item-001', json={'quantity': 99})
        items = admin_client.get('/api/items').get_json()
        item = next(i for i in items if i['id'] == 'item-001')
        assert item['quantity'] == 99

    def test_negative_quantity_returns_400(self, admin_client):
        r = admin_client.put('/api/items/item-001', json={'quantity': -1})
        assert r.status_code == 400

    def test_name_updated(self, admin_client):
        admin_client.put('/api/items/item-001', json={'name': 'Renamed Widget'})
        items = admin_client.get('/api/items').get_json()
        item = next(i for i in items if i['id'] == 'item-001')
        assert item['name'] == 'Renamed Widget'

    def test_threshold_updated(self, admin_client):
        admin_client.put('/api/items/item-001', json={'low_stock_threshold': 15})
        items = admin_client.get('/api/items').get_json()
        item = next(i for i in items if i['id'] == 'item-001')
        assert item['low_stock_threshold'] == 15

    def test_manual_adjust_transaction_created(self, admin_client):
        admin_client.put('/api/items/item-001', json={'quantity': 50})
        txns = admin_client.get('/api/transactions').get_json()
        assert any(t['action'] == 'manual_adjust' and t['item_id'] == 'item-001' for t in txns)


class TestDeleteItem:
    def test_admin_can_delete_item(self, admin_client):
        r = admin_client.delete('/api/items/item-001')
        assert r.status_code == 200
        items = admin_client.get('/api/items').get_json()
        assert not any(i['id'] == 'item-001' for i in items)

    def test_manager_cannot_delete_item(self, manager_client):
        r = manager_client.delete('/api/items/item-001')
        assert r.status_code == 403

    def test_viewer_cannot_delete_item(self, viewer_client):
        r = viewer_client.delete('/api/items/item-001')
        assert r.status_code == 403

    def test_delete_also_removes_tags(self, admin_client, test_db):
        conn = get_db()
        conn.execute("INSERT INTO rfid_tags (uid, item_id, state) VALUES ('T01', 'item-001', 'racked')")
        conn.commit()
        conn.close()
        admin_client.delete('/api/items/item-001')
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM rfid_tags WHERE item_id = 'item-001'")
        assert c.fetchone()[0] == 0
        conn.close()

    def test_delete_creates_item_deleted_transaction(self, admin_client):
        admin_client.delete('/api/items/item-001')
        txns = admin_client.get('/api/transactions').get_json()
        assert any(t['action'] == 'item_deleted' and t['item_id'] == 'item-001' for t in txns)


class TestReservation:
    def test_reserve_reduces_available_qty(self, manager_client):
        items = manager_client.get('/api/items').get_json()
        item = next(i for i in items if i['quantity'] >= 3)
        item_id = item['id']
        initial_avail = item['available_qty']
        r = manager_client.post(f'/api/items/{item_id}/reserve', json={'qty': 2})
        assert r.status_code == 200
        items = manager_client.get('/api/items').get_json()
        updated = next(i for i in items if i['id'] == item_id)
        assert updated['available_qty'] == initial_avail - 2

    def test_viewer_cannot_reserve(self, viewer_client):
        r = viewer_client.post('/api/items/item-001/reserve', json={'qty': 1})
        assert r.status_code == 403

    def test_reserve_more_than_available_returns_400(self, manager_client):
        # item-003 has qty=4, threshold=5 — quantity is small
        items = manager_client.get('/api/items').get_json()
        item = items[0]
        r = manager_client.post(f'/api/items/{item["id"]}/reserve',
                                json={'qty': item['quantity'] + 999})
        assert r.status_code == 400

    def test_unreserve_increases_available_qty(self, manager_client):
        items = manager_client.get('/api/items').get_json()
        item = next(i for i in items if i['quantity'] >= 5)
        item_id = item['id']
        manager_client.post(f'/api/items/{item_id}/reserve', json={'qty': 3})
        r = manager_client.delete(f'/api/items/{item_id}/reserve', json={'qty': 3})
        assert r.status_code == 200
        items = manager_client.get('/api/items').get_json()
        updated = next(i for i in items if i['id'] == item_id)
        assert updated['reserved_qty'] == 0

    def test_unreserve_cannot_go_below_zero(self, manager_client):
        items = manager_client.get('/api/items').get_json()
        item_id = items[0]['id']
        # Unreserve 100 when none reserved — should clamp to 0, not error
        r = manager_client.delete(f'/api/items/{item_id}/reserve', json={'qty': 100})
        assert r.status_code == 200
        items = manager_client.get('/api/items').get_json()
        updated = next(i for i in items if i['id'] == item_id)
        assert updated['reserved_qty'] == 0

    def test_nonexistent_item_returns_404(self, manager_client):
        r = manager_client.post('/api/items/item-NONE/reserve', json={'qty': 1})
        assert r.status_code == 404
