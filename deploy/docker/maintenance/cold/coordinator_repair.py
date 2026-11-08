"""Fixed asynchronous repair control, with no long HTTP effect or caller proof."""
import copy,threading
from functools import wraps
from owner_wire import Refused
from repair_state import digest

def exclusive(fn):
 @wraps(fn)
 def call(self,*args,**kwargs):
  with self.control_lock:
   other=self.repair_worker and self.repair_worker.is_alive() and threading.current_thread()is not self.repair_worker
   from scheduled_trigger import busy_other
   if self.normal_active or other or busy_other(self):raise Refused('coordinator operation active')
   self.normal_active=True
  try:return fn(self,*args,**kwargs)
  finally:
   with self.control_lock:self.normal_active=False
 return call

def inspect(c):
 return {'run':c.doc['run'],'incident_sha256':digest(c.doc),'phase':c.doc['phase'],'repair':copy.deepcopy(c.doc.get('repair_rpc'))}

def submit(c,body):
 if set(body)!={'command','run','incident_sha256'} or body['command']not in ('retry-upload','resume-originals','resume-sealed','finish-cleanup','recover-catalog'):raise Refused('fixed repair command')
 with c.control_lock:
  existing=c.doc.get('repair_rpc')
  if existing is not None:
   if existing['request']!=body:raise Refused('another repair intent exists')
   if body['command']!='finish-cleanup' or existing['phase']not in ('APPLYING','UNRESOLVED'):return inspect(c)
  from scheduled_trigger import busy_other
  if c.normal_active or busy_other(c) or c.repair_worker and c.repair_worker.is_alive():raise Refused('coordinator effect active')
  if existing is None and (body['run']!=c.doc['run'] or body['incident_sha256']!=digest(c.doc)):raise Refused('current incident binding')
  if body['command']=='retry-upload':
   from typed_coordinator_repair import preserve
   preserve(c,body)
  if body['command']=='finish-cleanup':
   if existing is None:
    from typed_cleanup_coordinator import preserve
    preserve(c,body)
   else:
    from typed_cleanup_coordinator import resume_preflight
    resume_preflight(c)
    from repaired_owner import immutable
    from typed_cleanup_repair import raw
    from owner_wire import decode
    archive=c.root/'cleanup-terminal-coordinator.json'
    if archive.with_name(archive.name+'.tmp').exists() or archive.is_symlink():raise Refused('ambiguous coordinator reconciliation')
    if not archive.exists():immutable(archive,c.doc)
    elif decode(raw(archive,262144)).get('repair_rpc',{}).get('request')!=body:raise Refused('coordinator reconciliation identity')
    c.update(repair_rpc={'request':copy.deepcopy(body),'phase':'APPLYING','result':None})
  if body['command']=='recover-catalog':
   from recover_catalog import preserve
   preserve(c,body)
  if existing is None:c.update(repair_rpc={'request':copy.deepcopy(body),'phase':'APPLYING','result':None})
  def execute():
   try:
    from repair_cli import retry_upload,resume_originals,finish_cleanup
    if body['command']=='retry-upload':
     from typed_coordinator_repair import retry_upload as typed_retry
     result=typed_retry(c)
    elif body['command']=='recover-catalog':
     from recover_catalog import recover
     result=recover(c)
    elif body['command']=='finish-cleanup':
     from typed_cleanup_coordinator import finish_cleanup as typed_finish
     result=typed_finish(c)
    else:result=resume_originals(c,sealed=body['command']=='resume-sealed')
    value=copy.deepcopy(c.doc['repair_rpc']);value.update(phase='RESOLVED',result=result);c.update(repair_rpc=value)
   except BaseException as exc:
    value=copy.deepcopy(c.doc['repair_rpc']);value.update(phase='UNRESOLVED',result={'error_type':type(exc).__name__,'action':'preserve incident; inspect evidence'})
    try:c.update(repair_rpc=value)
    except BaseException:pass # Durable APPLYING is not success and cannot replay.
  c.repair_worker=threading.Thread(target=execute,name='cold-coordinator-repair',daemon=False);c.repair_worker.start();return inspect(c)
