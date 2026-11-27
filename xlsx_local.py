"""Limited offline XLSX reader/writer for GHG tabular imports; no dependencies."""
import io
import math
import re
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

MAX_FILE = 10_000_000
MAX_UNPACKED = 40_000_000
MAX_ROWS = 5001
MAX_COLS = 32
MAX_CELL = 10000
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
PKG_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'


def xml_bytes(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('不支援含有 XML 宣告的檔案')
    return ET.fromstring(raw)


def read_xlsx(data):
    if len(data) > MAX_FILE:
        raise ValueError('檔案不得超過 10 MB')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 1000 or sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise ValueError('檔案解壓後過大')
            names = [x.filename for x in infos]
            if len(set(names)) != len(names):
                raise ValueError('檔案內含重複項目')
            if any('vbaProject' in n or n.startswith('xl/externalLinks/') for n in names):
                raise ValueError('不支援巨集或外部連結')
            wb = xml_bytes(z.read('xl/workbook.xml'))
            sheets = wb.find(NS + 'sheets')
            if sheets is None or not len(sheets):
                raise ValueError('沒有工作表')
            sheet = next((s for s in sheets if s.get('name') == '資料'), sheets[0])
            rels = xml_bytes(z.read('xl/_rels/workbook.xml.rels'))
            rid = sheet.get(REL + 'id')
            rel = next((x for x in rels if x.get('Id') == rid), None)
            if rel is None or rel.get('TargetMode') == 'External':
                raise ValueError('無法找到工作表')
            target = rel.get('Target', '').lstrip('/')
            path = target if target.startswith('xl/') else 'xl/' + target
            if '..' in path.split('/') or not path.startswith('xl/worksheets/'):
                raise ValueError('工作表路徑不正確')
            shared = []
            if 'xl/sharedStrings.xml' in names:
                root = xml_bytes(z.read('xl/sharedStrings.xml'))
                for si in root.findall(NS+'si'):
                    value = ''.join(t.text or '' for t in si.iter(NS+'t'))
                    if len(value) > MAX_CELL: raise ValueError('儲存格內容過長')
                    shared.append(value)
            sheet_xml = xml_bytes(z.read(path))
            matrix = []
            sheet_data = sheet_xml.find(NS+'sheetData')
            if sheet_data is None: return []
            for row in sheet_data.findall(NS+'row'):
                n = int(row.get('r', len(matrix)+1))
                if n < 1 or n > MAX_ROWS or n <= len(matrix): raise ValueError('列數超過限制或順序不正確')
                while len(matrix) < n: matrix.append([])
                for c in row.findall(NS+'c'):
                    ref = c.get('r', '')
                    match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]*)', ref)
                    if not match or int(match.group(2)) != n: raise ValueError('儲存格位置不正確')
                    col = 0
                    for ch in match.group(1): col = col*26 + ord(ch)-64
                    if col > MAX_COLS: raise ValueError('欄數超過限制')
                    if c.find(NS+'f') is not None: raise ValueError('匯入檔案不可含公式，請先貼上為值')
                    typ = c.get('t', 'n')
                    v = c.find(NS+'v')
                    if typ == 'inlineStr': value = ''.join(t.text or '' for t in c.iter(NS+'t'))
                    elif typ == 's':
                        index = int(v.text) if v is not None and v.text else -1
                        if index < 0 or index >= len(shared): raise ValueError('文字索引不正確')
                        value = shared[index]
                    elif typ in ('n', 'str', 'b'): value = (v.text or '') if v is not None else ''
                    else: raise ValueError('不支援的儲存格格式')
                    if len(value) > MAX_CELL: raise ValueError('儲存格內容過長')
                    while len(matrix[-1]) < col: matrix[-1].append('')
                    matrix[-1][col-1] = value
            return matrix
    except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError, TypeError, OverflowError) as e:
        raise ValueError('無法讀取 XLSX 檔案') from e


def colname(i):
    result = ''
    while i:
        i, r = divmod(i-1, 26)
        result = chr(65+r)+result
    return result


def write_xlsx(matrix):
    return write_workbook([('資料', matrix)], presentation=False)


def worksheet(matrix, presentation):
    if not isinstance(matrix, list) or len(matrix) > (1_048_576 if presentation else MAX_ROWS):
        raise ValueError('列數超過限制')
    rows = []
    width = 0
    for i, row in enumerate(matrix, 1):
        if not isinstance(row, list) or len(row) > MAX_COLS:
            raise ValueError('欄數超過限制')
        width = max(width, len(row))
        cells = []
        for j, raw in enumerate(row, 1):
            ref = f'{colname(j)}{i}'
            if presentation and type(raw) in (int, float) and math.isfinite(raw):
                cells.append(f'<c r="{ref}"><v>{raw}</v></c>')
                continue
            value = str(raw if raw is not None else '')
            if presentation:
                value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]', '�', value)
                if len(value) > 16000:
                    value = value[:15985] + '（內容已截短）'
            elif len(value) > MAX_CELL or any(ord(x) < 32 and x not in '\t\r\n' for x in value):
                raise ValueError('儲存格內容不正確')
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>')
        rows.append(f'<row r="{i}">{"".join(cells)}</row>')
    views = ''
    filters = ''
    if presentation and width:
        views = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
                 'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                 f'<cols><col min="1" max="{width}" width="21" customWidth="1"/></cols>')
        filters = f'<autoFilter ref="A1:{colname(width)}{len(rows)}"/>'
    return f'<worksheet xmlns="{NS[1:-1]}">{views}<sheetData>{"".join(rows)}</sheetData>{filters}</worksheet>'


def write_workbook(sheets, presentation=True):
    output = io.BytesIO()
    content_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    relationship_ns = REL[1:-1]
    package_ns = PKG_REL[1:-1]
    entries, overrides, relationships = [], [], []
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for i, (name, matrix) in enumerate(sheets, 1):
            path = f'worksheets/sheet{i}.xml'
            entries.append(f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>')
            overrides.append(f'<Override PartName="/xl/{path}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            relationships.append(f'<Relationship Id="rId{i}" Type="{relationship_ns}/worksheet" Target="{path}"/>')
            archive.writestr('xl/' + path, worksheet(matrix, presentation))
        archive.writestr('[Content_Types].xml', f'<Types xmlns="{content_ns}">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + ''.join(overrides) + '</Types>')
        archive.writestr('_rels/.rels', f'<Relationships xmlns="{package_ns}">'
            f'<Relationship Id="rId1" Type="{relationship_ns}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{NS[1:-1]}" xmlns:r="{relationship_ns}">'
            + ('<bookViews><workbookView/></bookViews>' if presentation else '')
            + '<sheets>' + ''.join(entries) + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels',
                         f'<Relationships xmlns="{package_ns}">{"".join(relationships)}</Relationships>')
    return output.getvalue()
