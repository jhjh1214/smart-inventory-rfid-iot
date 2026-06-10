"""Shared fixtures for all test modules."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import database
import app as flask_app
import mqtt_subscriber


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear persistent module-level state before and after every test."""
    flask_app._login_attempts.clear()
    mqtt_subscriber._worker_sessions.clear()
    yield
    flask_app._login_attempts.clear()
    mqtt_subscriber._worker_sessions.clear()


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Isolated SQLite database per test via monkeypatching DB_PATH."""
    db_file = str(tmp_path / 'test.db')
    monkeypatch.setattr(database, 'DB_PATH', db_file)
    monkeypatch.setattr(flask_app, 'DB_PATH', db_file)
    database.init_db()
    return db_file


@pytest.fixture
def app(test_db):
    flask_app.app.config.update({'TESTING': True, 'SECRET_KEY': 'test-secret'})
    yield flask_app.app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def login(client, username, password=''):
    return client.post('/api/login', json={'username': username, 'password': password})


# Each role fixture uses its own client instance (no shared 'with' block) so that
# multiple roles can coexist in the same test without Flask context conflicts.

@pytest.fixture
def admin_client(app):
    c = app.test_client()
    c.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    return c


@pytest.fixture
def manager_client(app):
    c = app.test_client()
    c.post('/api/login', json={'username': 'manager', 'password': 'manager123'})
    return c


@pytest.fixture
def viewer_client(app):
    c = app.test_client()
    c.post('/api/login', json={'username': 'viewer', 'password': 'viewer123'})
    return c
