"""One bounded same-boot repaired disposition; no archive eviction or data deletion."""
import copy,hashlib,threading,time,re
from pathlib import Path
from owner_wire import Refused,MEMBERS,canonical,decode
from metadata_retirement import read,save
from repaired_owner import immutable
from distributed_transfer import require_proof,preflight_catalog
from coordinator_repair import exclusive

def digest(v):return hashlib.sha256(canonical(v)).hexdigest()
def archive_root(c):
 from repair_slots import coordinator_slot
 return coordinator_slot(c)
def paths(c):
 root=archive_root(c);return root/'repaired-original.json',root/'repaired-retirement.json'
def state(c):return decode(read(paths(c)[1]))
def original(c):return decode(read(paths(c)[0]))
def save_state(c,s):save(paths(c)[1],s)
def summary(old):return {k:old['catalog_proof'][k] for k in ('catalog_sha256','catalog_snapshot')}|{'run':old['run']}
def catalog_repair_binding(c,old):
 # Revalidate retained originals on every resumed disposition, even after advance.
 rpc=old.get('repair_rpc',{});request=rpc.get('request',{})
 if request.get('command')!='recover-catalog':return
 import catalog_resolution as cr
 p=c.root/'catalog-repair-original.json'
 if p.with_name(p.name+'.tmp').exists() or p.with_name(p.name+'.tmp').is_symlink():raise Refused('interrupted catalog original custody')
 incident=decode(read(p));sha=digest(incident);run=old['run']
 if request!={'command':'recover-catalog','run':run,'incident_sha256':sha} or rpc.get('phase')!='RESOLVED':raise Refused('catalog repair request mismatch')
 if incident.get('run')!=run or incident.get('boots')!=old['boots'] or incident.get('seals')!=old['seals'] or incident.get('phase')!='FAILED':raise Refused('catalog incident identity mismatch')
 if old.get('repair_intent')!={'schema':'catalog-repair-v1','phase':'RESOLVED','original_sha256':sha,'run':run}:raise Refused('catalog repair intent mismatch')
 proof=old['catalog_proof']
 if rpc.get('result')!={'schema':'catalog-repair-v1','phase':'COMPLETE','run':run,'catalog_sha256':proof['catalog_sha256'],'original_incident_sha256':sha}:raise Refused('catalog repair terminal mismatch')
 resolution=cr.validate(c.catalog,run)
 if resolution['incident_sha256']!=sha or resolution['proof']!=proof:raise Refused('catalog repair resolution mismatch')

def preflight(c):
 old=copy.deepcopy(c.doc);rpc=old.get('repair_rpc',{});intent=old.get('repair_intent',{})
 if old.get('phase')!='COMPLETE' or rpc.get('phase')!='RESOLVED' or rpc.get('request',{}).get('command') not in ('retry-upload','finish-cleanup','recover-catalog') or intent.get('phase')!='RESOLVED' or old.get('original_recovery') is not None:raise Refused('only resolved typed repair eligible')
 catalog_repair_binding(c,old)
 run=old['run'];schedule=old.get('schedule')
 if type(schedule)is not dict or schedule!={'run':run,'due':schedule.get('due'),'trigger_id':'scheduled-'+str(schedule.get('due'))} or type(schedule.get('due'))is not int:raise Refused('original scheduled identity required')
 if decode(read(c.path))!=old:raise Refused('coordinator disk mismatch')
 proof=old['catalog_proof'];preflight_catalog(old['seals'],run)
 if set(old.get('cleaned',{}))!=set(MEMBERS) or c.catalog.read(run)!=proof:raise Refused('complete repaired cleanup required')
 for m in MEMBERS:
  require_proof(proof,old['seals'][m],proof['catalog']['members'][m]);v=c.status(m)
  if v['boot']!=old['boots'][m] or v['phase']!='OPEN' or v['run']!=run or type(v.get('child_pid'))is not int:raise Refused('same live repaired owners required')
  expected=dict(summary(old),schema='b2-cleanup-v1',member=m,deleted=True,locator_sha256=digest(proof['locator']))
  if old['cleaned'][m]!=expected:raise Refused('cleaned evidence mismatch')
 return old

def permit(c,body):
 s=state(c);old=original(c)
 if set(body)!={'kind','run','member','boot'} or body['member']not in MEMBERS or body['run']!=old['run'] or body['boot']!=old['boots'][body['member']] or s['original_sha256']!=digest(old):raise Refused('fixed repaired permit')
 return {'identity':s['identity'],'run':old['run'],'boots':old['boots'],'summary':summary(old),'proof':old['catalog_proof']}

def completion(c,due,run):
 s=state(c);old=original(c)
 if s['phase']not in ('RETIRED','ACKNOWLEDGED','DONE') or old['run']!=run or old['schedule']['due']!=due or s['original_sha256']!=digest(old) or set(s['retired'])!=set(MEMBERS):raise Refused('repaired completion unavailable')
 for m in MEMBERS:
  v=c.status(m)
  if v['boot']!=old['boots'][m] or v['phase']!='OPEN' or v['run']is not None or v.get('last_success')!=summary(old):raise Refused('retired owner unavailable')
 terminal=decode(read(archive_root(c)/'repaired-terminal.json'))
 if digest(terminal)!=s['terminal_sha256']:raise Refused('repaired terminal changed')
 return {'schema':'repaired-complete-v1','due':due,'trigger_id':old['schedule']['trigger_id'],'run':run,**{k:old['catalog_proof'][k] for k in ('catalog_sha256','catalog_snapshot')},'completion_sha256':digest(terminal),'incident_sha256':old['repair_intent']['original_sha256'],'archives_sha256':digest(s['archives'])}

@exclusive
def finish(c):
 op,sp=paths(c)
 for p in (op,sp):
  temp=p.with_name(p.name+'.tmp')
  if temp.exists() or temp.is_symlink() or p.is_symlink():raise Refused('ambiguous repaired publication')
 if not sp.exists():
  old=preflight(c)
  archived=op.exists()
  if archived and read(op)!=canonical(old):raise Refused('interrupted archive differs from current resolved document')
  if c.catalog.fresh(old['run'])!=old['catalog_proof']:raise Refused('fresh repaired catalog mismatch')
  # Two fixed immutable archives plus bounded transition record; refusal, never eviction.
  if not archived:immutable(op,old)
  s={'schema':'repaired-retirement-v1','identity':digest({'original':digest(old),'boots':old['boots']}),'original_sha256':digest(old),'phase':'PREPARED','archives':{},'retired':{},'terminal_sha256':None,'observer':None,'new':None}
  save_state(c,s)
 else:s=state(c);old=original(c)
 if s['original_sha256']!=digest(old):raise Refused('repaired archive binding')
 catalog_repair_binding(c,old)
 if s['phase']=='DONE':
  if c.doc!=s['new'] or decode(read(c.path))!=s['new']:raise Refused('completed repaired lineage changed')
  return s
 for m in MEMBERS:
  v=c.status(m)
  if v['boot']!=old['boots'][m] or v['phase']!='OPEN' or type(v.get('child_pid'))is not int or v['run']not in (old['run'],None):raise Refused('sameboot disposition required')
 if c.catalog.fresh(old['run'])!=old['catalog_proof']:raise Refused('fresh catalog changed')
 if s['phase']=='PREPARED':
  for m in MEMBERS:
   body={'member':m,'boot':old['boots'][m],'run':old['run'],'action':'repaired-archive','identity':s['identity']}
   a=c.clients[m].call('/submit',body)
   if set(a)!={'member','boot','run','identity','archive_sha256','ready'} or any(a[k]!=body[k] for k in ('member','boot','run','identity')) or a['ready']is not False or type(a['archive_sha256'])is not str or re.fullmatch('[0-9a-f]{64}',a['archive_sha256'])is None:raise Refused('owner archive identity')
   if m in s['archives'] and s['archives'][m]!=a:raise Refused('owner archive changed')
   s['archives'][m]=a;save_state(c,s)
  s['phase']='ARCHIVED';save_state(c,s)
 if s['phase']=='ARCHIVED':
  for m in MEMBERS:
   opid=old['run']+':'+m+':retire:1';v=c.status(m,opid);rec=v['operations'].get(opid)
   expected={'member':m,'boot':old['boots'][m],'run':old['run'],'action':'retire','result':summary(old)}
   if rec is None:
    if opid in c.doc['receipts']:raise Refused('uncertain retire submission remains fenced')
    got=c.command(m,'retire')
    if got!=summary(old):raise Refused('retire result mismatch')
    rec=c.status(m,opid)['operations'].get(opid)
   if rec is None or rec.get('phase')!='DONE' or rec.get('receipt')!=expected:raise Refused('retire outcome not exact DONE')
   s['retired'][m]=expected;save_state(c,s)
  immutable(archive_root(c)/'repaired-terminal.json',{'original_sha256':s['original_sha256'],'archives':s['archives'],'retired':s['retired'],'coordinator':copy.deepcopy(c.doc)})
  s['terminal_sha256']=digest(decode(read(archive_root(c)/'repaired-terminal.json')));s['phase']='RETIRED';save_state(c,s)
 if s['phase']=='RETIRED':
  p=completion(c,old['schedule']['due'],old['run'])
  observed=c.observer.call('/submit',{'action':'repaired-complete','due':p['due'],'run':p['run']})
  expected={'due':p['due'],'trigger_id':p['trigger_id'],'run':p['run'],'phase':'REPAIRED_COMPLETE','completion':p}
  if observed!=expected:raise Refused('repaired observer acknowledgment')
  s['observer']=observed;s['phase']='ACKNOWLEDGED';save_state(c,s)
 if s['phase']=='ACKNOWLEDGED':
  current=c.observer.call('/status',{'kind':'monitor_status'})
  if current.get('current')!=s['observer']:raise Refused('fresh repaired observer required')
  for m in MEMBERS:
   body={'member':m,'boot':old['boots'][m],'run':old['run'],'action':'repaired-ready','identity':s['identity']}
   value=c.clients[m].call('/submit',body)
   if value!=dict(s['archives'][m],ready=True):raise Refused('owner repaired-ready mismatch')
  # Retain all incident metadata. Move the fixed notification file directory,
  # rather than reuse its closed FAILED sequence for the next scheduled run.
  source=c.root/'notifications';dest=archive_root(c)/'repaired-notifications'
  if source.exists():
   if dest.exists() or source.is_symlink():raise Refused('notification archive collision')
   source.rename(dest)
   from metadata_retirement import sync
   sync(c.root)
  new={'schema':1,'run':'cold-'+digest(s['observer'])[:32],'phase':'OPEN','boots':old['boots'],'receipts':{},'barrier':None,'seals':{},'last_success':summary(old)}
  if new['run']==old['run']:raise Refused('repaired run collision')
  if s['new']is not None and s['new']!=new:raise Refused('next repaired identity mismatch')
  s['new']=new;save_state(c,s)
  if c.doc.get('run')not in (old['run'],new['run']):raise Refused('current repaired lineage')
  save(c.path,new);c.doc=copy.deepcopy(new);c.counts={};c.catalog.proof=None;c.completion.published=None
  s['phase']='DONE';save_state(c,s)
 return s

def control(c,path,body):
 if body!={'command':'finish-repaired-run'}:raise Refused('fixed repaired command')
 if path=='/repair-status':return {'active':bool(c.repair_worker and c.repair_worker.is_alive()),'phase':state(c)['phase'] if paths(c)[1].exists() else 'ABSENT','failure':getattr(c,'repaired_failure',None)}
 with c.control_lock:
  from scheduled_trigger import busy_other
  if c.normal_active or busy_other(c) or c.repair_worker and c.repair_worker.is_alive():raise Refused('coordinator active')
  c.repaired_failure=None
  def work():
   try:finish(c)
   except BaseException:c.repaired_failure='REPAIRED_DISPOSITION_REFUSED'
  c.repair_worker=threading.Thread(target=work,daemon=False);c.repair_worker.start()
 return {'accepted':True}
def operator_finish(client):
 try:client.call('/repair',{'command':'finish-repaired-run'})
 except Refused:pass
 end=time.monotonic()+600
 while time.monotonic()<end:
  s=client.call('/repair-status',{'command':'finish-repaired-run'})
  if not s['active']:
   if s['phase']=='DONE' and s['failure']is None:return s
   raise Refused('repaired disposition unresolved; retain evidence')
  time.sleep(.2)
 raise Refused('repaired disposition deadline')
