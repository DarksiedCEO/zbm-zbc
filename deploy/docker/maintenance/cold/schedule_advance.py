"""Run-level successful rollover. A single finish-advance resumes exact intent.

No restart adoption: owners must retain their authenticated original boot. The
observer is freshly queried on every attempt; a saved COMPLETE is not enough.
"""
import copy
from pathlib import Path
from owner_wire import Refused,MEMBERS,canonical,decode
from observer_link import Link
from metadata_retirement import read,save,sha,inventory,remove_exact,sync,CAP

def fresh(c,a):
 proof=a['completion'];summary=a['old']['last_success']
 status=c.observer.call('/status',{'kind':'monitor_status'})
 expected={'due':proof['due'],'trigger_id':proof['trigger_id'],'run':proof['run'],'phase':'COMPLETE','completion':proof}
 if status.get('current')!=expected:raise Refused('fresh observer COMPLETE required')
 for m in MEMBERS:
  v=c.status(m)
  if any(v.get(k)!=x for k,x in {'member':m,'boot':a['old']['boots'][m],'phase':'OPEN','run':None,'fenced':False,'last_success':summary}.items()):raise Refused('five retired owner identities required')
 return expected

def archive(c):
 a=decode(read(c.root/'advance.json'))
 if set(a)!={'schema','phase','old','new','completion','notification','catalog_context','catalog_inventory','identity'} or a['schema']!=1 or a['phase']not in ('PENDING','DONE'):raise Refused('advance schema')
 base={k:v for k,v in a.items() if k not in ('phase','identity')}
 if sha(canonical(base))!=a['identity']:raise Refused('advance identity')
 if a['old'].get('phase')!='RETIRED' or a['new'].get('phase')!='OPEN' or a['old']['boots']!=a['new']['boots'] or a['old']['run']==a['new']['run']:raise Refused('advance context')
 return a

def permit(c,run,member,boot):
 a=archive(c)
 if member not in MEMBERS or run!=a['old']['run'] or boot!=a['old']['boots'][member]:raise Refused('fixed metadata retirement identity')
 # Only current, exact OLD or predeclared NEW state can serve this intent.
 if decode(read(c.path)) not in (a['old'],a['new']):raise Refused('advance current document')
 return {'run':run,'boots':a['old']['boots'],'summary':a['old']['last_success'],'completion':a['completion'],'advance_sha256':a['identity'],'locator_sha256':sha(canonical(a['old']['catalog_proof']['locator']))}

def prepare(c):
 if not hasattr(c,'observer'):raise Refused('trusted observer required')
 old=copy.deepcopy(c.doc)
 if old.get('phase')!='RETIRED' or any(k in old for k in ('repair_rpc','repair_resolutions','original_recovery','alert')):raise Refused('only unrepaired success may advance')
 path=c.root/'advance.json'
 if path.exists():
  previous=archive(c)
  if previous['phase']!='DONE':raise Refused('finish-advance required')
  if previous['new']['run']!=old['run']:raise Refused('previous advance lineage')
 proof=c.completion.read(old['schedule']['due'],old['run'])
 notification=Link(c.root/'notifications',c.observer,proof['due'],proof['run']).read()
 expected={'due':proof['due'],'trigger_id':proof['trigger_id'],'run':proof['run'],'phase':'COMPLETE','completion':proof}
 if set(notification['records'])!={'started','complete'} or any(v.get('phase')!='DONE' for v in notification['records'].values()) or notification['records']['complete']!={'phase':'DONE','proof':proof,'result':expected}:raise Refused('exact DONE observer notification required')
 root,context=c.catalog.paths(old['run']);raw=read(context,32768);context_value=decode(raw)
 # completion.read already validates catalog context; bind its exact bytes again.
 items=inventory(root,'catalog')
 if not {'catalog-intent.json','catalog-state.json','catalog-locator.json'}<=set(items):raise Refused('catalog metadata incomplete')
 new={'schema':1,'run':'cold-'+sha(canonical(proof))[:32],'phase':'OPEN','boots':old['boots'],'receipts':{},'barrier':None,'seals':{},'last_success':old['last_success']}
 if new['run']==old['run']:raise Refused('run collision')
 a={'schema':1,'old':old,'new':new,'completion':proof,'notification':notification,'catalog_context':{'value':context_value,'sha256':sha(raw)},'catalog_inventory':items}
 a.update(identity=sha(canonical(a)),phase='PENDING')
 # Same ceiling as current coordinator record; refuse before mutation, no cap raise.
 if len(canonical(a))+1024>CAP:raise Refused('advance terminal reservation')
 fresh(c,a)
 if read(c.path)!=canonical(old) or c.doc!=old:raise Refused('advance current changed')
 save(path,a)
 return finish(c)

def finish(c):
 if not hasattr(c,'observer'):raise Refused('trusted observer required')
 if not (c.root/'advance.json').exists():return prepare(c)
 a=archive(c)
 if a['phase']=='DONE' and c.doc.get('phase')=='RETIRED' and c.doc['run']==a['new']['run']:return prepare(c)
 fresh(c,a)
 current=decode(read(c.path))
 if current not in (a['old'],a['new']) or c.doc!=current:raise Refused('advance exact old/new required')
 # A failed save may leave an exact prefix of the already predetermined NEW.
 if c.path.with_suffix('.tmp').exists():raise Refused('legacy coordinator temp requires supported loader')
 for m in MEMBERS:
  body={'member':m,'boot':a['old']['boots'][m],'run':a['old']['run'],'action':'metadata-retire','advance_sha256':a['identity']}
  expected={'member':m,'boot':body['boot'],'run':body['run'],'advance_sha256':a['identity'],'metadata_retired':True}
  if c.clients[m].call('/submit',body)!=expected:raise Refused('owner metadata retirement unconfirmed')
 root,context=c.catalog.paths(a['old']['run'])
 # Do not remove publication files until exact five owner acknowledgments.
 if context.is_symlink():raise Refused('catalog context symlink')
 if context.exists() and sha(read(context,32768))!=a['catalog_context']['sha256']:raise Refused('catalog context changed')
 for name,value in [('completion.json',a['completion']),('notifications/notification.json',a['notification'])]:
  p=c.root/name
  if p.is_symlink():raise Refused('publication symlink')
  if p.exists() and decode(read(p))!=value:raise Refused('publication metadata changed')
 remove_exact(root,a['catalog_inventory'])
 if context.exists():context.unlink();sync(context.parent)
 for name in ('completion.json','notifications/notification.json'):
  p=c.root/name
  if p.exists():p.unlink();sync(p.parent)
 # Use fixed atomic writer for resumable partial NEW replacement, not new run ID.
 save(c.path,a['new']);c.doc=copy.deepcopy(a['new']);c.counts={};c.catalog.proof=None;c.completion.published=None
 save(c.root/'advance.json',dict(a,phase='DONE'))
 return c.doc['run']

def require_no_pending(c):
 from repair_slots import inventory
 candidates=[d/'repaired-retirement.json' for d in inventory(c.root) if (d/'repaired-retirement.json').exists()]
 for item in candidates:
  retained=decode(read(item))
  if retained.get('phase')!='DONE' and (retained.get('new') or {}).get('run')==c.doc.get('run'):raise Refused('finish-repaired-run required')
 p=c.root/'repaired-retirement.json' # legacy retained subjects still refuse below
 if p.exists():
  r=decode(read(p))
  if r.get('phase')!='DONE' and (r.get('new') or {}).get('run')==c.doc.get('run'):raise Refused('finish-repaired-run required')
 p=c.root/'advance.json'
 if (c.root/'advance.json.tmp').exists() or (c.root/'run.json.tmp').exists() or (p.exists() and archive(c)['phase']!='DONE'):raise Refused('finish-advance required before new backup')

def control(c,path,body):
 import threading
 if body!={'command':'finish-advance'}:raise Refused('fixed finish-advance command')
 if path=='/repair-status':
  p=c.root/'advance.json';a=archive(c) if p.exists() else None
  return {'phase':a['phase'] if a else 'ABSENT','identity':a['identity'] if a else None,'active':bool(c.repair_worker and c.repair_worker.is_alive()),'failure':getattr(c,'advance_failure',None)}
 with c.control_lock:
  from scheduled_trigger import busy_other
  if c.normal_active or busy_other(c) or c.repair_worker and c.repair_worker.is_alive():raise Refused('coordinator operation active')
  c.advance_failure=None
  def work():
   try:c.finish_advance()
   except BaseException:c.advance_failure='FINISH_ADVANCE_REFUSED'
  c.repair_worker=threading.Thread(target=work,daemon=True);c.repair_worker.start()
 return {'accepted':True}

def operator_finish(client):
 import time
 # One invocation, one fixed action; lost submit is reconciled by status only.
 try:client.call('/repair',{'command':'finish-advance'})
 except Refused:pass
 end=time.monotonic()+60
 while time.monotonic()<end:
  v=client.call('/repair-status',{'command':'finish-advance'})
  if not v['active']:
   if v['phase']=='DONE' and v['failure']is None:return v
   raise Refused('finish-advance unavailable; evidence retained')
  time.sleep(.2)
 raise Refused('finish-advance status deadline')
