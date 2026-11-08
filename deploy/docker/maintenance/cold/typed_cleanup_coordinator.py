"""Same-boot full-set cleanup repair; all remote evidence freshly read first."""
import copy,os
from owner_wire import Refused,MEMBERS,canonical,decode
from distributed_transfer import require_proof,preflight_catalog
from repair_state import digest
from typed_cleanup_repair import raw

def preflight(c):
 if c.doc.get('phase')!='FAILED' or c.doc.get('alert',{}).get('stage')!='cleanup' or c.doc.get('repair_intent')is not None:raise Refused('typed cleanup-stage only')
 proof=c.doc.get('catalog_proof');seals=c.doc['seals'];run=c.doc['run']
 preflight_catalog(seals,run)
 if c.catalog.read(run)!=proof:raise Refused('retained typed catalog mismatch')
 for m in MEMBERS:
  require_proof(proof,seals[m],proof['catalog']['members'][m]);v=c.status(m,run+':'+m+':upload:1')
  if v.get('boot')!=c.doc['boots'][m] or v.get('phase')!='OPEN' or v.get('run')!=run or type(v.get('child_pid'))is not int:raise Refused('same live cleanup cohort required')
  op=run+':'+m+':upload:1';rec=v['operations'].get(op)
  if rec is None or rec.get('phase')!='DONE' or rec.get('receipt')!={'member':m,'boot':c.doc['boots'][m],'run':run,'action':'upload','result':proof['catalog']['members'][m]}:raise Refused('original typed upload DONE required')
 return proof

def preserve(c,request):
 preflight(c)
 data=canonical(c.doc)
 if digest(c.doc)!=request['incident_sha256'] or len(data)>262144:raise Refused('cleanup original incident binding')
 path=c.root/'typed-cleanup-original.json';tmp=path.with_suffix('.tmp')
 if path.exists() or tmp.exists():raise Refused('existing cleanup repair custody')
 fs=os.statvfs(c.root)
 if fs.f_bavail*fs.f_frsize<524288 or fs.f_favail<2:raise Refused('cleanup custody capacity')
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 try:
  offset=0
  while offset<len(data):
   n=os.write(fd,data[offset:])
   if type(n)is not int or not 0<n<=len(data)-offset:raise Refused('custody write progress')
   offset+=n
  os.fsync(fd)
 finally:os.close(fd)
 os.replace(tmp,path);fd=os.open(c.root,os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)

def resume_preflight(c):
 from types import SimpleNamespace
 from repaired_owner import immutable
 old=decode(raw(c.root/'typed-cleanup-original.json',262144));rpc=c.doc.get('repair_rpc',{});intent=c.doc.get('repair_intent',{})
 expected={'command':'finish-cleanup','run':old['run'],'incident_sha256':digest(old)}
 if rpc.get('request')!=expected or rpc.get('phase')not in ('APPLYING','UNRESOLVED') or intent.get('phase')not in ('APPLYING','UNRESOLVED','RESOLVED'):raise Refused('same cleanup intent required')
 if intent!={'schema':'typed-cleanup-repair-v1','phase':intent['phase'],'original_sha256':digest(old),'run':old['run']}:raise Refused('cleanup intent binding')
 if any(c.doc.get(k)!=old.get(k) for k in ('run','boots','seals','catalog_proof')):raise Refused('cleanup retained context changed')
 proof=preflight(SimpleNamespace(doc=old,catalog=c.catalog,status=c.status))
 for m in MEMBERS:
  op=old['run']+':'+m+':cleanup:1';rec=c.status(m,op)['operations'].get(op)
  if rec is None or rec.get('phase')not in ('DONE','FAILED','PENDING'):raise Refused('no new cleanup effects during reconciliation')
 return proof,old

def finish_cleanup(c):
 from repair_cli import owner_resolution
 resuming=c.doc.get('repair_intent')is not None
 if resuming:
  proof,old=resume_preflight(c)
  from repaired_owner import immutable
  archive=c.root/'cleanup-terminal-coordinator.json'
  if not archive.exists():immutable(archive,c.doc)
  elif decode(raw(archive,262144)).get('repair_rpc',{}).get('request')!=c.doc['repair_rpc']['request']:raise Refused('coordinator terminal archive mismatch')
 else:proof=preflight(c);old=decode(raw(c.root/'typed-cleanup-original.json',262144))
 if digest(old)!=c.doc['repair_rpc']['request']['incident_sha256'] or old['run']!=c.doc['run']:raise Refused('cleanup original custody mismatch')
 c.update(repair_intent={'schema':'typed-cleanup-repair-v1','phase':'APPLYING','original_sha256':digest(old),'run':c.doc['run']})
 try:
  # Explicit remote Restic check/dump of catalog plus ALL five member snapshots.
  # This does not reuse prior verify_remote operation IDs or cached DONE records.
  if c.catalog.fresh(c.doc['run'])!=proof:raise Refused('fresh full catalog mismatch')
  c.update(phase='REMOTE_VERIFIED');c.catalog.publish(proof);cleaned={}
  for m in MEMBERS:
   op=c.doc['run']+':'+m+':cleanup:1';v=c.status(m,op)
   if v['boot']!=c.doc['boots'][m] or v['phase']!='OPEN' or v['run']!=c.doc['run']:raise Refused('cleanup cohort changed')
   rec=v['operations'].get(op)
   expected={'schema':'b2-cleanup-v1','deleted':True,'run':c.doc['run'],'member':m,'catalog_sha256':proof['catalog_sha256'],'catalog_snapshot':proof['catalog_snapshot'],'locator_sha256':digest(proof['locator'])}
   if rec is None:
    if resuming:raise Refused('missing cleanup cannot replay')
    got=c.command(m,'cleanup')
   elif rec.get('phase')=='DONE':
    receipt=rec.get('receipt');context={'member':m,'boot':c.doc['boots'][m],'run':c.doc['run'],'action':'cleanup','result':expected}
    if receipt!=context:raise Refused('cleanup DONE mismatch')
    got=receipt['result']
   elif rec.get('phase')in ('FAILED','PENDING'):
    resolved=owner_resolution(c,m,op,'finish-cleanup');got=resolved['result']
    context={'member':m,'boot':c.doc['boots'][m],'run':c.doc['run'],'action':'cleanup','result':expected}
    if resolved!=context:raise Refused('cleanup resolution context')
    values=copy.deepcopy(c.doc.get('repair_resolutions',{}));values[op]=resolved;c.update(repair_resolutions=values)
   else:raise Refused('cleanup original state')
   if got!=expected:raise Refused('typed cleanup result mismatch')
   cleaned[m]=got;c.update(phase='CLEANING',cleaned=cleaned)
  intent=copy.deepcopy(c.doc['repair_intent']);intent['phase']='RESOLVED';c.update(phase='COMPLETE',repair_intent=intent,cleaned=cleaned)
  return {'schema':'typed-cleanup-repair-v1','phase':'COMPLETE','run':c.doc['run'],'catalog_sha256':proof['catalog_sha256'],'original_incident_sha256':digest(old)}
 except BaseException:
  intent=copy.deepcopy(c.doc['repair_intent']);intent['phase']='UNRESOLVED';c.update(phase='FAILED',repair_intent=intent);raise
