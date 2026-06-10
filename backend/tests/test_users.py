"""Tests for user management and password change."""
import pytest
from database import get_db


class TestGetUsers:
    def test_admin_can_list_users(self, admin_client):
        r = admin_client.get('/api/users')
        assert r.status_code == 200
        users = r.get_json()
        assert len(users) == 3
        assert any(u['username'] == 'admin' for u in users)

    def test_manager_cannot_list_users(self, manager_client):
        r = manager_client.get('/api/users')
        assert r.status_code == 403

    def test_viewer_cannot_list_users(self, viewer_client):
        r = viewer_client.get('/api/users')
        assert r.status_code == 403

    def test_password_hash_not_in_response(self, admin_client):
        users = admin_client.get('/api/users').get_json()
        for u in users:
            assert 'password_hash' not in u


class TestCreateUser:
    def test_admin_can_create_user(self, admin_client):
        r = admin_client.post('/api/users', json={
            'username': 'newstaff', 'password': 'secure99', 'role': 'viewer',
        })
        assert r.status_code == 201

    def test_manager_cannot_create_user(self, manager_client):
        r = manager_client.post('/api/users', json={
            'username': 'newstaff', 'password': 'secure99',
        })
        assert r.status_code == 403

    def test_short_password_returns_400(self, admin_client):
        r = admin_client.post('/api/users', json={
            'username': 'shortpw', 'password': '123',
        })
        assert r.status_code == 400
        assert '6' in r.get_json()['error']

    def test_exactly_six_char_password_accepted(self, admin_client):
        r = admin_client.post('/api/users', json={
            'username': 'sixchar', 'password': 'sixchr',
        })
        assert r.status_code == 201

    def test_duplicate_username_returns_400(self, admin_client):
        admin_client.post('/api/users', json={'username': 'dupuser', 'password': 'pass123'})
        r = admin_client.post('/api/users', json={'username': 'dupuser', 'password': 'pass456'})
        assert r.status_code == 400

    def test_missing_username_returns_400(self, admin_client):
        r = admin_client.post('/api/users', json={'password': 'pass123'})
        assert r.status_code == 400

    def test_default_role_is_viewer(self, admin_client):
        admin_client.post('/api/users', json={'username': 'defaultrole', 'password': 'pass123'})
        users = admin_client.get('/api/users').get_json()
        u = next(u for u in users if u['username'] == 'defaultrole')
        assert u['role'] == 'viewer'


class TestUpdateUser:
    def _get_viewer_id(self, admin_client):
        users = admin_client.get('/api/users').get_json()
        return next(u['id'] for u in users if u['username'] == 'viewer')

    def test_admin_can_update_role(self, admin_client):
        uid = self._get_viewer_id(admin_client)
        r = admin_client.put(f'/api/users/{uid}', json={'role': 'manager'})
        assert r.status_code == 200
        users = admin_client.get('/api/users').get_json()
        u = next(u for u in users if u['id'] == uid)
        assert u['role'] == 'manager'

    def test_admin_can_link_badge(self, admin_client):
        uid = self._get_viewer_id(admin_client)
        r = admin_client.put(f'/api/users/{uid}', json={'badge_uid': 'BADGE99'})
        assert r.status_code == 200
        users = admin_client.get('/api/users').get_json()
        u = next(u for u in users if u['id'] == uid)
        assert u['badge_uid'] == 'BADGE99'

    def test_empty_update_returns_400(self, admin_client):
        uid = self._get_viewer_id(admin_client)
        r = admin_client.put(f'/api/users/{uid}', json={})
        assert r.status_code == 400

    def test_manager_cannot_update_user(self, manager_client):
        r = manager_client.put('/api/users/1', json={'role': 'viewer'})
        assert r.status_code == 403


class TestDeleteUser:
    def test_admin_can_delete_user(self, admin_client):
        admin_client.post('/api/users', json={'username': 'todelete', 'password': 'pass123'})
        users = admin_client.get('/api/users').get_json()
        uid = next(u['id'] for u in users if u['username'] == 'todelete')
        r = admin_client.delete(f'/api/users/{uid}')
        assert r.status_code == 200
        users = admin_client.get('/api/users').get_json()
        assert not any(u['username'] == 'todelete' for u in users)

    def test_cannot_delete_own_account(self, admin_client):
        users = admin_client.get('/api/users').get_json()
        admin_id = next(u['id'] for u in users if u['username'] == 'admin')
        r = admin_client.delete(f'/api/users/{admin_id}')
        assert r.status_code == 400

    def test_manager_cannot_delete_user(self, manager_client):
        r = manager_client.delete('/api/users/1')
        assert r.status_code == 403


class TestChangePassword:
    def _get_viewer_id(self, admin_client):
        users = admin_client.get('/api/users').get_json()
        return next(u['id'] for u in users if u['username'] == 'viewer')

    def test_admin_can_change_any_password(self, admin_client):
        uid = self._get_viewer_id(admin_client)
        r = admin_client.put(f'/api/users/{uid}/password', json={'password': 'newpass999'})
        assert r.status_code == 200

    def test_admin_skips_current_password_check(self, admin_client):
        uid = self._get_viewer_id(admin_client)
        r = admin_client.put(f'/api/users/{uid}/password', json={'password': 'newpass999'})
        assert r.status_code == 200  # no current_password required

    def test_non_admin_requires_current_password(self, client):
        client.post('/api/login', json={'username': 'viewer', 'password': 'viewer123'})
        r = client.get('/api/users')
        # Get viewer id indirectly
        from database import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE username = 'viewer'")
        uid = c.fetchone()['id']
        conn.close()
        r = client.put(f'/api/users/{uid}/password', json={
            'password': 'newpass999',
            'current_password': 'viewer123',
        })
        assert r.status_code == 200

    def test_wrong_current_password_returns_403(self, client):
        client.post('/api/login', json={'username': 'viewer', 'password': 'viewer123'})
        from database import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE username = 'viewer'")
        uid = c.fetchone()['id']
        conn.close()
        r = client.put(f'/api/users/{uid}/password', json={
            'password': 'newpass999',
            'current_password': 'wrongpassword',
        })
        assert r.status_code == 403

    def test_short_new_password_returns_400(self, admin_client):
        uid = self._get_viewer_id(admin_client)
        r = admin_client.put(f'/api/users/{uid}/password', json={'password': '12345'})
        assert r.status_code == 400

    def test_cannot_change_other_users_password(self, client):
        client.post('/api/login', json={'username': 'viewer', 'password': 'viewer123'})
        from database import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE username = 'manager'")
        mgr_id = c.fetchone()['id']
        conn.close()
        r = client.put(f'/api/users/{mgr_id}/password', json={'password': 'newpass999'})
        assert r.status_code == 403

    def test_password_actually_changed(self, admin_client, client):
        from database import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE username = 'viewer'")
        uid = c.fetchone()['id']
        conn.close()
        admin_client.put(f'/api/users/{uid}/password', json={'password': 'changedpw99'})
        r = client.post('/api/login', json={'username': 'viewer', 'password': 'changedpw99'})
        assert r.status_code == 200
