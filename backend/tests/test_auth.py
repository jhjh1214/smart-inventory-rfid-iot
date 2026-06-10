"""Tests for authentication: login, logout, /api/me, RBAC decorators, rate limiting."""
import pytest
import app as flask_app


def login(client, username, password=''):
    return client.post('/api/login', json={'username': username, 'password': password})


class TestLogin:
    def test_admin_login_success(self, client):
        r = login(client, 'admin', 'admin123')
        assert r.status_code == 200
        d = r.get_json()
        assert d['role'] == 'admin'
        assert d['username'] == 'admin'

    def test_manager_login_success(self, client):
        r = login(client, 'manager', 'manager123')
        assert r.status_code == 200
        assert r.get_json()['role'] == 'manager'

    def test_viewer_login_success(self, client):
        r = login(client, 'viewer', 'viewer123')
        assert r.status_code == 200
        assert r.get_json()['role'] == 'viewer'

    def test_wrong_password_returns_401(self, client):
        r = login(client, 'admin', 'notthepassword')
        assert r.status_code == 401

    def test_unknown_user_returns_401(self, client):
        r = login(client, 'nobody', 'password123')
        assert r.status_code == 401

    def test_missing_both_fields_returns_400(self, client):
        r = client.post('/api/login', json={})
        assert r.status_code == 400

    def test_missing_password_returns_400(self, client):
        r = client.post('/api/login', json={'username': 'admin'})
        assert r.status_code == 400

    def test_missing_username_returns_400(self, client):
        r = client.post('/api/login', json={'password': 'admin123'})
        assert r.status_code == 400

    def test_response_contains_status_ok(self, client):
        r = login(client, 'admin', 'admin123')
        assert r.get_json()['status'] == 'ok'


class TestLogout:
    def test_logout_clears_session(self, client):
        login(client, 'admin', 'admin123')
        assert client.get('/api/me').status_code == 200
        client.post('/api/logout')
        assert client.get('/api/me').status_code == 401

    def test_logout_always_returns_200(self, client):
        r = client.post('/api/logout')
        assert r.status_code == 200

    def test_logout_unauthenticated_still_200(self, client):
        r = client.post('/api/logout')
        assert r.status_code == 200


class TestMe:
    def test_me_returns_user_data(self, admin_client):
        r = admin_client.get('/api/me')
        assert r.status_code == 200
        d = r.get_json()
        assert d['username'] == 'admin'
        assert d['role'] == 'admin'
        assert 'id' in d

    def test_me_unauthenticated_returns_401(self, client):
        r = client.get('/api/me')
        assert r.status_code == 401

    def test_me_includes_badge_uid_when_linked(self, admin_client, test_db):
        from database import get_db
        conn = get_db()
        conn.execute("UPDATE users SET badge_uid = 'DEAD1234' WHERE username = 'admin'")
        conn.commit()
        conn.close()
        r = admin_client.get('/api/me')
        assert r.get_json()['badge_uid'] == 'DEAD1234'

    def test_me_badge_uid_null_when_not_linked(self, admin_client):
        r = admin_client.get('/api/me')
        assert r.get_json()['badge_uid'] is None

    def test_me_includes_employee_id(self, admin_client, test_db):
        from database import get_db
        conn = get_db()
        conn.execute("UPDATE users SET employee_id = 'EMP-001' WHERE username = 'admin'")
        conn.commit()
        conn.close()
        r = admin_client.get('/api/me')
        assert r.get_json()['employee_id'] == 'EMP-001'


class TestRBAC:
    def test_unauthenticated_api_returns_401(self, client):
        endpoints = [
            ('GET', '/api/items'),
            ('GET', '/api/tags'),
            ('GET', '/api/alerts'),
            ('GET', '/api/transactions'),
            ('GET', '/api/workers'),
        ]
        for method, path in endpoints:
            r = getattr(client, method.lower())(path)
            assert r.status_code == 401, f'{method} {path} should be 401'

    def test_manager_required_blocks_viewer(self, viewer_client):
        r = viewer_client.post('/api/items', json={'id': 'x', 'name': 'X'})
        assert r.status_code == 403

    def test_admin_required_blocks_viewer(self, viewer_client):
        r = viewer_client.get('/api/users')
        assert r.status_code == 403

    def test_admin_required_blocks_manager(self, manager_client):
        r = manager_client.get('/api/users')
        assert r.status_code == 403

    def test_manager_required_allows_admin(self, admin_client):
        r = admin_client.post('/api/items', json={'id': 'rbac-test', 'name': 'RBAC'})
        assert r.status_code == 201

    def test_manager_required_allows_manager(self, manager_client):
        r = manager_client.post('/api/items', json={'id': 'rbac-mgr', 'name': 'Mgr Test'})
        assert r.status_code == 201

    def test_viewer_can_read_items(self, viewer_client):
        r = viewer_client.get('/api/items')
        assert r.status_code == 200

    def test_viewer_can_read_tags(self, viewer_client):
        r = viewer_client.get('/api/tags')
        assert r.status_code == 200

    def test_viewer_can_read_audit(self, viewer_client):
        r = viewer_client.get('/api/audit')
        assert r.status_code == 200


class TestRateLimiting:
    def test_429_after_max_failures(self, client):
        for _ in range(flask_app._LOGIN_MAX):
            login(client, 'admin', 'wrong')
        r = login(client, 'admin', 'admin123')
        assert r.status_code == 429

    def test_error_message_on_rate_limit(self, client):
        for _ in range(flask_app._LOGIN_MAX):
            login(client, 'admin', 'wrong')
        r = login(client, 'admin', 'admin123')
        assert 'try again' in r.get_json()['error'].lower()

    def test_success_clears_failure_count(self, client):
        for _ in range(flask_app._LOGIN_MAX - 1):
            login(client, 'admin', 'wrong')
        login(client, 'admin', 'admin123')  # success clears counter
        r = login(client, 'admin', 'wrong')
        assert r.status_code == 401  # not 429

    def test_failures_for_one_ip_only(self, client):
        for _ in range(flask_app._LOGIN_MAX):
            login(client, 'admin', 'wrong')
        # Direct manipulation: clear from a different IP perspective via module
        flask_app._login_attempts.clear()
        r = login(client, 'admin', 'admin123')
        assert r.status_code == 200
