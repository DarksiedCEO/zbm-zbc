"""Fixed success-only LOCAL metadata retirement. Never follows paths or deletes snapshots.

Caller owns its run-level mutex/process lock and supplies independently verified
completion authority. Inventory digests bind bytes; they are not that authority.
"""
import os,stat,hashlib,fcntl,re
from pathlib import Path
from owner_wire import Refused,canonical,decode
CAP=262144
OWNER_FILES={'transfer.lock','transfer.json','cleanup-intent.json','cleanup-result.json','cleanup.lock'}
CAT_FILES={'catalog-intent.json','catalog-state.json','catalog-locator.json','catalog.lock'}
DIRS={'scratch','transfer-scratch'}
def sha(raw):return hashlib.sha256(raw).hexdigest()
def read(path,limit=CAP):
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 try:
  s=os.fstat(fd)
  if not stat.S_ISREG(s.st_mode) or s.st_size>limit:raise Refused('metadata file type/bound')
  out=bytearray()
  while len(out)<=limit:
   x=os.read(fd,min(65536,limit+1-len(out)))
   if not x:break
   out.extend(x)
  if len(out)>limit:raise Refused('metadata file bound')
  return bytes(out)
 finally:os.close(fd)
def sync(root):
 fd=os.open(root,os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)
def save(path,value):
 """Exact idempotent atomic replacement; recover only own matching temp prefix."""
 path=Path(path)
 if path.is_symlink():raise Refused('metadata destination symlink')
 raw=canonical(value)
 if len(raw)>CAP:raise Refused('metadata record bound')
 tmp=path.with_name(path.name+'.tmp')
 fs=os.statvfs(path.parent)
 if fs.f_bavail*fs.f_frsize<2*CAP or fs.f_favail<4:raise Refused('metadata atomic headroom')
 if tmp.exists() or tmp.is_symlink():
  previous=read(tmp)
  if not raw.startswith(previous):raise Refused('different interrupted metadata write')
  tmp.unlink();sync(path.parent)
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 try:
  offset=0
  while offset<len(raw):
   n=os.write(fd,raw[offset:])
   if type(n)is not int or not 0<n<=len(raw)-offset:raise Refused('metadata write progress')
   offset+=n
  os.fsync(fd)
 finally:os.close(fd)
 os.replace(tmp,path);sync(path.parent)
def inventory(root,kind):
 root=Path(root)
 if root.is_symlink() or not root.is_dir():raise Refused('metadata run directory')
 allowed=OWNER_FILES if kind=='owner' else CAT_FILES
 out={};total=0
 for p in sorted(root.iterdir()):
  s=p.lstat()
  if stat.S_ISDIR(s.st_mode) and p.name in DIRS:
   if any(p.iterdir()):raise Refused('nonempty scratch retained')
   out[p.name]={'kind':'dir'}
  elif stat.S_ISREG(s.st_mode) and p.name in allowed:
   raw=read(p,32768)
   if p.name=='transfer.lock' and raw:raise Refused('transfer lock must be empty')
   total+=len(raw);out[p.name]={'kind':'file','bytes':len(raw),'sha256':sha(raw)}
  else:raise Refused('unknown or retained data in metadata directory')
 if len(out)>8 or total>65536:raise Refused('metadata inventory bound')
 return out
def remove_exact(root,items):
 """Missing inventory entries allowed only under a previously durable intent."""
 root=Path(root)
 if type(items)is not dict or len(items)>8:raise Refused('inventory shape')
 for n,v in items.items():
  if n not in OWNER_FILES|CAT_FILES|DIRS or type(v)is not dict or v.get('kind')not in ('file','dir'):raise Refused('inventory path')
  if v['kind']=='dir' and (n not in DIRS or v!={'kind':'dir'}):raise Refused('inventory directory')
  if v['kind']=='file' and (set(v)!={'kind','bytes','sha256'} or type(v['bytes'])is not int or not 0<=v['bytes']<=32768 or type(v['sha256'])is not str or len(v['sha256'])!=64):raise Refused('inventory file')
 if not root.exists():
  if root.is_symlink():raise Refused('metadata root symlink')
  return
 if root.is_symlink() or not root.is_dir():raise Refused('metadata root type')
 present={p.name for p in root.iterdir()}
 if not present<=set(items):raise Refused('metadata extra entry')
 # Validate EVERY surviving entry before deleting any additional entry.
 for n in present:
  p=root/n;v=items[n];s=p.lstat()
  if v['kind']=='dir':
   if not stat.S_ISDIR(s.st_mode) or any(p.iterdir()):raise Refused('metadata directory changed')
  elif not stat.S_ISREG(s.st_mode) or len(raw:=read(p,32768))!=v['bytes'] or sha(raw)!=v['sha256']:raise Refused('metadata bytes changed')
 for n in sorted(present):
  p=root/n
  if items[n]['kind']=='dir':p.rmdir()
  else:p.unlink()
  sync(root)
 root.rmdir();sync(root.parent)
def _retire_owner(owner,permit):
 """Called under actual owner's mutex; no app/backup effect is invoked."""
 run=permit['run'];summary=permit['summary'];proof=permit['completion']
 j=owner.journal.doc
 if owner.fenced or owner.journal.recovered or owner.phase!='OPEN' or j.get('run')is not None or j.get('last_success')!=summary or owner.boot!=permit['boots'][owner.member]:raise Refused('owner retirement binding')
 if j.get('retired_resolution_sha256') or getattr(owner,'backup_failure',None):raise Refused('incident metadata never retired')
 if owner.child is None or owner.child.poll()is not None or (owner.worker and owner.worker.is_alive()) or (owner.repairs.worker and owner.repairs.worker.is_alive()):raise Refused('owner not idle')
 path=owner.layout.control/'metadata-retirement.json';root=owner.layout.backup/run
 identity={'run':run,'boot':owner.boot,'member':owner.member,'advance_sha256':permit['advance_sha256'],'completion':proof}
 old=decode(read(path)) if path.exists() else None
 if old and old['identity']==identity:
  value=old
  if value['phase']not in ('PENDING','DONE'):raise Refused('metadata phase')
 elif old and old['phase']!='DONE':raise Refused('unresolved metadata retirement')
 else:
  items=inventory(root,'owner')
  if not {'transfer.lock','transfer.json','cleanup-intent.json','cleanup-result.json'}<=set(items):raise Refused('missing cleanup metadata')
  t=decode(read(root/'transfer.json'));c=decode(read(root/'cleanup-result.json'))
  expected={'schema':'b2-cleanup-v1','deleted':True,'run':run,'member':owner.member,'catalog_sha256':summary['catalog_sha256'],'catalog_snapshot':summary['catalog_snapshot'],'locator_sha256':permit['locator_sha256']}
  if t.get('phase')!='VERIFIED' or t.get('set_id')!=run or t.get('member')!=owner.member or c!={'phase':'DONE','receipt':expected}:raise Refused('owner cleanup metadata proof')
  value={'schema':1,'identity':identity,'phase':'PENDING','inventory':items}
  save(path,value)
 if value['phase']=='DONE':
  if root.exists() or root.is_symlink():raise Refused('retired metadata reappeared')
 else:
  remove_exact(root,value['inventory']);value=dict(value,phase='DONE');save(path,value)
 return {'run':run,'member':owner.member,'boot':owner.boot,'advance_sha256':permit['advance_sha256'],'metadata_retired':True}


def retire_owner(owner,permit):
 # The actual Adapter.upload creator uses transfer.lock (b2_adapter.py:131).
 # Keep its inode locked through inventory checking, deletion and terminal save.
 run=permit.get('run')
 if type(run)is not str or re.fullmatch('cold-[0-9a-f]{32}',run)is None:raise Refused('fixed metadata run')
 path=owner.layout.backup/run/'transfer.lock';fd=None
 try:
  if path.exists() or path.is_symlink():
   fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
   st=os.fstat(fd)
   if not stat.S_ISREG(st.st_mode) or st.st_size!=0:raise Refused('transfer lock must be regular empty file')
   try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except OSError:raise Refused('transfer lock still active') from None
   if (path.lstat().st_dev,path.lstat().st_ino)!=(st.st_dev,st.st_ino):raise Refused('transfer lock replaced')
  # A missing lock is accepted only by _retire_owner's prior durable same-ID
  # deletion intent; a fresh inventory requires the actual lock file.
  return _retire_owner(owner,permit)
 finally:
  if fd is not None:os.close(fd)
