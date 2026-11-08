import _runtime_path  # fixed repository-relative packaged runtime
import copy,unittest
from idle_rearm import eligible,intent
from owner_wire import Refused,MEMBERS
class Cases(unittest.TestCase):
 def setUp(self):
  self.success={'run':'cold-'+'a'*32,'catalog_sha256':'b'*64,'catalog_snapshot':'c'*64}
  self.j={'member':'ledger','boot':'a'*32,'run':None,'operations':{},'last_success':self.success,'last_retirement':{'record':{'phase':'DONE','action':'retire','receipt':{'result':self.success}}}}
 def test_pending_failed_current_refused(self):
  eligible(self.j,'ledger','a'*32,self.success)
  for phase in ('PENDING','FAILED','DONE'):
   j=copy.deepcopy(self.j);j['operations']['x']={'phase':phase}
   with self.assertRaises(Refused):eligible(j,'ledger','a'*32,self.success)
 def test_old_boot_and_catalog_refused(self):
  with self.assertRaises(Refused):eligible(self.j,'ledger','d'*32,self.success)
  j=copy.deepcopy(self.j);j['last_retirement']['record']['receipt']['result']={}
  with self.assertRaises(Refused):eligible(j,'ledger','a'*32,self.success)
 def test_intent_exact_roster_and_idle(self):
  old={'phase':'OPEN','run':'cold-'+'d'*32,'boots':dict.fromkeys(MEMBERS,'a'*32),'last_success':self.success,'receipts':{},'seals':{},'barrier':None}
  new=dict(old['boots'],ledger='e'*32);v=intent(old,new)
  self.assertEqual(v,intent(old,new))
  for key,value in [('schedule',{'due':1}),('phase','FAILED'),('receipts',{'x':{}})]:
   with self.assertRaises(Refused):intent(dict(old,**{key:value}),new)
if __name__=='__main__':unittest.main()
