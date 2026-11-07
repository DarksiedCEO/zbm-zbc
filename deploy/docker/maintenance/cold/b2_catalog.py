"""Offline-qualified fixed five-member catalog; no application admission handle."""
import fcntl,os,shutil,time
from pathlib import Path
import b2_adapter as b
from capacity_policy import output_bytes
CATBASE=b.BASE.replace('/restic/sets/','/restic/catalog/')
class Catalog:
 def __init__(self,root,seals,receipts,*,key_id,key,password):
  self.root=Path(root);self.credentials=dict(key_id=key_id,key=key,password=password)
  if not isinstance(seals,dict) or not isinstance(receipts,dict) or set(seals)!=set(b.MEMBERS) or set(receipts)!=set(b.MEMBERS):raise b.Refused('FIVE_MEMBERS_REQUIRED')
  self.seals=b.decode(b.canonical(seals));self.receipts=b.decode(b.canonical(receipts));self.run=self.seals['ledger']['set_id']
  for m in b.MEMBERS:
   a=b.Adapter(self.root,self.seals[m],**self.credentials);r=self.receipts[m]
   expected={'schema':'b2-restic-member-v1','set_id':self.run,'member':m,'repository':b.BASE+self.run+'/'+m,'source_snapshot':self.seals[m]['snapshot'],'snapshot':r.get('snapshot'),'manifest_sha256':b.digest(self.seals[m]['manifest']),'seal_sha256':b.digest(self.seals[m]),'verification':'full-read-data-and-exact-file-dumps'}
   if a.run!=self.run or a.member!=m or r!=expected or not b.hexid(r['snapshot']):raise b.Refused('MEMBER_BINDING')
  self.catalog={'schema':'b2-restic-catalog-v1','set_id':self.run,'members':self.receipts,'seals_sha256':b.digest(self.seals)}
  if len(b.canonical(self.catalog))>16384:raise b.Refused('CATALOG_BOUND')
  # Existing process primitive caps64; reject larger trees before init/backup.
  if 20+sum(len(s['manifest']['files']) for s in self.seals.values())>64:raise b.Refused('COMPLETE_VERIFICATION_COMMAND_BOUND')
  self.remote=CATBASE+self.run
 def save(self,name,value):
  raw=b.canonical(value)
  if len(raw)>32768:raise b.Refused('STATE_BOUND')
  tmp=self.root/(name+'.tmp');fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,self.root/name);fd=os.open(self.root,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
 def read(self,name):
  try:
   fd=os.open(self.root/name,os.O_RDONLY|os.O_NOFOLLOW)
   try:
    import stat
    if not stat.S_ISREG(os.fstat(fd).st_mode):raise b.Refused('STATE_TYPE')
    raw=os.read(fd,32769)
   finally:os.close(fd)
   if len(raw)>32768:raise b.Refused('STATE_BOUND')
   return b.decode(raw)
  except (OSError,ValueError):raise b.Refused('STATE_UNAVAILABLE') from None
 def lock(self):
  fd=os.open(self.root/'catalog.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except OSError:os.close(fd);raise b.Refused('CATALOG_ACTIVE') from None
  return fd
 def start(self):
  b.verify_binary();b.spool_guard(self.root)
  scratch=self.root/'transfer-scratch'
  if scratch.is_symlink():raise b.Refused('SCRATCH_PATH')
  scratch.mkdir(mode=0o700,exist_ok=True)
  self.budget=b.Budget.start(seconds=120,commands=64,output_bytes=output_bytes())
 def adapter(self,m):
  a=b.Adapter(self.root,self.seals[m],**self.credentials);a.budget=self.budget;return a
 def catcommand(self,*args,cwd=None):
  b.operational_command(self.remote,args)
  a=self.adapter('ledger');a.remote=self.remote
  if cwd is None:return a.command(*args)
  b.spool_guard(self.root)
  out=b.run_fixed([str(b.BINARY),'--no-cache','-r',self.remote,*args],env=a.env,cwd=cwd,budget=self.budget,output_limit=b.MIB)
  b.spool_guard(self.root);return out
 def verify_catalog(self,snapshot):
  self.catcommand('check','--read-data')
  rows=[b.decode(x) for x in self.catcommand('ls','--json',snapshot).splitlines()]
  if len(rows)>8 or [r.get('path') for r in rows if r.get('type')=='file']!=['/catalog.json'] or any(r.get('type')not in (None,'file','dir') for r in rows):raise b.Refused('CATALOG_TREE')
  if b.decode(self.catcommand('dump',snapshot,'/catalog.json'))!=self.catalog:raise b.Refused('CATALOG_CONTENT')
  for m in b.MEMBERS:self.adapter(m).verify(self.receipts[m]['snapshot'])
 def locator(self,snapshot):return {'schema':'b2-catalog-locator-v1','set_id':self.run,'repository':self.remote,'snapshot':snapshot,'catalog_sha256':b.digest(self.catalog),'members_sha256':b.digest(self.receipts),'seals_sha256':b.digest(self.seals)}
 def publish(self):
  fd=self.lock()
  try:
   if any((self.root/n).exists() for n in ('catalog-intent.json','catalog-intent.json.tmp','catalog-state.json','catalog-state.json.tmp','catalog-locator.json','catalog-locator.json.tmp')):raise b.Refused('EXISTING_CATALOG_INTENT')
   self.start();self.save('catalog-intent.json',{'phase':'PENDING','set_id':self.run,'catalog_sha256':b.digest(self.catalog)})
   self.save('catalog-state.json',{'phase':'PENDING','set_id':self.run,'catalog_sha256':b.digest(self.catalog)})
   try:
    stage=self.root/'catalog-stage';stage.mkdir(mode=0o700);p=stage/'catalog.json';p.write_bytes(b.canonical(self.catalog));p.chmod(0o600)
    self.catcommand('init');rows=[b.decode(x) for x in self.catcommand('backup','--json','.',cwd=stage).splitlines()]
    ids=[r.get('snapshot_id') for r in rows if r.get('message_type')=='summary']
    if len(ids)!=1 or not b.hexid(ids[0]):raise b.Refused('CATALOG_SNAPSHOT')
    locator=self.locator(ids[0]);self.verify_catalog(ids[0])
    self.save('catalog-locator.json',locator) # durable exact identity BEFORE publication
    shutil.rmtree(stage)
    self.save('catalog-state.json',{'phase':'PUBLISHED','set_id':self.run,'locator_sha256':b.digest(locator)})
    return locator
   except BaseException:
    try:self.save('catalog-state.json',{'phase':'UNKNOWN','set_id':self.run,'catalog_sha256':b.digest(self.catalog)})
    except BaseException:pass
    raise b.Refused('CATALOG_UNCERTAIN') from None
  finally:os.close(fd)
 def verify_locator(self):
  if self.read('catalog-intent.json')!={'phase':'PENDING','set_id':self.run,'catalog_sha256':b.digest(self.catalog)}:raise b.Refused('INTENT_BINDING')
  if (self.root/'catalog-locator.json.tmp').exists() or (self.root/'catalog-state.json.tmp').exists():raise b.Refused('INTERRUPTED_STATE')
  locator=self.read('catalog-locator.json')
  if not isinstance(locator,dict) or not b.hexid(locator.get('snapshot')) or locator!=self.locator(locator['snapshot']):raise b.Refused('LOCATOR_BINDING')
  self.verify_catalog(locator['snapshot']);return locator
 def recover(self):
  # Evidence only. Does not rewrite original UNKNOWN or confer deletion authority.
  fd=self.lock()
  try:self.start();return self.verify_locator()
  finally:os.close(fd)
 def _cleanup(self,owner_root,member):
  if member not in b.MEMBERS:raise b.Refused('MEMBER')
  root=Path(owner_root)
  if not root.is_absolute() or root.is_symlink() or root.resolve()!=root or not root.is_dir():raise b.Refused('OWNER_ROOT')
  # Trusted owner construction only: caller HTTP cannot supply root/member.
  fd=self.lock();ownfd=os.open(root/'cleanup.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
  try:
   fcntl.flock(ownfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   if (root/'cleanup-intent.json').exists():raise b.Refused('EXISTING_CLEANUP')
   self.start();locator=self.verify_locator()
   if self.read('catalog-state.json')!={'phase':'PUBLISHED','set_id':self.run,'locator_sha256':b.digest(locator)}:raise b.Refused('ORIGINAL_UNRESOLVED')
   source=root/'sealed'
   if source.is_symlink() or not source.is_dir() or b.local_inventory(source,b.limits(member)['repository'])!=self.seals[member]['repository_files'] or (root/'stage').exists():raise b.Refused('LOCAL_BINDING')
   # Owner-local intent before deletion; interruption remains pending, no auto retry.
   raw=b.canonical({'phase':'PENDING','member':member,'set_id':self.run,'locator_sha256':b.digest(locator)})
   out=os.open(root/'cleanup-intent.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
   with os.fdopen(out,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
   d=os.open(root,os.O_RDONLY)
   try:os.fsync(d)
   finally:os.close(d)
   shutil.rmtree(source)
   result={'phase':'DONE','deleted':True,'member':member,'set_id':self.run,'locator_sha256':b.digest(locator)}
   tmp=root/'cleanup-result.tmp'
   out=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
   with os.fdopen(out,'wb') as f:f.write(b.canonical(result));f.flush();os.fsync(f.fileno())
   os.replace(tmp,root/'cleanup-result.json');d=os.open(root,os.O_RDONLY)
   try:os.fsync(d)
   finally:os.close(d)
   return result
  finally:os.close(ownfd);os.close(fd)

class OwnerCleanup:
 """Bind only at trusted owner construction, never from request arguments.

 Deployment must verify catalog/receipts over authenticated owner transport and
 hold the owner's spool exclusion. This local contract has no HTTP endpoint.
 """
 def __init__(self,catalog,owner_root,member):
  if type(catalog)is not Catalog:raise b.Refused('FIXED_VERIFIER')
  self.catalog=catalog;self.root=Path(owner_root);self.member=member
 def execute(self):return self.catalog._cleanup(self.root,self.member)
