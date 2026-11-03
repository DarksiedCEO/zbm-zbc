import _runtime_path
import hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import member_restore as m
import b2_adapter as b
from b2_catalog import CATBASE

class MemberRestore(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();(self.root/'control').mkdir(mode=0o700);(self.root/'finance').mkdir(mode=0o700)
  self.run='cold-'+'a'*32;self.raw=b'actual synthetic stored bytes\n';self.man={'member':'finance','set_id':self.run,'files':{'book.jsonl':{'bytes':len(self.raw),'sha256':hashlib.sha256(self.raw).hexdigest()}}}
  members={x:{'schema':'b2-restic-member-v1','set_id':self.run,'member':x,'repository':b.BASE+self.run+'/'+x,'source_snapshot':'1'*64,'snapshot':'2'*64,'manifest_sha256':b.digest(self.man),'seal_sha256':'3'*64,'verification':'full-read-data-and-exact-file-dumps'} for x in b.MEMBERS}
  self.cat={'schema':'b2-restic-catalog-v1','set_id':self.run,'members':members,'seals_sha256':'4'*64};self.loc={'schema':'b2-catalog-locator-v1','set_id':self.run,'repository':CATBASE+self.run,'snapshot':'5'*64,'catalog_sha256':b.digest(self.cat),'members_sha256':b.digest(members),'seals_sha256':'4'*64};self.calls=[]
  self.stack=[]
  for name,value in [('VOLUME',self.root),('_privileged',lambda:None),('run_fixed',self.command)]:
   p=patch.object(m,name,value);p.start();self.stack.append(p)
  for target in ('member_restore.b.verify_binary','member_restore.os.fchown'):
   p=patch(target);p.start();self.stack.append(p)
 def tearDown(self):
  for p in reversed(self.stack):p.stop()
  self.tmp.cleanup()
 def command(self,argv,**kw):
  self.calls.append((argv,kw));args=argv[argv.index('-r')+2:];repo=argv[argv.index('-r')+1]
  if args[0]=='check':return b''
  if args[0]=='ls':return b'\n'.join(b.canonical({'type':'file','path':n}) for n in (['/catalog.json'] if '/catalog/' in repo else ['/manifest.json','/book.jsonl']))
  if args[-1]=='/catalog.json':return b.canonical(self.cat)
  if args[-1]=='/manifest.json':return b.canonical(self.man)
  return self.raw
 def go(self,member='finance',locator=None):return m.restore_member(member,'synthetic-password',{'key_id':'synthetic-id','application_key':'synthetic-key'},locator or self.loc)
 def test_normal_verified_only_member_and_secret_custody(self):
  proof=self.go();self.assertEqual(proof['manifest_sha256'],b.digest(self.man));self.assertEqual((self.root/'finance/book.jsonl').read_bytes(),self.raw);self.assertEqual(proof['application_start'],'NOT_PERFORMED')
  self.assertFalse((self.root/'control/member-restore-scratch').exists())
  for argv,kw in self.calls:
   self.assertNotIn('synthetic-key',' '.join(argv));self.assertEqual(kw['env']['AWS_SECRET_ACCESS_KEY'],'synthetic-key');self.assertNotIn('RESTIC_FROM_PASSWORD',kw['env'])
 def test_wrong_member_and_catalog_before_writes(self):
  for member,loc in [('arbitrary',self.loc),('finance',dict(self.loc,catalog_sha256='0'*64))]:
   with self.assertRaises(b.Refused):self.go(member,loc)
   self.assertEqual(list((self.root/'finance').iterdir()),[])
 def test_nonempty_symlink_and_owner_state_refused_without_remote(self):
  target=self.root/'finance';(target/'keep').write_bytes(b'original')
  with self.assertRaises(b.Refused):self.go()
  (target/'keep').unlink();target.rmdir();target.symlink_to(self.root/'control')
  with self.assertRaises(b.Refused):self.go()
  target.unlink();target.mkdir();(self.root/'control/owner-state.json').write_text('{}')
  with self.assertRaises(b.Refused):self.go()
  self.assertFalse(self.calls)
 def test_corrupt_and_truncated_content_write_nothing(self):
  for raw in (b'x'*len(self.raw),self.raw[:-1]):
   with patch.object(m,'run_fixed',side_effect=lambda argv,**kw: raw if argv[-1]=='/book.jsonl' else self.command(argv,**kw)):
    with self.assertRaisesRegex(b.Refused,'CONTENT_MISMATCH'):self.go()
   self.assertFalse(list((self.root/'finance').iterdir()))
 def test_write_failure_preserves_intent_and_refuses_retry(self):
  original=m.os.open
  def fail(path,*args,**kw):
   if str(path).endswith('/book.jsonl'):raise OSError('synthetic failure')
   return original(path,*args,**kw)
  with patch.object(m.os,'open',side_effect=fail):
   with self.assertRaises(OSError):self.go()
  self.assertTrue((self.root/'control/member-restore-intent.json').is_file())
  self.assertFalse((self.root/'control/member-restore-complete.json').exists())
  count=len(self.calls)
  with self.assertRaises(b.Refused):self.go()
  self.assertEqual(len(self.calls),count)
 def test_capacity_refusal_write_nothing(self):
  class Full:f_bavail=0;f_frsize=4096;f_favail=0
  with patch.object(m.os,'statvfs',return_value=Full()):
   with self.assertRaisesRegex(b.Refused,'DESTINATION_HEADROOM'):self.go()
  self.assertFalse(list((self.root/'finance').iterdir()))
 def test_exclusive_owner_and_prior_intent_refused(self):
  import fcntl
  fd=os.open(self.root/'control/owner-state.lock',os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  try:
   with self.assertRaises(b.Refused):self.go()
  finally:os.close(fd)
  (self.root/'control/member-restore-intent.json').write_text('{}')
  with self.assertRaises(b.Refused):self.go()
  self.assertFalse(self.calls)
if __name__=='__main__':unittest.main()
