"""Fixed same-boot archive-before-retire; retained backup metadata is not deleted."""
import copy,hashlib
from pathlib import Path
from owner_wire import Refused,Client,decode,canonical
from metadata_retirement import read,save,CAP
from distributed_transfer import require_proof
from backend_quiescence import require_quiescent

def load(path):return decode(read(path))
def immutable(path,value):
 if path.exists() or path.is_symlink():
  if load(path)!=value:raise Refused('repair archive occupied')
 else:save(path,value)
def dispatch(o,body):
 if set(body)!={'member','boot','run','action','identity'} or body['action'] not in ('repaired-archive','repaired-ready') or body['boot']!=o.boot:raise Refused('repaired owner context')
 if o.fenced or o.journal.recovered or o.phase!='OPEN' or o.child is None or o.child.poll()is not None or o.worker and o.worker.is_alive() or o.repairs.worker and o.repairs.worker.is_alive():raise Refused('repaired live owner required')
 require_quiescent()
 permission=Client('coordinator','coordinator.railway.internal',27002,o.token).call('/status',{'kind':'repaired-permit','run':body['run'],'member':o.member,'boot':o.boot})
 if permission['identity']!=body['identity']:raise Refused('repaired intent mismatch')
 summary=permission['summary']
 from repair_slots import slot
 archive_root=slot(o.root,body['run'],create=True);path=archive_root/'repaired-archive.json'
 if path.exists():
  archive=load(path)
  if archive['run']!=body['run'] or archive['boot']!=o.boot or archive['identity']!=body['identity']:raise Refused('owner archive occupied')
 else:
  if body['action']!='repaired-archive' or o.journal.doc['run']!=body['run']:raise Refused('original archive required')
  proof=permission['proof'];upload=proof['catalog']['members'][o.member];require_proof(proof,o.sealed,upload)
  if (o.backup/'sealed').exists() or (o.backup/'sealed').is_symlink() or (o.backup/'stage').exists() or (o.backup/'stage').is_symlink():raise Refused('retained capture cannot retire')
  resolved=o.repairs.resolved();ops=o.journal.doc['operations'];records={}
  for key,r in o.repairs.records.items():
   r.check();v=r.read()
   if v['identity']['run']==body['run']:
    if v['repair'] is None or v['repair']['phase']!='RESOLVED' or v['repair']['boot']!=o.boot:raise Refused('unresolved owner repair')
    records[key]=v
  if any(v['phase']!='DONE' and k not in resolved for k,v in ops.items()):raise Refused('owner unresolved operation')
  cleanup=[v['receipt'] for v in ops.values() if v['action']=='cleanup' and v['phase']=='DONE']+[v for v in resolved.values() if v['action']=='cleanup']
  expected=dict(summary,schema='b2-cleanup-v1',member=o.member,deleted=True,locator_sha256=hashlib.sha256(canonical(proof['locator'])).hexdigest())
  if len(cleanup)!=1 or cleanup[0]!=dict(member=o.member,boot=o.boot,run=body['run'],action='cleanup',result=expected):raise Refused('repaired cleanup not exact')
  archive={'schema':'repaired-owner-v1','identity':body['identity'],'run':body['run'],'member':o.member,'boot':o.boot,'summary':summary,'journal':copy.deepcopy(o.journal.doc),'repairs':records,'resolved':resolved,'backup_failure':getattr(o,'backup_failure',None)}
  if len(canonical(archive))>CAP:raise Refused('owner repair archive capacity')
  immutable(path,archive)
 digest=hashlib.sha256(canonical(archive)).hexdigest()
 if body['action']=='repaired-ready':
  j=o.journal.doc
  if j.get('run')is not None or j.get('last_success')!=summary:raise Refused('owner not retired')
  terminal=archive_root/'repaired-owner-terminal.json'
  expected={k:hashlib.sha256(canonical(v)).hexdigest() for k,v in archive['resolved'].items()}
  if terminal.exists():
   saved=load(terminal);clean=copy.deepcopy(saved);clean.pop('retired_resolution_sha256',None)
   if j not in (saved,clean) or saved.get('retired_resolution_sha256',{})!=expected:raise Refused('retired terminal changed')
  else:
   if j.get('retired_resolution_sha256',{})!=expected:raise Refused('retired overlay mismatch')
   immutable(terminal,copy.deepcopy(j));clean=copy.deepcopy(j);clean.pop('retired_resolution_sha256',None)
  if j!=clean:o.journal.persist(clean);o.journal.doc=clean
  o.backup_failure=None # immutable full failure remains in archive; new run may proceed
 return {'member':o.member,'boot':o.boot,'run':body['run'],'identity':body['identity'],'archive_sha256':digest,'ready':body['action']=='repaired-ready'}
