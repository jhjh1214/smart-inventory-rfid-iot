"""Tests for analytics endpoints and analytics.py functions."""
import pytest
from database import get_db
import analytics
from analytics import (
    get_item_analytics, get_all_analytics, get_transaction_trends,
    get_abc_analysis, get_inventory_summary, get_pipeline_summary,
    _exponential_smoothing, _eoq, _risk_score, _get_daily_usage, _recent_transactions,
)


def _seed_scan_txn(item_id, action, qty_change, days_ago=0):
    from datetime import datetime, timedelta
    conn = get_db()
    ts = (datetime.now() - timedelta(days=days_ago)).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        '''INSERT INTO transactions (item_id, action, quantity_change, previous_quantity,
           new_quantity, performed_by, device_id, timestamp)
           VALUES (?, ?, ?, 10, 9, 'test', 'test', ?)''',
        (item_id, action, qty_change, ts),
    )
    conn.commit()
    conn.close()


class TestAnalyticsHelpers:
    def test_exponential_smoothing_empty(self):
        assert _exponential_smoothing([]) == 1.0

    def test_exponential_smoothing_single(self):
        assert _exponential_smoothing([5.0]) == 5.0

    def test_exponential_smoothing_multiple(self):
        result = _exponential_smoothing([1.0, 2.0, 3.0])
        assert isinstance(result, float)
        assert result > 0

    def test_eoq_zero_demand(self):
        assert _eoq(0) == 0

    def test_eoq_positive_demand(self):
        result = _eoq(10)
        assert result > 0

    def test_risk_score_critical(self):
        assert _risk_score(1) == 90

    def test_risk_score_warning(self):
        assert _risk_score(5) == 60

    def test_risk_score_healthy(self):
        assert _risk_score(30) == 20


class TestGetItemAnalytics:
    def test_returns_required_fields(self, test_db):
        result = get_item_analytics('item-001', 10)
        for key in ('item_id', 'avg_daily_usage', 'forecast_demand', 'eoq',
                    'days_remaining', 'risk_score', 'daily_history'):
            assert key in result

    def test_item_id_matches(self, test_db):
        result = get_item_analytics('item-001', 10)
        assert result['item_id'] == 'item-001'

    def test_no_history_means_no_demand(self, test_db):
        # An item that has never been dispatched has zero demand, not the
        # 1.0/day placeholder the old always-empty series produced.
        result = get_item_analytics('item-001', 5)
        assert result['avg_daily_usage'] == 0.0
        assert result['days_remaining'] == 999.0

    def test_with_scan_history(self, test_db):
        _seed_scan_txn('item-001', 'warehouse_dispatch', -2, days_ago=1)
        result = get_item_analytics('item-001', 10)
        assert result['avg_daily_usage'] >= 0

    def test_daily_history_limited_to_7(self, test_db):
        for i in range(10):
            _seed_scan_txn('item-001', 'warehouse_dispatch', -1, days_ago=i)
        result = get_item_analytics('item-001', 5)
        assert len(result['daily_history']) <= 7


class TestGetAllAnalytics:
    def test_returns_list(self, test_db):
        result = get_all_analytics()
        assert isinstance(result, list)

    def test_length_matches_item_count(self, test_db):
        result = get_all_analytics()
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM items')
        count = c.fetchone()[0]
        conn.close()
        assert len(result) == count


class TestGetTransactionTrends:
    def test_returns_correct_length(self, test_db):
        result = get_transaction_trends(7)
        assert len(result) == 7

    def test_custom_days(self, test_db):
        result = get_transaction_trends(14)
        assert len(result) == 14

    def test_all_days_have_required_keys(self, test_db):
        result = get_transaction_trends(3)
        for day in result:
            assert 'day' in day
            assert 'received' in day
            assert 'dispatched' in day

    def test_missing_days_filled_with_zeros(self, test_db):
        result = get_transaction_trends(5)
        for day in result:
            assert isinstance(day['received'], int)
            assert isinstance(day['dispatched'], int)

    def test_legacy_scan_actions_are_not_counted(self, test_db):
        # Trends chart the pipeline's gate actions; the legacy single-reader
        # toggle must not inflate them.
        _seed_scan_txn('item-001', 'scan_in', 1, days_ago=0)
        _seed_scan_txn('item-001', 'scan_out', -1, days_ago=0)
        today = get_transaction_trends(1)[-1]
        assert today['received'] == 0
        assert today['dispatched'] == 0


class TestGetAbcAnalysis:
    def test_returns_dict(self, test_db):
        result = get_abc_analysis()
        assert isinstance(result, dict)

    def test_empty_when_no_transactions(self, test_db):
        result = get_abc_analysis()
        assert result == {}

    def test_classifies_items(self, test_db):
        for _ in range(10):
            _seed_scan_txn('item-001', 'warehouse_dispatch', -1, days_ago=1)
        _seed_scan_txn('item-002', 'warehouse_dispatch', -1, days_ago=1)
        result = get_abc_analysis()
        assert 'item-001' in result
        assert result['item-001']['class'] in ('A', 'B', 'C')

    def test_high_volume_is_class_a(self, test_db):
        # item-001 moves far more volume than the rest
        for _ in range(20):
            _seed_scan_txn('item-001', 'warehouse_dispatch', -1)
        _seed_scan_txn('item-002', 'warehouse_dispatch', -1)
        _seed_scan_txn('item-003', 'warehouse_dispatch', -1)
        _seed_scan_txn('item-004', 'warehouse_dispatch', -1)
        _seed_scan_txn('item-005', 'warehouse_dispatch', -1)
        result = get_abc_analysis()
        assert result['item-001']['class'] == 'A'


class TestGetInventorySummary:
    def test_returns_required_keys(self, test_db):
        result = get_inventory_summary()
        for key in ('total_items', 'healthy', 'low_stock', 'out_of_stock',
                    'dead_stock', 'health_score', 'today_scans', 'tags'):
            assert key in result

    def test_health_score_between_0_and_100(self, test_db):
        result = get_inventory_summary()
        assert 0 <= result['health_score'] <= 100

    def test_tags_dict_has_subtotals(self, test_db):
        result = get_inventory_summary()
        for key in ('out', 'in', 'consumed', 'total'):
            assert key in result['tags']


class TestGetPipelineSummary:
    def test_returns_required_keys(self, test_db):
        result = get_pipeline_summary()
        for key in ('totals', 'per_item', 'rack_stats', 'jobs'):
            assert key in result

    def test_totals_is_dict(self, test_db):
        assert isinstance(get_pipeline_summary()['totals'], dict)

    def test_per_item_is_list(self, test_db):
        assert isinstance(get_pipeline_summary()['per_item'], list)

    def test_rack_stats_is_list(self, test_db):
        assert isinstance(get_pipeline_summary()['rack_stats'], list)

    def test_jobs_is_list(self, test_db):
        assert isinstance(get_pipeline_summary()['jobs'], list)


class TestAnalyticsEndpoints:
    def test_get_analytics_returns_200(self, viewer_client):
        r = viewer_client.get('/api/analytics')
        assert r.status_code == 200
        assert isinstance(r.get_json(), list)

    def test_analytics_summary_returns_200(self, viewer_client):
        r = viewer_client.get('/api/analytics/summary')
        assert r.status_code == 200
        data = r.get_json()
        assert 'total_items' in data

    def test_analytics_trends_returns_200(self, viewer_client):
        r = viewer_client.get('/api/analytics/trends')
        assert r.status_code == 200
        assert isinstance(r.get_json(), list)

    def test_analytics_trends_days_param(self, viewer_client):
        r = viewer_client.get('/api/analytics/trends?days=3')
        assert r.status_code == 200
        assert len(r.get_json()) == 3

    def test_analytics_abc_returns_200(self, viewer_client):
        r = viewer_client.get('/api/analytics/abc')
        assert r.status_code == 200

    def test_analytics_unauthenticated_returns_401(self, client):
        r = client.get('/api/analytics')
        assert r.status_code == 401

    def test_analytics_summary_unauthenticated_returns_401(self, client):
        r = client.get('/api/analytics/summary')
        assert r.status_code == 401

    def test_pipeline_endpoint_returns_200(self, viewer_client):
        r = viewer_client.get('/api/pipeline')
        assert r.status_code == 200

    def test_dashboard_stats_endpoint(self, viewer_client):
        r = viewer_client.get('/api/dashboard')
        assert r.status_code == 200
        data = r.get_json()
        for key in ('total_items', 'total_quantity', 'low_stock_count', 'unread_alerts'):
            assert key in data


# ── Pipeline-driven analytics ─────────────────────────────────────────────────
# The four-station pipeline records warehouse_dispatch / warehouse_receive.
# scan_in / scan_out come only from the legacy single-reader handler, so
# analytics keyed on those actions never see real traffic.

class TestDemandSignal:
    def test_dispatch_drives_daily_usage(self, test_db):
        for d in range(1, 8):
            _seed_scan_txn('item-001', 'warehouse_dispatch', -2, days_ago=d)
        result = get_item_analytics('item-001', 50)
        # 7 days x 2 units over a 30-day zero-filled window
        assert result['avg_daily_usage'] > 0
        assert result['avg_daily_usage'] == pytest.approx(14 / 30, abs=0.01)

    def test_legacy_scan_out_is_not_demand(self, test_db):
        for d in range(1, 8):
            _seed_scan_txn('item-001', 'scan_out', -2, days_ago=d)
        result = get_item_analytics('item-001', 50)
        assert result['avg_daily_usage'] == 0.0

    def test_daily_usage_is_zero_filled(self, test_db):
        _seed_scan_txn('item-001', 'warehouse_dispatch', -3, days_ago=0)
        _seed_scan_txn('item-001', 'warehouse_dispatch', -1, days_ago=5)
        usage = _get_daily_usage('item-001', days=30)
        assert len(usage) == 30
        assert [u['used'] for u in usage].count(0) == 28
        assert usage[-1]['used'] == 3          # today
        assert usage[-6]['used'] == 1          # five days ago

    def test_gradient_boosting_runs_on_dispatch_history(self, test_db):
        for d in range(1, 15):
            _seed_scan_txn('item-001', 'warehouse_dispatch', -(d % 4 + 1), days_ago=d)
        result = get_item_analytics('item-001', 100)
        assert result['forecast_method'] == 'gradient_boosting'

    def test_sparse_history_stays_on_smoothing(self, test_db):
        # One active day is not enough to justify claiming an ML forecast.
        _seed_scan_txn('item-001', 'warehouse_dispatch', -1, days_ago=1)
        result = get_item_analytics('item-001', 100)
        assert result['forecast_method'] == 'exponential_smoothing'


class TestTrendsUsePipelineActions:
    def test_counts_warehouse_receive_and_dispatch(self, test_db):
        _seed_scan_txn('item-001', 'warehouse_receive', 1, days_ago=0)
        _seed_scan_txn('item-001', 'warehouse_dispatch', -1, days_ago=0)
        today = get_transaction_trends(1)[-1]
        assert today['received'] == 1
        assert today['dispatched'] == 1


class TestAbcIsCumulativePareto:
    def test_uses_pipeline_actions(self, test_db):
        _seed_scan_txn('item-001', 'warehouse_dispatch', -5, days_ago=1)
        result = get_abc_analysis()
        assert 'item-001' in result

    def test_dominant_item_is_class_a(self, test_db):
        _seed_scan_txn('item-001', 'warehouse_dispatch', -90, days_ago=1)
        for iid in ('item-002', 'item-003', 'item-004', 'item-005'):
            _seed_scan_txn(iid, 'warehouse_dispatch', -1, days_ago=1)
        result = get_abc_analysis()
        assert result['item-001']['class'] == 'A'
        # The long tail past 95% cumulative volume is C, never A.
        assert result['item-005']['class'] == 'C'

    def test_ranks_by_volume_not_row_count(self, test_db):
        # item-002 has more rows but far less volume.
        _seed_scan_txn('item-001', 'warehouse_dispatch', -100, days_ago=1)
        for _ in range(5):
            _seed_scan_txn('item-002', 'warehouse_dispatch', -1, days_ago=1)
        result = get_abc_analysis()
        assert result['item-001']['volume'] == 100
        assert result['item-002']['volume'] == 5
        assert result['item-001']['class'] == 'A'


class TestPerItemAnomalyRule:
    """Isolation Forest flags CONTAMINATION of every window by construction, so
    rolling raw flags up per item marks everything as anomalous once there is
    real traffic. An item must beat the rate chance would give it."""

    def _fake(self, monkeypatch, anomalies, scans):
        monkeypatch.setattr(analytics, 'detect_scan_anomalies',
                            lambda: [{'item_id': i} for i in anomalies])
        rows = []
        for item_id, n in scans.items():
            rows.extend({'item_id': item_id} for _ in range(n))
        monkeypatch.setattr(analytics, '_recent_transactions', lambda limit=None: rows)

    def test_single_anomaly_on_a_busy_item_is_noise(self, monkeypatch):
        # 200 scans at 5% contamination: 10 flags are expected by chance, so one
        # is not evidence of anything.
        self._fake(monkeypatch, ['item-001'], {'item-001': 200})
        assert analytics._anomaly_item_ids() == set()

    def test_anomalies_above_the_expected_rate_are_surfaced(self, monkeypatch):
        self._fake(monkeypatch, ['item-001'] * 15, {'item-001': 200})
        assert analytics._anomaly_item_ids() == {'item-001'}

    def test_quiet_item_needs_at_least_two(self, monkeypatch):
        # 10 scans expects 0.5 flags, but a lone outlier still should not trip it.
        self._fake(monkeypatch, ['item-002'], {'item-002': 10})
        assert analytics._anomaly_item_ids() == set()
        self._fake(monkeypatch, ['item-002', 'item-002'], {'item-002': 10})
        assert analytics._anomaly_item_ids() == {'item-002'}

    def test_no_anomalies_flags_nothing(self, monkeypatch):
        self._fake(monkeypatch, [], {'item-001': 100})
        assert analytics._anomaly_item_ids() == set()

    def test_busy_and_quiet_items_judged_separately(self, monkeypatch):
        # Same raw count, different volumes: only the quiet one stands out.
        self._fake(monkeypatch, ['busy', 'busy', 'quiet', 'quiet'],
                   {'busy': 400, 'quiet': 12})
        assert analytics._anomaly_item_ids() == {'quiet'}


class TestTagStateSummary:
    """The Overview doughnut buckets tags as In Warehouse / With Product /
    Consumed. Those names predate the pipeline; the query used to count only the
    legacy in/out/consumed states, so every pipeline tag reported as zero and the
    chart rendered empty."""

    def _seed_tag(self, uid, state):
        conn = get_db()
        conn.execute("INSERT INTO rfid_tags (uid, item_id, state) VALUES (?, 'item-001', ?)",
                     (uid, state))
        conn.commit()
        conn.close()

    def test_pipeline_states_are_counted(self, test_db):
        for uid, state in [('T-RACK', 'racked'), ('T-RECV', 'received'),
                           ('T-RET', 'returned'), ('T-TAG', 'tagged'),
                           ('T-TRAN', 'in_transit'), ('T-PICK', 'picked'),
                           ('T-DISP', 'dispatched')]:
            self._seed_tag(uid, state)

        tags = get_inventory_summary()['tags']
        assert tags['in'] == 3, 'racked, received and returned are in the warehouse'
        assert tags['out'] == 3, 'tagged, in_transit and picked are out with product'
        assert tags['consumed'] == 1, 'dispatched has left the building'

    def test_legacy_states_still_counted(self, test_db):
        for uid, state in [('L-IN', 'in'), ('L-OUT', 'out'), ('L-CONS', 'consumed')]:
            self._seed_tag(uid, state)
        tags = get_inventory_summary()['tags']
        assert tags['in'] == 1 and tags['out'] == 1 and tags['consumed'] == 1

    def test_total_counts_every_tag(self, test_db):
        for uid, state in [('X-1', 'racked'), ('X-2', 'dispatched'),
                           ('X-3', 'some_future_state')]:
            self._seed_tag(uid, state)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM rfid_tags')
        rows = c.fetchone()[0]
        conn.close()
        # Total is a straight row count, so an unbucketed state cannot make the
        # chart silently under-report the fleet.
        assert get_inventory_summary()['tags']['total'] == rows


class TestAnomalyWindow:
    def test_reads_the_most_recent_transactions(self, test_db):
        for i in range(12):
            _seed_scan_txn('item-001', 'warehouse_dispatch', -1, days_ago=20 - i)
        rows = _recent_transactions(limit=5)
        assert len(rows) == 5
        stamps = [r['timestamp'] for r in rows]
        assert stamps == sorted(stamps), 'rows must be chronological for the gap feature'
        newest_first = _recent_transactions(limit=12)
        assert stamps[-1] == newest_first[-1]['timestamp'], 'must end at the newest row'

    def test_window_is_not_the_oldest_rows(self, test_db):
        # Compared against independent SQL, not against another call to the
        # function under test - a self-comparison stays true either way.
        for i in range(12):
            _seed_scan_txn('item-00%d' % (i % 8 + 1), 'warehouse_dispatch', -1, days_ago=20 - i)
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id FROM transactions ORDER BY timestamp DESC, id DESC LIMIT 4')
        newest = sorted(r['id'] for r in c.fetchall())
        c.execute('SELECT id FROM transactions ORDER BY timestamp ASC, id ASC LIMIT 4')
        oldest = sorted(r['id'] for r in c.fetchall())
        conn.close()
        assert newest != oldest, 'fixture must distinguish newest from oldest'

        window = sorted(r['id'] for r in _recent_transactions(limit=4))
        assert window == newest
