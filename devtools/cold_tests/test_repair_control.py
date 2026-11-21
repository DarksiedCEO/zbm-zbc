import _runtime_path  # fixed repository-relative packaged runtime
import copy,threading,unittest
from types import SimpleNamespace
from unittest.mock import patch
from coordinator_repair import submit,inspect
from repair_state import digest
from owner_wire import Refused
class Tests(unittest.TestCase):
 def coordinator(self):
  c=SimpleNamespace(doc={'run':'cold-'+'a'*32,'phase':'FAILED'},control_lock=threading.RLock(),normal_active=False,repair_worker=None)
  c.update=lambda **kw:c.doc.update(copy.deepcopy(kw));c.retire=lambda:None
  return c
 def test_early_ack_durable_intent_and_no_lost_reply_reexecution(self):
  c=self.coordinator();held=threading.Event();entered=threading.Event()
  def effect(co):entered.set();held.wait(2);return {'phase':'COMPLETE'}
  request={'command':'retry-upload','run':c.doc['run'],'incident_sha256':digest(c.doc)}
  with patch('typed_coordinator_repair.preserve'),patch('typed_coordinator_repair.retry_upload',side_effect=effect) as worker:
   submit(c,request);self.assertTrue(entered.wait(1));self.assertEqual(c.doc['repair_rpc']['phase'],'APPLYING')
   submit(c,request);self.assertEqual(worker.call_count,1);held.set();c.repair_worker.join(2)
  self.assertEqual(c.doc['repair_rpc']['phase'],'RESOLVED')
 def test_wrong_identity_and_durable_failure_no_worker(self):
  c=self.coordinator();request={'command':'retry-upload','run':c.doc['run'],'incident_sha256':'f'*64}
  with self.assertRaises(Refused):submit(c,request)
  request['incident_sha256']=digest(c.doc)
  def fail(**kw):raise OSError('disk')
  c.update=fail
  with patch('typed_coordinator_repair.preserve'),self.assertRaises(OSError):submit(c,request)
  self.assertIsNone(c.repair_worker)
 def test_restart_applying_status_never_replays(self):
  c=self.coordinator();request={'command':'retry-upload','run':c.doc['run'],'incident_sha256':digest(c.doc)}
  c.doc['repair_rpc']={'request':request,'phase':'APPLYING','result':None}
  with patch('typed_coordinator_repair.retry_upload') as worker:submit(c,request);worker.assert_not_called()
if __name__=='__main__':unittest.main()
