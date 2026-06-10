"""Tests for CSV export/import and DB backup endpoints."""
import io
import pytest
from database import get_db


class TestExportItemsCSV:
    def test_viewer_can_export(self, viewer_client):
        r = viewer_client.get('/api/export/items')
        assert r.status_code == 200

    def test_content_type_is_csv(self, viewer_client):
        r = viewer_client.get('/api/export/items')
        assert 'text/csv' in r.content_type

    def test_has_attachment_header(self, viewer_client):
        r = viewer_client.get('/api/export/items')
        assert 'attachment' in r.headers.get('Content-Disposition', '')

    def test_has_header_row(self, viewer_client):
        r = viewer_client.get('/api/export/items')
        lines = r.data.decode().splitlines()
        assert lines[0].startswith('id,')

    def test_contains_seeded_items(self, viewer_client):
        r = viewer_client.get('/api/export/items')
        content = r.data.decode()
        assert 'item-001' in content

    def test_row_count_matches_items(self, viewer_client):
        r = viewer_client.get('/api/export/items')
        lines = [l for l in r.data.decode().splitlines() if l.strip()]
        # 1 header + N items
        items = viewer_client.get('/api/items').get_json()
        assert len(lines) == 1 + len(items)

    def test_unauthenticated_returns_401(self, client):
        r = client.get('/api/export/items')
        assert r.status_code == 401

    def test_columns_include_reserved_qty(self, viewer_client):
        r = viewer_client.get('/api/export/items')
        header = r.data.decode().splitlines()[0]
        assert 'reserved_qty' in header


class TestExportBackup:
    def test_admin_can_download_backup(self, admin_client):
        r = admin_client.get('/api/export/backup')
        assert r.status_code == 200

    def test_content_type_is_binary(self, admin_client):
        r = admin_client.get('/api/export/backup')
        assert r.content_type == 'application/octet-stream'

    def test_has_attachment_header(self, admin_client):
        r = admin_client.get('/api/export/backup')
        assert 'attachment' in r.headers.get('Content-Disposition', '')

    def test_filename_ends_with_db(self, admin_client):
        r = admin_client.get('/api/export/backup')
        cd = r.headers.get('Content-Disposition', '')
        assert '.db' in cd

    def test_viewer_cannot_download_backup(self, viewer_client):
        r = viewer_client.get('/api/export/backup')
        assert r.status_code == 403

    def test_manager_cannot_download_backup(self, manager_client):
        r = manager_client.get('/api/export/backup')
        assert r.status_code == 403


class TestImportItemsCSV:
    def _csv_bytes(self, rows):
        out = io.StringIO()
        out.write('id,name,quantity,unit,low_stock_threshold\n')
        for r in rows:
            out.write(','.join(str(v) for v in r) + '\n')
        return out.getvalue().encode()

    def test_manager_can_import(self, manager_client):
        data = self._csv_bytes([('import-01', 'Imported Widget', '5', 'pcs', '2')])
        r = manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        assert r.status_code == 200

    def test_viewer_cannot_import(self, viewer_client):
        data = self._csv_bytes([('import-02', 'Widget', '3', 'pcs', '1')])
        r = viewer_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        assert r.status_code == 403

    def test_new_item_created(self, manager_client):
        data = self._csv_bytes([('csv-new-01', 'CSV New Item', '7', 'pcs', '2')])
        manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        items = manager_client.get('/api/items').get_json()
        assert any(i['id'] == 'csv-new-01' for i in items)

    def test_response_includes_created_count(self, manager_client):
        data = self._csv_bytes([('csv-cnt-01', 'Count Item', '3', 'pcs', '1')])
        r = manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        assert r.get_json()['created'] == 1

    def test_existing_item_updated(self, manager_client):
        data = self._csv_bytes([('item-001', 'Updated Name', '99', 'pcs', '3')])
        manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        items = manager_client.get('/api/items').get_json()
        item = next(i for i in items if i['id'] == 'item-001')
        assert item['quantity'] == 99

    def test_response_includes_updated_count(self, manager_client):
        data = self._csv_bytes([('item-001', 'Renamed', '50', 'pcs', '2')])
        r = manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        assert r.get_json()['updated'] == 1

    def test_no_file_returns_400(self, manager_client):
        r = manager_client.post(
            '/api/import/items',
            data={},
            content_type='multipart/form-data',
        )
        assert r.status_code == 400

    def test_non_csv_file_returns_400(self, manager_client):
        r = manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(b'data'), 'items.txt')},
            content_type='multipart/form-data',
        )
        assert r.status_code == 400

    def test_row_missing_id_recorded_in_errors(self, manager_client):
        out = io.StringIO()
        out.write('id,name,quantity,unit,low_stock_threshold\n')
        out.write(',No ID,5,pcs,2\n')
        data = out.getvalue().encode()
        r = manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        assert len(r.get_json()['errors']) >= 1

    def test_import_creates_transaction(self, manager_client):
        data = self._csv_bytes([('csv-txn-01', 'TXN Item', '5', 'pcs', '2')])
        manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        txns = manager_client.get('/api/transactions').get_json()
        assert any(t['item_id'] == 'csv-txn-01' and t['action'] == 'item_added' for t in txns)

    def test_update_creates_manual_adjust_transaction(self, manager_client):
        data = self._csv_bytes([('item-001', 'Widget', '77', 'pcs', '5')])
        manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        txns = manager_client.get('/api/transactions').get_json()
        assert any(t['item_id'] == 'item-001' and t['action'] == 'manual_adjust' for t in txns)

    def test_negative_quantity_clamped_to_zero(self, manager_client):
        data = self._csv_bytes([('neg-csv', 'Neg CSV', '-5', 'pcs', '2')])
        manager_client.post(
            '/api/import/items',
            data={'file': (io.BytesIO(data), 'items.csv')},
            content_type='multipart/form-data',
        )
        items = manager_client.get('/api/items').get_json()
        item = next((i for i in items if i['id'] == 'neg-csv'), None)
        assert item is not None
        assert item['quantity'] == 0
