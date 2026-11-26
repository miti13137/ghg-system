import io
import sys
import unittest
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xlsx_local

class OfflineExcelTests(unittest.TestCase):
    def test_round_trip_with_text_and_formula_like_values(self):
        rows = [['紀錄編號', '排放源', '活動量'], ['001', '測試 & <廠區>', '=1+2'], ['', '  保留空格  ', '12.5']]
        data = xlsx_local.write_xlsx(rows)
        self.assertEqual(xlsx_local.read_xlsx(data), rows)
        try:
            import openpyxl
        except ImportError:
            return
        wb = openpyxl.load_workbook(io.BytesIO(data))
        self.assertEqual(wb.active['C2'].value, '=1+2')
        self.assertEqual(wb.active['C2'].data_type, 's')
        self.assertEqual(wb.active['B2'].value, '測試 & <廠區>')

    def test_excel_generated_file_and_formula_rejected(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl is an optional test-only dependency')
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = '資料'
        sheet.append(['名稱', '數值'])
        sheet.append(['測試', 3.5])
        buf = io.BytesIO()
        book.save(buf)
        self.assertEqual(xlsx_local.read_xlsx(buf.getvalue()), [['名稱', '數值'], ['測試', '3.5']])
        sheet['B2'] = '=1+2'
        buf = io.BytesIO()
        book.save(buf)
        with self.assertRaisesRegex(ValueError, '公式'):
            xlsx_local.read_xlsx(buf.getvalue())

    def test_reject_invalid_and_oversized(self):
        with self.assertRaises(ValueError): xlsx_local.read_xlsx(b'not a workbook')
        with self.assertRaises(ValueError): xlsx_local.write_xlsx([['x'] * 33])
        with self.assertRaises(ValueError): xlsx_local.write_xlsx([['x' * 10001]])

if __name__ == '__main__': unittest.main()
