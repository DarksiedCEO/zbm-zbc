"""Fixed owner-local repair executor. Original operation records never rewritten."""
import copy,threading
from pathlib import Path
from owner_wire import Refused,canonical
from repair_state import Repairs,digest
from backend_quiescence import require_quiescent

class OwnerRepairs:
 def __init__(self,owner):self.owner=owner;self.records={};self.worker=None
 def record(self,operation,command):
  o=self.owner
  incident=o.journal.doc['operations'].get(operation)
  if incident is None or incident['run']!=o.journal.doc['run']:raise Refused('original incident unavailable')
  ident={'member':o.member,'run':incident['run'],'operation':operation}
  key=digest({'identity':ident,'command':command})
  if key not in self.records:
   root=o.root/'repairs';root.mkdir(mode=0o700,exist_ok=True)
   entries=list(root.iterdir())
   if any(p.is_symlink() or not p.is_dir() for p in entries) or len(entries)>=8 and not (root/key).exists():raise Refused('repair incident capacity')
   self.records[key]=Repairs(root/key,ident,incident)
  return self.records[key]
 def wire(self,v):
  out={k:v[k] for k in ('schema','identity','incident_sha256')}
  p=v['repair']
  out['repair']=None if p is None else {k:x for k,x in p.items() if k!='evidence'}
  if len(canonical(out))>32768:raise Refused('repair status response bound')
  return out
 def status(self,operation,command):
  r=self.record(operation,command);r.check();return self.wire(r.read())
 def resolved(self):
  # Re-read independently locked, immutable incident records before every use.
  out={}
  for r in self.records.values():
   r.check();v=r.read();p=v['repair']
   if p and p['phase']=='RESOLVED' and p['command'] in ('retry-upload','finish-cleanup') and v['identity']['run']==self.owner.journal.doc['run']:
    if p['command']=='retry-upload':from typed_upload_repair import validate_resolution
    else:from typed_cleanup_repair import validate_resolution
    validate_resolution(self.owner,v);out[v['identity']['operation']]=p['result']
  return out
 def submit(self,body):
  o=self.owner
  if set(body)!={'member','boot','run','incident','incident_sha256','command'} or body['member']!=o.member or body['boot']!=o.boot or body['run']!=o.journal.doc['run']:raise Refused('repair exact context')
  command=body['command'];operation=body['incident']
  if command=='finish-cleanup':
   from typed_cleanup_repair import submit
   return submit(self,body)
  if command=='retry-upload':
   from typed_upload_repair import submit
   return submit(self,body)
  if command in ('quiesce-originals','verify-sealed','resume-originals','publish-originals'):
   return self.originals(body)
  raise Refused('legacy transfer repair unsupported for member-v1')
 def originals(self,body):
  o=self.owner;command=body['command'];operation=body['incident']
  incident=o.journal.doc['operations'].get(operation)
  if incident is None or digest(incident)!=body['incident_sha256']:raise Refused('original incident mismatch')
  if (o.worker and o.worker.is_alive()) or (self.worker and self.worker.is_alive()):raise Refused('owner effect active')
  require_quiescent()
  r=self.record(operation,command);state=r.read()
  if state['repair'] is not None:return self.wire(state)
  if command!='quiesce-originals':
   previous='quiesce-originals' if command in ('resume-originals','verify-sealed') else 'resume-originals'
   prior=self.status(operation,previous)['repair']
   if prior is None or prior['phase']!='RESOLVED' or prior['boot']!=o.boot:raise Refused('prior current-boot recovery stage missing')
  prepared=r.prepare(command,{'owner_boot':o.boot,'original_journal_sha256':digest(o.journal.doc)},o.boot);r.begin(prepared['repair_id'])
  def execute():
   try:
    from original_repair import quiesce,resume,publish,verify_sealed
    result={'quiesce-originals':quiesce,'verify-sealed':verify_sealed,'resume-originals':resume,'publish-originals':publish}[command](o)
    with o.mutex:r.finish(prepared['repair_id'],result)
   except BaseException:
    # Unlike sealed transfer failure, original ownership/integrity failure is
    # business-critical and must keep this owner's ingress closed.
    o.fenced=True
    try:o.front.close()
    except BaseException:pass
    with o.mutex:
     try:r.fail(prepared['repair_id'],'ORIGINAL_RECOVERY_UNRESOLVED')
     except BaseException:pass
  self.worker=threading.Thread(target=execute,name='cold-original-repair',daemon=False);self.worker.start();return self.wire(r.read())
 def close(self):
  for r in self.records.values():r.close()
