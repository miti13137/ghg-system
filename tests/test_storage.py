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
        self.cookie = ''
        login = json.dumps({'name': 'test-admin', 'password': 'fixture-password-only'})
        self.assertEqual(self.request('POST', '/api/setup', login)[0], 201)
        status, headers, _ = self.request('POST', '/api/login', login)
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.data = {'settings': {'year': 2026}, 'sources': [], 'factors': [], 'records': [], 'evidence': [], 'audit': []}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        h = {'X-GHG-Token': self.app.TOKEN, 'Cookie': self.cookie}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        conn.close()
        return result

    def create_user_cookie(self, role):
        fields = json.dumps({'name': 'test-' + role, 'password': 'fixture-password-only', 'role': role})
        self.assertEqual(self.request('POST', '/api/users', fields)[0], 201)
        status, headers, _ = self.request('POST', '/api/login', fields, {'Cookie': ''})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';', 1)[0]

    def test_anonymous_and_read_only_access(self):
        status, _, page = self.request('GET', '/', headers={'Cookie': ''})
        self.assertEqual(status, 200)
        self.assertIn(b'id="auth"', page)
        self.assertEqual(self.request('GET', '/api/inventory', headers={'Cookie': ''})[0], 404)
        viewer = self.create_user_cookie('viewer')
        self.assertEqual(self.request('GET', '/api/inventory', headers={'Cookie': viewer})[0], 200)
        self.assertEqual(self.request('PUT', '/api/inventory', '{}', {'Cookie': viewer})[0], 403)
        self.assertEqual(self.request('POST', '/api/users', '{}', {'Cookie': viewer})[0], 403)
        self.assertEqual(self.request('POST', '/api/logout', headers={'Cookie': viewer})[0], 200)
        self.assertEqual(self.request('GET', '/api/inventory', headers={'Cookie': viewer})[0], 404)

    def test_admin_can_disable_an_account_and_end_its_session(self):
        editor = self.create_user_cookie('editor')
        disable = json.dumps({'name': 'test-editor', 'action': 'disable'})
        self.assertEqual(self.request('POST', '/api/users/update', disable, {'Cookie': editor})[0], 403)
        self.assertEqual(self.request('POST', '/api/users/update', disable)[0], 200)
        self.assertEqual(self.request('GET', '/api/inventory', headers={'Cookie': editor})[0], 404)
        users = json.loads(self.request('GET', '/api/users')[2])
        self.assertEqual(next(u for u in users if u['name'] == 'test-editor')['enabled'], False)
        self.assertTrue(all(set(u) == {'name', 'role', 'enabled'} for u in users))

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
        history = json.loads(self.request('GET', '/api/history')[2])
        self.assertTrue(any(x['reason'] == '備份還原' for x in history))
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

    def test_reviewed_records_protected_and_history(self):
        self.data['records'] = [{'id': 'record1', 'status': '待審核', 'qty': 100}]
        _, h, _ = self.request('GET', '/api/inventory')
        _, h, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': h['X-GHG-Revision']})
        pending_revision = h['X-GHG-Revision']
        self.data['records'][0]['status'] = '已審核'
        self.assertEqual(self.request('PUT', '/api/inventory', json.dumps(self.data),
                         {'X-GHG-Revision': pending_revision})[0], 403)
        reviewer = self.create_user_cookie('reviewer')
        self.data['records'][0]['qty'] = 999
        self.assertEqual(self.request('PUT', '/api/inventory', json.dumps(self.data),
                         {'X-GHG-Revision': pending_revision, 'Cookie': reviewer})[0], 403)
        self.data['records'][0]['qty'] = 100
        status, h, _ = self.request('PUT', '/api/inventory', json.dumps(self.data),
                                    {'X-GHG-Revision': pending_revision, 'Cookie': reviewer})
        self.assertEqual(status, 200)
        reviewed_revision = h['X-GHG-Revision']
        initial_history = json.loads(self.request('GET', '/api/history')[2])
        self.assertTrue(any(x['collection'] == 'records' and x['operation'] == '修改' and json.loads(x['after'])['status'] == '已審核' for x in initial_history))
        self.data['records'][0]['qty'] = 999
        self.assertEqual(self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': reviewed_revision})[0], 400)
        self.data['records'] = []
        self.assertEqual(self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': reviewed_revision})[0], 400)
        self.assertEqual(len(json.loads(self.request('GET', '/api/history')[2])), len(initial_history))
        self.assertTrue(any(x['actor'] == 'test-reviewer' for x in initial_history))
        self.data['records'] = [{'id':'record1','status':'退回','qty':100}]
        status, h, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': reviewed_revision, 'Cookie': reviewer})
        self.assertEqual(status, 200)
        self.data['records'][0]['qty'] = 101
        self.assertEqual(self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': h['X-GHG-Revision']})[0], 200)
        history = json.loads(self.request('GET', '/api/history')[2])
        self.assertTrue(any(x['collection'] == 'records' and x['operation'] == '修改' and json.loads(x['before'])['qty'] == 100 and json.loads(x['after'])['qty'] == 101 for x in history))
        self.assertEqual(self.request('GET', '/api/history', headers={'X-GHG-Token':'bad'})[0], 404)

    def test_quality_check_endpoint(self):
        self.data['settings'] = {'year': 2026}
        self.data['records'] = [{'id':'mock','month':'invalid','qty':10}]
        _, h, _ = self.request('GET', '/api/inventory')
        self.assertEqual(self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': h['X-GHG-Revision']})[0], 200)
        status, _, data = self.request('GET', '/api/quality')
        self.assertEqual(status, 200)
        self.assertTrue(any('月份格式' in item['message'] for item in json.loads(data)))
        self.assertEqual(self.request('GET', '/api/quality', headers={'X-GHG-Token':'bad'})[0], 404)

    def test_optional_assessment_legacy_and_history(self):
        _, hdr, _ = self.request('GET', '/api/inventory')
        status, hdr, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': hdr['X-GHG-Revision']})
        self.assertEqual(status, 200)
        self.data['assessments'] = [{'id':'a1','sourceId':'s1','year':2025,'total':14,'threshold':12,'decision':'重大','method':'模擬方法','assessor':'模擬人員','reason':'模擬理由'}]
        status, hdr, _ = self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': hdr['X-GHG-Revision']})
        self.assertEqual(status, 200)
        history = json.loads(self.request('GET', '/api/history')[2])
        self.assertTrue(any(x['collection'] == 'assessments' and x['operation'] == '新增' for x in history))
        self.assertEqual(json.loads(self.request('GET', '/api/inventory')[2])['assessments'][0]['total'], 14)
        self.data['assessments'] = 'invalid'
        self.assertEqual(self.request('PUT', '/api/inventory', json.dumps(self.data), {'X-GHG-Revision': hdr['X-GHG-Revision']})[0], 400)

    def test_annual_report_filters_and_escapes_saved_data(self):
        self.data['settings']['company'] = '<模擬公司>'
        self.data['records'] = [
            {'id': 'active', 'month': '2026-01', 'amount': 0.04,
             'sourceSnapshot': {'cat': 2, 'name': '當年用電'}, 'status': '待審核'},
            {'id': 'prior', 'month': '2025-01', 'amount': 99,
             'sourceSnapshot': {'cat': 1, 'name': '前年度不應出現'}},
            {'id': 'invalid', 'month': '2026-02', 'amount': 10},
        ]
        self.data['evidence'] = [{'recordId': 'active', 'name': '模擬憑證'}]
        self.data['assessments'] = [{'year': 2026, 'reason': '年度評估理由'}]
        _, revision, _ = self.request('GET', '/api/inventory')
        self.assertEqual(self.request('PUT', '/api/inventory', json.dumps(self.data),
                         {'X-GHG-Revision': revision['X-GHG-Revision']})[0], 200)
        status, headers, raw = self.request('GET', '/api/report/2026')
        self.assertEqual(status, 200)
        self.assertIn('text/html', headers['Content-Type'])
        html = raw.decode()
        for value in ('&lt;模擬公司&gt;', '0.04 tCO₂e', '模擬憑證', '年度評估理由',
                      '未審核 2 筆', '1 筆因類別或排放量無效'):
            self.assertIn(value, html)
        self.assertNotIn('前年度不應出現', html)
        self.assertEqual(self.request('GET', '/api/report/2026', headers={'X-GHG-Token': 'bad'})[0], 404)

    def test_year_boundaries_and_totals_are_isolated(self):
        self.data['settings'] = {'year': 2026, 'company': '本年度甲公司'}
        self.data['yearSettings'] = [
            {'id': '2025', 'company': '舊年度乙公司', 'boundary': '股權比例法', 'sites': '測試廠'},
            {'id': '2024', 'company': '無活動資料年度'},
        ]
        self.data['records'] = [
            {'month': '2025-01', 'sourceSnapshot': {'cat': 2}, 'amount': 0, 'status': '待審核'},
            {'month': '2026-02', 'sourceSnapshot': {'cat': 3}, 'amount': 1.25},
            {'month': '2026-03', 'amount': 100},
        ]
        _, headers, _ = self.request('GET', '/api/inventory')
        status, headers, _ = self.request('PUT', '/api/inventory', json.dumps(self.data),
                                         {'X-GHG-Revision': headers['X-GHG-Revision']})
        self.assertEqual(status, 200)
        old_report = self.request('GET', '/api/report/2025')[2].decode()
        self.assertIn('舊年度乙公司', old_report)
        self.assertNotIn('本年度甲公司', old_report)
        unconfigured = self.request('GET', '/api/report/2023')[2].decode()
        self.assertIn('尚未填寫完整', unconfigured)
        self.assertNotIn('本年度甲公司', unconfigured)
        rows = json.loads(self.request('GET', '/api/annual-totals')[2])
        self.assertEqual([(r['year'], r['total'], r['pending'], r['invalid']) for r in rows],
                         [('2024', None, 0, 0), ('2025', '0.0', 1, 0), ('2026', '1.25', 2, 1)])
        self.data['yearSettings'].append({'id': '2025'})
        self.assertEqual(self.request('PUT', '/api/inventory', json.dumps(self.data),
                         {'X-GHG-Revision': headers['X-GHG-Revision']})[0], 400)

    def test_v1_migration_requires_matching_preview_and_never_overwrites(self):
        self.data['sources'] = [{'id': 's1', 'cat': 2, 'name': '模擬用電'}]
        self.data['records'] = [{'id': 'r1', 'month': '2026-01', 'amount': 3.5,
                                 'status': '已審核', 'sourceSnapshot': self.data['sources'][0]}]
        saved = json.dumps(self.data, ensure_ascii=False).encode('utf-8-sig')
        status, _, body = self.request('POST', '/api/migrate-v1/preview', saved)
        self.assertEqual(status, 200)
        preview = json.loads(body)
        self.assertEqual(preview['counts']['records'], 1)
        self.assertEqual(self.request('GET', '/api/inventory')[2], b'null')
        token = {'X-GHG-Import-Preview': preview['preview']}
        self.data['records'][0]['amount'] = 99
        self.assertEqual(self.request('POST', '/api/migrate-v1/apply', json.dumps(self.data), token)[0], 409)
        self.assertEqual(self.request('POST', '/api/migrate-v1/apply', saved, token)[0], 200)
        self.assertEqual(json.loads(self.request('GET', '/api/inventory')[2])['records'][0]['amount'], 3.5)
        self.assertEqual(self.request('POST', '/api/migrate-v1/apply', saved, token)[0], 409)
        events = json.loads(self.request('GET', '/api/history')[2])
        self.assertTrue(any(event['reason'] == 'V1 資料移轉' for event in events))

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
