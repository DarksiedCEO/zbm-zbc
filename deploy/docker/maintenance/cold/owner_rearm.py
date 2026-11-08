"""Owner-local stages of one current-boot idle rearm; never adopts old PIDs."""
import copy,hashlib,os,threading
from owner_wire import Refused,Client,canonical,decode
from metadata_retirement import read,save
from idle_rearm import current_journal
from incident_archive import Archive
from backend_quiescence import require_quiescent,require_fresh_namespace
ACTIONS=('close','lock','bind','start','verify','publish')
def load(o):return decode(read(o.root/'idle-rearm.json'))
def permission(o,identity):
 v=Client('coordinator','coordinator.railway.internal',27002,o.token).call('/status',{'kind':'idle-rearm-permit','member':o.member,'boot':o.boot,'identity':identity})
 if v['identity']!=identity or v['boots'][o.member]!=o.boot:raise Refused('rearm permission binding')
 return v

def dispatch(o,path,body):
 if set(body)!={'member','boot','identity','action'} or body['boot']!=o.boot or body['action']not in ACTIONS:raise Refused('rearm context')
 p=o.root/'idle-rearm.json'
 if path=='/rearm-status':
  v=load(o)
  if v['identity']!=body['identity'] or v['boot']!=o.boot:raise Refused('rearm status binding')
  return v
 if path!='/rearm':raise Refused('rearm route')
 if o.worker and o.worker.is_alive() or o.repairs.worker and o.repairs.worker.is_alive():raise Refused('owner active')
 permit=permission(o,body['identity']);action=body['action']
 if p.exists():
  v=load(o)
  if v['identity']!=body['identity'] or v['boot']!=o.boot:raise Refused('interrupted rearm requires restore')
  if action in v['actions']:return v
  previous=list(v['actions'])
  # Canonical JSON key order is not lifecycle order.
  if set(previous)!=set(ACTIONS[:ACTIONS.index(action)]) or any(r['phase']!='DONE' for r in v['actions'].values()):raise Refused('rearm predecessor unresolved')
 else:
  if action!='close' or o.fenced or o.relay.unknown or o.relay.active:raise Refused('rearm first stage refusal')
  original=current_journal(o,permit)
  for record in o.repairs.records.values():
   record.check();r=record.read()
   if r.get('repair') and r['repair']['phase']!='RESOLVED':raise Refused('unresolved repair remains')
  from incident_archive import checked_capacity
  checked_capacity(o.root,o.member,Client('observer','observer.railway.internal',27003,o.token))
  if o.journal.recovered:require_fresh_namespace()
  v={'schema':'owner-idle-rearm-v1','identity':body['identity'],'boot':o.boot,'original_sha256':original,'actions':{}}
 v=copy.deepcopy(v);v['actions'][action]={'phase':'PENDING'};save(p,v)
 def work():
  try:
   result=effect(o,action,permit,v)
   with o.mutex:
    saved=load(o)
    if saved!=v:raise Refused('rearm state changed')
    done=copy.deepcopy(v);done['actions'][action]={'phase':'DONE','result':result};save(p,done)
  except BaseException:
   with o.mutex:
    o.fenced=True;o.front.close();o.relay_mode='CLOSED'
    # PENDING is intentionally retained if a safety write itself fails.
    failed=copy.deepcopy(v);failed['actions'][action]={'phase':'UNKNOWN','fallback':'remote-restore'}
    try:save(p,failed)
    except BaseException:pass
 o.worker=threading.Thread(target=work,daemon=True);o.worker.start();return v

def effect(o,action,p,v):
 require_quiescent()
 if action=='close':
  current_journal(o,p);o.front.close()
  if o.child is not None:
   if o.scheduler_control is not None:o.scheduler_control.command('pause')
   o.relay_mode='DRAIN'
  o.phase='CLOSED';return {'public':'CLOSED'}
 if action=='lock':
  current_journal(o,p)
  from original_repair import quiesce
  return quiesce(o)
 if action=='bind':
  current_journal(o,p);o.guard_identity()
  if p.get('phase')!='LOCKED' or set(p.get('locks',{}))!=set(p['boots']):raise Refused('all-owner durable lock barrier required')
  if p['locks'][o.member]!=load(o)['actions']['lock']['result']:raise Refused('own lock barrier changed')
  from cold_capture import source_inventory
  if source_inventory(o.source,o.member)!=o.repair_source:raise Refused('locked source changed')
  archive=Archive(o.root).append({'identity':p['identity'],'phase':'RESOLVED','evidence':{'kind':'retired-journal-before-rearm','journal':o.journal.doc,'permission':p}})
  new=copy.deepcopy(o.journal.doc);new['boot']=o.boot;new['rearm_identity']=p['identity']
  # Durable new-boot linkage precedes enabling any constructor dependency.
  o.journal.persist(new);o.journal.doc=new;o.journal.recovered=False
  with o.relay.lock:
   if o.relay.active or o.relay.unknown:raise Refused('relay not quiescent')
   o.relay.boots=None;o.relay.bind(p['boots'])
  return {'archive':archive,'boot':o.boot,'public':'CLOSED'}
 if action=='start':
  o.guard_identity()
  from cold_capture import source_inventory
  if source_inventory(o.source,o.member)!=o.repair_source:raise Refused('original source changed')
  os.close(o.guard);o.guard=None;return o.start(initial=False)
 if action=='verify':return o.effect('verify',p['run'])
 if action=='publish':
  if p.get('phase')!='VERIFIED' or set(p.get('verified',{}))!=set(p['boots']):raise Refused('all-owner verification required')
  return o.effect('publish',p['run'])
 raise Refused('unsupported rearm stage')
