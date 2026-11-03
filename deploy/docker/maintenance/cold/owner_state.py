"""Small current-run journal. Durable intent precedes every native effect."""
import copy,fcntl,os
from pathlib import Path
from owner_wire import canonical,decode,Refused
CAP=512*1024;RECEIPT=24576;MAX_OPS=16
class Journal:
 def __init__(self,path,member,boot):
  self.path=Path(path);self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  self.fd=os.open(self.path.with_suffix('.lock'),os.O_CREAT|os.O_RDWR,0o600)
  try:fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BaseException:os.close(self.fd);raise Refused('owner already active')
  self.temp=self.path.with_suffix('.tmp');self.recovered=self.path.exists() or self.temp.exists()
  if self.temp.exists():raise Refused('interrupted state replacement')
  if self.path.exists():
   if self.path.stat().st_size>CAP:raise Refused('journal bound')
   self.doc=decode(self.path.read_bytes())
   if self.doc.get('member')!=member:raise Refused('journal member mismatch')
  else:
   self.doc={'schema':1,'member':member,'boot':boot,'run':None,'operations':{}}
   self.persist(self.doc)
 def persist(self,value):
  raw=canonical(value)
  if len(raw)>CAP:raise Refused('journal capacity')
  fd=os.open(self.temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
  try:
   with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
   os.replace(self.temp,self.path)
   parent=os.open(self.path.parent,os.O_RDONLY)
   try:os.fsync(parent)
   finally:os.close(parent)
  except BaseException:raise
 def begin(self,run,op,action,*,resolved=None):
  if self.recovered:raise Refused('recovered run requires supported repair')
  recovery=self.doc.get('last_recovery')
  if recovery:
   if run==recovery['proof']['run']:raise Refused('retired recovery run cannot restart')
   if self.doc['run'] is None and self.doc.get('last_success') is None and run!=recovery['proof']['next_run']:raise Refused('fixed first maintenance run required')
  last=self.doc.get('last_retirement')
  if last and last['operation']==op:return False
  if self.doc.get('last_success',{}).get('run')==run:raise Refused('retired run cannot restart')
  old=self.doc['operations'].get(op)
  if old:
   if old['run']!=run or old['action']!=action:raise Refused('operation identity conflict')
   return False
  if self.doc['run']not in (None,run):raise Refused('different run cannot replace history')
  resolved=resolved or {}
  if any(k not in self.doc['operations'] or v.get('run')!=run or v.get('member')!=self.doc['member'] or v.get('action')!=self.doc['operations'][k]['action'] for k,v in resolved.items()):raise Refused('repair resolution context')
  if len(self.doc['operations'])>=MAX_OPS or any(x['phase']!='DONE' and k not in resolved for k,x in self.doc['operations'].items()):raise Refused('operation pending or exhausted')
  new=copy.deepcopy(self.doc);new['run']=run;new['operations'][op]={'run':run,'action':action,'phase':'PENDING'}
  # Reserve the maximum terminal response before acknowledging or invoking effect.
  if len(canonical(new))+RECEIPT+512>CAP:raise Refused('terminal receipt reservation')
  self.persist(new);self.doc=new;return True
 def finish(self,op,receipt):
  if len(canonical(receipt))>RECEIPT:raise Refused('receipt bound')
  if self.doc['operations'][op]['phase']!='PENDING':raise Refused('no pending operation')
  new=copy.deepcopy(self.doc);new['operations'][op].update(phase='DONE',receipt=receipt)
  self.persist(new);self.doc=new
 def fail_backup(self,op,failure):
  if self.doc['operations'][op]['phase']!='PENDING':raise Refused('no pending backup')
  new=copy.deepcopy(self.doc);new['operations'][op].update(phase='FAILED',failure=failure)
  self.persist(new);self.doc=new
 def retire(self,proof,operation=None,*,resolved=None):
  ops=self.doc['operations'];run=self.doc['run']
  resolved=resolved or {}
  if any(k not in ops or v.get('run')!=run or v.get('member')!=self.doc['member'] or v.get('action')!=ops[k]['action'] for k,v in resolved.items()):raise Refused('retirement resolution context')
  if proof.get('run')!=run or any(v['phase']!='DONE' and k not in resolved for k,v in ops.items() if k!=operation):raise Refused('unresolved run cannot retire')
  cleanup=[v for v in ops.values() if v['action']=='cleanup' and v['phase']=='DONE']
  cleanup.extend({'receipt':v} for v in resolved.values() if v['action']=='cleanup')
  if len(cleanup)!=1 or cleanup[0]['receipt']['result'].get('deleted')is not True or cleanup[0]['receipt']['result'].get('catalog_sha256')!=proof.get('catalog_sha256'):raise Refused('cleanup proof mismatch')
  new=copy.deepcopy(self.doc);new.update(run=None,operations={},last_success=proof)
  if resolved:
   import hashlib
   new['retired_resolution_sha256']={k:hashlib.sha256(canonical(v)).hexdigest() for k,v in resolved.items()}
  if operation is not None:
   if ops[operation]['action']!='retire' or ops[operation]['phase']!='PENDING':raise Refused('retirement intent')
   receipt={'member':new['member'],'boot':new['boot'],'run':run,'action':'retire','result':proof}
   new['last_retirement']={'operation':operation,'record':{'run':run,'action':'retire','phase':'DONE','receipt':receipt}}
  self.persist(new);self.doc=new
 def retire_recovery(self,proof):
  import hashlib,re
  fields={'schema','member','boot','run','next_run','operations_sha256','recovery_sha256'}
  if type(proof)is not dict or set(proof)!=fields or proof['schema']!='recovery-bootstrap-v1':raise Refused('recovery retirement schema')
  if self.recovered or self.doc.get('last_recovery') is not None:raise Refused('recovery retirement already used/uncertain')
  if any(proof[k]!=self.doc[k] for k in ('member','boot','run')) or proof['next_run']==proof['run'] or not re.fullmatch('cold-[0-9a-f]{32}',str(proof['next_run'])) or not re.fullmatch('[0-9a-f]{64}',str(proof['recovery_sha256'])):raise Refused('recovery retirement identity')
  ops=self.doc['operations'];run=proof['run'];m=proof['member']
  expected={f'{run}:{m}:start:1':'start',f'{run}:{m}:verify:1':'verify',f'{run}:{m}:verify:2':'verify'}
  if set(ops)!=set(expected) or hashlib.sha256(canonical(ops)).hexdigest()!=proof['operations_sha256']:raise Refused('recovery operation binding')
  for op,action in expected.items():
   v=ops[op];r=v.get('receipt',{})
   if v.get('phase')!='DONE' or v.get('action')!=action or v.get('run')!=run or any(r.get(k)!=value for k,value in {'member':m,'boot':proof['boot'],'run':run,'action':action}.items()):raise Refused('unproved recovery operation')
  original=copy.deepcopy(self.doc);new=copy.deepcopy(self.doc)
  new.update(run=None,operations={},last_recovery={'proof':copy.deepcopy(proof),'original':original})
  # Full original receipts remain durable; reserve a normal terminal receipt.
  if len(canonical(new))+RECEIPT+512>CAP:raise Refused('recovery retention capacity')
  self.persist(new);self.doc=new
 def close(self):os.close(self.fd)
