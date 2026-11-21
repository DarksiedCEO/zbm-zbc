import _runtime_path  # fixed repository-relative packaged runtime
import tempfile,unittest
from pathlib import Path
from repair_slots import slot,inventory
from incident_archive import Archive
from owner_wire import Refused
class Cases(unittest.TestCase):
 def test_shared_four_slots_no_overwrite(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   for i in range(3):slot(root,'cold-'+str(i)*32,create=True)
   a=Archive(root);a.append({'identity':'four','phase':'RESOLVED','evidence':{}})
   with self.assertRaises(Refused):slot(root,'cold-'+'f'*32,create=True)
   self.assertEqual(len(inventory(root)),3);self.assertEqual(a.alert()['delivery'],'PENDING_NOT_SENT')
 def test_total_bytes_refusal(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p=slot(root,'cold-'+'a'*32,create=True)
   for i in range(5):(p/str(i)).write_bytes(b'x'*250000)
   with self.assertRaises(Refused):inventory(root)
if __name__=='__main__':unittest.main()
