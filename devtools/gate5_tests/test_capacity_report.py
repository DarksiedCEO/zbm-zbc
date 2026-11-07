import datetime as dt
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('capacity_report', Path(__file__).parents[2]/'deploy/gate5/capacity_report.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class CapacityReportTest(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026,11,7,18,tzinfo=dt.timezone.utc)
        self.data = dict(measured_at=self.now.isoformat(), stores={n:dict(source_bytes=100,one_client_growth_bytes=100) for n in m.MEMBERS})
    def test_all_five_and_projected_threshold(self):
        self.assertEqual(m.report(self.data,self.now)['status'],'CAPACITY_ONLY_PASS')
        self.data['stores']['seo']['one_client_growth_bytes']=262044
        self.assertEqual(m.report(self.data,self.now)['status'],'HOLD')
    def test_exact_half_requires_action(self):
        self.data['stores']['ledger']['source_bytes']=1048576
        self.assertEqual(m.report(self.data,self.now)['status'],'HOLD')
    def test_unknown_growth_and_invalid_counts(self):
        self.data['stores']['finance']['one_client_growth_bytes']=None
        self.assertEqual(m.report(self.data,self.now)['status'],'HOLD')
        self.data['stores']['finance']['source_bytes']=True
        with self.assertRaises(ValueError):m.report(self.data,self.now)
    def test_stale_or_missing_store(self):
        with self.assertRaises(ValueError):m.report(self.data,self.now+dt.timedelta(hours=2))
        del self.data['stores']['seo']
        with self.assertRaises(ValueError):m.report(self.data,self.now)
