"""One fresh member volume, exact catalog, no application startup or admission.

Call only in explicit recovery boot before Owner/Journal construction. A failed
write retains an immutable intent and partial target; another attempt refuses.
"""
import fcntl,hashlib,os,re,shutil,stat
from pathlib import Path
import b2_adapter as b
from b2_catalog import CATBASE
from bounded_process import Budget,run_fixed
from member_limits import limits
from remote_restore import validate_catalog,validate_manifest,tree
from restore_transport import credentials

VOLUME=Path('/volume')

def _privileged():
 if os.geteuid()!=0:raise b.Refused('RESTORE_UID')

def _directory(path):
 st=path.lstat()
 if not stat.S_ISDIR(st.st_mode) or path.resolve()!=path or st.st_uid!=os.geteuid() or st.st_mode&0o022:raise b.Refused('OWNED_DIRECTORY')
 return st

def _sync(path):
 fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:os.fsync(fd)
 finally:os.close(fd)

def _record(path,value):
 raw=b.canonical(value)
 if len(raw)>4096:raise b.Refused('PROOF_BOUND')
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 _sync(path.parent)

def restore_member(member,password,credential,locator):
 """Return member-restore-v1 proof. No caller paths, executors, or health flags."""
 if type(member)is not str or member not in b.MEMBERS:raise b.Refused('MEMBER')
 _privileged();credentials(password,credential)
 # Fully validate locator shape before opening a repository; full catalog binds later.
 keys={'schema','set_id','repository','snapshot','catalog_sha256','members_sha256','seals_sha256'}
 if type(locator)is not dict or set(locator)!=keys or locator['schema']!='b2-catalog-locator-v1' or type(locator['set_id'])is not str or not re.fullmatch('cold-[0-9a-f]{32}',locator['set_id']) or locator['repository']!=CATBASE+locator['set_id'] or not all(b.hexid(locator[k]) for k in ('snapshot','catalog_sha256','members_sha256','seals_sha256')):raise b.Refused('LOCATOR_BINDING')
 locator=b.decode(b.canonical(locator));root=VOLUME;target=root/member;control=root/'control';scratch=control/'member-restore-scratch'
 try:
  dev=_directory(root).st_dev
  if any(_directory(p).st_dev!=dev for p in (target,control)):raise b.Refused('VOLUME_DEVICE')
 except OSError:raise b.Refused('FRESH_LAYOUT') from None
 identity=target.stat().st_ino
 if any(target.iterdir()):raise b.Refused('FRESH_TARGET')
 fd=os.open(control/'owner-state.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 made_scratch=False
 try:
  st=os.fstat(fd)
  if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_uid!=os.geteuid() or st.st_mode&0o077:raise b.Refused('OWNER_LOCK')
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise b.Refused('OWNER_ACTIVE') from None
  # No previous owner or recovery evidence may be adopted or overwritten.
  if set(p.name for p in control.iterdir())!={'owner-state.lock'}:raise b.Refused('FRESH_CONTROL')
  b.verify_binary();scratch.mkdir(mode=0o700);made_scratch=True
  budget=Budget.start(seconds=120,commands=64,output_bytes=8*b.MIB)
  env={'PATH':'/usr/bin:/bin','HOME':str(scratch),'TMPDIR':str(scratch),'RESTIC_PASSWORD':password,'AWS_ACCESS_KEY_ID':credential['key_id'],'AWS_SECRET_ACCESS_KEY':credential['application_key'],'AWS_DEFAULT_REGION':'us-west-004','RESTIC_RETRY_LOCK':'0s'}
  def command(which,*args):
   repo=locator['repository'] if which=='catalog' else b.BASE+locator['set_id']+'/'+member
   cap=b.MIB if which=='catalog' else limits(member)['source']
   return run_fixed([str(b.BINARY),'--no-cache','--no-lock','-r',repo,*args],env=env,cwd=scratch,budget=budget,output_limit=cap)
  command('catalog','check','--read-data');tree(command('catalog','ls','--json',locator['snapshot']),{'/catalog.json'})
  cat=b.decode(command('catalog','dump',locator['snapshot'],'/catalog.json'));run=validate_catalog(locator,cat)
  receipt=cat['members'][member];snap=receipt['snapshot'];command(member,'check','--read-data')
  manifest=validate_manifest(b.decode(command(member,'dump',snap,'/manifest.json')),receipt)
  tree(command(member,'ls','--json',snap),{'/manifest.json'}|{'/'+n for n in manifest['files']})
  names=set(manifest['files']);dirs={Path('.')}
  for name in names:
   parents=set(Path(name).parents)
   if any(str(p) in names for p in parents):raise b.Refused('FILE_DIRECTORY_ALIAS')
   dirs.update(parents)
  # Bound directory growth too, not just file count and bytes.
  if len(dirs)+len(names)>128 or len(names)>budget.commands_left:raise b.Refused('RESTORE_ENTRY_BOUND')
  amount=sum(v['bytes'] for v in manifest['files'].values())
  fs=os.statvfs(target)
  if fs.f_bavail*fs.f_frsize<amount+b.MIB or fs.f_favail<len(dirs)+len(names)+16:raise b.Refused('DESTINATION_HEADROOM')
  data={}
  for name,meta in manifest['files'].items():
   raw=command(member,'dump',snap,'/'+name)
   if len(raw)!=meta['bytes'] or hashlib.sha256(raw).hexdigest()!=meta['sha256']:raise b.Refused('CONTENT_MISMATCH')
   data[name]=raw
  # Revalidate destination identity and headroom after remote I/O, before intent.
  current=_directory(target)
  if current.st_dev!=dev or current.st_ino!=identity or any(target.iterdir()):raise b.Refused('TARGET_CHANGED')
  fs=os.statvfs(target)
  if fs.f_bavail*fs.f_frsize<amount+b.MIB or fs.f_favail<len(dirs)+len(names)+16:raise b.Refused('DESTINATION_HEADROOM')
  proof={'schema':'member-restore-v1','member':member,'set_id':run,'catalog_snapshot':locator['snapshot'],'catalog_sha256':locator['catalog_sha256'],'snapshot':snap,'manifest_sha256':b.digest(manifest),'application_start':'NOT_PERFORMED'}
  _record(control/'member-restore-intent.json',proof)
  for name,raw in data.items():
   path=target/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
   out=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
   with os.fdopen(out,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  if b.local_inventory(target,total=limits(member)['source'])!=manifest['files']:raise b.Refused('RESTORED_BYTES')
  paths=sorted([target,*target.rglob('*')],key=lambda p:len(p.parts),reverse=True)
  for path in paths:
   if path.is_dir():_sync(path)
  for path in paths:
   handoff=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
   try:
    os.fchown(handoff,10001,10001);os.fsync(handoff)
   finally:os.close(handoff)
  _sync(root)
  _record(control/'member-restore-complete.json',proof)
  return proof
 finally:
  try:
   if made_scratch:shutil.rmtree(scratch)
  finally:os.close(fd)
