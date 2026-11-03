"""Bounded one-incident repair custody, never independent effect authority.

A fixed owner-local executor must derive evidence/results itself. This module
accepts no healthy flag and performs no app/backup effects. APPLYING loaded after
restart is unresolved; begin never replays it. Original incident stays immutable.
"""
import copy,fcntl,hashlib,os,re,stat,threading
from pathlib import Path
from owner_wire import canonical,decode,Refused
CAP=65536;TERMINAL=24576
COMMANDS=('quiesce-originals','verify-sealed','resume-originals','publish-originals','resume-sealed','retry-upload','verify-remote','finish-cleanup')
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()
def require(v):
 if not v:raise Refused('repair evidence/state refused')
class Repairs:
 def __init__(self,root,identity,incident):
  require(type(identity)is dict and set(identity)=={'member','run','operation'})
  require(identity['member'] in ('ledger','finance','legal','compliance','seo'))
  require(type(identity['run'])is str and re.fullmatch('cold-[0-9a-f]{32}',identity['run']) is not None)
  require(type(identity['operation'])is str and identity['operation'].startswith(identity['run']+':'+identity['member']+':') and len(identity['operation'])<=160)
  require(type(incident)is dict and incident.get('run')==identity['run'] and incident.get('phase') in ('PENDING','FAILED','DONE'))
  require(len(canonical(incident))<=TERMINAL)
  self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
  require(self.root.is_dir() and not self.root.is_symlink())
  self.identity=copy.deepcopy(identity);self.incident=copy.deepcopy(incident);self.closed=False;self.poisoned=False;self.lock=threading.RLock()
  self.path=self.root/'repair.json';self.temp=self.path.with_suffix('.tmp');self.fd=None
  try:
   self.fd=os.open(self.root/'repair.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
   require(stat.S_ISREG(os.fstat(self.fd).st_mode));fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   self.lock_id=(os.fstat(self.fd).st_dev,os.fstat(self.fd).st_ino)
   require(not self.temp.exists())
   self.doc=self.load()
   if self.doc is None:
    value={'schema':1,'identity':self.identity,'incident':self.incident,'incident_sha256':digest(self.incident),'repair':None}
    self.persist(value);self.doc=value
   require(self.doc['identity']==self.identity and self.doc['incident']==self.incident and self.doc['incident_sha256']==digest(self.incident))
  except BaseException:self.close();raise
 def load(self):
  try:fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  except FileNotFoundError:return None
  try:
   require(stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_size<=CAP)
   data=os.read(fd,CAP+1);require(len(data)<=CAP);v=decode(data)
  finally:os.close(fd)
  require(type(v)is dict and set(v)=={'schema','identity','incident','incident_sha256','repair'} and type(v['schema'])is int and v['schema']==1)
  r=v['repair']
  if r is not None:
   require(type(r)is dict and set(r)=={'repair_id','command','boot','evidence','evidence_sha256','phase','result','failure'})
   require(r['command'] in COMMANDS and r['phase'] in ('PREPARED','APPLYING','RESOLVED','UNRESOLVED'))
   require(r['evidence_sha256']==digest(r['evidence']))
   require(r['repair_id']==digest({'identity':v['identity'],'incident_sha256':v['incident_sha256'],'command':r['command'],'boot':r['boot'],'evidence_sha256':r['evidence_sha256']}))
   require((r['result'] is not None)==(r['phase']=='RESOLVED'))
   require((r['failure'] is not None)==(r['phase']=='UNRESOLVED'))
  return v
 def check(self):
  require(not self.closed and not self.poisoned and not self.temp.exists())
  st=(self.root/'repair.lock').lstat();require((st.st_dev,st.st_ino)==self.lock_id)
  require(self.load()==self.doc)
 def persist(self,v):
  raw=canonical(v);require(len(raw)<=CAP)
  fs=os.statvfs(self.root);require(fs.f_bavail*fs.f_frsize>=2*CAP and fs.f_favail>=2)
  fd=os.open(self.temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  try:
   offset=0
   while offset<len(raw):
    n=os.write(fd,raw[offset:]);require(type(n)is int and n>0);offset+=n
   os.fsync(fd)
  finally:os.close(fd)
  os.replace(self.temp,self.path);parent=os.open(self.root,os.O_RDONLY)
  try:os.fsync(parent)
  finally:os.close(parent)
 def commit(self,v):
  try:self.persist(v)
  except BaseException:self.poisoned=True;raise
  self.doc=v
 def read(self):
  with self.lock:
   # Diagnostic last-known memory remains available after a failed write; it is
   # not permission to execute. Effect methods call check and refuse poison.
   return copy.deepcopy(self.doc)
 def prepare(self,command,evidence,boot):
  with self.lock:
   self.check();require(command in COMMANDS and type(evidence)is dict)
   require(type(boot)is str and re.fullmatch('[0-9a-f]{32}',boot)is not None)
   ident={'identity':self.identity,'incident_sha256':digest(self.incident),'command':command,'boot':boot,'evidence_sha256':digest(evidence)}
   r={'repair_id':digest(ident),'command':command,'boot':boot,'evidence':copy.deepcopy(evidence),'evidence_sha256':digest(evidence),'phase':'PREPARED','result':None,'failure':None}
   old=self.doc['repair']
   if old is not None:
    require(old==r);return copy.deepcopy(old)
   v=copy.deepcopy(self.doc);v['repair']=r
   require(len(canonical(v))+TERMINAL+512<=CAP)
   self.commit(v);return copy.deepcopy(r)
 def begin(self,rid):
  with self.lock:
   self.check();r=self.doc['repair'];require(r is not None and r['repair_id']==rid and r['phase']=='PREPARED')
   v=copy.deepcopy(self.doc);v['repair']['phase']='APPLYING';self.commit(v)
 def finish(self,rid,result):
  with self.lock:
   self.check();r=self.doc['repair'];require(r is not None and r['repair_id']==rid and r['phase']=='APPLYING')
   require(type(result)is dict and len(canonical(result))<=TERMINAL)
   v=copy.deepcopy(self.doc);v['repair'].update(phase='RESOLVED',result=copy.deepcopy(result));self.commit(v)
 def fail(self,rid,code):
  with self.lock:
   self.check();r=self.doc['repair'];require(r is not None and r['repair_id']==rid and r['phase']=='APPLYING')
   require(type(code)is str and re.fullmatch('[A-Z_]{1,64}',code)is not None)
   v=copy.deepcopy(self.doc);v['repair'].update(phase='UNRESOLVED',failure=code);self.commit(v)
 def close(self):
  if self.closed:return
  self.closed=True
  if self.fd is not None:os.close(self.fd)
