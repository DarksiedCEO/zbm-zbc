import _runtime_path
import unittest,tempfile,json,hashlib
from pathlib import Path
from unittest.mock import Mock,patch
from owner_wire import canonical,Refused
class ServiceTests(unittest.TestCase):
 def test_immutable_selection_restart_and_fresh_namespace(self):
  from recovery_service import Service
  from b2_catalog import CATBASE
  run='cold-'+'a'*32;selection={'locator':{'schema':'b2-catalog-locator-v1','set_id':run,'repository':CATBASE+run,**{k:'b'*64 for k in ('snapshot','catalog_sha256','members_sha256','seals_sha256')}},'cutoff_utc':'2026-11-03T00:00:00Z'}
  scope={k:k+'_test' for k in ('account','invoice','client','session','payment_intent','acceptance','tenant','audit')};scope['mode']='TEST'
  cfg={'token':'a'*64,'password':'synthetic-password','credentials':{'key_id':'id','key':'key'},'stripe_key':'_'.join(('sk','test','key')),'gateway_auth':{'service_token':'service','caller_token':'caller'}}
  with tempfile.TemporaryDirectory() as d:
   with patch('recovery_service.threading.Thread'):
    s=Service(Path(d).resolve(),cfg);v=s.dispatch('/submit',{'kind':'recover','selection':selection,'scope':scope})
    self.assertEqual(v['state'],'RUNNING');self.assertTrue((Path(d)/'recovery-selection.json').exists())
    self.assertEqual(s.dispatch('/submit',{'kind':'recover','selection':selection,'scope':scope}),v)
    changed=json.loads(json.dumps(selection));changed['locator']['snapshot']='d'*64
    with self.assertRaises(Refused):s.dispatch('/submit',{'kind':'recover','selection':changed,'scope':scope})
   os=__import__('os');os.close(s.fd);s.fd=None
   r=Service(Path(d).resolve(),cfg);self.assertEqual(r.status()['state'],'LOCKED');self.assertIsNone(r.worker);os.close(r.fd);r.fd=None
 def test_next_namespace_is_not_selected_catalog(self):
  from recovery_service import construct_coordinator
  cfg={'token':'a'*64,'password':'pw','credentials':{'key_id':'id','key':'key'}}
  with patch('coordinator.Coordinator') as C:
   construct_coordinator(Path('/volume/control'),cfg)
   self.assertNotIn('run_id',C.call_args.kwargs);self.assertEqual(C.call_args.kwargs['serve_control'],False)

 def test_worker_publication_and_unknown_are_retained(self):
  from recovery_service import Service
  from b2_catalog import CATBASE
  run='cold-'+'a'*32;value={'kind':'recover','selection':{'locator':{'schema':'b2-catalog-locator-v1','set_id':run,'repository':CATBASE+run,**{k:'b'*64 for k in ('snapshot','catalog_sha256','members_sha256','seals_sha256')}},'cutoff_utc':'2026-11-03T00:00:00Z'},'scope':{k:k+'_test' for k in ('account','invoice','client','session','payment_intent','acceptance','tenant','audit')}};value['scope']['mode']='TEST'
  cfg={'token':'a'*64,'password':'password','credentials':{'key_id':'id','key':'key'},'stripe_key':'_'.join(('sk','test','key')),'gateway_auth':{'service_token':'service','caller_token':'caller'}}
  for failure in (False,True):
   with tempfile.TemporaryDirectory() as d,patch('recovery_service.threading.Thread'),patch('recovery_service.controller.retain_locked') as alert:
    s=Service(Path(d).resolve(),cfg);s.dispatch('/submit',value);co=Mock();co.doc={'run':'cold-'+'b'*32};co.catalog_server=None
    def execute(*a,**kw):
     self.assertTrue((Path(d)/'recovery-selection.json').exists())
     self.assertIs(kw['coordinator_factory'](),co)
     if failure:raise RuntimeError('unknown')
     co.publish();return {'state':'APPLICATION_RECOVERED'}
    with patch('recovery_service.threading.Timer'),patch('recovery_service.construct_coordinator',return_value=co),patch('recovery_service.controller.execute',side_effect=execute):s.work()
    self.assertEqual(s.status()['state'],'LOCKED' if failure else 'OPEN')
    if failure:alert.assert_called();co.front.close.assert_called();self.assertFalse((Path(d)/'recovery-service-result.json').exists())
    else:
     self.assertEqual(json.loads((Path(d)/'recovery-service-result.json').read_text())['maintenance_run'],'cold-'+'b'*32)
     self.assertNotEqual(co.doc['run'],run);alert.assert_not_called()
    __import__('os').close(s.fd);s.fd=None

 def test_delivery_wait_uses_separate_pre_replay_observation(self):
  from recovery_service import Service
  from recovery_gateway import Inbox
  from test_recovery_gateway import fixture
  s=object.__new__(Service);s.co=object();s.inbox=Inbox();inv,envelope=fixture()
  with patch('recovery_service.controller.observation',return_value=b'{"data":{"ops":[]}}\n') as observe:
   iterator=s.deliveries(inv);received=[]
   import threading,time
   def producer():
    for _ in range(100):
     if s.inbox.inventory is not None:break
     time.sleep(.001)
    received.append(s.inbox.accept(envelope))
   t=threading.Thread(target=producer);t.start();raw,sig=next(iterator);t.join(1)
   self.assertEqual(raw.decode(),envelope['payload']);self.assertEqual(sig,envelope['signature'])
   self.assertEqual(s.inbox.rows['evt_test']['state'],'PENDING')
   with self.assertRaises(StopIteration):next(iterator)
   self.assertEqual(s.inbox.rows['evt_test']['state'],'DONE');observe.assert_called_once_with(s.co,'finance','finance-log',1)
