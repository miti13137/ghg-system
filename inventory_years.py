"""Year-specific organization settings for the single-inventory data model."""
from decimal import Decimal
import re

from inventory_quality import numeric

YEAR = re.compile(r'[0-9]{4}')
BOUNDARY_FIELDS = ('company', 'boundary', 'sites', 'owner', 'baseYear')


def validate_year_settings(entries):
    seen = set()
    for entry in entries:
        year = entry.get('id')
        if not isinstance(year, str) or not YEAR.fullmatch(year) or year in seen:
            raise ValueError('年度設定須使用不重複的四位數年度')
        if any(not isinstance(entry.get(key, ''), str) for key in BOUNDARY_FIELDS):
            raise ValueError('年度組織設定格式不正確')
        seen.add(year)


def settings_for_year(data, year):
    """Legacy fields belong to their saved year only."""
    for entry in data.get('yearSettings', []):
        if entry.get('id') == year:
            return entry
    current = data['settings']
    return current if str(current.get('year', '')) == year else {}

def annual_totals(data):
    """Summarize the stored amounts by year without treating missing data as zero."""
    rows = {
        entry['id']: {'year': entry['id'], 'total': None, 'pending': 0, 'invalid': 0}
        for entry in data.get('yearSettings', [])
    }
    for record in data['records']:
        month = str(record.get('month', ''))
        if not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', month):
            continue
        year = month[:4]
        row = rows.setdefault(year, {'year': year, 'total': None, 'pending': 0, 'invalid': 0})
        if record.get('status') != '已審核':
            row['pending'] += 1
        amount = numeric(record.get('amount'))
        source = record.get('sourceSnapshot')
        cat = source.get('cat') if isinstance(source, dict) else None
        if amount is None or amount < 0 or type(cat) is not int or not 1 <= cat <= 6:
            row['invalid'] += 1
            continue
        row['total'] = (row['total'] or Decimal(0)) + Decimal(str(amount))
    output = []
    for year, row in sorted(rows.items()):
        boundary = settings_for_year(data, year)
        row['company'] = boundary.get('company') or ''
        row['sites'] = boundary.get('sites') or ''
        row['boundary'] = boundary.get('boundary') or ''
        row['total'] = None if row['total'] is None else format(row['total'], 'f')
        output.append(row)
    return output
