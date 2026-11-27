"""Offline evidence pack scoped to the selected inventory year."""
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
import re
from zipfile import ZIP_DEFLATED, ZipFile

from annual_report import CATEGORIES, annual_report
from inventory_quality import numeric
from inventory_years import annual_totals, settings_for_year
from xlsx_local import write_workbook

MAX_EVIDENCE_BYTES = 150_000_000


def fields(headers, keys, items):
    return [list(headers)] + [[item.get(key) for key in keys] for item in items]


def snapshot(record, name):
    value = record.get(name)
    return value if isinstance(value, dict) else {}


def build_pack(data, year, evidence_lookup, history):
    records = [r for r in data['records']
               if re.fullmatch(year + r'-(0[1-9]|1[0-2])', str(r.get('month', '')))]
    record_ids = {r['id'] for r in records if isinstance(r.get('id'), str) and r['id']}
    source_ids = {r['sourceId'] for r in records if isinstance(r.get('sourceId'), str)}
    assessments = [a for a in data.get('assessments', []) if str(a.get('year')) == year]
    source_ids.update(a['sourceId'] for a in assessments if isinstance(a.get('sourceId'), str))
    findings = [a for a in data['audit'] if isinstance(a.get('record'), str) and a['record'] in record_ids]
    unlinked = sum(not a.get('record') for a in data['audit'])
    total = next((row for row in annual_totals(data) if row['year'] == year), {})
    boundary = settings_for_year(data, year)
    contents = BytesIO()
    attached_bytes = 0
    with ZipFile(contents, 'w', ZIP_DEFLATED) as archive:
        evidence = [['活動紀錄編號', '文件名稱', '原檔名', '保存位置與索引', '備註', 'ZIP 內路徑', '檔案狀態', 'SHA-256']]
        missing = 0
        for entry in data['evidence']:
            record_id = entry.get('recordId')
            if not isinstance(record_id, str) or record_id not in record_ids:
                continue
            file_id = entry.get('fileId')
            manifest = ''
            digest = ''
            state = '只有索引'
            if file_id:
                state = '檔案缺失或完整性檢查失敗'
                blob = evidence_lookup(file_id) if isinstance(file_id, str) else None
                if blob:
                    filename, expected_hash, content = blob
                    if sha256(content).hexdigest() == expected_hash and entry.get('sha256') == expected_hash:
                        attached_bytes += len(content)
                        if attached_bytes > MAX_EVIDENCE_BYTES:
                            raise ValueError('已連結的佐證超過 150 MB，無法產生單一查證包。')
                        extension = re.search(r'\.[a-zA-Z0-9]{1,10}$', filename or '')
                        manifest = f'evidence/{len(evidence):06d}' + (extension[0].lower() if extension else '.bin')
                        archive.writestr(manifest, content)
                        digest, state = expected_hash, '已附原檔'
            missing += not manifest
            evidence.append([record_id, entry.get('name'), entry.get('filename'), entry.get('location'),
                             entry.get('note'), manifest, state, digest])
        activities = [['紀錄編號', '月份', '廠區', '排放源編號', '排放源', '類別', '活動量', '活動單位',
                       '係數編號', '係數名稱', '係數值', '係數模式', '係數來源', '年度／版本',
                       '氣體', 'GWP', 'GWP 版本', '排放量 tCO₂e', '審核狀態', '憑證', '品質', '不確定性', '備註']]
        factors = [['紀錄編號', '係數編號', '係數名稱', '係數值', '活動單位', '係數模式',
                    '氣體', '係數來源', '年度／版本', 'GWP', 'GWP 版本']]
        category_totals = {}
        for record in records:
            source = snapshot(record, 'sourceSnapshot')
            factor = snapshot(record, 'factorSnapshot')
            category = source.get('cat')
            amount = numeric(record.get('amount'))
            if type(category) is int and 1 <= category <= 6 and amount is not None and amount >= 0:
                count, subtotal = category_totals.get(category, (0, Decimal(0)))
                category_totals[category] = count + 1, subtotal + Decimal(str(amount))
            activities.append([
                record.get('id'), record.get('month'), source.get('site'), record.get('sourceId'),
                source.get('name'), source.get('cat'), numeric(record.get('qty')), record.get('unit'),
                record.get('factorId'), factor.get('name'), numeric(factor.get('value')), factor.get('type'),
                factor.get('origin'), factor.get('year'), factor.get('gas'), numeric(factor.get('gwp')),
                factor.get('gwpversion'), amount, record.get('status'),
                record.get('proof'), record.get('quality'), record.get('uncertainty'), record.get('note')])
            factors.append([record.get('id'), record.get('factorId'), factor.get('name'), numeric(factor.get('value')),
                            factor.get('unit'), factor.get('type'), factor.get('gas'), factor.get('origin'),
                            factor.get('year'), numeric(factor.get('gwp')), factor.get('gwpversion')])
        sources = [s for s in data['sources'] if isinstance(s.get('id'), str) and s['id'] in source_ids]
        review = [['活動紀錄編號', '操作時間（UTC）', '帳號', '原審核狀態', '新審核狀態', '異動來源']]
        for record_id, when, actor, before_json, after_json, reason in history:
            if record_id not in record_ids:
                continue
            before = (json.loads(before_json) or {}).get('status') if before_json else None
            after = (json.loads(after_json) or {}).get('status') if after_json else None
            if before != after:
                review.append([record_id, when, actor or '未記錄', before, after, reason])
        overview = [
            ['項目', '內容'], ['盤查年度', year], ['公司', boundary.get('company')], ['廠區', boundary.get('sites')],
            ['邊界方法', boundary.get('boundary')], ['盤查負責人', boundary.get('owner')],
            ['基準年', boundary.get('baseYear')], ['總排放量 tCO₂e', numeric(total.get('total'))],
            ['活動紀錄筆數', len(records)], ['未審核筆數', total.get('pending', 0)],
            ['未列入合計筆數', total.get('invalid', 0)], ['未附佐證原檔的索引筆數', missing],
            ['沒有佐證索引的活動筆數', len(record_ids - {row[0] for row in evidence[1:]})],
            ['未指定活動紀錄的查證事項（不在本包）', unlinked],
            ['使用說明', '用活動紀錄編號核對計算、審核及佐證。排放源表為已關聯排放源的現行設定，歷史採活動清冊快照。'],
            ['資料範圍', '包含未審核資料。Excel 係數與 GWP 抄錄自原始計算快照。報告仍需完成公司審核。'],
        ]
        sheets = [
            ('年度總覽', overview),
            ('六類排放彙總', [['類別', '排放分類', '計入筆數', '排放量 tCO₂e']] + [
                [i, name, category_totals[i][0], numeric(category_totals[i][1])]
                if i in category_totals else [i, name, 0, '無可彙總資料']
                for i, name in enumerate(CATEGORIES, 1)
            ]),
            ('活動數據與排放清冊', activities),
            ('係數與GWP快照', factors),
            ('排放源現行設定', fields(
                ['排放源編號', '名稱', '類別', '廠區', '部門', '說明', '納入狀態', '理由'],
                ['id', 'name', 'cat', 'site', 'dept', 'desc', 'included', 'reason'], sources)),
            ('間接排放顯著性', fields(
                ['排放源編號', '年度', '總分', '門檻', '判定', '評估人員', '依據', '理由'],
                ['sourceId', 'year', 'total', 'threshold', 'decision', 'assessor', 'method', 'reason'], assessments)),
            ('佐證文件索引', evidence),
            ('審核紀錄', review),
            ('查證改善事項', fields(
                ['活動紀錄編號', '事項編號', '查證發現', '責任人', '狀態', '改善說明'],
                ['record', 'id', 'name', 'owner', 'status', 'note'], findings)),
        ]
        archive.writestr(f'GHG_{year}_verification.xlsx', write_workbook(sheets))
        archive.writestr(f'GHG_{year}_report-draft.html', annual_report(data, year))
    return contents.getvalue()
