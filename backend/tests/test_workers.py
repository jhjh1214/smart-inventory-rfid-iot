"""Tests for worker management and session endpoints."""
import pytest
import time
from database import get_db
import mqtt_subscriber


class TestGetWorkers:
    def test_returns_seeded_workers(self, viewer_client):
        r = viewer_client.get('/api/workers')
        assert r.status_code == 200
        workers = r.get_json()
        assert len(workers) == 4

    def test_includes_all_fields(self, viewer_client):
        workers = viewer_client.get('/api/workers').get_json()
        w = workers[0]
        for field in ('id', 'employee_id', 'name', 'role', 'active'):
            assert field in w

    def test_includes_active_station_from_session(self, viewer_client):
        mqtt_subscriber._worker_sessions['device-99'] = {
            'employee_id': 'EMP-001',
            'name': 'Alice Tan',
            'role': 'supervisor',
            'zone': 'warehouse',
            'expires': time.time() + 300,
        }
        workers = viewer_client.get('/api/workers').get_json()
        alice = next(w for w in workers if w['employee_id'] == 'EMP-001')
        assert alice['active_station'] == 'device-99'

    def test_unauthenticated_returns_401(self, client):
        r = client.get('/api/workers')
        assert r.status_code == 401


class TestCreateWorker:
    def test_manager_can_create_worker(self, manager_client):
        r = manager_client.post('/api/workers', json={
            'employee_id': 'EMP-100', 'name': 'Test Worker', 'role': 'operator',
        })
        assert r.status_code == 201

    def test_admin_can_create_worker(self, admin_client):
        r = admin_client.post('/api/workers', json={
            'employee_id': 'EMP-101', 'name': 'Admin Worker',
        })
        assert r.status_code == 201

    def test_viewer_cannot_create_worker(self, viewer_client):
        r = viewer_client.post('/api/workers', json={
            'employee_id': 'EMP-102', 'name': 'Viewer Worker',
        })
        assert r.status_code == 403

    def test_invalid_employee_id_format_returns_400(self, manager_client):
        r = manager_client.post('/api/workers', json={
            'employee_id': 'WORKER-1', 'name': 'Bad ID',
        })
        assert r.status_code == 400

    def test_employee_id_normalised_to_uppercase(self, manager_client):
        manager_client.post('/api/workers', json={
            'employee_id': 'emp-200', 'name': 'Lower Case',
        })
        workers = manager_client.get('/api/workers').get_json()
        assert any(w['employee_id'] == 'EMP-200' for w in workers)

    def test_duplicate_employee_id_returns_400(self, manager_client):
        manager_client.post('/api/workers', json={'employee_id': 'EMP-201', 'name': 'First'})
        r = manager_client.post('/api/workers', json={'employee_id': 'EMP-201', 'name': 'Second'})
        assert r.status_code == 400

    def test_missing_name_returns_400(self, manager_client):
        r = manager_client.post('/api/workers', json={'employee_id': 'EMP-202'})
        assert r.status_code == 400

    def test_zone_stored_when_provided(self, manager_client):
        manager_client.post('/api/workers', json={
            'employee_id': 'EMP-203', 'name': 'Zoned Worker', 'zone': 'factory',
        })
        workers = manager_client.get('/api/workers').get_json()
        w = next(w for w in workers if w['employee_id'] == 'EMP-203')
        assert w['zone'] == 'factory'


class TestUpdateWorker:
    def _get_worker_id(self, client, employee_id='EMP-001'):
        workers = client.get('/api/workers').get_json()
        return next(w['id'] for w in workers if w['employee_id'] == employee_id)

    def test_manager_can_deactivate_worker(self, manager_client):
        wid = self._get_worker_id(manager_client)
        r = manager_client.put(f'/api/workers/{wid}', json={'active': 0})
        assert r.status_code == 200
        workers = manager_client.get('/api/workers').get_json()
        w = next(w for w in workers if w['id'] == wid)
        assert w['active'] == 0

    def test_manager_can_activate_worker(self, manager_client, test_db):
        conn = get_db()
        conn.execute("UPDATE workers SET active = 0 WHERE employee_id = 'EMP-001'")
        conn.commit()
        conn.close()
        wid = self._get_worker_id(manager_client)
        r = manager_client.put(f'/api/workers/{wid}', json={'active': 1})
        assert r.status_code == 200

    def test_viewer_cannot_update_worker(self, viewer_client):
        r = viewer_client.put('/api/workers/1', json={'active': 0})
        assert r.status_code == 403

    def test_update_role(self, manager_client):
        wid = self._get_worker_id(manager_client, 'EMP-002')
        manager_client.put(f'/api/workers/{wid}', json={'role': 'supervisor'})
        workers = manager_client.get('/api/workers').get_json()
        w = next(w for w in workers if w['id'] == wid)
        assert w['role'] == 'supervisor'

    def test_empty_update_returns_400(self, manager_client):
        r = manager_client.put('/api/workers/1', json={})
        assert r.status_code == 400


class TestDeleteWorker:
    def test_admin_can_delete_worker(self, admin_client):
        workers = admin_client.get('/api/workers').get_json()
        wid = workers[0]['id']
        r = admin_client.delete(f'/api/workers/{wid}')
        assert r.status_code == 200
        workers = admin_client.get('/api/workers').get_json()
        assert not any(w['id'] == wid for w in workers)

    def test_manager_cannot_delete_worker(self, manager_client):
        r = manager_client.delete('/api/workers/1')
        assert r.status_code == 403


class TestWorkerSessions:
    def test_sessions_endpoint_returns_dict(self, viewer_client):
        r = viewer_client.get('/api/workers/sessions')
        assert r.status_code == 200
        assert isinstance(r.get_json(), dict)

    def test_returns_active_sessions(self, viewer_client):
        mqtt_subscriber._worker_sessions['device-42'] = {
            'employee_id': 'EMP-001',
            'name': 'Alice Tan',
            'role': 'supervisor',
            'zone': 'warehouse',
            'expires': time.time() + 300,
        }
        sessions = viewer_client.get('/api/workers/sessions').get_json()
        assert 'device-42' in sessions

    def test_expired_sessions_not_returned(self, viewer_client):
        mqtt_subscriber._worker_sessions['device-old'] = {
            'employee_id': 'EMP-002',
            'name': 'Bob Lim',
            'role': 'operator',
            'zone': 'warehouse',
            'expires': time.time() - 10,  # already expired
        }
        sessions = viewer_client.get('/api/workers/sessions').get_json()
        assert 'device-old' not in sessions

    def test_session_includes_expires_in(self, viewer_client):
        mqtt_subscriber._worker_sessions['device-55'] = {
            'employee_id': 'EMP-001',
            'name': 'Alice',
            'role': 'supervisor',
            'zone': 'warehouse',
            'expires': time.time() + 200,
        }
        sessions = viewer_client.get('/api/workers/sessions').get_json()
        assert 'expires_in' in sessions['device-55']
        assert sessions['device-55']['expires_in'] > 0
