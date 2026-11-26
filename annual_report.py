"""Build a self-contained annual inventory draft from a saved inventory."""
from decimal import Decimal
from html import escape
import re

from inventory_quality import numeric
from inventory_years import settings_for_year

CATEGORIES = (
    '直接排放', '輸入能源間接排放', '運輸間接排放',
    '組織使用產品間接排放', '組織產品相關間接排放', '其他間接排放',
)


def table(headers, rows):
    head = ''.join(f'<th>{escape(title)}</th>' for title in headers)
    body = ''.join(
        '<tr>' + ''.join(f'<td>{escape(str(value if value is not None else "—"))}</td>' for value in row) + '</tr>'
        for row in rows
    )
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>' if body else '<p>無已登錄資料。</p>'


def annual_report(data, year):
    settings = settings_for_year(data, year)
    records = [r for r in data['records'] if re.fullmatch(year + r'-(0[1-9]|1[0-2])', str(r.get('month', '')))]
    evidence = {}
    for item in data['evidence']:
        if isinstance(item.get('recordId'), str):
            evidence.setdefault(item['recordId'], []).append(str(item.get('name') or '未命名文件'))
    totals = [Decimal(0) for _ in CATEGORIES]
    counts = [0 for _ in CATEGORIES]
    details = []
    incomplete = 0
    for record in records:
        source = record.get('sourceSnapshot')
        factor = record.get('factorSnapshot')
        source = source if isinstance(source, dict) else {}
        factor = factor if isinstance(factor, dict) else {}
        cat = source.get('cat')
        amount = numeric(record.get('amount'))
        if type(cat) is int and 1 <= cat <= 6 and amount is not None and amount >= 0:
            totals[cat - 1] += Decimal(str(amount))
            counts[cat - 1] += 1
        else:
            incomplete += 1
        record_id = record.get('id')
        linked = evidence.get(record_id, []) if isinstance(record_id, str) else []
        details.append((
            record.get('month'), record_id, source.get('site'), source.get('name'), cat,
            record.get('qty'), record.get('unit'), factor.get('name'), factor.get('value'),
            factor.get('type'), factor.get('origin'), factor.get('year'), factor.get('gwp'),
            factor.get('gwpversion'), record.get('amount'), record.get('status'),
            record.get('proof'), '、'.join(linked), record.get('quality'), record.get('uncertainty'),
        ))
    total = sum(totals)
    pending = sum(r.get('status') != '已審核' for r in records)
    invalid_months = sum(
        not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', str(r.get('month', '')))
        for r in data['records']
    )
    boundary_notice = '' if settings.get('company') and settings.get('boundary') and settings.get('sites') else '<p>本年度的組織邊界資料尚未填寫完整。</p>'
    boundaries = table(('項目', '內容'), [
        ('公司', settings.get('company')), ('邊界方法', settings.get('boundary')),
        ('廠區', settings.get('sites')), ('負責人', settings.get('owner')),
        ('設定基準年', settings.get('baseYear')),
    ])
    summary = table(('類別', '計入筆數', 'tCO₂e'), [
        (f'{i+1}｜{name}', counts[i], format(totals[i], 'f') if counts[i] else '無可彙總資料')
        for i, name in enumerate(CATEGORIES)
    ])
    assessments = table(('排放源', '總分', '門檻', '判定', '評估人員', '程序依據', '理由'), [
        ((a['sourceSnapshot'].get('name') if isinstance(a.get('sourceSnapshot'), dict) else None) or a.get('sourceId'),
         a.get('total'), a.get('threshold'), a.get('decision'), a.get('assessor'), a.get('method'), a.get('reason'))
        for a in data.get('assessments', []) if str(a.get('year')) == year
    ])
    activity = table((
        '月份', '紀錄編號', '廠區', '排放源', '類別', '活動量', '單位', '係數', '係數值',
        '換算模式', '來源', '年度／版本', 'GWP', 'GWP 版本', 'tCO₂e', '審核',
        '憑證', '佐證文件', '品質', '不確定性',
    ), details)
    total_text = format(total, 'f') + ' tCO₂e' if any(counts) else '無可彙總資料'
    return f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<title>{year} 年溫室氣體盤查報告草稿</title>
<style>
body{{font:15px/1.55 system-ui,sans-serif;color:#172b38;margin:2rem}}
h1,h2{{color:#164d43}} table{{border-collapse:collapse;width:100%;margin-bottom:1.5rem}}
th,td{{border:1px solid #b0c2bf;padding:.35rem;text-align:left;overflow-wrap:anywhere}}
th{{background:#eaf3f0}} .scroll{{overflow-x:auto}} .items{{font-size:.8rem}}
@media print{{@page{{size:landscape;margin:12mm}}body{{margin:0;font-size:10pt}}.scroll{{overflow:visible}}
.items{{table-layout:fixed;font-size:7pt}}thead{{display:table-header-group}}tr{{break-inside:avoid}}}}
</style><h1>{year} 年溫室氣體盤查報告草稿</h1>
<p>資料來源為本機已儲存的盤查資料。彙總含未審核紀錄，發布前請核對資料品質、報告邊界及計算依據。</p>
<h2>組織與盤查邊界</h2>{boundary_notice}{boundaries}
<h2>年度排放彙總</h2><p>可彙總排放量 {total_text}，活動紀錄 {len(records)} 筆，未審核 {pending} 筆。
本年度有 {incomplete} 筆因類別或排放量無效而未計入合計。全資料庫另有 {invalid_months} 筆月份無效，無法歸入年度。</p>{summary}
<h2>間接排放顯著性評估</h2>{assessments}
<h2>活動數據清冊</h2><div class="scroll">{activity.replace('<table>', '<table class="items">', 1)}</div>
<p>查證前請補充正式盤查程序、排除依據、基準年重算規則及品質保證文件。</p></html>'''
