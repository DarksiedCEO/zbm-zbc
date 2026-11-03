"""Durable run-level cold sequence over authenticated owner APIs only."""
import os,time,secrets,copy,threading,fcntl
from pathlib import Path
from owner_wire import Server,Client,canonical,decode,Refused,MEMBERS
from cold_front import Front
from coordinator_repair import exclusive
START=('ledger','legal','compliance','finance','seo');STOP=('seo','finance','compliance','legal','ledger')
class Coordinator:
 def __init__(self,root,token,password,*,recover=False,transport_credentials=None,run_id=None,serve_control=True):
  if type(serve_control)is not bool:raise Refused('fixed control host mode')
  if run_id is not None and (not isinstance(run_id,str) or not __import__('re').fullmatch('cold-[0-9a-f]{32}',run_id)):raise Refused('fixed run identity')
  self.control_lock=threading.RLock();self.normal_active=False;self.repair_worker=None
  self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True);self.path=self.root/'run.json'
  self.owner_fd=os.open(self.root/'coordinator.lock',os.O_CREAT|os.O_RDWR,0o600)
  try:fcntl.flock(self.owner_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BaseException:os.close(self.owner_fd);raise Refused('coordinator already active')
  if self.path.with_suffix('.tmp').exists():raise Refused('interrupted coordinator publication; recovery unresolved')
  if self.path.exists() and not recover:raise Refused('retained run needs supported recovery')
  retained=None
  if self.path.exists():
   if self.path.stat().st_size>262144:raise Refused('retained coordinator bound')
   retained=decode(self.path.read_bytes())
   if not isinstance(retained,dict) or retained.get('schema')!=1:raise Refused('retained coordinator schema')
  self.clients={m:Client(m,m+'.railway.internal',27002,token) for m in MEMBERS}
  if retained is None:
   self.doc={'schema':1,'run':run_id or 'cold-'+secrets.token_hex(16),'phase':'INITIALIZING','boots':{},'receipts':{},'barrier':None,'seals':{}};self.save(self.doc)
  else:self.doc=retained
  from catalog import Catalog
  self.catalog=Catalog(password,transport_credentials)
  from scheduled_completion import Completion
  self.completion=Completion(self)
  self.catalog_server=None;self.catalog_thread=None
  if serve_control:
   self.catalog_server=Server(('0.0.0.0',27002),token,self.catalog_status)
   self.catalog_thread=threading.Thread(target=self.catalog_server.serve_forever,daemon=True);self.catalog_thread.start()
  self.observer=Client('observer','observer.railway.internal',27003,token)
  self.counts={};self.front=Front({m:(m+'.railway.internal',27001) for m in MEMBERS})
 def save(self,value):
  raw=canonical(value)
  if len(raw)>262144:raise Refused('coordinator journal bound')
  tmp=self.path.with_suffix('.tmp');fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,self.path);fd=os.open(self.root,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
 def update(self,**fields):
  value=copy.deepcopy(self.doc);value.update(fields);self.save(value);self.doc=value
 def status(self,m,op=None):
  v=self.clients[m].call('/status',{'member':m,'boot':self.doc['boots'].get(m,'discover'),'operation':op})
  if v.get('member')!=m or v.get('fenced')is not False:raise Refused('owner '+m+' uncertain: '+str(v.get('failure','unavailable'))[:80])
  if m in self.doc['boots'] and v.get('boot')!=self.doc['boots'][m]:raise Refused('owner boot changed')
  return v
 def command(self,m,action):
  from schedule_advance import require_no_pending
  require_no_pending(self)
  i=self.counts.get((m,action),0)+1
  if i>2:raise Refused('fixed run action bound')
  op=self.doc['run']+':'+m+':'+action+':'+str(i);body={'member':m,'boot':self.doc['boots'][m],'run':self.doc['run'],'action':action,'operation':op}
  # Durable source intent before request; no resend on lost acknowledgment.
  rs=copy.deepcopy(self.doc['receipts']);rs[op]={'phase':'PENDING','body':body}
  reserved=copy.deepcopy(self.doc);reserved['receipts']=rs
  if len(canonical(reserved))+24576+512>262144:raise Refused('coordinator terminal reservation')
  self.update(receipts=rs)
  self.counts[(m,action)]=i
  try:self.clients[m].call('/submit',body)
  except Refused:pass
  end=time.monotonic()+(110 if action in ('seal','upload','verify_remote','cleanup') else 50)
  while time.monotonic()<end:
   v=self.status(m,op);record=v['operations'].get(op)
   if record and record.get('phase')=='FAILED':raise Refused('backup operation failed; retained')
   if record and record.get('phase')=='DONE':
    receipt=record.get('receipt')
    if not isinstance(receipt,dict) or any(receipt.get(k)!=body[k] for k in ('member','boot','run','action')):raise Refused('owner completion context')
    rs=copy.deepcopy(self.doc['receipts']);rs[op]={'phase':'DONE','body':body,'receipt':receipt};self.update(receipts=rs)
    return receipt['result']
   time.sleep(.05)
  raise Refused('operation completion unknown')
 def discover(self):
  boots={}
  for m in MEMBERS:
   end=time.monotonic()+30
   while True:
    try:v=self.status(m);break
    except Refused:
     if time.monotonic()>end:raise
     time.sleep(.1)
   if v['phase']!='CLOSED' or v['run']is not None or v['child_pid']is not None:raise Refused('owner not fresh closed')
   boots[m]=v['boot']
  self.update(boots=boots)
 @exclusive
 def start(self):
  from schedule_advance import require_no_pending
  require_no_pending(self)
  self.discover()
  for m in MEMBERS:self.clients[m].call('/relay-bind',{'member':m,'boot':self.doc['boots'][m],'boots':self.doc['boots']})
  for m in START:self.command(m,'start')
  for m in MEMBERS:self.command(m,'verify')
  self.publish()
 def publish(self):
  # Central external front remains closed throughout individual owner publication.
  for m in MEMBERS:
   v=self.status(m)
   if v['phase']!='VERIFIED_CLOSED' or type(v['child_pid'])is not int:raise Refused('not all owners currently ready')
  self.update(phase='PUBLISHING')
  for m in MEMBERS:self.command(m,'publish')
  for m in MEMBERS:
   if self.status(m)['phase']!='OPEN':raise Refused('owner not open')
  self.update(phase='OPEN');self.front.open()
 def require_stopped(self):
  for m in MEMBERS:
   v=self.status(m)
   if v['phase']not in ('STOPPED_LOCKED','SEALED_LOCKED') or v['child_pid']is not None:raise Refused('stopped owner barrier changed')
 @exclusive
 def cycle(self):
  from schedule_advance import require_no_pending
  require_no_pending(self)
  for m in MEMBERS:
   v=self.clients[m].call('/status',{'member':m,'boot':self.doc['boots'][m],'inspect':'capacity'})
   if v.get('phase')!='OPEN' or v.get('fenced') is not False or v.get('boot')!=self.doc['boots'][m]:raise Refused('preflight owner binding')
  self.update(phase='CLOSING');started=time.monotonic();self.front.close()
  for m in MEMBERS:self.command(m,'close')
  stopped={}
  for m in STOP:stopped[m]=self.command(m,'stop')
  self.require_stopped();self.update(phase='ALL_STOPPED_LOCKED',barrier={'run':self.doc['run'],'boots':self.doc['boots'],'stopped':stopped})
  seals={}
  for m in MEMBERS:
   self.require_stopped();seals[m]=self.command(m,'seal');self.update(phase='SEALING',seals=seals)
  self.require_stopped()
  if set(seals)!=set(MEMBERS):raise Refused('incomplete seals')
  self.update(phase='ALL_SEALED')
  # Normal restart only. No uncertainty/adoption/recovery path exists here.
  for m in START:self.command(m,'restart')
  for m in MEMBERS:self.command(m,'verify')
  self.publish();elapsed=time.monotonic()-started
  self.update(phase='OPEN_SEALED',pause_seconds=elapsed)
  return {'run':self.doc['run'],'pause_seconds':elapsed,'members':list(seals),'seals_retained':True,'upload':'NOT_IN_THIS_BLOCK'}
 def stop_final(self):
  self.front.close()
  for m in MEMBERS:self.command(m,'close')
  for m in STOP:self.command(m,'stop')
  self.require_stopped();self.update(phase='FINISHED_CLOSED');self.front.stop()

 def bind_schedule(self,due):
  from schedule_advance import require_no_pending
  require_no_pending(self)
  if type(due)is not int or not 0<=due<=253402300799:raise Refused('schedule due')
  value={'due':due,'trigger_id':'scheduled-'+str(due),'run':self.doc['run']}
  if self.doc.get('schedule') is not None:
   if self.doc['schedule']!=value:raise Refused('schedule identity conflict')
   return value
  if self.doc['phase']!='OPEN':raise Refused('schedule must precede cold effects')
  self.update(schedule=value);return value
 def finish_recovery_bootstrap(self):
  from recovery_bootstrap import advance
  return advance(self)
 def catalog_status(self,path,body):
  if path=='/status' and body.get('kind')=='recovery-bootstrap-permit':
   from recovery_bootstrap import permit
   return permit(self,body)
  if path in ('/repair','/repair-status') and body.get('command')=='rearm-idle':
   from coordinator_rearm import control
   return control(self,path,body)
  if path=='/status' and body.get('kind')=='idle-rearm-permit':
   from coordinator_rearm import permit
   return permit(self,body)
  if path in ('/repair','/repair-status') and body.get('command')=='finish-repaired-run':
   from repaired_retirement import control
   return control(self,path,body)
  if path=='/status' and body.get('kind')=='repaired-permit':
   from repaired_retirement import permit
   return permit(self,body)
  if path=='/status' and set(body)=={'kind','due','run'} and body['kind']=='repaired_complete':
   from repaired_retirement import completion
   return completion(self,body['due'],body['run'])
  if path in ('/submit','/status') and body.get('kind')=='scheduled_backup':
   from scheduled_trigger import control
   return control(self,path,body)
  if path=='/status' and body.get('kind')=='advance-permit':
   if set(body)!={'kind','run','member','boot'}:raise Refused('advance permit context')
   from schedule_advance import permit
   return permit(self,body['run'],body['member'],body['boot'])
  if path in ('/repair','/repair-status') and body.get('command')=='finish-advance':
   from schedule_advance import control
   return control(self,path,body)
  if path=='/repair':
   from coordinator_repair import submit
   return submit(self,body)
  if path=='/repair-status' and body=={'kind':'repair_inspect'}:
   from coordinator_repair import inspect
   return inspect(self)
  if path=='/status' and set(body)=={'kind','run','due'} and body['kind']=='scheduled_complete':
   return self.completion.read(body['due'],body['run'])
  if path!='/status' or set(body)!={'kind','run'} or body['kind']!='complete_catalog':raise Refused('fixed catalog status')
  # Publish only after durable complete proof and all-five fresh verification.
  proof=self.catalog.read(body['run']);disk=decode(self.path.read_bytes())
  if disk.get('run')!=body['run'] or disk.get('catalog_proof')!=proof or disk.get('phase')not in ('REMOTE_VERIFIED','CLEANING','COMPLETE','RETIRING','RETIRED'):raise Refused('catalog durable binding')
  return proof
 @exclusive
 def transfer(self):
  if self.doc['phase']!='OPEN_SEALED':raise Refused('not reopened sealed cohort')
  from distributed_transfer import preflight_catalog
  preflight_catalog(self.doc['seals'],self.doc['run'])
  self.update(phase='UPLOADING');uploads={};cleaned={};stage='upload'
  stage_member=None
  try:
   for m in MEMBERS:
    stage_member=m;uploads[m]=self.command(m,'upload')
   stage='catalog';stage_member=None
   proof=self.catalog.create(self.doc['run'],uploads,self.doc['seals'])
   stage='verify_remote'
   for m in MEMBERS:
    stage_member=m
    if self.command(m,'verify_remote')!=uploads[m]:raise Refused('fresh remote mismatch')
   stage='publish';stage_member=None
   self.update(phase='REMOTE_VERIFIED',catalog_proof=proof);self.catalog.publish(proof)
   stage='cleanup'
   for m in MEMBERS:
    stage_member=m;cleaned[m]=self.command(m,'cleanup');self.update(phase='CLEANING',cleaned=cleaned)
   self.update(phase='COMPLETE',cleaned=cleaned)
   return {'phase':'COMPLETE','run':self.doc['run'],'catalog_sha256':proof['catalog_sha256'],'catalog_snapshot':proof['catalog_snapshot'],'cleaned':list(cleaned)}
  except BaseException as exc:
   allowed={'coordinator terminal reservation','coordinator journal bound','fixed run action bound','backup operation failed; retained','operation completion unknown','fresh remote mismatch','control outcome unavailable','operation pending or exhausted','catalog durable binding'}
   code=str(exc) if str(exc) in allowed else 'UNCLASSIFIED_REFUSAL'
   alert={'stage':stage,'member':stage_member,'refusal_code':code,'coordinator_bytes':len(canonical(self.doc)),'run':self.doc['run'],'event':'backup_transfer_failed','primary':'andre@zbestmedia.com','copy':'hello@zbestmedia.com','independent_fallback':False,'delivery':'PENDING_NOT_SENT','error_type':type(exc).__name__}
   self.update(phase='FAILED',alert=alert,completed_uploads=list(uploads),cleaned=cleaned)
   # No front/owner publication operation here: business stays open.
   return {'phase':'FAILED','run':self.doc['run'],'completed_uploads':list(uploads),'retained_required':True,'alert':alert}
 @exclusive
 def retire(self):
  if self.doc['phase']!='COMPLETE' or set(self.doc.get('cleaned',{}))!=set(MEMBERS):raise Refused('incomplete run cannot retire')
  self.update(phase='RETIRING')
  for m in MEMBERS:self.command(m,'retire')
  summary={'run':self.doc['run'],'catalog_sha256':self.doc['catalog_proof']['catalog_sha256'],'catalog_snapshot':self.doc['catalog_proof']['catalog_snapshot']}
  self.update(phase='RETIRED',last_success=summary)
  return summary
 @exclusive
 def next_run(self):
  from schedule_advance import prepare
  return prepare(self)
 @exclusive
 def finish_advance(self):
  from schedule_advance import finish
  return finish(self)
