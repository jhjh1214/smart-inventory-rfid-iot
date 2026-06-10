"""Tests for webhook CRUD and test endpoint."""
import pytest
from database import get_db


def _create_webhook(client, name='Alert Hook', url='http://example.com/hook', events='low_stock'):
    return client.post('/api/webhooks', json={'name': name, 'url': url, 'events': events})


class TestGetWebhooks:
    def test_admin_can_list_webhooks(self, admin_client):
        r = admin_client.get('/api/webhooks')
        assert r.status_code == 200
        assert isinstance(r.get_json(), list)

    def test_manager_cannot_list_webhooks(self, manager_client):
        r = manager_client.get('/api/webhooks')
        assert r.status_code == 403

    def test_viewer_cannot_list_webhooks(self, viewer_client):
        r = viewer_client.get('/api/webhooks')
        assert r.status_code == 403

    def test_unauthenticated_returns_401(self, client):
        r = client.get('/api/webhooks')
        assert r.status_code == 401

    def test_returns_created_webhooks(self, admin_client):
        _create_webhook(admin_client)
        whs = admin_client.get('/api/webhooks').get_json()
        assert len(whs) >= 1

    def test_includes_all_fields(self, admin_client):
        _create_webhook(admin_client)
        wh = admin_client.get('/api/webhooks').get_json()[0]
        for field in ('id', 'name', 'url', 'events', 'active'):
            assert field in wh


class TestCreateWebhook:
    def test_admin_can_create(self, admin_client):
        r = _create_webhook(admin_client)
        assert r.status_code == 201

    def test_returns_id(self, admin_client):
        r = _create_webhook(admin_client)
        assert 'id' in r.get_json()

    def test_manager_cannot_create(self, manager_client):
        r = _create_webhook(manager_client)
        assert r.status_code == 403

    def test_missing_name_returns_400(self, admin_client):
        r = admin_client.post('/api/webhooks', json={'url': 'http://example.com/hook'})
        assert r.status_code == 400

    def test_missing_url_returns_400(self, admin_client):
        r = admin_client.post('/api/webhooks', json={'name': 'Test'})
        assert r.status_code == 400

    def test_invalid_url_scheme_returns_400(self, admin_client):
        r = admin_client.post('/api/webhooks', json={
            'name': 'Bad', 'url': 'ftp://example.com/hook',
        })
        assert r.status_code == 400

    def test_http_url_accepted(self, admin_client):
        r = admin_client.post('/api/webhooks', json={
            'name': 'HTTP', 'url': 'http://example.com/hook',
        })
        assert r.status_code == 201

    def test_https_url_accepted(self, admin_client):
        r = admin_client.post('/api/webhooks', json={
            'name': 'HTTPS', 'url': 'https://example.com/hook',
        })
        assert r.status_code == 201

    def test_default_events_stored(self, admin_client):
        admin_client.post('/api/webhooks', json={
            'name': 'Default Events', 'url': 'http://example.com/hook',
        })
        wh = admin_client.get('/api/webhooks').get_json()[0]
        assert wh['events'] == 'low_stock,security'

    def test_custom_events_stored(self, admin_client):
        _create_webhook(admin_client, events='security')
        wh = admin_client.get('/api/webhooks').get_json()[0]
        assert wh['events'] == 'security'


class TestUpdateWebhook:
    def _wh_id(self, admin_client):
        _create_webhook(admin_client)
        return admin_client.get('/api/webhooks').get_json()[0]['id']

    def test_admin_can_update_name(self, admin_client):
        wid = self._wh_id(admin_client)
        r = admin_client.put(f'/api/webhooks/{wid}', json={'name': 'Renamed'})
        assert r.status_code == 200

    def test_admin_can_deactivate(self, admin_client):
        wid = self._wh_id(admin_client)
        admin_client.put(f'/api/webhooks/{wid}', json={'active': 0})
        whs = admin_client.get('/api/webhooks').get_json()
        wh = next(w for w in whs if w['id'] == wid)
        assert wh['active'] == 0

    def test_admin_can_update_url(self, admin_client):
        wid = self._wh_id(admin_client)
        admin_client.put(f'/api/webhooks/{wid}', json={'url': 'http://new-url.com/hook'})
        whs = admin_client.get('/api/webhooks').get_json()
        wh = next(w for w in whs if w['id'] == wid)
        assert wh['url'] == 'http://new-url.com/hook'

    def test_empty_update_returns_400(self, admin_client):
        wid = self._wh_id(admin_client)
        r = admin_client.put(f'/api/webhooks/{wid}', json={})
        assert r.status_code == 400

    def test_manager_cannot_update(self, manager_client, admin_client):
        _create_webhook(admin_client)
        wid = admin_client.get('/api/webhooks').get_json()[0]['id']
        r = manager_client.put(f'/api/webhooks/{wid}', json={'name': 'Hack'})
        assert r.status_code == 403


class TestDeleteWebhook:
    def test_admin_can_delete(self, admin_client):
        _create_webhook(admin_client)
        wid = admin_client.get('/api/webhooks').get_json()[0]['id']
        r = admin_client.delete(f'/api/webhooks/{wid}')
        assert r.status_code == 200
        whs = admin_client.get('/api/webhooks').get_json()
        assert not any(w['id'] == wid for w in whs)

    def test_manager_cannot_delete(self, manager_client, admin_client):
        _create_webhook(admin_client)
        wid = admin_client.get('/api/webhooks').get_json()[0]['id']
        r = manager_client.delete(f'/api/webhooks/{wid}')
        assert r.status_code == 403

    def test_delete_nonexistent_returns_200(self, admin_client):
        r = admin_client.delete('/api/webhooks/99999')
        assert r.status_code == 200


class TestTestWebhook:
    def test_admin_can_test_webhook(self, admin_client):
        _create_webhook(admin_client)
        wid = admin_client.get('/api/webhooks').get_json()[0]['id']
        r = admin_client.post(f'/api/webhooks/{wid}/test')
        assert r.status_code == 200

    def test_test_nonexistent_returns_404(self, admin_client):
        r = admin_client.post('/api/webhooks/99999/test')
        assert r.status_code == 404

    def test_manager_cannot_test_webhook(self, manager_client, admin_client):
        _create_webhook(admin_client)
        wid = admin_client.get('/api/webhooks').get_json()[0]['id']
        r = manager_client.post(f'/api/webhooks/{wid}/test')
        assert r.status_code == 403

    def test_response_has_message(self, admin_client):
        _create_webhook(admin_client)
        wid = admin_client.get('/api/webhooks').get_json()[0]['id']
        data = admin_client.post(f'/api/webhooks/{wid}/test').get_json()
        assert 'message' in data
