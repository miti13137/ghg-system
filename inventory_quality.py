"""Offline inventory quality checks. No official coefficients are included."""
from collections import Counter
import math
import re

COLLECTIONS = ('sources', 'factors', 'records', 'evidence', 'audit')
MONTH = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


def numeric(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def check(data):
    issues = []
    def issue(level, group, item, message):
        issues.append({'level': level, 'group': group, 'item': str(item or '（未命名）'), 'message': message})
    settings = data.get('settings', {})
    if not settings.get('company'): issue('提醒', '組織設定', '', '尚未填寫公司名稱')
    if not settings.get('baseYear'): issue('提醒', '組織設定', '', '尚未設定基準年')
    if not settings.get('sites'): issue('提醒', '組織設定', '', '尚未說明廠區及組織邊界')
    for kind in COLLECTIONS:
        values = data.get(kind, [])
        ids = [str(x.get('id', '')) for x in values if isinstance(x, dict)]
        for value, count in Counter(ids).items():
            if not value: issue('錯誤', kind, '', '資料缺少紀錄編號')
            elif count > 1: issue('錯誤', kind, value, '紀錄編號重複')
    sources = data.get('sources', [])
    factors = data.get('factors', [])
    records = data.get('records', [])
    evidence = data.get('evidence', [])
    for n in range(1, 7):
        if not any(x.get('cat') == n for x in sources):
            issue('提醒', '排放源', '類別' + str(n), '尚未鑑別排放源；不能直接視為零排放')
    for x in sources:
        name = x.get('name') or x.get('id')
        if x.get('cat') not in range(1, 7): issue('錯誤', '排放源', name, '類別應為 1 至 6')
        if x.get('included') not in ('yes', 'no'): issue('錯誤', '排放源', name, '納入狀態不正確')
        if x.get('included') == 'no' and not x.get('reason'): issue('提醒', '排放源', name, '排除項目尚未記錄理由')
    def factor_issues(f):
        result = []
        value = numeric(f.get('value'))
        if value is None or value < 0: result.append('係數值不是有效非負數')
        if not f.get('unit'): result.append('缺少活動單位')
        if not f.get('origin'): result.append('缺少來源機構及文件')
        if not f.get('year'): result.append('缺少適用年度／版本')
        if f.get('type') not in ('tco2e', 'kgco2e', 'kgGas'): result.append('係數單位模式不正確')
        if f.get('type') == 'kgGas':
            gwp = numeric(f.get('gwp'))
            if gwp is None or gwp <= 0: result.append('缺少有效 GWP')
            if not f.get('gwpversion'): result.append('缺少 GWP 版本')
            if not f.get('gas') or f.get('gas') == 'CO₂e': result.append('應指定實際溫室氣體')
        return result
    assessments = data.get('assessments', [])
    year = str(settings.get('year', ''))
    for source in sources:
        if source.get('cat') in (3, 4, 5, 6) and not any(x.get('sourceId') == source.get('id') and str(x.get('year', '')) == year for x in assessments):
            issue('提醒', '顯著性評估', source.get('name') or source.get('id'), '本年度尚未記錄顯著性評估')
    for a in assessments:
        if not a.get('method') or not a.get('reason') or not a.get('assessor') or a.get('decision') not in ('重大', '非重大'):
            issue('提醒', '顯著性評估', a.get('id'), '評估標準、人員、判定或理由尚不完整')
        if not any(x.get('id') == a.get('sourceId') for x in sources):
            issue('提醒', '顯著性評估', a.get('id'), '關聯排放源不存在，請核對歷史評估')
        scores, threshold = a.get('scores'), a.get('threshold')
        if not isinstance(scores, list) or len(scores) != 4 or any(not isinstance(x, int) or isinstance(x, bool) or x < 1 or x > 5 for x in scores) or not isinstance(threshold, int) or isinstance(threshold, bool) or not 4 <= threshold <= 20:
            issue('錯誤', '顯著性評估', a.get('id'), '分數或門檻不正確')
        elif a.get('total') != sum(scores) or a.get('suggested') != ('重大' if sum(scores) >= threshold else '非重大'):
            issue('錯誤', '顯著性評估', a.get('id'), '總分或門檻結果與各項分數不一致')
    for f in factors:
        for detail in factor_issues(f): issue('錯誤', '排放係數', f.get('name') or f.get('id'), detail)
    for r in records:
        name = str(r.get('month') or '') + ' / ' + str(r.get('sourceSnapshot', {}).get('name') or r.get('id') or '') if isinstance(r.get('sourceSnapshot', {}), dict) else str(r.get('id') or '')
        month = r.get('month')
        if not isinstance(month, str) or not MONTH.fullmatch(month): issue('錯誤', '活動數據', name, '月份格式應為 YYYY-MM')
        q, amount = numeric(r.get('qty')), numeric(r.get('amount'))
        if q is None or q < 0: issue('錯誤', '活動數據', name, '活動量不是有效非負數')
        if amount is None or amount < 0: issue('錯誤', '活動數據', name, '排放量不是有效非負數')
        snapshot, source = r.get('factorSnapshot'), r.get('sourceSnapshot')
        if not isinstance(source, dict) or source.get('cat') not in range(1, 7): issue('錯誤', '活動數據', name, '缺少有效的排放源類別快照')
        if not isinstance(snapshot, dict): issue('錯誤', '活動數據', name, '缺少排放係數快照，無法重算')
        else:
            failures = factor_issues(snapshot)
            for detail in failures: issue('錯誤', '活動數據', name, '係數快照：' + detail)
            if r.get('unit') != snapshot.get('unit'): issue('錯誤', '活動數據', name, '活動數據與係數單位不一致')
            if not failures and q is not None and q >= 0 and amount is not None:
                multiplier = 1 if snapshot['type'] == 'tco2e' else 0.001 if snapshot['type'] == 'kgco2e' else numeric(snapshot['gwp']) / 1000
                expected = q * numeric(snapshot['value']) * multiplier
                if not math.isfinite(expected) or not math.isclose(amount, expected, rel_tol=1e-9, abs_tol=1e-12):
                    issue('錯誤', '活動數據', name, '排放量與活動量、係數及 GWP 快照重新計算結果不符')
        if not r.get('proof') and not any(e.get('recordId') == r.get('id') for e in evidence): issue('提醒', '活動數據', name, '尚未填寫憑證或連結佐證')
        if r.get('status') != '已審核': issue('提醒', '活動數據', name, '尚未審核')
        if r.get('quality') == '估算' and not r.get('uncertainty'): issue('提醒', '活動數據', name, '估算數據尚未說明方法或不確定性')
    record_ids = {x.get('id') for x in records}
    for e in evidence:
        if e.get('recordId') not in record_ids: issue('提醒', '佐證', e.get('name') or e.get('id'), '關聯的活動紀錄不存在')
    return issues
