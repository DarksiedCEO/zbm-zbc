import _runtime_path  # fixed repository-relative packaged runtime
import unittest,tempfile,threading,copy,hashlib
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import b2_adapter as b
import distributed_transfer as d
from owner import Owner
from owner_repair import OwnerRepairs
from owner_state import Journal
from repair_state import digest
from owner_wire import Refused
class Tests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);root=Path(self.t.name).resolve();run='cold-'+'1'*32
  files={'pack':{'bytes':3,'sha256':hashlib.sha256(b'enc').hexdigest()},'data/aa':{'bytes':3,'sha256':hashlib.sha256(b'old').hexdigest()}}
  seals={m:{'member':m,'set_id':run,'snapshot':'a'*64,'manifest':{'member':m,'set_id':run,'files':{'log':{'bytes':3,'sha256':hashlib.sha256(b'abc').hexdigest()}}},'repository_files':copy.deepcopy(files)} for m in b.MEMBERS}
  uploads={m:{'schema':'b2-restic-member-v1','member':m,'set_id':run,'repository':b.BASE+run+'/'+m,'source_snapshot':'a'*64,'snapshot':'b'*64,'manifest_sha256':b.digest(seals[m]['manifest']),'seal_sha256':b.digest(seals[m]),'verification':'full-read-data-and-exact-file-dumps'} for m in b.MEMBERS}
  cat={'schema':'b2-restic-catalog-v1','set_id':run,'members':uploads,'seals_sha256':b.digest(seals)}
  loc={'schema':'b2-catalog-locator-v1','set_id':run,'repository':d.CATBASE+run,'snapshot':'c'*64,'catalog_sha256':b.digest(cat),'members_sha256':b.digest(uploads),'seals_sha256':b.digest(seals)}
  self.proof={'catalog':cat,'locator':loc,'catalog_snapshot':'c'*64,'catalog_sha256':b.digest(cat)};self.loc=loc;self.seals=seals;self.uploads=uploads
  backup=root/'backup';backup.mkdir();(backup/'transfer.lock').touch();(backup/'cleanup.lock').touch();(backup/'sealed').mkdir();(backup/'sealed'/'pack').write_bytes(b'enc');(backup/'sealed'/'data').mkdir() # aa already deleted, exact partial subset
  (backup/'cleanup-intent.json').write_bytes(b.canonical({'phase':'PENDING','run':run,'member':'finance','locator_sha256':b.digest(loc)}))
  (backup/'cleanup-failure.json').write_bytes(b.canonical({'phase':'UNKNOWN','run':run,'member':'finance'}))
  (backup/'transfer.json').write_bytes(b.canonical({'schema':'b2-transfer-state-v1','set_id':run,'member':'finance','seal_sha256':b.digest(seals['finance']),'phase':'VERIFIED','receipt':uploads['finance']}))
  j=Journal(root/'owner.json','finance','a'*32);self.addCleanup(j.close)
  uploadop=run+':finance:upload:1';j.begin(run,uploadop,'upload');j.finish(uploadop,{'member':'finance','boot':'a'*32,'run':run,'action':'upload','result':uploads['finance']})
  op=run+':finance:cleanup:1';j.begin(run,op,'cleanup');j.fail_backup(op,{'type':'synthetic interrupted deletion'})
  o=Owner.__new__(Owner);o.root=root;o.backup=backup;o.sealed=seals['finance'];o.password='synthetic-password';o.transport_credentials={'key_id':'synthetic-id','key':'synthetic-key'};o.member='finance';o.boot='a'*32;o.token='d'*64;o.journal=j;o.fenced=False;o.phase='OPEN';o.child=NS(poll=lambda:None);o.worker=None;o.mutex=threading.RLock()
  self.o=o;self.m=OwnerRepairs(o);o.repairs=self.m;self.addCleanup(self.m.close)
  self.body={'member':'finance','boot':'a'*32,'run':run,'incident':op,'incident_sha256':digest(j.doc['operations'][op]),'command':'finish-cleanup'};self.original=copy.deepcopy(j.doc)
 def test_partial_cleanup_supported_without_original_rewrite(self):
  with patch('owner_wire.Client.call',return_value=self.proof),patch.object(d.Transfer,'fresh',return_value=self.loc) as fresh:
   self.o.dispatch('/repair',self.body);self.m.worker.join(2)
  self.assertFalse(self.m.worker.is_alive());v=self.m.status(self.body['incident'],'finish-cleanup')
  self.assertEqual(v['repair']['phase'],'RESOLVED');fresh.assert_called_once();self.assertFalse((self.o.backup/'sealed').exists())
  self.assertEqual(self.o.journal.doc,self.original);self.assertEqual(self.o.phase,'OPEN')


 def run_repair(self):
  self.o.dispatch('/repair',self.body)
  if self.m.worker:self.m.worker.join(2)
  return self.m.status(self.body['incident'],'finish-cleanup')
 def test_remote_missing_never_deletes_or_retries(self):
  with patch('owner_wire.Client.call',return_value=self.proof),patch.object(d.Transfer,'fresh',side_effect=b.Refused('REMOTE_MISSING')) as fresh:
   first=self.run_repair();second=self.run_repair();fresh.assert_called_once()
  self.assertEqual(first['repair']['phase'],'UNRESOLVED');self.assertEqual(first,second)
  self.assertTrue((self.o.backup/'sealed'/'pack').exists());self.assertEqual(self.o.journal.doc,self.original)
 def test_changed_unknown_and_symlink_subset_refuse(self):
  import typed_cleanup_repair as t
  for kind in ('changed','unknown','symlink','directory'):
   source=self.o.backup/'sealed';p=source/('pack' if kind=='changed' else 'unexpected')
   if kind=='changed':p.write_bytes(b'bad')
   elif kind=='unknown':p.write_bytes(b'new')
   elif kind=='symlink':p.symlink_to('/tmp')
   else:p.mkdir()
   with self.subTest(kind=kind),self.assertRaises(Refused):t.subset(self.o)
   if kind=='changed':p.write_bytes(b'enc')
   elif kind=='directory':p.rmdir()
   else:p.unlink()
 def test_stale_boot_and_wrong_catalog_refuse(self):
  with patch('owner_wire.Client.call',return_value=self.proof),patch.object(d.Transfer,'fresh') as fresh:
   with self.assertRaises(Refused):self.o.dispatch('/repair',dict(self.body,boot='f'*32))
   bad=copy.deepcopy(self.proof);bad['locator']['snapshot']='d'*64
   with patch('owner_wire.Client.call',return_value=bad):
    with self.assertRaises(b.Refused):self.o.dispatch('/repair',self.body)
   fresh.assert_not_called()
  self.assertTrue((self.o.backup/'sealed'/'pack').exists())
 def test_missing_or_different_original_intent_refuse(self):
  import typed_cleanup_repair as t
  p=self.o.backup/'cleanup-intent.json';saved=p.read_bytes();p.unlink()
  with self.assertRaises(FileNotFoundError):t.artifacts(self.o,self.proof)
  p.write_bytes(b'{}')
  with self.assertRaises(Refused):t.artifacts(self.o,self.proof)
  p.write_bytes(saved)
 def test_terminal_persistence_failure_never_repeats_deletion(self):
  rec=self.m.record(self.body['incident'],'finish-cleanup')
  with patch('owner_wire.Client.call',return_value=self.proof),patch.object(d.Transfer,'fresh',return_value=self.loc) as fresh,patch.object(rec,'finish',side_effect=OSError('synthetic failed durable terminal')):
   self.run_repair();again=self.run_repair();self.assertEqual(fresh.call_count,2) # same-ID absence reconciliation rereads remote; no deletion
  self.assertEqual(again['repair']['phase'],'UNRESOLVED');self.assertFalse((self.o.backup/'sealed').exists());self.assertEqual(self.o.journal.doc,self.original)
 def test_resolved_overlay_and_duplicate_are_bound(self):
  with patch('owner_wire.Client.call',return_value=self.proof),patch.object(d.Transfer,'fresh',return_value=self.loc) as fresh:
   first=self.run_repair();second=self.run_repair();fresh.assert_called_once()
  self.assertEqual(first,second);self.assertEqual(self.m.resolved()[self.body['incident']]['result'],first['repair']['result']['result'])
  p=self.o.backup/'cleanup-intent.json';p.write_bytes(b'{}')
  with self.assertRaises(Refused):self.m.resolved()
 def test_loaded_applying_never_dispatches(self):
  rec=self.m.record(self.body['incident'],'finish-cleanup');p=rec.prepare('finish-cleanup',{'schema':'typed-cleanup-repair-v1'},self.o.boot);rec.begin(p['repair_id']);self.m.close()
  self.m=OwnerRepairs(self.o);self.o.repairs=self.m;self.addCleanup(self.m.close)
  with patch.object(d.Transfer,'fresh') as fresh:
   self.assertEqual(self.run_repair()['repair']['phase'],'APPLYING');fresh.assert_not_called()

 def test_existing_cleanup_worker_lock_refuses_without_effect(self):
  import os,fcntl
  fd=os.open(self.o.backup/'cleanup.lock',os.O_RDWR)
  try:
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with patch('owner_wire.Client.call',return_value=self.proof),patch.object(d.Transfer,'fresh') as fresh:
    with self.assertRaises(BlockingIOError):self.o.dispatch('/repair',self.body)
    fresh.assert_not_called()
   self.assertTrue((self.o.backup/'sealed'/'pack').exists())
  finally:os.close(fd)
