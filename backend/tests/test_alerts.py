"""Tests for alert endpoints: list, mark read, clear."""
import pytest
from database import get_db


def _seed_alert(test_db, item_id='item-001', alert_type='low_stock',
                message='Stock low', is_read=0):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        'INSERT INTO alerts (item_id, alert_type, message, is_read) VALUES (?, ?, ?, ?)',
        (item_id, alert_type, message, is_read),
    )
    conn.commit()
    alert_id = c.lastrowid
    conn.close()
    return alert_id


class TestGetAlerts:
    def test_returns_empty_list_initially(self, viewer_client):
        r = viewer_client.get('/api/alerts')
        assert r.status_code == 200
        assert r.get_json() == []

    def test_returns_alerts(self, viewer_client, test_db):
        _seed_alert(test_db, alert_type='security', message='Unauthorised scan')
        alerts = viewer_client.get('/api/alerts').get_json()
        assert len(alerts) == 1
        assert alerts[0]['alert_type'] == 'security'

    def test_includes_item_name(self, viewer_client, test_db):
        _seed_alert(test_db, item_id='item-001')
        alerts = viewer_client.get('/api/alerts').get_json()
        assert alerts[0]['item_name'] is not None

    def test_ordered_newest_first(self, viewer_client, test_db):
        aid1 = _seed_alert(test_db, message='First')
        aid2 = _seed_alert(test_db, message='Second')
        alerts = viewer_client.get('/api/alerts').get_json()
        assert alerts[0]['message'] == 'Second'

    def test_unauthenticated_returns_401(self, client):
        r = client.get('/api/alerts')
        assert r.status_code == 401


class TestMarkAlertRead:
    def test_mark_single_alert_read(self, viewer_client, test_db):
        aid = _seed_alert(test_db)
        r = viewer_client.post(f'/api/alerts/{aid}/read')
        assert r.status_code == 200
        alerts = viewer_client.get('/api/alerts').get_json()
        assert alerts[0]['is_read'] == 1

    def test_nonexistent_alert_id_returns_200(self, viewer_client):
        r = viewer_client.post('/api/alerts/99999/read')
        assert r.status_code == 200  # no error for missing id

    def test_multiple_alerts_only_one_marked(self, viewer_client, test_db):
        aid1 = _seed_alert(test_db, message='Alert 1')
        aid2 = _seed_alert(test_db, message='Alert 2')
        viewer_client.post(f'/api/alerts/{aid1}/read')
        alerts = viewer_client.get('/api/alerts').get_json()
        a1 = next(a for a in alerts if a['id'] == aid1)
        a2 = next(a for a in alerts if a['id'] == aid2)
        assert a1['is_read'] == 1
        assert a2['is_read'] == 0


class TestMarkAllRead:
    def test_marks_all_alerts_read(self, viewer_client, test_db):
        _seed_alert(test_db, message='A1')
        _seed_alert(test_db, message='A2')
        _seed_alert(test_db, message='A3')
        r = viewer_client.post('/api/alerts/read-all')
        assert r.status_code == 200
        alerts = viewer_client.get('/api/alerts').get_json()
        assert all(a['is_read'] == 1 for a in alerts)

    def test_already_read_alerts_remain_read(self, viewer_client, test_db):
        _seed_alert(test_db, is_read=1, message='Already read')
        _seed_alert(test_db, is_read=0, message='Unread')
        viewer_client.post('/api/alerts/read-all')
        alerts = viewer_client.get('/api/alerts').get_json()
        assert all(a['is_read'] == 1 for a in alerts)


class TestDeleteReadAlerts:
    def test_removes_only_read_alerts(self, viewer_client, test_db):
        _seed_alert(test_db, is_read=1, message='Read alert')
        _seed_alert(test_db, is_read=0, message='Unread alert')
        r = viewer_client.delete('/api/alerts/read')
        assert r.status_code == 200
        alerts = viewer_client.get('/api/alerts').get_json()
        assert len(alerts) == 1
        assert alerts[0]['message'] == 'Unread alert'

    def test_deletes_all_read_alerts(self, viewer_client, test_db):
        for i in range(3):
            _seed_alert(test_db, is_read=1, message=f'Read {i}')
        viewer_client.delete('/api/alerts/read')
        alerts = viewer_client.get('/api/alerts').get_json()
        assert len(alerts) == 0

    def test_no_alerts_to_delete_returns_200(self, viewer_client):
        r = viewer_client.delete('/api/alerts/read')
        assert r.status_code == 200
