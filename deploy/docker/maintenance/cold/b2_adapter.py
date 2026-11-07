"""Fixed B2 member-copy boundary. No app authority, catalog publication or deletion.

Trusted owner must hold source exclusion and own a quota-bounded spool. This
module never treats remote objects as local paths or grants cleanup authority.
"""
import fcntl,hashlib,json,os,re,stat
from pathlib import Path
from bounded_process import Budget,run_fixed
from member_limits import limits
from capacity_policy import spool_bytes,output_bytes
class Refused(RuntimeError):pass
MEMBERS=('ledger','finance','legal','compliance','seo')
BINARY=Path('/opt/zbm-maintenance/bin/restic')
BINARY_SHA='20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639'
BASE='s3:https://s3.us-west-004.backblazeb2.com/zbm-zbc-backups-20261101/restic/sets/'
MIB=1048576

def operational_command(repo,args):
 """Operational backups never access preservation or perform remote retention.

 Local archive-slot retirement is separate. Any future remote retention policy
 requires a separate reviewed boundary; this one refuses deletion everywhere.
 """
 prefix=BASE.removesuffix('sets/')
 pattern=re.escape(prefix)+r'(?:sets/cold-[0-9a-f]{32}/(?:'+ '|'.join(MEMBERS)+r')|catalog/cold-[0-9a-f]{32})'
 if not isinstance(repo,str) or re.fullmatch(pattern,repo) is None:raise Refused('OPERATIONAL_REPOSITORY_ONLY')
 if not args or args[0] not in ('init','copy','snapshots','check','ls','dump','backup','restore'):raise Refused('REMOTE_RETENTION_FORBIDDEN')
 if any(str(x).split('=',1)[0] in ('-r','--repo','--repository-file') or (str(x).startswith('-r') and not str(x).startswith('--') and len(str(x))>2) for x in args[1:]):raise Refused('REPOSITORY_OVERRIDE_FORBIDDEN')

def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()
def hexid(v):return isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) is not None
def decode(raw):
 if not isinstance(raw,bytes) or len(raw)>MIB:raise Refused('RESPONSE_BOUND')
 def pairs(rows):
  out={}
  for k,v in rows:
   if k in out:raise Refused('DUPLICATE_FIELD')
   out[k]=v
  return out
 try:return json.loads(raw,object_pairs_hook=pairs)
 except (ValueError,UnicodeError,RecursionError):raise Refused('INVALID_JSON') from None

def verify_binary():
 if BINARY.is_symlink() or not BINARY.is_file() or not os.access(BINARY,os.X_OK) or hashlib.sha256(BINARY.read_bytes()).hexdigest()!=BINARY_SHA:raise Refused('BINARY_PIN')
def destination_snapshot(raw,source):
 rows=decode(raw)
 if not isinstance(rows,list) or len(rows)!=1:raise Refused('SNAPSHOT_SET')
 row=rows[0]
 if not isinstance(row,dict) or not hexid(row.get('id')) or (row.get('original',row['id'])!=source and row['id']!=source):raise Refused('SNAPSHOT_BINDING')
 return row['id']

def files_shape(files,limit,total):
 if not isinstance(files,dict) or not 1<=len(files)<=limit:raise Refused('FILE_COUNT')
 size=0
 for name,meta in files.items():
  if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*',name) or any(x in ('.','..') for x in name.split('/')) or len(name)>256:raise Refused('FILE_PATH')
  if not isinstance(meta,dict) or set(meta)!={'bytes','sha256'} or type(meta['bytes'])is not int or not 0<=meta['bytes']<=total or not hexid(meta['sha256']):raise Refused('FILE_METADATA')
  size+=meta['bytes']
 if size>total:raise Refused('FILE_BYTES')

def local_inventory(root,total=2*MIB):
 out={};count=0;size=0
 for base,dirs,files in os.walk(root,followlinks=False):
  for name in dirs+files:
   p=Path(base)/name;st=p.lstat();count+=1
   if count>4096 or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):raise Refused('LOCAL_ENTRIES')
   if stat.S_ISREG(st.st_mode):
    size+=st.st_size
    if size>total or len(out)>=64:raise Refused('LOCAL_BYTES')
    h=hashlib.sha256()
    with p.open('rb') as f:
     left=st.st_size
     while True:
      part=f.read(min(65536,left+1))
      if not part:break
      left-=len(part)
      if left<0:raise Refused('LOCAL_GROWTH')
      h.update(part)
    if left:raise Refused('LOCAL_CHANGE')
    out[str(p.relative_to(root))]={'bytes':st.st_size,'sha256':h.hexdigest()}
 return out

def spool_guard(root):
 count=0;size=0
 for base,dirs,files in os.walk(root,followlinks=False):
  for name in dirs+files:
   st=(Path(base)/name).lstat();count+=1
   if count>4096 or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):raise Refused('SPOOL_ENTRY_BOUND')
   if stat.S_ISREG(st.st_mode):size+=st.st_size
   if size>spool_bytes():raise Refused('SPOOL_BYTE_BOUND')
 fs=os.statvfs(root)
 if fs.f_bavail*fs.f_frsize<2*MIB or fs.f_favail<128:raise Refused('LOCAL_HEADROOM')

class Adapter:
 def __init__(self,root,seal,*,key_id,key,password):
  # No caller endpoint, destination path, arbitrary command or projector.
  self.root=Path(root)
  if not self.root.is_absolute() or self.root.is_symlink() or self.root.resolve()!=self.root or not self.root.is_dir():raise Refused('OWNED_ROOT')
  if not isinstance(seal,dict) or set(seal)!={'member','set_id','snapshot','manifest','repository_files'}:raise Refused('SEAL_SCHEMA')
  self.seal=decode(canonical(seal));self.member=seal['member'];self.run=seal['set_id']
  if self.member not in MEMBERS or not isinstance(self.run,str) or not re.fullmatch('cold-[0-9a-f]{32}',self.run) or not hexid(seal['snapshot']):raise Refused('SEAL_CONTEXT')
  manifest=seal['manifest']
  if not isinstance(manifest,dict) or set(manifest)!={'member','set_id','files'} or manifest['member']!=self.member or manifest['set_id']!=self.run:raise Refused('MANIFEST_CONTEXT')
  self.profile=limits(self.member)
  files_shape(manifest['files'],32,self.profile['source']);files_shape(seal['repository_files'],64,self.profile['repository'])
  if 'manifest.json' in manifest['files']:raise Refused('MANIFEST_ALIAS')
  for value in (key_id,key,password):
   if not isinstance(value,str) or not 1<=len(value)<=4096 or any(c in value for c in ('\x00','\n','\r')):raise Refused('CREDENTIAL_SHAPE')
  self.env={'PATH':'/usr/bin:/bin','HOME':str(self.root),'TMPDIR':str(self.root/'transfer-scratch'),'RESTIC_PASSWORD':password,'RESTIC_FROM_PASSWORD':password,'AWS_ACCESS_KEY_ID':key_id,'AWS_SECRET_ACCESS_KEY':key,'AWS_DEFAULT_REGION':'us-west-004','RESTIC_RETRY_LOCK':'0s'}
  self.source=self.root/'sealed';self.remote=BASE+self.run+'/'+self.member;self.used=False
 def save(self,phase,**fields):
  raw=canonical({'schema':'b2-transfer-state-v1','set_id':self.run,'member':self.member,'seal_sha256':digest(self.seal),'phase':phase,**fields})
  if len(raw)>8192:raise Refused('STATE_BOUND')
  tmp=self.root/'transfer.tmp'
  fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  try:
   with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
   os.replace(tmp,self.root/'transfer.json');d=os.open(self.root,os.O_RDONLY)
   try:os.fsync(d)
   finally:os.close(d)
  except BaseException:raise Refused('STATE_UNCERTAIN') from None
 def command(self,*args):
  operational_command(self.remote,args)
  spool_guard(self.root)
  out=run_fixed([str(BINARY),'--no-cache','-r',self.remote,*args],env=self.env,cwd=self.root,budget=self.budget,output_limit=self.profile['source'] if args and args[0]=='dump' else MIB)
  spool_guard(self.root)
  return out
 def verify(self,snapshot):
  self.command('check','--read-data')
  rows=self.command('ls','--json',snapshot).splitlines()
  if len(rows)>128:raise Refused('TREE_BOUND')
  paths=[]
  for raw in rows:
   row=decode(raw)
   if not isinstance(row,dict):raise Refused('TREE_SCHEMA')
   if row.get('type')=='file':paths.append(row.get('path'))
   elif row.get('type') not in (None,'dir'):raise Refused('TREE_TYPE')
  expected={'/'+n for n in self.seal['manifest']['files']}|{'/manifest.json'}
  if len(paths)!=len(set(paths)) or set(paths)!=expected:raise Refused('TREE_COVERAGE')
  if decode(self.command('dump',snapshot,'/manifest.json'))!=self.seal['manifest']:raise Refused('MANIFEST_MISMATCH')
  for name,meta in self.seal['manifest']['files'].items():
   raw=self.command('dump',snapshot,'/'+name)
   if len(raw)!=meta['bytes'] or hashlib.sha256(raw).hexdigest()!=meta['sha256']:raise Refused('CONTENT_MISMATCH')
 def upload(self):
  if self.used:raise Refused('NO_AUTOMATIC_RETRY')
  self.used=True;fd=os.open(self.root/'transfer.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
  try:
   try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except OSError:raise Refused('OPERATION_ACTIVE') from None
   if (self.root/'transfer.json').exists() or (self.root/'transfer.tmp').exists():raise Refused('EXISTING_INTENT')
   verify_binary();spool_guard(self.root)
   if self.source.is_symlink() or not self.source.is_dir() or local_inventory(self.source,self.profile['repository'])!=self.seal['repository_files']:raise Refused('SEALED_SOURCE_CHANGED')
   scratch=self.root/'transfer-scratch';scratch.mkdir(mode=0o700,exist_ok=False)
   fs=os.statvfs(self.root)
   if fs.f_bavail*fs.f_frsize<2*MIB or fs.f_favail<128:raise Refused('LOCAL_HEADROOM')
   self.budget=Budget.start(seconds=120,commands=40,output_bytes=output_bytes(self.member))
   self.save('PENDING') # durable before any remote effect; restart never replays
   try:
    self.command('init')
    self.command('copy','--from-repo',str(self.source),self.seal['snapshot'])
    snapshot=destination_snapshot(self.command('snapshots','--json'),self.seal['snapshot'])
    self.verify(snapshot)
    if local_inventory(self.source,self.profile['repository'])!=self.seal['repository_files']:raise Refused('SEALED_SOURCE_CHANGED')
    receipt={'schema':'b2-restic-member-v1','set_id':self.run,'member':self.member,'repository':self.remote,'source_snapshot':self.seal['snapshot'],'snapshot':snapshot,'manifest_sha256':digest(self.seal['manifest']),'seal_sha256':digest(self.seal),'verification':'full-read-data-and-exact-file-dumps'}
    self.save('VERIFIED',receipt=receipt);return receipt
   except BaseException:
    self.budget.failed=True
    try:self.save('UNCERTAIN',reason='TRANSFER_UNCERTAIN',retained=True,repair_required=True)
    except BaseException:pass # PENDING/temp still forbids replay
    raise Refused('TRANSFER_UNCERTAIN') from None
  finally:os.close(fd)
