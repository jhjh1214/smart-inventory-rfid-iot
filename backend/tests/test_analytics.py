"""Tests for analytics endpoints and analytics.py functions."""
import pytest
from database import get_db
from analytics import (
    get_item_analytics, get_all_analytics, get_transaction_trends,
    get_abc_analysis, get_inventory_summary, get_pipeline_summary,
    _exponential_smoothing, _eoq, _risk_score,
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

    def test_no_history_defaults(self, test_db):
        result = get_item_analytics('item-001', 5)
        assert result['avg_daily_usage'] == 1.0

    def test_with_scan_history(self, test_db):
        _seed_scan_txn('item-001', 'scan_out', -2, days_ago=1)
        result = get_item_analytics('item-001', 10)
        assert result['avg_daily_usage'] >= 0

    def test_daily_history_limited_to_7(self, test_db):
        for i in range(10):
            _seed_scan_txn('item-001', 'scan_out', -1, days_ago=i)
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

    def test_counts_scan_in_and_out(self, test_db):
        _seed_scan_txn('item-001', 'scan_in', 1, days_ago=0)
        _seed_scan_txn('item-001', 'scan_out', -1, days_ago=0)
        result = get_transaction_trends(1)
        today = result[-1]
        assert today['received'] >= 1
        assert today['dispatched'] >= 1


class TestGetAbcAnalysis:
    def test_returns_dict(self, test_db):
        result = get_abc_analysis()
        assert isinstance(result, dict)

    def test_empty_when_no_transactions(self, test_db):
        result = get_abc_analysis()
        assert result == {}

    def test_classifies_items(self, test_db):
        for _ in range(10):
            _seed_scan_txn('item-001', 'scan_out', -1, days_ago=1)
        _seed_scan_txn('item-002', 'scan_out', -1, days_ago=1)
        result = get_abc_analysis()
        assert 'item-001' in result
        assert result['item-001']['class'] in ('A', 'B', 'C')

    def test_high_volume_is_class_a(self, test_db):
        # item-001 gets many txns, item-002 few
        for _ in range(20):
            _seed_scan_txn('item-001', 'scan_out', -1)
        _seed_scan_txn('item-002', 'scan_out', -1)
        _seed_scan_txn('item-003', 'scan_out', -1)
        _seed_scan_txn('item-004', 'scan_out', -1)
        _seed_scan_txn('item-005', 'scan_out', -1)
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
