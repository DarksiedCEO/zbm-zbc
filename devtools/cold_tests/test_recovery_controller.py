import _runtime_path
import unittest
class Tests(unittest.TestCase):
 def test_provider_creation_bounds_not_operator_cutoff(self):
  from recovery_controller import provider_scope
  class Port:
   def _call(self,method,path,query=None):return 'ok',200,{'id':'cs_test','object':'checkout.session','created':100,'livemode':False,'client_reference_id':'inv_test','payment_intent':'pi_test'}
  p=provider_scope(Port(),{'account':'acct_test','invoice':'inv_test','client':'client_test','session':'cs_test','payment_intent':'pi_test'},200)
  self.assertEqual(p.cutoff,100)
  with self.assertRaises(Exception):provider_scope(Port(),{'account':'acct_test','invoice':'inv_changed','client':'client_test','session':'cs_test','payment_intent':'pi_test'},200)
 def test_stale_provider_creation_refuses(self):
  from recovery_controller import provider_scope
  class Port:
   def _call(self,*a,**kw):return 'ok',200,{'id':'cs_test','object':'checkout.session','created':1,'livemode':False,'client_reference_id':'inv_test','payment_intent':'pi_test'}
  with self.assertRaises(Exception):provider_scope(Port(),{'account':'acct_test','invoice':'inv_test','client':'client_test','session':'cs_test','payment_intent':'pi_test'},30*86400)

class SequenceTests(unittest.TestCase):
 def test_complete_sequence_and_failclosed(self):
  from recovery_controller import execute
  from types import SimpleNamespace
  from unittest.mock import patch
  from owner_wire import MEMBERS
  from coordinator import START
  for bad in (False,True):
   order=[];run='cold-'+'a'*32
   class Client:
    def __init__(self,m):self.m=m
    def call(self,path,b):
     if b.get('action')=='recovery-source':return {'member':self.m,'boot':'boot','set_id':run,'manifest_sha256':'bad' if bad else 'a'*64}
     order.append('relay:'+self.m);return {}
   class Co:
    def __init__(self):self.front=SimpleNamespace(close=lambda:order.append('close'));self.doc={'boots':{m:'boot' for m in MEMBERS}};self.clients={m:Client(m) for m in MEMBERS}
    def discover(self):order.append('discover')
    def command(self,m,a):order.append(a+':'+m)
    def update(self,**kw):order.append('durable-proof')
    def finish_recovery_bootstrap(self):order.append('bootstrap-retained')
    def publish(self):order.append('publish')
   value={'schema':'fixed-application-recovery-v1','restore':{},'coordination_token':'a'*64,'stripe_key':'_'.join(('sk','test','disposable')),'scope':{k:k+'_test' for k in ('account','invoice','client','session','payment_intent','acceptance','tenant','audit')}};value['scope']['mode']='TEST'
   data={'state':'DATA_RESTORED_CLOSED','business_admission':'NOT_OPENED','selected_catalog':{'set_id':run},'data':{'members':{m:{'manifest_sha256':'a'*64} for m in MEMBERS}}}
   # Constructor-only mocks isolate command sequencing, not native/providerevidence.
   provider=SimpleNamespace(inventory=lambda:{'events':{}},reconcile=lambda i,s: {'state':'RECONCILED_CLOSED'})
   with patch('recovery_controller.provider_scope',return_value=provider),patch('recovery_controller.bind'),patch('recovery_controller.observation',side_effect=lambda co,m,key,n: [] if key=='ledger' else {}),patch('recovery_controller.business_checks',return_value={}):
    if bad:
     with self.assertRaises(Exception):execute(value,restore=lambda _:data,coordinator_factory=lambda *a:Co(),provider_factory=lambda *a:None,deliveries=lambda _:[])
     self.assertNotIn('publish',order);self.assertFalse(any(x.startswith('start:') for x in order))
    else:
     r=execute(value,restore=lambda _:data,coordinator_factory=lambda *a:Co(),provider_factory=lambda *a:None,deliveries=lambda _:[])
     self.assertEqual(r['business_admission'],'OPEN_TEST');self.assertEqual(order[-3:],['durable-proof','bootstrap-retained','publish']);self.assertEqual([x for x in order if x.startswith('start:')],['start:'+m for m in START])
 def test_live_profile_mismatch_refuses_before_restore(self):
  from recovery_controller import execute
  calls=[]
  v={'schema':'fixed-application-recovery-v1','restore':{},'coordination_token':'a'*64,'stripe_key':'_'.join(('sk','test','disposable')),'scope':{k:k+'_test' for k in ('account','invoice','client','session','payment_intent','acceptance','tenant','audit')}};v['scope']['mode']='LIVE'
  with self.assertRaises(Exception):execute(v,restore=lambda _:calls.append('restore'),coordinator_factory=None,provider_factory=None,deliveries=None)
  self.assertEqual(calls,[])

class InterruptedTests(unittest.TestCase):
 def values(self):
  scope={'acceptance':'a','audit':'b','tenant':'t','invoice':'i'};legal={'acceptance_id':'a','doc_id':'d','version':'1.0','doc_sha256':'a'*64}
  return scope,{'invoice_id':'i','legal_ref':legal},dict(legal,evidence_sufficient=True,basis_in_force=True),{'audit_id':'b','tenant_id':'t','invoice_id':'i','status':'interrupted','completed_at':'2026-11-03T00:00:00Z','interrupted_reason':'RUN_FAILED','report':None,'report_sha256':None}
 def test_terminal_interrupted_no_report_is_safe_refusal(self):
  from recovery_controller import business_checks
  s,i,l,a=self.values();v=business_checks(i,l,a,[],s)
  self.assertEqual(v['audit_disposition'],'TERMINAL_INTERRUPTED_NO_REEXECUTION')
 def test_ambiguous_or_report_bearing_interruption_refuses(self):
  from recovery_controller import business_checks
  for changes in ({'status':'running_elsewhere_or_interrupted'},{'status':'running'},{'report':{}},{'report_sha256':'b'*64},{'completed_at':None},{'invoice_id':'other'}):
   s,i,l,a=self.values();a.update(changes)
   with self.assertRaises(Exception):business_checks(i,l,a,[],s)
  s,i,l,a=self.values()
  with self.assertRaises(Exception):business_checks(i,l,a,[{'report_id':'r'}],s)

from unittest.mock import patch
class EntrypointTests(unittest.TestCase):
 def test_configured_token_mismatch_refuses_before_execution(self):
  from recovery_controller import main
  with patch('recovery_controller.frame',return_value={'coordination_token':'b'*64}),patch('recovery_controller.execute') as execute:
   self.assertEqual(main(argv=[],expected_token='a'*64),2);execute.assert_not_called()

class LockedTests(unittest.TestCase):
 def test_durable_refusal_before_observer_and_same_identity(self):
  import tempfile,json
  from pathlib import Path
  from recovery_controller import retain_locked
  from b2_catalog import CATBASE
  run='cold-'+'a'*32;loc={'schema':'b2-catalog-locator-v1','set_id':run,'repository':CATBASE+run,**{k:'b'*64 for k in ('snapshot','catalog_sha256','members_sha256','seals_sha256')}}
  value={'coordination_token':'a'*64,'restore':{'selection':{'locator':loc,'cutoff_utc':'2026-11-03T00:00:00Z'}}}
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();calls=[]
   class Client:
    def call(self,path,body):
     assert json.loads((root/'recovery-locked.json').read_text())==body
     calls.append(body);return {'event_id':'c'*64,'retained':True}
   retain_locked(value,root=root,client=Client());retain_locked(value,root=root,client=Client());self.assertEqual(calls[0],calls[1])
   value['restore']['selection']['locator']['snapshot']='d'*64
   with self.assertRaises(Exception):retain_locked(value,root=root,client=Client())
   self.assertEqual(len(calls),2)
