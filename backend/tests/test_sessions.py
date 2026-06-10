"""Tests for worker session helpers: TTL, DB persistence, _performed_by."""
import time
import pytest
from database import get_db
import mqtt_subscriber


def _seed_worker(employee_id='EMP-001', active=1, name='Alice Tan', role='supervisor'):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM workers WHERE employee_id = ?', (employee_id,))
    if not c.fetchone():
        conn.execute(
            'INSERT INTO workers (employee_id, name, role, active) VALUES (?, ?, ?, ?)',
            (employee_id, name, role, active),
        )
        conn.commit()
    conn.close()


class TestGetCurrentWorker:
    def test_returns_none_when_no_session(self, test_db):
        w = mqtt_subscriber._get_current_worker('device-notexist')
        assert w is None

    def test_returns_session_when_active(self, test_db):
        mqtt_subscriber._worker_sessions['device-act'] = {
            'employee_id': 'EMP-001', 'name': 'Alice', 'role': 'supervisor',
            'zone': 'warehouse', 'expires': time.time() + 300,
        }
        w = mqtt_subscriber._get_current_worker('device-act')
        assert w is not None
        assert w['employee_id'] == 'EMP-001'

    def test_returns_none_for_expired_session(self, test_db):
        mqtt_subscriber._worker_sessions['device-exp'] = {
            'employee_id': 'EMP-002', 'name': 'Bob', 'role': 'operator',
            'zone': 'warehouse', 'expires': time.time() - 5,
        }
        w = mqtt_subscriber._get_current_worker('device-exp')
        assert w is None

    def test_expired_session_removed_from_dict(self, test_db):
        mqtt_subscriber._worker_sessions['device-rm'] = {
            'employee_id': 'EMP-003', 'name': 'Charlie', 'role': 'operator',
            'zone': 'general', 'expires': time.time() - 1,
        }
        mqtt_subscriber._get_current_worker('device-rm')
        assert 'device-rm' not in mqtt_subscriber._worker_sessions

    def test_returns_none_for_none_device_id(self, test_db):
        assert mqtt_subscriber._get_current_worker(None) is None


class TestPerformedBy:
    def test_returns_system_when_no_session(self, test_db):
        result = mqtt_subscriber._performed_by('device-nosess')
        assert result == 'system'

    def test_returns_worker_name_and_id(self, test_db):
        mqtt_subscriber._worker_sessions['device-perf'] = {
            'employee_id': 'EMP-010', 'name': 'Dave Lee', 'role': 'operator',
            'zone': 'warehouse', 'expires': time.time() + 300,
        }
        result = mqtt_subscriber._performed_by('device-perf')
        assert 'Dave Lee' in result
        assert 'EMP-010' in result


class TestSaveAndLoadSessions:
    def test_save_session_persists_to_db(self, test_db):
        sess = {
            'employee_id': 'EMP-099', 'name': 'Stored Worker', 'role': 'operator',
            'zone': 'general', 'expires': int(time.time()) + 300,
        }
        mqtt_subscriber._save_session('device-save', sess)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM worker_sessions WHERE device_id = ?', ('device-save',))
        row = c.fetchone()
        conn.close()
        assert row is not None
        assert row['employee_id'] == 'EMP-099'

    def test_load_sessions_restores_active_sessions(self, test_db):
        expires = int(time.time()) + 300
        conn = get_db()
        conn.execute(
            '''INSERT OR REPLACE INTO worker_sessions
               (device_id, employee_id, name, role, zone, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            ('device-load', 'EMP-050', 'Load Worker', 'operator', 'warehouse', expires),
        )
        conn.commit()
        conn.close()

        mqtt_subscriber._worker_sessions.pop('device-load', None)
        mqtt_subscriber._load_sessions()
        assert 'device-load' in mqtt_subscriber._worker_sessions

    def test_load_sessions_ignores_expired(self, test_db):
        expires = int(time.time()) - 10  # already expired
        conn = get_db()
        conn.execute(
            '''INSERT OR REPLACE INTO worker_sessions
               (device_id, employee_id, name, role, zone, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            ('device-old-load', 'EMP-051', 'Old Worker', 'operator', 'general', expires),
        )
        conn.commit()
        conn.close()

        mqtt_subscriber._worker_sessions.pop('device-old-load', None)
        mqtt_subscriber._load_sessions()
        assert 'device-old-load' not in mqtt_subscriber._worker_sessions

    def test_delete_session_removes_from_db(self, test_db):
        conn = get_db()
        conn.execute(
            '''INSERT OR REPLACE INTO worker_sessions
               (device_id, employee_id, name, role, zone, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            ('device-del', 'EMP-052', 'Del Worker', 'operator', 'general', int(time.time()) + 300),
        )
        conn.commit()
        conn.close()

        mqtt_subscriber._delete_session('device-del')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT device_id FROM worker_sessions WHERE device_id = ?', ('device-del',))
        row = c.fetchone()
        conn.close()
        assert row is None


class TestGetWorkerSessions:
    def test_returns_dict(self, test_db):
        result = mqtt_subscriber.get_worker_sessions()
        assert isinstance(result, dict)

    def test_active_session_included(self, test_db):
        mqtt_subscriber._worker_sessions['dev-gs'] = {
            'employee_id': 'EMP-020', 'name': 'Eve', 'role': 'operator',
            'zone': 'warehouse', 'expires': time.time() + 200,
        }
        result = mqtt_subscriber.get_worker_sessions()
        assert 'dev-gs' in result

    def test_expired_session_excluded(self, test_db):
        mqtt_subscriber._worker_sessions['dev-exp-gs'] = {
            'employee_id': 'EMP-021', 'name': 'Frank', 'role': 'operator',
            'zone': 'warehouse', 'expires': time.time() - 1,
        }
        result = mqtt_subscriber.get_worker_sessions()
        assert 'dev-exp-gs' not in result

    def test_expires_in_is_positive(self, test_db):
        mqtt_subscriber._worker_sessions['dev-exp-in'] = {
            'employee_id': 'EMP-022', 'name': 'Grace', 'role': 'supervisor',
            'zone': 'warehouse', 'expires': time.time() + 150,
        }
        result = mqtt_subscriber.get_worker_sessions()
        assert result['dev-exp-in']['expires_in'] > 0

    def test_expired_sessions_removed_from_dict(self, test_db):
        mqtt_subscriber._worker_sessions['dev-cleanup'] = {
            'employee_id': 'EMP-023', 'name': 'Heidi', 'role': 'operator',
            'zone': 'general', 'expires': time.time() - 1,
        }
        mqtt_subscriber.get_worker_sessions()
        assert 'dev-cleanup' not in mqtt_subscriber._worker_sessions


class TestWorkerBadgeHandler:
    def test_badge_scan_creates_session(self, test_db):
        _seed_worker('EMP-AUTH', active=1, name='Badge Worker')
        mqtt_subscriber._handle_worker_badge('inventory/scan', {
            'tag_uid': 'BADGE-001',
            'item_id': 'EMP-AUTH',
            'device_id': 'device-badge',
        })
        assert 'device-badge' in mqtt_subscriber._worker_sessions

    def test_inactive_worker_not_granted_session(self, test_db):
        _seed_worker('EMP-INACTIVE', active=0, name='Inactive Worker')
        mqtt_subscriber._handle_worker_badge('inventory/scan', {
            'tag_uid': 'BADGE-002',
            'item_id': 'EMP-INACTIVE',
            'device_id': 'device-inactive',
        })
        assert 'device-inactive' not in mqtt_subscriber._worker_sessions

    def test_session_has_correct_employee_id(self, test_db):
        _seed_worker('EMP-CHECK', active=1, name='Check Worker')
        mqtt_subscriber._handle_worker_badge('inventory/scan', {
            'tag_uid': 'BADGE-003',
            'item_id': 'EMP-CHECK',
            'device_id': 'device-check',
        })
        sess = mqtt_subscriber._worker_sessions.get('device-check')
        assert sess is not None
        assert sess['employee_id'] == 'EMP-CHECK'

    def test_session_ttl_set(self, test_db):
        _seed_worker('EMP-TTL', active=1, name='TTL Worker')
        before = time.time()
        mqtt_subscriber._handle_worker_badge('inventory/scan', {
            'tag_uid': 'BADGE-004',
            'item_id': 'EMP-TTL',
            'device_id': 'device-ttl',
        })
        sess = mqtt_subscriber._worker_sessions.get('device-ttl')
        assert sess is not None
        assert sess['expires'] > before + 200

    def test_unknown_worker_auto_created(self, test_db):
        mqtt_subscriber._handle_worker_badge('inventory/scan', {
            'tag_uid': 'BADGE-NEW',
            'item_id': 'EMP-NEW999',
            'device_id': 'device-new',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id FROM workers WHERE employee_id = ?', ('EMP-NEW999',))
        row = c.fetchone()
        conn.close()
        assert row is not None
