import _runtime_path
import hashlib,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import capacity_policy as p
import cold_capture,quota_capture,b2_adapter as b,bounded_process as process
from test_member_restore import MemberRestore

def policy(n=2*p.MIB):return json.dumps({'version':1,'sources':{m:n for m in p.MEMBERS}})
class Capacity(unittest.TestCase):
 def setUp(self):self.state=patch.object(p,'_loaded',None);self.state.start()
 def tearDown(self):self.state.stop()
 def load(self,n=2*p.MIB):return p.initialize({p.SETTING:policy(n)})
 def test_original_fixture_profile(self):
  self.assertEqual(p.member('finance'),{'source':524288,'stage':1048576,'repository':2097152,'control_reserve':1048576})
  self.assertEqual(p.member('ledger')['source'],2*p.MIB)
  self.assertEqual((p.capture_bytes('seo'),p.spool_bytes(),p.output_bytes()),(8*p.MIB,24*p.MIB,8*p.MIB))
 def test_required_immutable_and_digest(self):
  with self.assertRaises(ValueError):p.initialize({})
  digest=self.load();self.assertEqual(digest,hashlib.sha256(p.parse(policy()).encode()).hexdigest())
  self.assertEqual(self.load(),digest)
  with self.assertRaises(ValueError):self.load(p.MIB)
 def test_malformed_and_oversized_refused(self):
  cases=['{}','{"version":1,"version":1,"sources":{}}',policy(0),policy(True),policy(8*p.MIB+1),policy(7*p.MIB)]
  for raw in cases:
   with self.subTest(raw=raw[:30]),self.assertRaises(ValueError):p.parse(raw)
 def test_derivation_bounded(self):
  rows={m:8*p.MIB for m in p.MEMBERS};rows['seo']=1;rows['legal']=7*p.MIB
  self.assertLess(sum(rows.values()),32*p.MIB)
  p.initialize({p.SETTING:json.dumps({'version':1,'sources':rows})})
  self.assertEqual(p.member('ledger')['repository'],18*p.MIB)
  self.assertEqual(p.capture_bytes('ledger'),55*p.MIB)
  self.assertEqual(p.spool_bytes(),165*p.MIB)
  self.assertLessEqual(p.output_bytes(),96*p.MIB)
 def test_source_bytes_and_fixed_count(self):
  self.load()
  with tempfile.TemporaryDirectory() as d:
   f=Path(d)/'book';f.write_bytes(b'x'*(2*p.MIB));self.assertEqual(cold_capture.source_inventory(d,'finance')['book']['bytes'],2*p.MIB)
   with f.open('ab') as out:out.write(b'x')
   with self.assertRaises(ValueError):cold_capture.source_inventory(d,'finance')
   f.unlink()
   for n in range(33):(Path(d)/str(n)).write_bytes(b'x')
   with self.assertRaises(ValueError):cold_capture.source_inventory(d,'finance')
 def test_stage_retained_and_repository_bounds(self):
  self.load()
  with tempfile.TemporaryDirectory() as d:
   manifest={'member':'finance','files':{'book':{'bytes':2*p.MIB,'sha256':'1'*64}}}
   self.assertEqual(quota_capture.preflight(d,manifest)['limit'],p.capture_bytes('finance'))
   manifest['files']['book']['bytes']=3*p.MIB
   with self.assertRaises(ValueError):quota_capture.preflight(d,manifest)
   repo=Path(d)/'pack'
   with repo.open('wb') as f:f.truncate(p.member('finance')['repository']+1)
   with self.assertRaisesRegex(b.Refused,'LOCAL_BYTES'):b.local_inventory(d,p.member('finance')['repository'])
   with repo.open('wb') as f:f.truncate(p.capture_bytes('finance'))
   manifest['files']['book']['bytes']=2*p.MIB
   with self.assertRaises(ValueError):quota_capture.preflight(d,manifest)
 def test_spool_actual_sparse_file_bound(self):
  self.load()
  with tempfile.TemporaryDirectory() as d:
   f=Path(d)/'spool'
   with f.open('wb') as out:out.truncate(p.spool_bytes())
   b.spool_guard(d)
   with f.open('ab') as out:out.write(b'x')
   with self.assertRaisesRegex(b.Refused,'SPOOL_BYTE_BOUND'):b.spool_guard(d)
 def test_coordinator_refuses_different_or_missing_owner_policy(self):
  from coordinator import Coordinator
  from owner_wire import Refused
  from types import SimpleNamespace
  digest=self.load();co=Coordinator.__new__(Coordinator);co.doc={'boots':{}}
  row={'member':'finance','fenced':False,'boot':'synthetic'}
  co.clients={'finance':SimpleNamespace(call=lambda *a:row)}
  for value in (None,'0'*64):
   if value is None:row.pop('capacity_policy_sha256',None)
   else:row['capacity_policy_sha256']=value
   with self.assertRaisesRegex(Refused,'capacity policy mismatch'):co.status('finance')
  row['capacity_policy_sha256']=digest;self.assertIs(co.status('finance'),row)
 def test_packaged_restore_missing_policy_refuses_before_input(self):
  import packaged_restore,io
  stream=unittest.mock.Mock();out=io.StringIO()
  with patch.dict(os.environ,{},clear=True):
   self.assertEqual(packaged_restore.main(stream,out,[]),2)
  stream.readline.assert_not_called();self.assertEqual(out.getvalue(),'RESTORE_REFUSED\n')
 def test_pure_derivation_does_not_mutate_startup(self):
  value=p.derive_member(policy(),'finance');self.assertEqual(value['source'],2*p.MIB);self.assertFalse(p.configured())
 def test_native_recovery_qualification_refuses_before_volume_writes(self):
  import recovery_owner_boot,admin_entrypoint
  from unittest.mock import Mock
  self.load(2*p.MIB)
  with self.assertRaisesRegex(ValueError,'unqualified'):p.require_application_recovery(policy())
  with patch('volume_layout.Layout') as layout,patch('recovery_owner_boot.os.open') as opened:
   with self.assertRaisesRegex(ValueError,'unqualified'):recovery_owner_boot.run({})
   layout.assert_not_called();opened.assert_not_called()
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64,p.SETTING:policy()}
  with patch.object(admin_entrypoint,'volume') as volume:
   with self.assertRaisesRegex(ValueError,'unqualified'):admin_entrypoint.main(env,['recover'])
   volume.assert_not_called()
  legacy=json.dumps({'version':1,'sources':{m:2*p.MIB if m in ('ledger','compliance') else p.MIB//2 for m in p.MEMBERS}})
  self.assertTrue(p.require_application_recovery(legacy))
 def test_actual_subprocess_aggregate_and_per_command(self):
  self.load();budget=process.Budget.start(output_bytes=p.output_bytes('finance'))
  with patch.object(process.backend_quiescence,'created'),patch.object(process.backend_quiescence,'reaped'):
   cmd=[sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*(5*1048576))']
   for _ in range(2):self.assertEqual(len(process.run_fixed(cmd,env={'PATH':os.environ['PATH']},cwd=None,budget=budget,output_limit=6*p.MIB)),5*p.MIB)
   self.assertFalse(budget.failed)
   legacy=process.Budget.start()
   process.run_fixed(cmd,env={'PATH':os.environ['PATH']},cwd=None,budget=legacy,output_limit=6*p.MIB)
   with self.assertRaisesRegex(process.TransferRefused,'OUTPUT_LIMIT'):process.run_fixed(cmd,env={'PATH':os.environ['PATH']},cwd=None,budget=legacy,output_limit=6*p.MIB)
   self.assertTrue(legacy.failed)
   with self.assertRaisesRegex(process.TransferRefused,'OUTPUT_LIMIT'):
    process.run_fixed(cmd,env={'PATH':os.environ['PATH']},cwd=None,budget=budget,output_limit=1024)
   self.assertTrue(budget.failed)
   with self.assertRaisesRegex(process.TransferRefused,'BUDGET_EXHAUSTED'):process.run_fixed(cmd,env={},cwd=None,budget=budget)
  with self.assertRaises(process.TransferRefused):process.Budget.start(output_bytes=96*p.MIB+1)

class RestoreCapacity(MemberRestore):
 def setUp(self):
  super().setUp();self.policy_state=patch.object(p,'_loaded',None);self.policy_state.start()
 def tearDown(self):self.policy_state.stop();super().tearDown()
 def larger_manifest(self):
  self.raw=b'x'*(2*p.MIB);self.man['files']['book.jsonl']={'bytes':len(self.raw),'sha256':hashlib.sha256(self.raw).hexdigest()}
  self.cat['members']['finance']['manifest_sha256']=b.digest(self.man)
  self.loc['members_sha256']=b.digest(self.cat['members']);self.loc['catalog_sha256']=b.digest(self.cat)
 def test_expanded_restore_actual_file_and_incompatible_no_writes(self):
  self.larger_manifest()
  p.initialize({p.SETTING:policy(p.MIB)})
  with self.assertRaises(b.Refused):self.go()
  self.assertFalse(list((self.root/'finance').iterdir()))
  # Separate immutable startup, same backup under a compatible local policy.
  p._loaded=None;p.initialize({p.SETTING:policy(2*p.MIB)})
  # Failed restore deliberately left its intent: fresh target is required.
  for path in (self.root/'control').iterdir():
   if path.is_file():path.unlink()
  self.go();self.assertEqual((self.root/'finance/book.jsonl').read_bytes(),self.raw)
if __name__=='__main__':unittest.main()
