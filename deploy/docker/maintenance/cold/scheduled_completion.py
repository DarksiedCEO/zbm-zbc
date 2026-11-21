"""Publish completion in the normal worker; serve only bounded durable evidence.

This certifies a completed backup, not perpetual health or future remote
availability. It performs no provider requests and grants no repair authority.
The caller must retain this per-run instance and serialize publish with the
normal worker. A new process or instance requires explicit reconciliation;
visible files alone never establish successful publication after an error.
"""
import os,stat,copy
from owner_wire import Refused,MEMBERS,decode,canonical
import b2_adapter as b
from distributed_transfer import require_proof

class Completion:
 def __init__(self,coordinator):
  self.c=coordinator;self.path=coordinator.root/'completion.json'
  self.published=None
 def load(self,path,limit):
  try:
   fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
   try:
    s=os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_size>limit:raise Refused('completion file bound')
    with os.fdopen(os.dup(fd),'rb') as f:raw=f.read(limit+1)
    if len(raw)>limit:raise Refused('completion file bound')
    return decode(raw)
   finally:os.close(fd)
  except (OSError,ValueError):raise Refused('completion file unavailable') from None
 def evidence(self,due,run):
  c=self.c
  if type(due)is not int or not 0<=due<=253402300799:raise Refused('completion due')
  before=self.load(c.path,262144)
  if before!=c.doc or before.get('phase')!='RETIRED' or before.get('run')!=run or before.get('schedule')!={'due':due,'run':run,'trigger_id':'scheduled-'+str(due)}:raise Refused('completion current cohort')
  proof=c.catalog.read(run)
  if before.get('catalog_proof')!=proof or set(before.get('seals',{}))!=set(MEMBERS) or set(before.get('cleaned',{}))!=set(MEMBERS) or set(before.get('boots',{}))!=set(MEMBERS):raise Refused('completion cohort incomplete')
  try:
   if proof['catalog']['seals_sha256']!=b.digest(before['seals']):raise Refused('completion seals binding')
   for m in MEMBERS:require_proof(proof,before['seals'][m],proof['catalog']['members'][m])
  except (b.Refused,KeyError,TypeError):raise Refused('completion versioned catalog') from None
  summary={'run':run,'catalog_sha256':proof['catalog_sha256'],'catalog_snapshot':proof['catalog_snapshot']}
  if before.get('last_success')!=summary:raise Refused('completion retirement summary')
  receipts=before.get('receipts',{})
  if not isinstance(receipts,dict) or any(v.get('phase')!='DONE' for v in receipts.values()):raise Refused('completion unresolved operation')
  for m in MEMBERS:
   expected=dict(summary,schema='b2-cleanup-v1',member=m,deleted=True,locator_sha256=b.digest(proof['locator']))
   if before['cleaned'][m]!=expected:raise Refused('completion cleanup evidence')
   for action,result in [('cleanup',expected),('retire',summary)]:
    records=[v for v in receipts.values() if v.get('body',{}).get('member')==m and v.get('body',{}).get('action')==action]
    if len(records)!=1:raise Refused('completion terminal receipt count')
    v=records[0];context={'member':m,'boot':before['boots'][m],'run':run,'action':action}
    if any(v['body'].get(k)!=x for k,x in context.items()) or v.get('receipt')!=dict(context,result=result):raise Refused('completion terminal identity')
  value={'due':due,'trigger_id':'scheduled-'+str(due),'run':run,'catalog_sha256':proof['catalog_sha256'],'catalog_snapshot':proof['catalog_snapshot'],'completion_sha256':b.digest(before)}
  return before,value
 def publish(self,due,run):
  before,value=self.evidence(due,run)
  for m in MEMBERS:
   v=self.c.status(m)
   expected={'member':m,'boot':before['boots'][m],'run':None,'phase':'OPEN','fenced':False,'last_success':before['last_success']}
   if any(v.get(k)!=x for k,x in expected.items()):raise Refused('completion owner retirement')
  if self.load(self.c.path,262144)!=before or self.c.doc!=before:raise Refused('completion changed during finalization')
  tmp=self.path.with_suffix('.json.tmp')
  if self.path.exists() or tmp.exists():raise Refused('completion publication already attempted')
  raw=canonical(value)
  fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,self.path);fd=os.open(self.c.root,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
  self.published=copy.deepcopy(value)
  return value
 def read(self,due,run):
  if self.published is None:raise Refused('completion publication unconfirmed; reconciliation required')
  before,expected=self.evidence(due,run)
  if self.path.with_suffix('.json.tmp').exists():raise Refused('completion publication interrupted')
  actual=self.load(self.path,2048)
  if actual!=expected or actual!=self.published or self.load(self.c.path,262144)!=before or self.c.doc!=before:raise Refused('completion durable evidence changed')
  return actual
