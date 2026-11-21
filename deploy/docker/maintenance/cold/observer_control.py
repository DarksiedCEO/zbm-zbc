"""Observer-owned durable schedule; coordinator never mounts this storage.

Only owner_wire.Server's authenticated dispatch may expose this boundary.
Completion is fetched from the fixed coordinator verification endpoint, never
accepted as a caller-provided healthy flag. No timer or email is installed here.
"""
import threading,time,os,fcntl,stat
from pathlib import Path
from contextlib import contextmanager
from owner_wire import Client,Refused,Server
from schedule_state import Schedule,valid_due,valid_run

class Control:
 def __init__(self,path,token,*,first_due,cadence,grace):
  self._token=token
  self.completion=Client('coordinator','coordinator.railway.internal',27002,token)
  self.path=Path(path);self.lock=threading.RLock();self.fd=None;self.schedule=None;self.closed=False
  self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  self.lock_path=self.path.with_suffix('.owner.lock')
  try:
   self.fd=os.open(self.lock_path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
   st=os.fstat(self.fd)
   if not stat.S_ISREG(st.st_mode):raise Refused('observer lock type')
   fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB);self.lock_id=(st.st_dev,st.st_ino)
   self.schedule=Schedule(self.path,check_same_thread=False)
   self.schedule.arm(first_due,cadence,grace=grace)
  except BaseException:
   self.close();raise Refused('observer ownership/configuration refused') from None
 def server(self,address=('0.0.0.0',27003)):
  return Server(address,self._token,self.dispatch)
 def close(self):
  with self.lock:
   if self.closed:return
   self.closed=True
   if self.schedule is not None:self.schedule.close()
   if self.fd is not None:os.close(self.fd);self.fd=None
 @contextmanager
 def store(self):
  # One connection for this exclusive observer lifetime. Reopening Schedule on
  # every tick would incorrectly turn a live in-flight email into UNKNOWN.
  with self.lock:
   if self.closed:raise Refused('observer closed')
   st=self.lock_path.lstat()
   if not stat.S_ISREG(st.st_mode) or (st.st_dev,st.st_ino)!=self.lock_id:raise Refused('observer lock replaced')
   yield self.schedule
 def tick(self):
  with self.store() as s:return s.observe(int(time.time()))
 def dispatch(self,path,body):
  if path=='/status':
   if body!={'kind':'monitor_status'}:raise Refused('fixed observer status')
   with self.store() as s:return {'monitoring':s.monitoring(),'current':s.current()}
  if path=='/submit' and type(body)is dict and body.get('action')=='recovery-locked':
   if set(body)!={'action','run','identity'}:raise Refused('recovery alert schema')
   with self.store() as store:return store.recovery_locked(body['run'],body['identity'])
  if path=='/submit' and type(body)is dict and body.get('action')=='archive-full':
   if set(body)!={'action','member','identity'}:raise Refused('archive full alert schema')
   with self.store() as store:return store.archive_full(body['member'],body['identity'])
  if path=='/submit' and type(body)is dict and body.get('action')=='repaired-complete':
   if set(body)!={'action','due','run'}:raise Refused('fixed repaired observer request')
   valid_due(body['due']);valid_run(body['run'])
   with self.store() as store:
    proof=self.completion.call('/status',{'kind':'repaired_complete','due':body['due'],'run':body['run']})
    if proof.get('due')!=body['due'] or proof.get('run')!=body['run']:raise Refused('repaired response identity')
    from repaired_observer import complete
    return complete(store,proof)
  if path!='/submit' or type(body)is not dict or set(body)!={'action','due','run'} or body['action'] not in ('started','complete','failed'):
   raise Refused('fixed observer control')
  action,due,run=body['action'],body['due'],body['run'];valid_due(due);valid_run(run)
  with self.store() as s:
   if action=='started':
    old=s.current()
    if old is not None and old['due']==due and old['run']==run and old['phase'] in ('DISPATCHED','COMPLETE'):
     return old
    s.bind(due,run);s.dispatched(due,run);return s.current()
   old=s.current()
   if old is None or old['due']!=due or old['run']!=run:raise Refused('observer current identity mismatch')
   if action=='failed':
    if old['phase']=='FAILED':return old
    s.failed();return s.current()
   if old['phase'] not in ('DISPATCHED','COMPLETE'):raise Refused('completion not eligible')
   proof=self.completion.call('/status',{'kind':'scheduled_complete','run':run,'due':due})
   if type(proof)is not dict or proof.get('due')!=due or proof.get('run')!=run:raise Refused('completion identity mismatch')
   if old['phase']=='COMPLETE':
    if old['completion']!=proof:raise Refused('completion replay changed')
    return old
   s.complete(proof);return s.current()
