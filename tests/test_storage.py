import http.client
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
