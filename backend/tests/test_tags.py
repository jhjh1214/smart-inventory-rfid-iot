"""Tests for RFID tag endpoints: CRUD, return, reassignment."""
import pytest
from database import get_db


def _seed_tag(test_db, uid, item_id, state='out', rack_location=None):
    conn = get_db()
    conn.execute(
        'INSERT INTO rfid_tags (uid, item_id, state, rack_location) VALUES (?, ?, ?, ?)',
        (uid, item_id, state, rack_location),
    )
    conn.commit()
    conn.close()


class TestGetTags:
    def test_returns_empty_list_when_no_tags(self, viewer_client):
        r = viewer_client.get('/api/tags')
        assert r.status_code == 200
        assert r.get_json() == []

    def test_returns_registered_tags(self, viewer_client, test_db):
        _seed_tag(test_db, 'AABBCCDD', 'item-001', 'tagged')
        tags = viewer_client.get('/api/tags').get_json()
        assert len(tags) == 1
        assert tags[0]['uid'] == 'AABBCCDD'
        assert tags[0]['state'] == 'tagged'

    def test_includes_item_name(self, viewer_client, test_db):
        _seed_tag(test_db, 'T01', 'item-001', 'racked')
        tags = viewer_client.get('/api/tags').get_json()
        assert tags[0]['item_name'] is not None

    def test_unauthenticated_returns_401(self, client):
        r = client.get('/api/tags')
        assert r.status_code == 401


class TestRegisterTag:
    def test_registers_tag(self, viewer_client):
        r = viewer_client.post('/api/tags', json={
            'uid': 'NEW001', 'item_id': 'item-001', 'state': 'out',
        })
        assert r.status_code == 201

    def test_upserts_existing_tag(self, viewer_client, test_db):
        _seed_tag(test_db, 'T01', 'item-001', 'out')
        viewer_client.post('/api/tags', json={'uid': 'T01', 'item_id': 'item-001', 'state': 'in'})
        tags = viewer_client.get('/api/tags').get_json()
        t = next(t for t in tags if t['uid'] == 'T01')
        assert t['state'] == 'in'

    def test_default_state_is_out(self, viewer_client):
        viewer_client.post('/api/tags', json={'uid': 'DEFAULT01', 'item_id': 'item-001'})
        tags = viewer_client.get('/api/tags').get_json()
        t = next(t for t in tags if t['uid'] == 'DEFAULT01')
        assert t['state'] == 'out'


class TestDeleteTag:
    def test_admin_can_delete_tag(self, admin_client, test_db):
        _seed_tag(test_db, 'DEL01', 'item-001', 'out')
        r = admin_client.delete('/api/tags/DEL01')
        assert r.status_code == 200
        tags = admin_client.get('/api/tags').get_json()
        assert not any(t['uid'] == 'DEL01' for t in tags)

    def test_manager_cannot_delete_tag(self, manager_client, test_db):
        _seed_tag(test_db, 'DEL01', 'item-001', 'out')
        r = manager_client.delete('/api/tags/DEL01')
        assert r.status_code == 403

    def test_viewer_cannot_delete_tag(self, viewer_client, test_db):
        _seed_tag(test_db, 'DEL01', 'item-001', 'out')
        r = viewer_client.delete('/api/tags/DEL01')
        assert r.status_code == 403

    def test_delete_creates_tag_removed_transaction(self, admin_client, test_db):
        _seed_tag(test_db, 'DEL01', 'item-001', 'racked')
        admin_client.delete('/api/tags/DEL01')
        txns = admin_client.get('/api/transactions').get_json()
        assert any(t['action'] == 'tag_removed' and t['tag_uid'] == 'DEL01' for t in txns)

    def test_delete_nonexistent_tag_returns_200(self, admin_client):
        r = admin_client.delete('/api/tags/GHOST01')
        assert r.status_code == 200


class TestReturnTag:
    def test_dispatched_tag_can_be_returned(self, admin_client, test_db):
        _seed_tag(test_db, 'RET01', 'item-001', 'dispatched')
        r = admin_client.post('/api/tags/RET01/return', json={'note': 'Test return'})
        assert r.status_code == 200
        tags = admin_client.get('/api/tags').get_json()
        t = next(t for t in tags if t['uid'] == 'RET01')
        assert t['state'] == 'return_pending'

    def test_consumed_tag_can_be_returned(self, admin_client, test_db):
        _seed_tag(test_db, 'RET02', 'item-001', 'consumed')
        r = admin_client.post('/api/tags/RET02/return', json={})
        assert r.status_code == 200

    def test_tagged_state_cannot_be_returned(self, admin_client, test_db):
        _seed_tag(test_db, 'RET03', 'item-001', 'tagged')
        r = admin_client.post('/api/tags/RET03/return', json={})
        assert r.status_code == 400

    def test_racked_state_cannot_be_returned(self, admin_client, test_db):
        _seed_tag(test_db, 'RET04', 'item-001', 'racked', rack_location='A1')
        r = admin_client.post('/api/tags/RET04/return', json={})
        assert r.status_code == 400

    def test_return_nonexistent_tag_returns_404(self, admin_client):
        r = admin_client.post('/api/tags/GHOST/return', json={})
        assert r.status_code == 404

    def test_manager_cannot_return_tag(self, manager_client, test_db):
        _seed_tag(test_db, 'RET05', 'item-001', 'dispatched')
        r = manager_client.post('/api/tags/RET05/return', json={})
        assert r.status_code == 403

    def test_return_creates_return_requested_transaction(self, admin_client, test_db):
        _seed_tag(test_db, 'RET06', 'item-001', 'dispatched')
        admin_client.post('/api/tags/RET06/return', json={'note': 'customer return'})
        txns = admin_client.get('/api/transactions').get_json()
        assert any(t['action'] == 'return_requested' and t['tag_uid'] == 'RET06' for t in txns)


class TestReassignTag:
    def test_reassign_creates_new_uid(self, admin_client, test_db):
        _seed_tag(test_db, 'OLD01', 'item-001', 'racked', rack_location='A1')
        r = admin_client.post('/api/tags/OLD01/reassign', json={'new_uid': 'NEW01'})
        assert r.status_code == 200
        tags = admin_client.get('/api/tags').get_json()
        uids = [t['uid'] for t in tags]
        assert 'NEW01' in uids
        assert 'OLD01' not in uids

    def test_new_uid_preserves_state(self, admin_client, test_db):
        _seed_tag(test_db, 'OLD02', 'item-001', 'racked', rack_location='B2')
        admin_client.post('/api/tags/OLD02/reassign', json={'new_uid': 'NEW02'})
        tags = admin_client.get('/api/tags').get_json()
        t = next(t for t in tags if t['uid'] == 'NEW02')
        assert t['state'] == 'racked'
        assert t['rack_location'] == 'B2'

    def test_new_uid_stores_previous_uid(self, admin_client, test_db):
        _seed_tag(test_db, 'OLD03', 'item-001', 'in_transit')
        admin_client.post('/api/tags/OLD03/reassign', json={'new_uid': 'NEW03'})
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT previous_uid FROM rfid_tags WHERE uid = ?', ('NEW03',))
        row = c.fetchone()
        conn.close()
        assert row['previous_uid'] == 'OLD03'

    def test_reassign_already_registered_uid_returns_409(self, admin_client, test_db):
        _seed_tag(test_db, 'OLD04', 'item-001', 'racked')
        _seed_tag(test_db, 'EXISTING', 'item-002', 'tagged')
        r = admin_client.post('/api/tags/OLD04/reassign', json={'new_uid': 'EXISTING'})
        assert r.status_code == 409

    def test_reassign_to_same_uid_returns_400(self, admin_client, test_db):
        _seed_tag(test_db, 'SAME01', 'item-001', 'racked')
        r = admin_client.post('/api/tags/SAME01/reassign', json={'new_uid': 'SAME01'})
        assert r.status_code == 400

    def test_reassign_nonexistent_tag_returns_404(self, admin_client):
        r = admin_client.post('/api/tags/GHOST/reassign', json={'new_uid': 'NEWGHOST'})
        assert r.status_code == 404

    def test_manager_cannot_reassign_tag(self, manager_client, test_db):
        _seed_tag(test_db, 'MGR01', 'item-001', 'racked')
        r = manager_client.post('/api/tags/MGR01/reassign', json={'new_uid': 'MGRNEW01'})
        assert r.status_code == 403

    def test_reassign_creates_tag_reassigned_transaction(self, admin_client, test_db):
        _seed_tag(test_db, 'TXN01', 'item-001', 'racked')
        admin_client.post('/api/tags/TXN01/reassign', json={'new_uid': 'TXNNEW01'})
        txns = admin_client.get('/api/transactions').get_json()
        assert any(t['action'] == 'tag_reassigned' and t['tag_uid'] == 'TXNNEW01' for t in txns)

    def test_new_uid_uppercased(self, admin_client, test_db):
        _seed_tag(test_db, 'UPPER01', 'item-001', 'tagged')
        r = admin_client.post('/api/tags/UPPER01/reassign', json={'new_uid': 'lower01'})
        assert r.status_code == 200
        assert r.get_json()['new_uid'] == 'LOWER01'
