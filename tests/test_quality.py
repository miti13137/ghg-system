import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import inventory_quality

class QualityTests(unittest.TestCase):
    def setUp(self):
        self.factor = {'id':'f1', 'name':'模擬係數', 'unit':'kWh', 'value':0.4, 'type':'kgco2e', 'gas':'CO₂e', 'origin':'模擬來源，非官方數據', 'year':'測試版'}
        self.source = {'id':'s1','name':'模擬電力','cat':2,'included':'yes'}
        self.record = {'id':'r1','month':'2025-01','qty':100,'unit':'kWh','amount':0.04,'factorSnapshot':copy.deepcopy(self.factor),'sourceSnapshot':copy.deepcopy(self.source),'proof':'模擬憑證','status':'已審核','quality':'帳單／原始紀錄'}
        self.data = {'settings':{'company':'模擬公司','baseYear':'2025','sites':'模擬廠'},'sources':[self.source],'factors':[self.factor],'records':[self.record],'evidence':[],'audit':[]}
    def errors(self): return [x for x in inventory_quality.check(self.data) if x['level']=='錯誤']
    def test_calculation_and_unit_errors(self):
        self.assertEqual(self.errors(), [])
        self.record['amount'] = 40
        self.assertTrue(any('重新計算' in e['message'] for e in self.errors()))
        self.record['amount'] = 0.04
        self.record['unit'] = 'L'
        self.assertTrue(any('單位不一致' in e['message'] for e in self.errors()))
    def test_gwp_and_metadata(self):
        f = self.record['factorSnapshot']
        f.update({'type':'kgGas','gas':'CH₄','gwp':25,'gwpversion':'模擬版'})
        self.record['amount']=1.0
        self.assertEqual(self.errors(), [])
        f['gwpversion']=''
        self.assertTrue(any('GWP 版本' in e['message'] for e in self.errors()))
        f['gwpversion']='模擬版'
        f['origin']=''
        self.assertTrue(any('來源' in e['message'] for e in self.errors()))
    def test_invalid_numbers_duplicate_and_missing_proof(self):
        self.record['qty']='NaN'
        self.data['records'].append(copy.deepcopy(self.record))
        self.record['proof']=''
        found=inventory_quality.check(self.data)
        self.assertTrue(any('有效非負數' in e['message'] for e in found))
        self.assertTrue(any('重複' in e['message'] for e in found))
        self.assertTrue(any('佐證' in e['message'] for e in found))
    def test_tco2e(self):
        self.record['factorSnapshot'].update(type='tco2e',value=0.002)
        self.record['amount']=0.2
        self.assertEqual(self.errors(), [])

if __name__=='__main__': unittest.main()
