"""Run-level observer notifications; no business admission or provider authority.

Normal coordinator worker calls serially. Ambiguous sends are never retried;
reconcile reads the authenticated observer's current record without sending.
"""
import os,stat,copy
from pathlib import Path
from owner_wire import Refused,canonical,decode
from schedule_state import valid_due,valid_run
import b2_adapter as b

class Link:
 def __init__(self,root,client,due,run):
  valid_due(due);valid_run(run)
  self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
  if self.root.is_symlink() or self.root.resolve()!=self.root:raise Refused('notification root')
  if client.member!='observer':raise Refused('fixed observer role')
  self.client=client;self.due=due;self.run=run;self.path=self.root/'notification.json'
 def read(self):
  if self.path.with_suffix('.tmp').exists():raise Refused('notification publication interrupted')
  if not self.path.exists():return {'due':self.due,'run':self.run,'records':{}}
  fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
  try:
   s=os.fstat(fd)
   if not stat.S_ISREG(s.st_mode) or s.st_size>8192:raise Refused('notification bound')
   with os.fdopen(os.dup(fd),'rb') as f:v=decode(f.read(8193))
  finally:os.close(fd)
  if set(v)!={'due','run','records'} or v['due']!=self.due or v['run']!=self.run or not isinstance(v['records'],dict) or not set(v['records'])<=set(('started','complete','failed')):raise Refused('notification context')
  return v
 def save(self,v):
  raw=canonical(v)
  if len(raw)>8192:raise Refused('notification bound')
  tmp=self.path.with_suffix('.tmp');fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,self.path);fd=os.open(self.root,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
 def proof(self,action,proof):
  if action not in ('started','complete','failed'):raise Refused('fixed notification')
  if action!='complete':
   if proof is not None:raise Refused('unexpected completion')
   return
  if not isinstance(proof,dict) or set(proof)!={'due','trigger_id','run','catalog_sha256','catalog_snapshot','completion_sha256'} or proof['due']!=self.due or proof['run']!=self.run or proof['trigger_id']!='scheduled-'+str(self.due) or not all(b.hexid(proof[k]) for k in ('catalog_sha256','catalog_snapshot','completion_sha256')):raise Refused('exact completion required')
 def verified(self,action,value,proof):
  if not isinstance(value,dict) or set(value)!={'due','trigger_id','run','phase','completion'} or value['due']!=self.due or value['run']!=self.run or value['trigger_id']!='scheduled-'+str(self.due) or value['phase']!={'started':'DISPATCHED','complete':'COMPLETE','failed':'FAILED'}[action] or value['completion']!=proof:raise Refused('observer response mismatch')
  return value
 def send(self,action,proof=None):
  self.proof(action,proof);v=self.read();records=v['records']
  if action in records:
   old=records[action]
   if old.get('phase')!='DONE' or old.get('proof')!=proof:raise Refused('notification pending; reconcile only')
   return self.verified(action,old['result'],proof)
  if any(r.get('phase')!='DONE' for r in records.values()) or 'complete' in records or 'failed' in records:raise Refused('notification sequence closed')
  if action!='started' and 'started' not in records:raise Refused('start acknowledgment required')
  records[action]={'phase':'PENDING','proof':copy.deepcopy(proof),'result':None};self.save(v)
  value=self.client.call('/submit',{'action':action,'due':self.due,'run':self.run})
  value=self.verified(action,value,proof)
  records[action].update(phase='DONE',result=value);self.save(v);return value
 def reconcile(self,action):
  v=self.read();r=v['records'].get(action)
  if not isinstance(r,dict) or r.get('phase')!='PENDING':raise Refused('pending notification required')
  self.proof(action,r['proof'])
  status=self.client.call('/status',{'kind':'monitor_status'})
  value=self.verified(action,status.get('current'),r['proof'])
  r.update(phase='DONE',result=value);self.save(v);return value
