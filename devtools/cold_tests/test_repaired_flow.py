import _runtime_path  # fixed repository-relative packaged runtime
import unittest,copy,threading,tempfile
from types import SimpleNamespace as NS
from pathlib import Path
from unittest.mock import patch
from test_typed_cleanup_coordinator import CoordinatorTests as Base
from test_typed_cleanup import Tests as OwnerBase
import repaired_retirement as rr
import repaired_owner as ro
from repaired_observer import complete
from metadata_retirement import save
from owner_wire import MEMBERS,Refused,decode
from schedule_state import Schedule
class Flow(unittest.TestCase):
 fresh=Base.fresh;cleanup=Base.cleanup;expected=Base.expected
 def setUp(self):
  Base.setUp(self);c=self.c;c.path=c.root/'run.json';c.doc.update(schedule={'due':100,'trigger_id':'scheduled-100','run':c.doc['run']},phase='COMPLETE',repair_rpc={'phase':'RESOLVED','request':{'command':'finish-cleanup'}},repair_intent={'phase':'RESOLVED','original_sha256':'f'*64},receipts={'original':{'phase':'FAILED','failure':'kept'}},cleaned={m:self.expected(m) for m in MEMBERS})
  c.control_lock=threading.RLock();c.normal_active=False;c.repair_worker=None;c.counts={};c.completion=NS(published=None);c.update=lambda **kw:None
  save(c.path,c.doc);self.old=copy.deepcopy(c.doc);self.s=Schedule(c.root/'observer.db');self.addCleanup(self.s.close);self.s.arm(100,86400);self.s.bind(100,c.doc['run']);self.s.dispatched(100,c.doc['run']);self.s.failed();self.archives={}
  def observer(path,body):
   if path=='/status':return {'current':self.s.current(),'monitoring':self.s.monitoring()}
   return complete(self.s,rr.completion(c,body['due'],body['run']))
  c.observer=NS(call=observer)
  def call(m,path,body):
   rr.permit(c,{'kind':'repaired-permit','run':body['run'],'member':m,'boot':body['boot']})
   value={'member':m,'boot':body['boot'],'run':body['run'],'identity':body['identity'],'archive_sha256':str(list(MEMBERS).index(m)+1)*64,'ready':body['action']=='repaired-ready'}
   self.archives[m]=value;return value
  c.clients={m:NS(call=lambda path,body,m=m:call(m,path,body)) for m in MEMBERS}
  def command(m,a):
   self.assertEqual(a,'retire');self.assertEqual(set(self.archives),set(MEMBERS));self.assertIn('fresh-all-five',self.events)
   run=self.old['run'];op=run+':'+m+':retire:1';receipt={'member':m,'boot':self.old['boots'][m],'run':run,'action':'retire','result':rr.summary(self.old)}
   self.states[m]['operations'][op]={'phase':'DONE','receipt':receipt};self.states[m].update(run=None,last_success=rr.summary(self.old));c.doc['receipts'][op]={'phase':'DONE','receipt':receipt};save(c.path,c.doc);return receipt['result']
  c.command=command
 def test_full_disposition_preserves_incident_and_advances(self):
  result=rr.finish(self.c);self.assertEqual(result['phase'],'DONE');self.assertEqual(self.c.doc['phase'],'OPEN');self.assertNotEqual(self.c.doc['run'],self.old['run']);self.assertEqual(self.s.current()['phase'],'REPAIRED_COMPLETE');self.assertEqual(self.s.monitoring()['expected_due'],86500)
  self.assertEqual(rr.original(self.c),self.old);self.assertEqual(rr.finish(self.c),result)
 def test_upload_disposition_uses_same_strict_path(self):
  self.c.doc['repair_rpc']['request']['command']='retry-upload';save(self.c.path,self.c.doc);self.old=copy.deepcopy(self.c.doc)
  self.assertEqual(rr.finish(self.c)['phase'],'DONE')
 def test_remote_failure_before_archive_or_retirement(self):
  self.c.catalog.fresh=lambda run:(_ for _ in ()).throw(Refused('missing remote'))
  with self.assertRaises(Refused):rr.finish(self.c)
  self.assertFalse(rr.paths(self.c)[0].exists());self.assertEqual(self.archives,{})
 def test_unresolved_and_newboot_refuse(self):
  self.c.doc['repair_rpc']['phase']='APPLYING';save(self.c.path,self.c.doc)
  with self.assertRaises(Refused):rr.finish(self.c)
  self.c.doc['repair_rpc']['phase']='RESOLVED';save(self.c.path,self.c.doc);self.states['finance']['boot']='e'*32
  with self.assertRaises(Refused):rr.finish(self.c)
 def test_lost_observer_ack_resumes_exact_state(self):
  real=self.c.observer.call;lost=[True]
  def callback(path,body):
   result=real(path,body)
   if path=='/submit' and lost[0]:lost[0]=False;raise Refused('lost acknowledgment')
   return result
  self.c.observer.call=callback
  with self.assertRaises(Refused):rr.finish(self.c)
  self.assertEqual(rr.state(self.c)['phase'],'RETIRED');self.assertEqual(rr.finish(self.c)['phase'],'DONE');self.assertEqual(rr.original(self.c),self.old)
 def test_incomplete_retire_refuses_new_identity(self):
  real=self.c.command
  def interrupted(m,a):
   if m=='finance':self.c.doc['receipts'][self.old['run']+':finance:retire:1']={'phase':'PENDING'};raise Refused('unknown before owner ack')
   return real(m,a)
  self.c.command=interrupted
  with self.assertRaises(Refused):rr.finish(self.c)
  self.c.command=real
  with self.assertRaisesRegex(Refused,'uncertain retire'):rr.finish(self.c)
  self.assertEqual(rr.state(self.c)['phase'],'ARCHIVED');self.assertEqual(self.s.current()['phase'],'FAILED')
class ActualOwner(unittest.TestCase):
 def setUp(self):OwnerBase.setUp(self)
 run_repair=OwnerBase.run_repair
 def test_actual_journal_archive_before_retire_and_ready(self):
  import distributed_transfer as d
  with patch('owner_wire.Client.call',return_value=self.proof),patch.object(d.Transfer,'fresh',return_value=self.loc):self.run_repair()
  permission={'identity':'a'*64,'summary':{'run':self.o.sealed['set_id'],'catalog_sha256':self.proof['catalog_sha256'],'catalog_snapshot':self.proof['catalog_snapshot']},'proof':self.proof}
  body={'member':'finance','boot':self.o.boot,'run':self.o.sealed['set_id'],'action':'repaired-archive','identity':'a'*64}
  with patch('repaired_owner.Client.call',return_value=permission):
   result=ro.dispatch(self.o,body);arch=ro.load(self.o.root/'repair-archives'/body['run']/'repaired-archive.json');self.assertEqual(arch['journal'],self.o.journal.doc)
   op=body['run']+':finance:retire:1';resolved=self.m.resolved();self.o.journal.begin(body['run'],op,'retire',resolved=resolved);self.o.journal.retire(permission['summary'],op,resolved=resolved)
   ready=ro.dispatch(self.o,dict(body,action='repaired-ready'));self.assertEqual(ready,dict(result,ready=True));self.assertEqual(ro.load(self.o.root/'repair-archives'/body['run']/'repaired-archive.json'),arch)

class Interruptions(Flow):
 def test_next_run_blocked_until_disposition_done(self):
  from schedule_advance import require_no_pending
  actual=rr.save_state;held=[True]
  def fail_terminal(c,s):
   if s['phase']=='DONE' and held[0]:held[0]=False;raise OSError('synthetic terminal persistence')
   return actual(c,s)
  with patch.object(rr,'save_state',side_effect=fail_terminal):
   with self.assertRaises(OSError):rr.finish(self.c)
  self.assertEqual(self.c.doc['phase'],'OPEN')
  with self.assertRaisesRegex(Refused,'finish-repaired-run'):require_no_pending(self.c)
  self.assertEqual(rr.finish(self.c)['phase'],'DONE');require_no_pending(self.c)
 def test_fixed_archive_not_overwritten_by_another_run(self):
  rr.finish(self.c);saved=rr.original(self.c);original_path=rr.paths(self.c)[0];self.c.doc['run']='cold-'+'d'*32;save(self.c.path,self.c.doc)
  with self.assertRaises(Refused):rr.finish(self.c)
  self.assertEqual(rr.decode(rr.read(original_path)),saved)
 def test_capacity_refusal_before_original_archive(self):
  with patch('metadata_retirement.os.statvfs',return_value=NS(f_bavail=0,f_frsize=4096,f_favail=0)):
   with self.assertRaisesRegex(Refused,'headroom'):rr.finish(self.c)
  self.assertFalse(rr.paths(self.c)[0].exists());self.assertEqual(self.archives,{})
