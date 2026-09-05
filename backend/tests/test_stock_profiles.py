"""Tests for per-item stock profiles.

A profile adds no pipeline stages - it changes two decisions the warehouse gate
makes when goods leave. 'consumable' must reproduce the original behaviour
exactly, so an existing catalogue is unaffected until an item is reclassified.
"""
import pytest

import mqtt_subscriber
import stock_profiles
from database import get_db


class MockClient:
    def publish(self, *a, **kw):
        pass


def _set_profile(item_id, profile):
    conn = get_db()
    conn.execute('UPDATE items SET stock_profile = ? WHERE id = ?', (profile, item_id))
    conn.commit()
    conn.close()


def _seed_tag(uid, item_id, state):
    conn = get_db()
    conn.execute('INSERT INTO rfid_tags (uid, item_id, state) VALUES (?, ?, ?)',
                 (uid, item_id, state))
    conn.commit()
    conn.close()


def _tag(uid):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM rfid_tags WHERE uid = ?', (uid,))
    row = c.fetchone()
    conn.close()
    return row


def _qty(item_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT quantity FROM items WHERE id = ?', (item_id,))
    q = c.fetchone()['quantity']
    conn.close()
    return q


def _supervisor_on(device_id, station='warehouse_gate'):
    """An active supervisor session opened at this station.

    _get_current_worker reads the in-process _worker_sessions cache, not the
    table, so a session written only to SQLite would not be seen. `station`
    matters: the dispatch check requires a session opened at the gate, so a
    session without it is not a supervised dispatch.
    """
    import time
    mqtt_subscriber._worker_sessions[device_id] = {
        'station': station,
        'employee_id': 'EMP-SUP',
        'name':        'Sup',
        'role':        'supervisor',
        'zone':        'general',
        'expires':     time.time() + 300,
    }


@pytest.fixture(autouse=True)
def _clear_sessions():
    """The session cache is module-level; leaking it across tests would make
    the supervisor cases order-dependent."""
    mqtt_subscriber._worker_sessions.clear()
    yield
    mqtt_subscriber._worker_sessions.clear()


def _dispatch(uid, device_id='gate-01'):
    mqtt_subscriber._handle_warehouse_gate(MockClient(), {
        'tag_uid': uid, 'device_id': device_id,
    })


# ── The policy table ──────────────────────────────────────────────────────────

class TestPolicyTable:
    def test_default_is_consumable(self):
        assert stock_profiles.DEFAULT == 'consumable'

    def test_consumable_reproduces_original_behaviour(self):
        rule = stock_profiles.policy('consumable')
        assert rule['dispatch_terminal'] is True
        assert rule['require_supervisor'] is False

    def test_unknown_profile_falls_back(self):
        # A row written by an older build must never crash the pipeline.
        for junk in ('nonsense', '', None, 42):
            assert stock_profiles.policy(junk) == stock_profiles.PROFILES['consumable']

    def test_normalise_is_case_insensitive(self):
        assert stock_profiles.normalise('  Returnable ') == 'returnable'

    def test_validity(self):
        assert stock_profiles.is_valid('serialised')
        assert not stock_profiles.is_valid('serialized')   # British spelling only

    def test_describe_covers_every_profile(self):
        described = stock_profiles.describe()
        assert {d['name'] for d in described} == set(stock_profiles.NAMES)
        assert all(d['label'] and d['description'] for d in described)


# ── Consumable: unchanged behaviour ───────────────────────────────────────────

class TestConsumableIsUnchanged:
    def test_dispatch_is_terminal(self, test_db):
        _set_profile('item-001', 'consumable')
        _seed_tag('C-1', 'item-001', 'racked')
        _dispatch('C-1')
        assert _tag('C-1')['state'] == 'dispatched'

    def test_dispatch_proceeds_without_supervisor(self, test_db):
        # CLAUDE.md section 5: ordinary stock must never deadlock on a badge.
        _set_profile('item-001', 'consumable')
        before = _qty('item-001')
        _seed_tag('C-2', 'item-001', 'racked')
        _dispatch('C-2')
        assert _qty('item-001') == before - 1

    def test_unverified_dispatch_still_alerts(self, test_db):
        _set_profile('item-001', 'consumable')
        _seed_tag('C-3', 'item-001', 'racked')
        _dispatch('C-3')
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT message FROM alerts WHERE alert_type='security' "
                  "AND message LIKE '%UNVERIFIED DISPATCH%'")
        assert c.fetchone() is not None
        conn.close()

    def test_new_items_default_to_consumable(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT stock_profile FROM items WHERE id = 'item-001'")
        assert c.fetchone()['stock_profile'] == 'consumable'
        conn.close()


# ── Returnable: dispatch opens a return ───────────────────────────────────────

class TestReturnable:
    def test_dispatch_opens_a_return(self, test_db):
        _set_profile('item-001', 'returnable')
        _seed_tag('R-1', 'item-001', 'racked')
        _dispatch('R-1')
        assert _tag('R-1')['state'] == 'return_pending', \
            'a tool going out is expected back, with no admin step'

    def test_stock_still_leaves(self, test_db):
        _set_profile('item-001', 'returnable')
        before = _qty('item-001')
        _seed_tag('R-2', 'item-001', 'racked')
        _dispatch('R-2')
        assert _qty('item-001') == before - 1, 'the goods did physically leave'

    def test_still_counts_as_demand(self, test_db):
        # The forecast reads warehouse_dispatch; a returnable issue is still an issue.
        _set_profile('item-001', 'returnable')
        _seed_tag('R-3', 'item-001', 'racked')
        _dispatch('R-3')
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT action FROM transactions WHERE tag_uid = 'R-3'")
        assert c.fetchone()['action'] == 'warehouse_dispatch'
        conn.close()

    def test_rack_scan_closes_the_loop(self, test_db):
        # return_pending -> returned -> racked is the existing rack handler, so a
        # returnable asset completes its cycle with no dashboard interaction.
        _set_profile('item-001', 'returnable')
        _seed_tag('R-4', 'item-001', 'racked')
        _dispatch('R-4')
        assert _tag('R-4')['state'] == 'return_pending'

        after_dispatch = _qty('item-001')
        mqtt_subscriber._handle_warehouse_rack(MockClient(), {
            'tag_uid': 'R-4', 'item_id': 'item-001',
            'rack_location': 'A1', 'device_id': 'rack-01',
        })
        assert _tag('R-4')['state'] == 'racked'
        assert _qty('item-001') == after_dispatch + 1, 'the asset came back'


# ── Serialised: dispatch is refused without a supervisor ──────────────────────

class TestSerialised:
    def test_dispatch_blocked_without_supervisor(self, test_db):
        _set_profile('item-001', 'serialised')
        before = _qty('item-001')
        _seed_tag('S-1', 'item-001', 'racked')
        _dispatch('S-1')

        assert _tag('S-1')['state'] == 'racked', 'nothing moved'
        assert _qty('item-001') == before, 'no quantity change'

    def test_block_raises_a_security_alert(self, test_db):
        _set_profile('item-001', 'serialised')
        _seed_tag('S-2', 'item-001', 'racked')
        _dispatch('S-2')
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT message FROM alerts WHERE alert_type = 'security' "
                  "AND message LIKE '%BLOCKED DISPATCH%'")
        assert c.fetchone() is not None
        conn.close()

    def test_no_audit_row_for_a_blocked_dispatch(self, test_db):
        _set_profile('item-001', 'serialised')
        _seed_tag('S-3', 'item-001', 'racked')
        _dispatch('S-3')
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM transactions WHERE tag_uid = 'S-3'")
        assert c.fetchone()[0] == 0
        conn.close()

    def test_supervisor_lets_it_through(self, test_db):
        _set_profile('item-001', 'serialised')
        _supervisor_on('gate-supervised')
        before = _qty('item-001')
        _seed_tag('S-4', 'item-001', 'racked')
        _dispatch('S-4', device_id='gate-supervised')

        assert _tag('S-4')['state'] == 'dispatched'
        assert _qty('item-001') == before - 1


# ── API surface ───────────────────────────────────────────────────────────────

class TestProfileApi:
    def test_catalogue_endpoint(self, admin_client):
        body = admin_client.get('/api/stock-profiles').get_json()
        assert {p['name'] for p in body} == set(stock_profiles.NAMES)

    def test_catalogue_requires_login(self, client):
        assert client.get('/api/stock-profiles').status_code == 401

    def test_items_expose_the_profile(self, admin_client, test_db):
        items = admin_client.get('/api/items').get_json()
        assert all('stock_profile' in i for i in items)

    def test_manager_can_reclassify(self, manager_client, test_db):
        r = manager_client.put('/api/items/item-001', json={'stock_profile': 'returnable'})
        assert r.status_code == 200
        items = manager_client.get('/api/items').get_json()
        assert next(i for i in items if i['id'] == 'item-001')['stock_profile'] == 'returnable'

    def test_invalid_profile_rejected(self, manager_client, test_db):
        r = manager_client.put('/api/items/item-001', json={'stock_profile': 'nonsense'})
        assert r.status_code == 400
        assert 'stock_profile' in r.get_json()['error']

    def test_viewer_cannot_reclassify(self, viewer_client, test_db):
        r = viewer_client.put('/api/items/item-001', json={'stock_profile': 'serialised'})
        assert r.status_code == 403

    def test_created_item_takes_a_profile(self, manager_client, test_db):
        manager_client.post('/api/items', json={
            'id': 'tool-001', 'name': 'Torque Wrench', 'quantity': 2,
            'stock_profile': 'returnable',
        })
        items = manager_client.get('/api/items').get_json()
        assert next(i for i in items if i['id'] == 'tool-001')['stock_profile'] == 'returnable'

    def test_created_item_defaults_to_consumable(self, manager_client, test_db):
        manager_client.post('/api/items', json={'id': 'plain-001', 'name': 'Plain', 'quantity': 1})
        items = manager_client.get('/api/items').get_json()
        assert next(i for i in items if i['id'] == 'plain-001')['stock_profile'] == 'consumable'


class TestMigration:
    def test_profile_column_exists_and_defaults(self, test_db):
        conn = get_db()
        c = conn.cursor()
        c.execute('PRAGMA table_info(items)')
        cols = {r['name']: r for r in c.fetchall()}
        assert 'stock_profile' in cols
        c.execute('SELECT COUNT(*) FROM items WHERE stock_profile IS NULL')
        assert c.fetchone()[0] == 0, 'every existing row must get the default'
        conn.close()
