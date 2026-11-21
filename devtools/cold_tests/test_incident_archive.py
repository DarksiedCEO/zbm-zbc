import _runtime_path  # fixed repository-relative packaged runtime
import tempfile,unittest
from pathlib import Path
from owner_wire import Refused
from incident_archive import Archive,SLOT_BYTES,CONTROL_HEADROOM
class Cases(unittest.TestCase):
 def test_four_slots_retained_full_alert(self):
  with tempfile.TemporaryDirectory() as d:
   a=Archive(Path(d));before={}
   for i in range(4):a.append({'identity':str(i),'phase':'RESOLVED','evidence':{'n':i}})
   before={p.name:p.read_bytes() for p in a.root.iterdir()}
   with self.assertRaises(Refused):a.append({'identity':'5','phase':'RESOLVED','evidence':{}})
   self.assertEqual(before,{p.name:p.read_bytes() for p in a.root.iterdir()})
   self.assertEqual(a.alert()['event'],'incident_archive_full')
 def test_duplicate_and_conflict(self):
  with tempfile.TemporaryDirectory() as d:
   a=Archive(Path(d));v={'identity':'x','phase':'RESOLVED','evidence':{}}
   self.assertEqual(a.append(v),a.append(v))
   with self.assertRaises(Refused):a.append(dict(v,evidence={'different':True}))
 def test_bounds_interrupted_unresolved(self):
  with tempfile.TemporaryDirectory() as d:
   a=Archive(Path(d))
   for v in ({'identity':'x','phase':'PENDING','evidence':{}},{'identity':'x','phase':'RESOLVED','evidence':{'x':'x'*SLOT_BYTES}}):
    with self.assertRaises(Refused):a.append(v)
   (a.root/'slot-0.json.tmp').write_text('{}')
   with self.assertRaises(Refused):a.append({'identity':'x','phase':'RESOLVED','evidence':{}})
if __name__=='__main__':unittest.main()

class OutboxCases(unittest.TestCase):
 def test_existing_outbox_exact_restart(self):
  from schedule_state import Schedule
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'outbox.db';s=Schedule(p)
   a=s.archive_full('ledger','a'*64);s.close();s=Schedule(p)
   self.assertEqual(a,s.archive_full('ledger','a'*64))
   self.assertEqual(len(s.pending()),1);s.close()
