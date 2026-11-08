import _runtime_path  # fixed repository-relative packaged runtime
import copy,unittest
from unittest.mock import patch
from types import SimpleNamespace
from owner_wire import Refused,MEMBERS
from idle_rearm import intent
from repair_cli import operator_command
class Cases(unittest.TestCase):
 def state(self,phase='DONE'):
  old={'phase':'OPEN','run':'cold-'+'c'*32,'boots':dict.fromkeys(MEMBERS,'a'*32),'last_success':{'run':'cold-'+'d'*32,'catalog_sha256':'e'*64,'catalog_snapshot':'f'*64},'receipts':{},'seals':{},'barrier':None}
  v=intent(old,dict(old['boots'],finance='b'*32));v.update(phase=phase,observer={'monitoring':{'state':'ARMED','expected_due':200},'current':{'due':100}},locks={m:{'member':m} for m in MEMBERS},verified={m:{'member':m} for m in MEMBERS});return v
 def client(self,states,*,lost=False):
  calls=[]
  def call(path,body):
   calls.append((path,copy.deepcopy(body)))
   if path=='/repair':
    if lost:raise Refused('lost submit acknowledgment')
    return {'phase':'STARTING'}
   return copy.deepcopy(states.pop(0) if len(states)>1 else states[0])
  return SimpleNamespace(call=call),calls
 def test_fixed_command_success_and_lost_ack_no_resubmit(self):
  for lost in (False,True):
   expected=self.state();c,calls=self.client([self.state('VERIFIED'),expected],lost=lost)
   with patch('repair_cli.time.sleep'):self.assertEqual(operator_command(c,'rearm-idle'),expected)
   self.assertEqual([x for x in calls if x[0]=='/repair'],[('/repair',{'command':'rearm-idle'})])
   self.assertTrue(all(b=={'command':'rearm-idle'} for _,b in calls))
 def test_unknown_incomplete_and_changed_identity_refuse(self):
  bad=self.state();bad['verified'].pop('seo')
  changed=self.state();changed['identity']='0'*64
  for states in ([self.state('UNKNOWN')],[bad],[self.state('LOCKED'),changed]):
   c,calls=self.client(list(states))
   with patch('repair_cli.time.sleep'),self.assertRaises(Refused):operator_command(c,'rearm-idle')
   self.assertEqual(sum(p=='/repair' for p,_ in calls),1)
 def test_deadline_no_reexecution(self):
  c,calls=self.client([self.state('PREPARED')])
  with patch('repair_cli.time.monotonic',side_effect=[0,0,601]),patch('repair_cli.time.sleep'),self.assertRaisesRegex(Refused,'deadline'):operator_command(c,'rearm-idle')
  self.assertEqual(sum(p=='/repair' for p,_ in calls),1)
 def test_initial_status_unavailable_only_reads_retry(self):
  final=self.state();c,calls=self.client([final]);original=c.call;count=[0]
  def call(path,body):
   if path=='/repair-status' and count[0]==0:
    count[0]+=1;calls.append((path,dict(body)));raise Refused('not yet persisted')
   return original(path,body)
  c.call=call
  with patch('repair_cli.time.sleep'):self.assertEqual(operator_command(c,'rearm-idle'),final)
  self.assertEqual(sum(p=='/repair' for p,_ in calls),1)
  self.assertEqual(sum(p=='/repair-status' for p,_ in calls),2)
 def test_arbitrary_reset_never_sent(self):
  c,calls=self.client([self.state()])
  with self.assertRaises(Refused):operator_command(c,'rearm-idle --healthy')
  self.assertEqual(calls,[])
if __name__=='__main__':unittest.main()
