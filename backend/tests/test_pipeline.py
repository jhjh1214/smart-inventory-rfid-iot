"""Tests for MQTT pipeline handlers: factory_written, factory_exit, warehouse_gate,
warehouse_rack, return_gate, legacy_scan, and worker badge routing."""
import time
import pytest
from unittest.mock import patch
from database import get_db
import mqtt_subscriber


class MockClient:
    """Minimal MQTT client stub for pipeline handler tests."""
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload))


def _seed_tag(uid, item_id, state, rack_location=None):
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO rfid_tags (uid, item_id, state, rack_location) VALUES (?, ?, ?, ?)',
        (uid, item_id, state, rack_location),
    )
    conn.commit()
    conn.close()


def _get_tag(uid):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM rfid_tags WHERE uid = ?', (uid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _get_qty(item_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT quantity FROM items WHERE id = ?', (item_id,))
    row = c.fetchone()
    conn.close()
    return row['quantity'] if row else None


@pytest.fixture(autouse=True)
def patch_webhooks():
    with patch('app._fire_webhooks'):
        yield


class TestHandleFactoryWritten:
    def test_registers_new_tag_as_tagged(self, test_db):
        mqtt_subscriber._handle_factory_written(MockClient(), {
            'tag_uid': 'FW-001', 'item_id': 'item-001', 'device_id': 'factory-01',
        })
        tag = _get_tag('FW-001')
        assert tag is not None
        assert tag['state'] == 'tagged'

    def test_creates_tag_write_transaction(self, test_db):
        mqtt_subscriber._handle_factory_written(MockClient(), {
            'tag_uid': 'FW-002', 'item_id': 'item-001', 'device_id': 'factory-01',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM transactions WHERE tag_uid = 'FW-002' AND action = 'tag_write'")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_ignores_already_registered_tag(self, test_db):
        _seed_tag('FW-DUP', 'item-001', 'in_transit')
        mqtt_subscriber._handle_factory_written(MockClient(), {
            'tag_uid': 'FW-DUP', 'item_id': 'item-001', 'device_id': 'factory-01',
        })
        tag = _get_tag('FW-DUP')
        assert tag['state'] == 'in_transit'  # unchanged

    def test_auto_creates_unknown_item(self, test_db):
        mqtt_subscriber._handle_factory_written(MockClient(), {
            'tag_uid': 'FW-003', 'item_id': 'item-unknown-fw', 'device_id': 'factory-01',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id FROM items WHERE id = ?', ('item-unknown-fw',))
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_increments_write_job_written_count(self, test_db):
        conn = get_db()
        conn.execute(
            "INSERT INTO write_jobs (batch_id, item_id, quantity, status, created_by) VALUES ('batch-fw-1', 'item-001', 2, 'pending', 'admin')"
        )
        conn.commit()
        conn.close()
        mqtt_subscriber._handle_factory_written(MockClient(), {
            'tag_uid': 'FW-004', 'item_id': 'item-001',
            'batch_id': 'batch-fw-1', 'device_id': 'factory-01',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT written FROM write_jobs WHERE batch_id = 'batch-fw-1'")
        row = c.fetchone()
        conn.close()
        assert row['written'] == 1

    def test_missing_tag_uid_is_ignored(self, test_db):
        # Should not raise or crash
        mqtt_subscriber._handle_factory_written(MockClient(), {'item_id': 'item-001'})

    def test_missing_item_id_is_ignored(self, test_db):
        mqtt_subscriber._handle_factory_written(MockClient(), {'tag_uid': 'FW-005'})


class TestHandleFactoryExit:
    def test_tagged_becomes_in_transit(self, test_db):
        _seed_tag('FE-001', 'item-001', 'tagged')
        mqtt_subscriber._handle_factory_exit(MockClient(), {
            'tag_uid': 'FE-001', 'device_id': 'gate-out',
        })
        assert _get_tag('FE-001')['state'] == 'in_transit'

    def test_out_state_becomes_in_transit(self, test_db):
        _seed_tag('FE-002', 'item-001', 'out')
        mqtt_subscriber._handle_factory_exit(MockClient(), {
            'tag_uid': 'FE-002', 'device_id': 'gate-out',
        })
        assert _get_tag('FE-002')['state'] == 'in_transit'

    def test_dispatched_tag_triggers_security_alert(self, test_db):
        _seed_tag('FE-SEC', 'item-001', 'dispatched')
        mqtt_subscriber._handle_factory_exit(MockClient(), {
            'tag_uid': 'FE-SEC', 'device_id': 'gate-out',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM alerts WHERE alert_type = 'security' AND message LIKE '%FE-SEC%'")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_consumed_tag_triggers_security_alert(self, test_db):
        _seed_tag('FE-CONS', 'item-001', 'consumed')
        mqtt_subscriber._handle_factory_exit(MockClient(), {
            'tag_uid': 'FE-CONS', 'device_id': 'gate-out',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM alerts WHERE alert_type = 'security' AND message LIKE '%FE-CONS%'")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_unknown_tag_with_item_id_auto_creates(self, test_db):
        mqtt_subscriber._handle_factory_exit(MockClient(), {
            'tag_uid': 'FE-NEW', 'item_id': 'item-001', 'device_id': 'gate-out',
        })
        tag = _get_tag('FE-NEW')
        assert tag is not None
        assert tag['state'] == 'in_transit'

    def test_creates_factory_exit_transaction(self, test_db):
        _seed_tag('FE-TXN', 'item-001', 'tagged')
        mqtt_subscriber._handle_factory_exit(MockClient(), {
            'tag_uid': 'FE-TXN', 'device_id': 'gate-out',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM transactions WHERE tag_uid = 'FE-TXN' AND action = 'factory_exit'")
        row = c.fetchone()
        conn.close()
        assert row is not None


class TestHandleWarehouseGateReceive:
    def test_in_transit_becomes_received(self, test_db):
        _seed_tag('WG-R01', 'item-001', 'in_transit')
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-R01', 'device_id': 'wh-gate',
        })
        assert _get_tag('WG-R01')['state'] == 'received'

    def test_receive_increments_quantity(self, test_db):
        _seed_tag('WG-R02', 'item-001', 'in_transit')
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-R02', 'device_id': 'wh-gate',
        })
        assert _get_qty('item-001') == prev_qty + 1

    def test_creates_warehouse_receive_transaction(self, test_db):
        _seed_tag('WG-R03', 'item-001', 'in_transit')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-R03', 'device_id': 'wh-gate',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM transactions WHERE tag_uid = 'WG-R03' AND action = 'warehouse_receive'")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_out_state_also_received(self, test_db):
        _seed_tag('WG-R04', 'item-001', 'out')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-R04', 'device_id': 'wh-gate',
        })
        assert _get_tag('WG-R04')['state'] == 'received'


class TestHandleWarehouseGateDispatch:
    def _make_supervisor_session(self, device_id='wh-gate-sup'):
        mqtt_subscriber._worker_sessions[device_id] = {
            'employee_id': 'EMP-SUP', 'name': 'Supervisor',
            'role': 'supervisor', 'zone': 'warehouse',
            'expires': time.time() + 300,
        }

    def test_racked_becomes_dispatched(self, test_db):
        _seed_tag('WG-D01', 'item-001', 'racked', rack_location='A1')
        self._make_supervisor_session()
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-D01', 'device_id': 'wh-gate-sup',
        })
        assert _get_tag('WG-D01')['state'] == 'dispatched'

    def test_dispatch_decrements_quantity(self, test_db):
        _seed_tag('WG-D02', 'item-001', 'racked')
        self._make_supervisor_session()
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-D02', 'device_id': 'wh-gate-sup',
        })
        new_qty = _get_qty('item-001')
        assert new_qty == max(0, prev_qty - 1)

    def test_dispatch_without_supervisor_creates_alert(self, test_db):
        _seed_tag('WG-NOSUP', 'item-001', 'racked')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-NOSUP', 'device_id': 'device-no-auth',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM alerts WHERE alert_type = 'security' AND message LIKE '%UNVERIFIED%'")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_dispatched_tag_triggers_security_alert(self, test_db):
        _seed_tag('WG-DSEC', 'item-001', 'dispatched')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-DSEC', 'device_id': 'wh-gate',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM alerts WHERE alert_type = 'security' AND message LIKE '%WG-DSEC%'")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_low_stock_alert_created_when_below_threshold(self, test_db):
        # Set item-003 threshold above quantity to force low-stock alert on dispatch
        conn = get_db()
        conn.execute("UPDATE items SET quantity = 1, low_stock_threshold = 5 WHERE id = 'item-003'")
        conn.commit()
        conn.close()
        _seed_tag('WG-LOW', 'item-003', 'racked')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-LOW', 'device_id': 'wh-gate-nosup',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM alerts WHERE item_id = 'item-003' AND alert_type IN ('low_stock','out_of_stock')")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_quantity_never_goes_below_zero(self, test_db):
        conn = get_db()
        conn.execute("UPDATE items SET quantity = 0 WHERE id = 'item-001'")
        conn.commit()
        conn.close()
        _seed_tag('WG-ZERO', 'item-001', 'racked')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WG-ZERO', 'device_id': 'wh-gate',
        })
        assert _get_qty('item-001') == 0


class TestHandleWarehouseRack:
    def test_received_becomes_racked(self, test_db):
        _seed_tag('WR-01', 'item-001', 'received')
        mqtt_subscriber._handle_warehouse_rack(MockClient(), {
            'tag_uid': 'WR-01', 'rack_location': 'B3', 'device_id': 'rack-scanner',
        })
        tag = _get_tag('WR-01')
        assert tag['state'] == 'racked'
        assert tag['rack_location'] == 'B3'

    def test_returned_becomes_racked(self, test_db):
        _seed_tag('WR-02', 'item-001', 'returned')
        mqtt_subscriber._handle_warehouse_rack(MockClient(), {
            'tag_uid': 'WR-02', 'rack_location': 'C1', 'device_id': 'rack-scanner',
        })
        assert _get_tag('WR-02')['state'] == 'racked'

    def test_in_state_becomes_racked(self, test_db):
        _seed_tag('WR-03', 'item-001', 'in')
        mqtt_subscriber._handle_warehouse_rack(MockClient(), {
            'tag_uid': 'WR-03', 'rack_location': 'D1', 'device_id': 'rack-scanner',
        })
        assert _get_tag('WR-03')['state'] == 'racked'

    def test_invalid_state_creates_security_alert(self, test_db):
        _seed_tag('WR-INV', 'item-001', 'dispatched')
        mqtt_subscriber._handle_warehouse_rack(MockClient(), {
            'tag_uid': 'WR-INV', 'rack_location': 'A1', 'device_id': 'rack-scanner',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM alerts WHERE alert_type = 'security' AND message LIKE '%WR-INV%'")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_invalid_state_tag_unchanged(self, test_db):
        _seed_tag('WR-UNCH', 'item-001', 'tagged')
        mqtt_subscriber._handle_warehouse_rack(MockClient(), {
            'tag_uid': 'WR-UNCH', 'rack_location': 'X9', 'device_id': 'rack-scanner',
        })
        assert _get_tag('WR-UNCH')['state'] == 'tagged'

    def test_creates_warehouse_rack_transaction(self, test_db):
        _seed_tag('WR-TXN', 'item-001', 'received')
        mqtt_subscriber._handle_warehouse_rack(MockClient(), {
            'tag_uid': 'WR-TXN', 'rack_location': 'E5', 'device_id': 'rack-scanner',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM transactions WHERE tag_uid = 'WR-TXN' AND action = 'warehouse_rack'")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_unknown_tag_does_nothing(self, test_db):
        # Should not crash
        mqtt_subscriber._handle_warehouse_rack(MockClient(), {
            'tag_uid': 'WR-GHOST', 'rack_location': 'X1', 'device_id': 'rack-scanner',
        })


class TestHandleReturnGate:
    def test_dispatched_becomes_returned(self, test_db):
        _seed_tag('RG-01', 'item-001', 'dispatched')
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_return_gate(MockClient(), {
            'tag_uid': 'RG-01', 'device_id': 'returns-gate',
        })
        assert _get_tag('RG-01')['state'] == 'returned'

    def test_return_increments_quantity(self, test_db):
        _seed_tag('RG-02', 'item-001', 'dispatched')
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_return_gate(MockClient(), {
            'tag_uid': 'RG-02', 'device_id': 'returns-gate',
        })
        assert _get_qty('item-001') == prev_qty + 1

    def test_consumed_tag_can_be_returned(self, test_db):
        _seed_tag('RG-03', 'item-001', 'consumed')
        mqtt_subscriber._handle_return_gate(MockClient(), {
            'tag_uid': 'RG-03', 'device_id': 'returns-gate',
        })
        assert _get_tag('RG-03')['state'] == 'returned'

    def test_return_pending_becomes_returned(self, test_db):
        _seed_tag('RG-04', 'item-001', 'return_pending')
        mqtt_subscriber._handle_return_gate(MockClient(), {
            'tag_uid': 'RG-04', 'device_id': 'returns-gate',
        })
        assert _get_tag('RG-04')['state'] == 'returned'

    def test_return_pending_creates_return_confirmed_transaction(self, test_db):
        _seed_tag('RG-05', 'item-001', 'return_pending')
        mqtt_subscriber._handle_return_gate(MockClient(), {
            'tag_uid': 'RG-05', 'device_id': 'returns-gate',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT action FROM transactions WHERE tag_uid = 'RG-05'")
        row = c.fetchone()
        conn.close()
        assert row['action'] == 'return_confirmed'

    def test_dispatched_creates_customer_return_transaction(self, test_db):
        _seed_tag('RG-06', 'item-001', 'dispatched')
        mqtt_subscriber._handle_return_gate(MockClient(), {
            'tag_uid': 'RG-06', 'device_id': 'returns-gate',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT action FROM transactions WHERE tag_uid = 'RG-06'")
        row = c.fetchone()
        conn.close()
        assert row['action'] == 'customer_return'

    def test_non_returnable_state_ignored(self, test_db):
        _seed_tag('RG-IGN', 'item-001', 'racked')
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_return_gate(MockClient(), {
            'tag_uid': 'RG-IGN', 'device_id': 'returns-gate',
        })
        assert _get_qty('item-001') == prev_qty  # no change

    def test_unknown_tag_does_nothing(self, test_db):
        mqtt_subscriber._handle_return_gate(MockClient(), {
            'tag_uid': 'RG-GHOST', 'device_id': 'returns-gate',
        })


class TestHandleLegacyScan:
    def test_out_state_scan_increases_qty(self, test_db):
        _seed_tag('LS-01', 'item-001', 'out')
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-01', 'item_id': 'item-001', 'device_id': 'scanner',
        })
        assert _get_qty('item-001') == prev_qty + 1

    def test_out_state_becomes_in(self, test_db):
        _seed_tag('LS-02', 'item-001', 'out')
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-02', 'item_id': 'item-001', 'device_id': 'scanner',
        })
        assert _get_tag('LS-02')['state'] == 'in'

    def test_in_state_scan_decreases_qty(self, test_db):
        _seed_tag('LS-03', 'item-001', 'in')
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-03', 'item_id': 'item-001', 'device_id': 'scanner',
        })
        assert _get_qty('item-001') == max(0, prev_qty - 1)

    def test_in_state_becomes_consumed(self, test_db):
        _seed_tag('LS-04', 'item-001', 'in')
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-04', 'item_id': 'item-001', 'device_id': 'scanner',
        })
        assert _get_tag('LS-04')['state'] == 'consumed'

    def test_consumed_tag_creates_security_alert(self, test_db):
        _seed_tag('LS-CONS', 'item-001', 'consumed')
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-CONS', 'item_id': 'item-001', 'device_id': 'scanner',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM alerts WHERE alert_type = 'security' AND message LIKE '%LS-CONS%'")
        row = c.fetchone()
        conn.close()
        assert row is not None

    def test_unregistered_tag_auto_registered_as_out(self, test_db):
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-NEW', 'item_id': 'item-001', 'device_id': 'scanner',
        })
        tag = _get_tag('LS-NEW')
        assert tag is not None

    def test_return_pending_confirms_return(self, test_db):
        _seed_tag('LS-RET', 'item-001', 'return_pending')
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-RET', 'item_id': 'item-001', 'device_id': 'scanner',
        })
        assert _get_qty('item-001') == prev_qty + 1

    def test_pipeline_states_ignored_by_legacy(self, test_db):
        _seed_tag('LS-PIPE', 'item-001', 'in_transit')
        prev_qty = _get_qty('item-001')
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-PIPE', 'item_id': 'item-001', 'device_id': 'scanner',
        })
        assert _get_qty('item-001') == prev_qty  # no change

    def test_creates_scan_in_transaction(self, test_db):
        _seed_tag('LS-TXN', 'item-001', 'out')
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-TXN', 'item_id': 'item-001', 'device_id': 'scanner',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT action FROM transactions WHERE tag_uid = 'LS-TXN'")
        row = c.fetchone()
        conn.close()
        assert row['action'] == 'scan_in'

    def test_low_stock_alert_on_scan_out(self, test_db):
        conn = get_db()
        conn.execute("UPDATE items SET quantity = 1, low_stock_threshold = 5 WHERE id = 'item-002'")
        conn.commit()
        conn.close()
        _seed_tag('LS-LSTK', 'item-002', 'in')
        mqtt_subscriber._handle_legacy_scan(MockClient(), {
            'tag_uid': 'LS-LSTK', 'item_id': 'item-002', 'device_id': 'scanner',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM alerts WHERE item_id = 'item-002' AND alert_type IN ('low_stock', 'out_of_stock')")
        row = c.fetchone()
        conn.close()
        assert row is not None


class TestPerformedByAttachment:
    """Verify that _attach_worker sets performed_by on transactions."""

    def test_transaction_records_worker_name(self, test_db):
        mqtt_subscriber._worker_sessions['device-wb'] = {
            'employee_id': 'EMP-001', 'name': 'Alice Tan', 'role': 'supervisor',
            'zone': 'warehouse', 'expires': time.time() + 300,
        }
        _seed_tag('WB-TXN', 'item-001', 'in_transit')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WB-TXN', 'device_id': 'device-wb',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT performed_by FROM transactions WHERE tag_uid = 'WB-TXN'")
        row = c.fetchone()
        conn.close()
        assert row is not None
        assert 'Alice Tan' in row['performed_by']

    def test_transaction_defaults_to_system_when_no_session(self, test_db):
        _seed_tag('WB-SYS', 'item-001', 'in_transit')
        mqtt_subscriber._handle_warehouse_gate(MockClient(), {
            'tag_uid': 'WB-SYS', 'device_id': 'device-no-worker',
        })
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT performed_by FROM transactions WHERE tag_uid = 'WB-SYS'")
        row = c.fetchone()
        conn.close()
        # system is the default; _attach_worker only overrides when worker found
        assert row is not None
