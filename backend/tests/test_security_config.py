"""Tests for session hardening, response headers, and the production fail-closed.

These cover README remediation item 1. They deliberately do not assert a
Content-Security-Policy: the dashboard loads Tailwind, Chart.js and jsPDF from
public CDNs, so a useful CSP has to wait until those are vendored.
"""
import subprocess
import sys

import app as flask_app


class TestSessionCookie:
    def test_httponly(self):
        assert flask_app.app.config['SESSION_COOKIE_HTTPONLY'] is True

    def test_samesite_lax(self):
        # Blocks the session cookie riding along on cross-site POSTs, which is
        # the practical CSRF mitigation until real tokens exist.
        assert flask_app.app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'

    def test_secure_is_opt_in(self):
        # Forcing Secure on a plain-HTTP LAN would drop every session, so it is
        # off unless SESSION_COOKIE_SECURE is set.
        assert flask_app.app.config['SESSION_COOKIE_SECURE'] is False

    def test_login_sets_httponly_cookie(self, client, test_db):
        r = client.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
        assert r.status_code == 200
        cookies = r.headers.getlist('Set-Cookie')
        assert cookies, 'login must set a session cookie'
        assert any('HttpOnly' in c for c in cookies)
        assert any('SameSite=Lax' in c for c in cookies)


class TestUploadCap:
    def test_max_content_length_is_set(self):
        # Without a cap, a large CSV import is an easy memory-exhaustion route.
        assert flask_app.app.config['MAX_CONTENT_LENGTH'] == 16 * 1024 * 1024


class TestSecurityHeaders:
    def test_headers_on_page(self, client):
        r = client.get('/login')
        assert r.headers['X-Content-Type-Options'] == 'nosniff'
        assert r.headers['X-Frame-Options'] == 'DENY'
        assert r.headers['Referrer-Policy'] == 'no-referrer'

    def test_headers_on_api(self, client):
        r = client.get('/api/status')
        assert r.headers['X-Content-Type-Options'] == 'nosniff'

    def test_headers_on_error_response(self, client):
        # 401s go through after_request too.
        r = client.get('/api/items')
        assert r.status_code == 401
        assert r.headers['X-Frame-Options'] == 'DENY'


class TestProductionFailsClosed:
    """INVENTORY_ENV=production with no SECRET_KEY must refuse to start.

    Run in a subprocess: the guard runs at import time, and app is already
    imported in this process.
    """

    def _import_app(self, env):
        return subprocess.run(
            [sys.executable, '-c', 'import app'],
            cwd='backend', env=env, capture_output=True, text=True)

    def _env(self, **overrides):
        import os
        env = dict(os.environ)
        env.pop('SECRET_KEY', None)
        env.update(overrides)
        return env

    def test_production_without_secret_key_refuses(self):
        r = self._import_app(self._env(INVENTORY_ENV='production'))
        assert r.returncode != 0
        assert 'SECRET_KEY must be set' in r.stderr

    def test_production_with_secret_key_starts(self):
        r = self._import_app(self._env(INVENTORY_ENV='production',
                                       SECRET_KEY='a-real-key-for-this-test'))
        assert r.returncode == 0, r.stderr

    def test_development_warns_but_starts(self):
        r = self._import_app(self._env(INVENTORY_ENV=''))
        assert r.returncode == 0, r.stderr
        assert 'SECRET_KEY is not set' in r.stdout


class TestErrorsDoNotLeakInternals:
    """Three INSERT paths returned the raw driver message, e.g.
    'UNIQUE constraint failed: items.id', handing table and column names to any
    caller."""

    def test_duplicate_item_message_is_clean(self, admin_client, test_db):
        r = admin_client.post('/api/items', json={'id': 'item-001', 'name': 'Dup'})
        assert r.status_code == 400
        msg = r.get_json()['error']
        assert 'UNIQUE constraint' not in msg
        assert 'items.' not in msg
        assert 'item-001' in msg, 'still says which id clashed'

    def test_duplicate_user_message_is_clean(self, admin_client, test_db):
        r = admin_client.post('/api/users', json={'username': 'admin', 'password': 'x'})
        assert r.status_code == 400
        msg = r.get_json()['error']
        assert 'UNIQUE constraint' not in msg and 'users.' not in msg

    def test_duplicate_worker_message_is_clean(self, manager_client, test_db):
        payload = {'employee_id': 'EMP-001', 'name': 'Dup', 'role': 'operator'}
        manager_client.post('/api/workers', json=payload)
        r = manager_client.post('/api/workers', json=payload)
        assert r.status_code == 400
        msg = r.get_json()['error']
        assert 'UNIQUE constraint' not in msg and 'workers.' not in msg


class TestBackupIsConsistent:
    def test_backup_downloads_a_usable_database(self, admin_client, test_db):
        # A plain copy of a WAL database can miss commits still in the log.
        r = admin_client.get('/api/export/backup')
        assert r.status_code == 200
        assert r.data[:16] == b'SQLite format 3\x00'

        import os
        import sqlite3
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.db')
        os.write(fd, r.data)
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            # The snapshot must carry the rows, not just the schema.
            assert conn.execute('SELECT COUNT(*) FROM items').fetchone()[0] > 0
            conn.close()
        finally:
            os.unlink(path)
