"""Tests for the LLM assistant: tool layer, provider registry, and endpoint.

No test makes a network call. The tool layer is exercised against the real
temporary database; the endpoint tests monkeypatch assistant.ask, so the suite
never needs an API key or either optional SDK.
"""
import json

import pytest

import app as flask_app
import assistant
import assistant_tools as tools
from database import get_db


def _seed_txn(item_id, action, qty_change, days_ago=0, device='test-dev'):
    from datetime import datetime, timedelta
    conn = get_db()
    ts = (datetime.now() - timedelta(days=days_ago)).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        """INSERT INTO transactions (item_id, action, quantity_change, previous_quantity,
           new_quantity, performed_by, device_id, timestamp)
           VALUES (?, ?, ?, 10, 9, 'test', ?, ?)""",
        (item_id, action, qty_change, device, ts))
    conn.commit()
    conn.close()


# ── Tool layer ────────────────────────────────────────────────────────────────

class TestListInventory:
    def test_returns_items(self, test_db):
        data = json.loads(tools.list_inventory())
        assert data['count'] > 0
        assert {'id', 'name', 'quantity'} <= set(data['items'][0])

    def test_low_stock_only_filters(self, test_db):
        every = json.loads(tools.list_inventory())
        low = json.loads(tools.list_inventory(low_stock_only=True))
        assert low['count'] <= every['count']
        for item in low['items']:
            assert item['quantity'] <= item['low_stock_threshold']


class TestGetDemandForecast:
    def test_known_item(self, test_db):
        data = json.loads(tools.get_demand_forecast('item-001'))
        assert data['item_id'] == 'item-001'
        for key in ('forecast_demand', 'forecast_method', 'eoq', 'days_remaining'):
            assert key in data

    def test_unknown_item_reports_error(self, test_db):
        data = json.loads(tools.get_demand_forecast('does-not-exist'))
        assert 'error' in data


class TestSearchTransactions:
    def test_filters_by_item(self, test_db):
        _seed_txn('item-001', 'warehouse_dispatch', -1)
        _seed_txn('item-002', 'warehouse_dispatch', -1)
        data = json.loads(tools.search_transactions(item_id='item-001'))
        assert data['count'] >= 1
        assert {t['item_id'] for t in data['transactions']} == {'item-001'}

    def test_filters_by_action(self, test_db):
        _seed_txn('item-001', 'warehouse_dispatch', -1)
        _seed_txn('item-001', 'warehouse_receive', 1)
        data = json.loads(tools.search_transactions(action='warehouse_receive'))
        assert {t['action'] for t in data['transactions']} == {'warehouse_receive'}

    def test_limit_is_capped(self, test_db):
        for _ in range(5):
            _seed_txn('item-001', 'warehouse_dispatch', -1)
        data = json.loads(tools.search_transactions(limit=99999))
        assert data['count'] <= tools.ROW_LIMIT

    def test_newest_first(self, test_db):
        _seed_txn('item-001', 'warehouse_dispatch', -1, days_ago=5)
        _seed_txn('item-001', 'warehouse_receive', 1, days_ago=0)
        data = json.loads(tools.search_transactions(item_id='item-001'))
        stamps = [t['timestamp'] for t in data['transactions']]
        assert stamps == sorted(stamps, reverse=True)

    def test_sql_is_parameterised(self, test_db):
        # A quoted payload must be treated as a literal id, not executed.
        payload = "item-001' OR '1'='1"
        data = json.loads(tools.search_transactions(item_id=payload))
        assert data['count'] == 0
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM items")
        assert c.fetchone()[0] > 0, 'items table must survive the injection attempt'
        conn.close()


class TestListAlerts:
    def test_unread_filter(self, test_db):
        conn = get_db()
        conn.execute("INSERT INTO alerts (item_id, alert_type, message, is_read) "
                     "VALUES ('item-001', 'security', 'unread one', 0)")
        conn.execute("INSERT INTO alerts (item_id, alert_type, message, is_read) "
                     "VALUES ('item-001', 'security', 'read one', 1)")
        conn.commit()
        conn.close()

        unread = json.loads(tools.list_alerts(unread_only=True))
        assert all(a['is_read'] == 0 for a in unread['alerts'])
        every = json.loads(tools.list_alerts(unread_only=False))
        assert every['count'] > unread['count']


class TestPipelineStatus:
    def test_returns_stage_counts(self, test_db):
        data = json.loads(tools.get_pipeline_status())
        assert isinstance(data, dict)


class TestToolsAreReadOnly:
    def test_no_tool_mutates_state(self, test_db):
        _seed_txn('item-001', 'warehouse_dispatch', -1)

        def snapshot():
            conn = get_db()
            c = conn.cursor()
            c.execute('SELECT id, quantity FROM items ORDER BY id')
            items = c.fetchall()
            c.execute('SELECT COUNT(*) FROM transactions')
            txns = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM rfid_tags')
            tags = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM alerts')
            alerts = c.fetchone()[0]
            conn.close()
            return ([tuple(r) for r in items], txns, tags, alerts)

        before = snapshot()
        tools.list_inventory()
        tools.list_inventory(low_stock_only=True)
        tools.get_demand_forecast('item-001')
        tools.search_transactions(item_id='item-001')
        tools.list_alerts(unread_only=False)
        tools.get_pipeline_status()
        assert snapshot() == before

    def test_tool_registry_matches_exported_names(self, test_db):
        assert set(tools.TOOLS_BY_NAME) == {fn.__name__ for fn in tools.TOOLS}
        assert 'list_inventory' in tools.TOOLS_BY_NAME


# ── Input handling ────────────────────────────────────────────────────────────

class TestInputHandling:
    def test_blank_question_rejected(self):
        for bad in ('', '   ', None, 42):
            with pytest.raises(ValueError):
                tools.validate_question(bad)

    def test_overlong_question_rejected(self):
        with pytest.raises(ValueError):
            tools.validate_question('x' * (tools.MAX_QUESTION_CHARS + 1))

    def test_question_is_stripped(self):
        assert tools.validate_question('  how many left?  ') == 'how many left?'

    def test_history_drops_malformed_turns(self):
        cleaned = tools.clean_history([
            {'role': 'user', 'content': 'ok'},
            {'role': 'system', 'content': 'not allowed'},
            {'role': 'assistant', 'content': ''},
            'not a dict',
            {'role': 'assistant', 'content': 'fine'},
        ])
        assert cleaned == [{'role': 'user', 'content': 'ok'},
                           {'role': 'assistant', 'content': 'fine'}]

    def test_history_is_truncated(self):
        long_history = [{'role': 'user', 'content': 'q%d' % i} for i in range(60)]
        assert len(tools.clean_history(long_history)) == tools.MAX_HISTORY_TURNS

    def test_history_content_is_length_capped(self):
        cleaned = tools.clean_history([{'role': 'user', 'content': 'x' * 9000}])
        assert len(cleaned[0]['content']) == tools.MAX_QUESTION_CHARS


# ── Provider registry ─────────────────────────────────────────────────────────

class TestProviderRegistry:
    def test_defaults_to_gemini(self, monkeypatch):
        monkeypatch.delenv('ASSISTANT_PROVIDER', raising=False)
        assert assistant.provider_name() == 'gemini'

    def test_provider_is_configurable(self, monkeypatch):
        monkeypatch.setenv('ASSISTANT_PROVIDER', 'anthropic')
        assert assistant.provider_name() == 'anthropic'

    def test_unknown_provider_is_unavailable(self, monkeypatch):
        monkeypatch.setenv('ASSISTANT_PROVIDER', 'nope')
        assert assistant.is_available() is False
        assert 'nope' in assistant.unavailable_reason()

    def test_ask_raises_when_unconfigured(self, monkeypatch):
        monkeypatch.setenv('ASSISTANT_PROVIDER', 'nope')
        with pytest.raises(assistant.AssistantUnavailable):
            assistant.ask('anything')

    def test_status_reports_tools(self, monkeypatch):
        monkeypatch.setenv('ASSISTANT_PROVIDER', 'gemini')
        monkeypatch.delenv('GEMINI_API_KEY', raising=False)
        monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
        st = assistant.status()
        assert st['provider'] == 'gemini'
        assert st['available'] is False
        assert st['reason']
        assert 'list_inventory' in st['tools']

    def test_both_adapters_share_one_interface(self):
        import assistant_anthropic
        import assistant_gemini
        for mod in (assistant_gemini, assistant_anthropic):
            for attr in ('NAME', 'ask', 'is_available', 'unavailable_reason'):
                assert hasattr(mod, attr), '%s missing %s' % (mod.__name__, attr)


# ── Endpoint ──────────────────────────────────────────────────────────────────

@pytest.fixture
def stub_ask(monkeypatch):
    """Replace the model call with a recorder - no SDK, no network, no key."""
    calls = []

    def _fake(question, history=None):
        calls.append({'question': question, 'history': history})
        return {'answer': 'Two items are below threshold.',
                'tools_used': ['list_inventory'],
                'model': 'stub-model',
                'provider': 'stub'}

    monkeypatch.setattr(assistant, 'ask', _fake)
    return calls


class TestAssistantEndpoint:
    def test_requires_login(self, client):
        r = client.post('/api/assistant', json={'question': 'hi'})
        assert r.status_code == 401

    def test_status_requires_login(self, client):
        assert client.get('/api/assistant').status_code == 401

    def test_status_shape(self, admin_client):
        body = admin_client.get('/api/assistant').get_json()
        assert 'available' in body and 'provider' in body and 'tools' in body

    def test_viewer_may_ask(self, viewer_client, stub_ask):
        # Read-only by construction, so the read-only role is allowed.
        r = viewer_client.post('/api/assistant', json={'question': 'what is low?'})
        assert r.status_code == 200
        assert r.get_json()['answer']

    def test_successful_answer(self, admin_client, stub_ask):
        r = admin_client.post('/api/assistant', json={'question': 'what is low?'})
        body = r.get_json()
        assert r.status_code == 200
        assert body['status'] == 'ok'
        assert body['tools_used'] == ['list_inventory']
        assert stub_ask[0]['question'] == 'what is low?'

    def test_history_is_forwarded(self, admin_client, stub_ask):
        history = [{'role': 'user', 'content': 'earlier'}]
        admin_client.post('/api/assistant',
                          json={'question': 'and now?', 'history': history})
        assert stub_ask[0]['history'] == history

    def test_missing_question_is_400(self, admin_client):
        r = admin_client.post('/api/assistant', json={})
        assert r.status_code == 400
        assert 'error' in r.get_json()

    def test_overlong_question_is_400(self, admin_client):
        r = admin_client.post('/api/assistant',
                              json={'question': 'x' * (tools.MAX_QUESTION_CHARS + 1)})
        assert r.status_code == 400

    def test_non_list_history_is_400(self, admin_client):
        r = admin_client.post('/api/assistant',
                              json={'question': 'hi', 'history': 'nope'})
        assert r.status_code == 400

    def test_unconfigured_is_503(self, admin_client, monkeypatch):
        def _boom(question, history=None):
            raise assistant.AssistantUnavailable('no key')
        monkeypatch.setattr(assistant, 'ask', _boom)
        r = admin_client.post('/api/assistant', json={'question': 'hi'})
        assert r.status_code == 503
        assert 'no key' in r.get_json()['error']

    def test_provider_failure_is_502_without_leaking_detail(self, admin_client, monkeypatch):
        def _boom(question, history=None):
            raise assistant.AssistantError('quota exceeded for project 12345')
        monkeypatch.setattr(assistant, 'ask', _boom)
        r = admin_client.post('/api/assistant', json={'question': 'hi'})
        assert r.status_code == 502
        assert '12345' not in r.get_json()['error'], 'must not leak provider internals'


# ── SDK contract ──────────────────────────────────────────────────────────────
# These run only when the optional SDK is installed. They make no network call:
# both SDKs build their schema by introspecting the function signature and
# docstring, so a change that breaks that contract fails here rather than at the
# first live question.

class TestToolSchemasAreSdkCompatible:
    def test_gemini_can_declare_every_tool(self):
        genai = pytest.importorskip('google.genai')
        from google.genai import types as gtypes
        api = genai.Client(api_key='offline-schema-check')._api_client
        for fn in tools.TOOLS:
            decl = gtypes.FunctionDeclaration.from_callable(callable=fn, client=api)
            assert decl.name == fn.__name__
            assert decl.description, '%s needs a docstring' % fn.__name__

    def test_gemini_marks_only_untyped_defaults_optional(self):
        genai = pytest.importorskip('google.genai')
        from google.genai import types as gtypes
        api = genai.Client(api_key='offline-schema-check')._api_client
        decl = gtypes.FunctionDeclaration.from_callable(
            callable=tools.get_demand_forecast, client=api)
        params = decl.model_dump(exclude_none=True).get('parameters') or {}
        assert params.get('required') == ['item_id']

    def test_anthropic_can_wrap_every_tool(self):
        pytest.importorskip('anthropic')
        from anthropic import beta_tool
        for fn in tools.TOOLS:
            schema = beta_tool(fn).to_dict()
            assert schema['name'] == fn.__name__
            assert schema['description']
            assert schema['input_schema']['type'] == 'object'
