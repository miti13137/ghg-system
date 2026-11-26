import http.client
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from http.server import HTTPServer

ROOT = Path(__file__).resolve().parents[1]

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ['LOCALAPPDATA'] = self.temp.name
        spec = importlib.util.spec_from_file_location('ghg_server_test', ROOT / 'local_server.py')
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        self.server = HTTPServer(('127.0.0.1', 0), self.app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.data = {'settings': {'year': 2026}, 'sources': [], 'factors': [], 'records': [], 'evidence': [], 'audit': []}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        h = {'X-GHG-Token': self.app.TOKEN}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        conn.close()
        return result

    def test_stale_write_and_backup(self):
        status, headers, data = self.request('GET', '/api/inventory')
        self.assertEqual((status, data), (200, b'null'))
        rev0 = headers['X-GHG-Revision']
        body = json.dumps(self.data)
        status, headers, _ = self.request('PUT', '/api/inventory', body, {'X-GHG-Revision': rev0})
        self.assertEqual(status, 200)
        rev1 = headers['X-GHG-Revision']
        self.data['settings']['company'] = '模擬公司'
        status, _, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': rev0})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(self.request('GET', '/api/inventory')[2])['settings'], {'year': 2026})
        status, _, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': rev1})
        self.assertEqual(status, 200)
        copies = list(self.app.BACKUPS.glob('*.sqlite3'))
        self.assertEqual(len(copies), 1)
        with sqlite3.connect(copies[0]) as con:
            previous = json.loads(con.execute('select payload from inventory').fetchone()[0])
            self.assertEqual(previous['settings'], {'year': 2026})
            self.assertEqual(con.execute('pragma integrity_check').fetchone()[0], 'ok')

    def test_backup_list_restore_and_stale_rejection(self):
        _, hdr, _ = self.request('GET', '/api/inventory')
        initial = hdr['X-GHG-Revision']
        status, hdr, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': initial})
        self.assertEqual(status, 200)
        v1 = hdr['X-GHG-Revision']
        status, _, _ = self.request('POST', '/api/backups/create', headers={'X-GHG-Revision': v1})
        self.assertEqual(status, 200)
        status, _, raw = self.request('GET', '/api/backups')
        self.assertEqual(status, 200)
        name = json.loads(raw)[0]
        self.data['settings']['company'] = '模擬第二版'
        status, hdr, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': v1})
        self.assertEqual(status, 200)
        v2 = hdr['X-GHG-Revision']
        status, _, _ = self.request('POST', '/api/backups/restore', json.dumps({'name': name}), {'X-GHG-Revision': v1})
        self.assertEqual(status, 409)
        status, _, _ = self.request('POST', '/api/backups/restore', json.dumps({'name': '../../inventory.sqlite3'}), {'X-GHG-Revision': v2})
        self.assertEqual(status, 400)
        status, _, _ = self.request('POST', '/api/backups/restore', json.dumps({'name': name}), {'X-GHG-Revision': v2})
        self.assertEqual(status, 200)
        restored = json.loads(self.request('GET', '/api/inventory')[2])
        self.assertEqual(restored['settings'], {'year': 2026})
        copies = list(self.app.BACKUPS.glob('*.sqlite3'))
        self.assertGreaterEqual(len(copies), 2)
        snapshots = []
        for copy in copies:
            with sqlite3.connect(copy) as con:
                snapshots.append(json.loads(con.execute('select payload from inventory').fetchone()[0]))
        self.assertTrue(any(x['settings'].get('company') == '模擬第二版' for x in snapshots))
        self.assertEqual(self.request('GET', '/api/backups', headers={'X-GHG-Token':'bad'})[0], 404)

    def test_evidence_upload_download_and_backup(self):
        self.data['records'] = [{'id': 'mock-record', 'month': '2026-01'}]
        _, hdr, _ = self.request('GET', '/api/inventory')
        status, hdr, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': hdr['X-GHG-Revision']})
        self.assertEqual(status, 200)
        old_revision = hdr['X-GHG-Revision']
        data = b'simulated evidence file'
        upload = {'recordId': 'mock-record', 'filename': '模擬.pdf', 'name': '模擬電費單', 'note': '測試', 'content': base64.b64encode(data).decode()}
        status, hdr, _ = self.request('POST', '/api/evidence/upload', json.dumps(upload), {'X-GHG-Revision': old_revision})
        self.assertEqual(status, 200)
        current_revision = hdr['X-GHG-Revision']
        status, _, raw = self.request('GET', '/api/inventory')
        evidence = json.loads(raw)['evidence'][0]
        file_id = evidence['fileId']
        self.assertEqual(evidence['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.request('GET', '/api/evidence/' + file_id)[2], data)
        self.assertEqual(self.request('GET', '/api/evidence/' + file_id, headers={'X-GHG-Token':'bad'})[0], 404)
        status, _, _ = self.request('POST', '/api/evidence/upload', json.dumps(upload), {'X-GHG-Revision': old_revision})
        self.assertEqual(status, 409)
        self.assertEqual(len(json.loads(self.request('GET', '/api/inventory')[2])['evidence']), 1)
        status, _, _ = self.request('POST', '/api/backups/create', headers={'X-GHG-Revision': current_revision})
        self.assertEqual(status, 200)
        names = json.loads(self.request('GET', '/api/backups')[2])
        found = False
        for name in names:
            with sqlite3.connect(self.app.BACKUPS / name) as con:
                item = con.execute('select content from evidence_files where id=?', (file_id,)).fetchone()
                if item and item[0] == data: found = True
        self.assertTrue(found)
        with sqlite3.connect(self.app.DB) as con:
            con.execute('update evidence_files set content=? where id=?', (b'corrupt', file_id))
        self.assertEqual(self.request('GET', '/api/evidence/' + file_id)[0], 500)
        for name in names:
            with sqlite3.connect(self.app.BACKUPS / name) as con:
                item = con.execute('select content from evidence_files where id=?', (file_id,)).fetchone()
                if item and item[0] == data:
                    chosen = name
                    break
        status, _, _ = self.request('POST', '/api/backups/restore', json.dumps({'name': chosen}), {'X-GHG-Revision': current_revision})
        self.assertEqual(status, 200)
        self.assertEqual(self.request('GET', '/api/evidence/' + file_id)[2], data)

    def test_evidence_validation_keeps_data_unchanged(self):
        self.data['records'] = [{'id': 'mock-record'}]
        _, h, _ = self.request('GET', '/api/inventory')
        _, h, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': h['X-GHG-Revision']})
        revision = h['X-GHG-Revision']
        upload = {'recordId': 'missing', 'filename': 'test.pdf', 'name':'test', 'note':'', 'content':base64.b64encode(b'test').decode()}
        self.assertEqual(self.request('POST', '/api/evidence/upload', json.dumps(upload), {'X-GHG-Revision': revision})[0], 400)
        upload['recordId'] = 'mock-record'
        upload['filename'] = '../test.pdf'
        self.assertEqual(self.request('POST', '/api/evidence/upload', json.dumps(upload), {'X-GHG-Revision': revision})[0], 400)
        self.assertEqual(json.loads(self.request('GET', '/api/inventory')[2])['evidence'], [])

    def test_offline_excel_endpoints(self):
        matrix = [['名稱', '數值'], ['模擬資料', '12']]
        status, headers, workbook = self.request('POST', '/api/xlsx/write', json.dumps(matrix).encode())
        self.assertEqual(status, 200)
        self.assertIn('spreadsheetml', headers['Content-Type'])
        status, _, content = self.request('POST', '/api/xlsx/read', workbook)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content), matrix)
        self.assertEqual(self.request('POST', '/api/xlsx/read', workbook, {'X-GHG-Token':'bad'})[0], 403)
        self.assertEqual(self.request('POST', '/api/xlsx/read', b'invalid')[0], 400)

    def test_access_validation_and_offline_html(self):
        status, _, html = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertNotIn(b'cdn.jsdelivr.net', html)
        self.assertIn(b'X-GHG-Revision', html)
        self.assertEqual(self.request('GET', '/api/inventory', headers={'X-GHG-Token': 'bad'})[0], 404)
        status, h, _ = self.request('GET', '/api/inventory')
        self.assertEqual(self.request('PUT', '/api/inventory', '{}', {'X-GHG-Revision': h['X-GHG-Revision']})[0], 400)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.example'})[0], 403)

if __name__ == '__main__':
    unittest.main()
