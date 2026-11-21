import _runtime_path
import copy,hashlib,tempfile,unittest
from pathlib import Path
from owner_state import Journal
from owner_wire import Refused,canonical
OLD='cold-'+'a'*32;NEW='cold-'+'b'*32
class Bootstrap(unittest.TestCase):
 def journal(self,p):
  j=Journal(p,'ledger','boot')
  for action,i in [('start',1),('verify',1),('verify',2)]:
   op=f'{OLD}:ledger:{action}:{i}';j.begin(OLD,op,action);j.finish(op,{'member':'ledger','boot':'boot','run':OLD,'action':action,'result':{'public':'CLOSED'}})
  return j
 def permit(self,j):
  return {'schema':'recovery-bootstrap-v1','member':'ledger','boot':'boot','run':OLD,'next_run':NEW,'operations_sha256':hashlib.sha256(canonical(j.doc['operations'])).hexdigest(),'recovery_sha256':'c'*64}
 def test_count_and_different_run_require_proved_transition(self):
  from coordinator import Coordinator
  c=Coordinator.__new__(Coordinator);c.counts={('ledger','verify'):2};c.root=Path('/unused');c.doc={}
  with self.assertRaisesRegex(Refused,'fixed run action bound'):c.command('ledger','verify')
  with tempfile.TemporaryDirectory() as d:
   j=self.journal(Path(d)/'owner.json');original=copy.deepcopy(j.doc)
   with self.assertRaisesRegex(Refused,'different run'):j.begin(NEW,NEW+':ledger:publish:1','publish')
   p=self.permit(j);j.retire_recovery(p)
   self.assertEqual(j.doc['last_recovery']['original'],original)
   self.assertIsNone(j.doc['run']);self.assertEqual(j.doc['operations'],{})
   self.assertTrue(j.begin(NEW,NEW+':ledger:publish:1','publish'));j.close()
 def test_pending_mismatch_boot_and_second_transition_refuse(self):
  with tempfile.TemporaryDirectory() as d:
   j=self.journal(Path(d)/'owner.json');p=self.permit(j)
   for field,value in [('boot','other'),('operations_sha256','d'*64),('next_run',OLD)]:
    wrong=dict(p);wrong[field]=value
    with self.assertRaises(Refused):j.retire_recovery(wrong)
   j.doc['operations'][OLD+':ledger:verify:2']['phase']='PENDING'
   with self.assertRaises(Refused):j.retire_recovery(self.permit(j))
   j.doc['operations'][OLD+':ledger:verify:2']['phase']='DONE';j.retire_recovery(p)
   with self.assertRaises(Refused):j.retire_recovery(p)
   j.close()
if __name__=='__main__':unittest.main()

class Composition(unittest.TestCase):
 def test_actual_coordinator_transition_retains_evidence_and_next_verify(self):
  import types
  from coordinator import Coordinator
  from owner_wire import MEMBERS
  from recovery_bootstrap import permit,digest
  with tempfile.TemporaryDirectory() as d:
   c=Coordinator.__new__(Coordinator);c.root=Path(d);c.path=c.root/'run.json';c.counts={};c.front=types.SimpleNamespace(opened=False);c.clients={};journals={};phases={m:'VERIFIED_CLOSED' for m in MEMBERS}
   c.doc={'schema':1,'run':OLD,'phase':'RECOVERY_RECONCILED_CLOSED','boots':{m:'boot-'+m for m in MEMBERS},'receipts':{},'barrier':None,'seals':{},'recovery':{'selected_catalog':{'set_id':'cold-'+'c'*32},'provider':{'state':'RECONCILED_CLOSED'},'applications':{'state':'VERIFIED'},'mode':'TEST','operator_cutoff_used':False}}
   for m in MEMBERS:
    j=Journal(c.root/(m+'.json'),m,'boot-'+m);journals[m]=j
    for action,i in [('start',1),('verify',1),('verify',2)]:
     op=f'{OLD}:{m}:{action}:{i}';b={'member':m,'boot':'boot-'+m,'run':OLD,'action':action,'operation':op};r={k:b[k] for k in ('member','boot','run','action')};r['result']={'public':'CLOSED'};j.begin(OLD,op,action);j.finish(op,r);c.doc['receipts'][op]={'phase':'DONE','body':b,'receipt':r};c.counts[(m,action)]=i
   def status(m,op=None):return {'member':m,'boot':'boot-'+m,'phase':phases[m],'run':journals[m].doc['run'],'child_pid':123,'fenced':False,'operations':copy.deepcopy(journals[m].doc['operations'])}
   c.status=status
   class Client:
    def __init__(self,m):self.m=m
    def call(self,path,b):
     m=self.m;j=journals[m]
     if b['action']=='recovery-bootstrap-retire':
      p=permit(c,{'kind':'recovery-bootstrap-permit','member':m,'boot':'boot-'+m,'run':OLD});self.outer.assertEqual(digest(p),b['proof_sha256']);j.retire_recovery(p);return {'member':m,'boot':'boot-'+m,'proof_sha256':digest(p),'state':'RECOVERY_BOOTSTRAP_RETAINED'}
     j.begin(b['run'],b['operation'],b['action']);r={k:b[k] for k in ('member','boot','run','action')};r['result']={'native':'checked'};j.finish(b['operation'],r);return {}
   Client.outer=self
   c.clients={m:Client(m) for m in MEMBERS};c.save(c.doc);old=copy.deepcopy(c.doc)
   c.finish_recovery_bootstrap();new=c.doc['run'];self.assertNotEqual(new,OLD);self.assertNotEqual(new,old['recovery']['selected_catalog']['set_id']);self.assertEqual(c.counts,{})
   archive=__import__('json').loads((c.root/'recovery-bootstrap.json').read_bytes());self.assertEqual(archive['old'],old)
   for m in MEMBERS:
    self.assertEqual(journals[m].doc['last_recovery']['original']['run'],OLD)
    self.assertEqual(c.command(m,'verify'),{'native':'checked'});self.assertEqual(c.counts[(m,'verify')],1)
   with self.assertRaises(Refused):c.finish_recovery_bootstrap()
   for j in journals.values():j.close()

class Persistence(unittest.TestCase):
 def test_failed_retirement_write_preserves_original_and_restart_refuses(self):
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'owner.json';helper=Bootstrap();j=helper.journal(path);before=path.read_bytes();p=helper.permit(j)
   with patch.object(j,'persist',side_effect=OSError('synthetic write refusal')):
    with self.assertRaises(OSError):j.retire_recovery(p)
   self.assertEqual(path.read_bytes(),before);self.assertNotIn('last_recovery',j.doc);j.close()
   reopened=Journal(path,'ledger','new-boot')
   with self.assertRaises(Refused):reopened.retire_recovery(p)
   reopened.close()
 def test_retired_identity_and_wrong_first_maintenance_refuse(self):
  with tempfile.TemporaryDirectory() as d:
   h=Bootstrap();j=h.journal(Path(d)/'owner.json');j.retire_recovery(h.permit(j))
   for run in (OLD,'cold-'+'e'*32):
    with self.assertRaises(Refused):j.begin(run,run+':ledger:publish:1','publish')
   j.close()
