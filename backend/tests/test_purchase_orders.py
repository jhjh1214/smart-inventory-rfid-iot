"""Tests for purchase order CRUD, status filtering, and auto-fulfil on receive."""
import pytest
from database import get_db


def _create_po(client, item_id='item-001', expected_qty=10, note=''):
    return client.post('/api/purchase-orders', json={
        'item_id': item_id, 'expected_qty': expected_qty, 'note': note,
    })


class TestGetPurchaseOrders:
    def test_returns_200(self, viewer_client):
        r = viewer_client.get('/api/purchase-orders')
        assert r.status_code == 200

    def test_returns_empty_list_initially(self, viewer_client, test_db):
        conn = get_db()
        conn.execute('DELETE FROM purchase_orders')
        conn.commit()
        conn.close()
        assert viewer_client.get('/api/purchase-orders').get_json() == []

    def test_returns_created_po(self, manager_client):
        _create_po(manager_client)
        pos = manager_client.get('/api/purchase-orders').get_json()
        assert len(pos) >= 1

    def test_includes_item_name(self, manager_client):
        _create_po(manager_client, 'item-001')
        pos = manager_client.get('/api/purchase-orders').get_json()
        assert pos[0]['item_name'] is not None

    def test_status_filter_open(self, manager_client, test_db):
        conn = get_db()
        conn.execute('DELETE FROM purchase_orders')
        conn.commit()
        conn.close()
        _create_po(manager_client)
        pos = manager_client.get('/api/purchase-orders?status=open').get_json()
        assert all(p['status'] == 'open' for p in pos)

    def test_status_filter_returns_only_matching(self, manager_client, test_db):
        conn = get_db()
        conn.execute('DELETE FROM purchase_orders')
        conn.commit()
        conn.close()
        r = _create_po(manager_client)
        po_id = r.get_json()['id']
        manager_client.put(f'/api/purchase-orders/{po_id}', json={'status': 'complete'})
        pos = manager_client.get('/api/purchase-orders?status=open').get_json()
        assert all(p['status'] == 'open' for p in pos)

    def test_unauthenticated_returns_401(self, client):
        r = client.get('/api/purchase-orders')
        assert r.status_code == 401


class TestCreatePurchaseOrder:
    def test_manager_can_create(self, manager_client):
        r = _create_po(manager_client)
        assert r.status_code == 201

    def test_admin_can_create(self, admin_client):
        r = _create_po(admin_client)
        assert r.status_code == 201

    def test_viewer_cannot_create(self, viewer_client):
        r = _create_po(viewer_client)
        assert r.status_code == 403

    def test_missing_item_id_returns_400(self, manager_client):
        r = manager_client.post('/api/purchase-orders', json={'expected_qty': 5})
        assert r.status_code == 400

    def test_zero_quantity_returns_400(self, manager_client):
        r = manager_client.post('/api/purchase-orders', json={'item_id': 'item-001', 'expected_qty': 0})
        assert r.status_code == 400

    def test_nonexistent_item_returns_404(self, manager_client):
        r = manager_client.post('/api/purchase-orders', json={'item_id': 'no-such-item', 'expected_qty': 5})
        assert r.status_code == 404

    def test_default_status_is_open(self, manager_client):
        _create_po(manager_client)
        pos = manager_client.get('/api/purchase-orders').get_json()
        assert pos[0]['status'] == 'open'

    def test_returns_id_in_response(self, manager_client):
        r = _create_po(manager_client)
        assert 'id' in r.get_json()

    def test_note_stored(self, manager_client):
        _create_po(manager_client, note='Urgent order')
        pos = manager_client.get('/api/purchase-orders').get_json()
        assert pos[0]['note'] == 'Urgent order'


class TestUpdatePurchaseOrder:
    def _po_id(self, manager_client):
        _create_po(manager_client)
        return manager_client.get('/api/purchase-orders').get_json()[0]['id']

    def test_manager_can_update_received_qty(self, manager_client):
        pid = self._po_id(manager_client)
        r = manager_client.put(f'/api/purchase-orders/{pid}', json={'received_qty': 3})
        assert r.status_code == 200

    def test_partial_delivery_sets_status_partial(self, manager_client):
        r = _create_po(manager_client, expected_qty=10)
        pid = r.get_json()['id']
        manager_client.put(f'/api/purchase-orders/{pid}', json={'received_qty': 5})
        pos = manager_client.get('/api/purchase-orders').get_json()
        po = next(p for p in pos if p['id'] == pid)
        assert po['status'] == 'partial'

    def test_full_delivery_sets_status_complete(self, manager_client):
        r = _create_po(manager_client, expected_qty=5)
        pid = r.get_json()['id']
        manager_client.put(f'/api/purchase-orders/{pid}', json={'received_qty': 5})
        pos = manager_client.get('/api/purchase-orders').get_json()
        po = next(p for p in pos if p['id'] == pid)
        assert po['status'] == 'complete'

    def test_over_delivery_also_completes(self, manager_client):
        r = _create_po(manager_client, expected_qty=5)
        pid = r.get_json()['id']
        manager_client.put(f'/api/purchase-orders/{pid}', json={'received_qty': 10})
        pos = manager_client.get('/api/purchase-orders').get_json()
        po = next(p for p in pos if p['id'] == pid)
        assert po['status'] == 'complete'

    def test_viewer_cannot_update(self, viewer_client, manager_client):
        r = _create_po(manager_client, expected_qty=10)
        pid = r.get_json()['id']
        r2 = viewer_client.put(f'/api/purchase-orders/{pid}', json={'received_qty': 3})
        assert r2.status_code == 403

    def test_empty_update_returns_400(self, manager_client):
        r = _create_po(manager_client)
        pid = r.get_json()['id']
        r2 = manager_client.put(f'/api/purchase-orders/{pid}', json={})
        assert r2.status_code == 400

    def test_nonexistent_po_returns_404(self, manager_client):
        r = manager_client.put('/api/purchase-orders/99999', json={'received_qty': 1})
        assert r.status_code == 404

    def test_update_note(self, manager_client):
        r = _create_po(manager_client)
        pid = r.get_json()['id']
        manager_client.put(f'/api/purchase-orders/{pid}', json={'note': 'Updated note'})
        pos = manager_client.get('/api/purchase-orders').get_json()
        po = next(p for p in pos if p['id'] == pid)
        assert po['note'] == 'Updated note'


class TestDeletePurchaseOrder:
    def test_manager_can_delete(self, manager_client):
        r = _create_po(manager_client)
        pid = r.get_json()['id']
        r2 = manager_client.delete(f'/api/purchase-orders/{pid}')
        assert r2.status_code == 200
        pos = manager_client.get('/api/purchase-orders').get_json()
        assert not any(p['id'] == pid for p in pos)

    def test_viewer_cannot_delete(self, viewer_client, manager_client):
        r = _create_po(manager_client)
        pid = r.get_json()['id']
        r2 = viewer_client.delete(f'/api/purchase-orders/{pid}')
        assert r2.status_code == 403

    def test_delete_nonexistent_returns_200(self, manager_client):
        r = manager_client.delete('/api/purchase-orders/99999')
        assert r.status_code == 200


class TestPurchaseOrderAutoFulfil:
    """PO received_qty auto-increments when warehouse receives matching item."""

    def test_warehouse_receive_increments_po_received_qty(self, manager_client, test_db):
        import mqtt_subscriber
        r = _create_po(manager_client, item_id='item-001', expected_qty=5)
        pid = r.get_json()['id']

        # Simulate warehouse receive via handler
        conn = get_db()
        conn.execute(
            "INSERT INTO rfid_tags (uid, item_id, state) VALUES ('PO-TAG-01', 'item-001', 'in_transit')"
        )
        conn.commit()
        conn.close()

        class MockClient:
            def publish(self, *a, **kw): pass

        from unittest.mock import patch
        with patch('app._fire_webhooks'):
            mqtt_subscriber._handle_warehouse_gate(MockClient(), {
                'tag_uid': 'PO-TAG-01', 'device_id': 'gate-01',
            })

        pos = manager_client.get('/api/purchase-orders').get_json()
        po = next(p for p in pos if p['id'] == pid)
        assert po['received_qty'] == 1

    def test_po_completes_when_fully_received(self, manager_client, test_db):
        import mqtt_subscriber
        r = _create_po(manager_client, item_id='item-001', expected_qty=2)
        pid = r.get_json()['id']

        class MockClient:
            def publish(self, *a, **kw): pass

        for i in range(2):
            conn = get_db()
            conn.execute(
                f"INSERT INTO rfid_tags (uid, item_id, state) VALUES ('PO-FULL-{i}', 'item-001', 'in_transit')"
            )
            conn.commit()
            conn.close()
            from unittest.mock import patch
            with patch('app._fire_webhooks'):
                mqtt_subscriber._handle_warehouse_gate(MockClient(), {
                    'tag_uid': f'PO-FULL-{i}', 'device_id': 'gate-01',
                })

        pos = manager_client.get('/api/purchase-orders').get_json()
        po = next(p for p in pos if p['id'] == pid)
        assert po['status'] == 'complete'
