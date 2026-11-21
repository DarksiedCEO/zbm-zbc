"""One same-boot upload repair followed by normal typed full-set verification."""
import copy,os,stat
from pathlib import Path
from owner_wire import Refused,MEMBERS,canonical,decode
from distributed_transfer import preflight_catalog,require_member
from repair_state import digest

def preflight(c):
 if c.doc.get('phase')!='FAILED' or c.doc.get('alert',{}).get('stage')!='upload' or c.doc.get('repair_intent') is not None:raise Refused('typed retry upload-stage only')
 if c.doc.get('catalog_proof') is not None or c.doc.get('cleaned'):raise Refused('catalog or cleanup ambiguity unsupported')
 root,context=c.catalog.paths(c.doc['run'])
 if any(p.exists() or p.is_symlink() for p in (root,context,context.with_name(context.name+'.tmp'))):raise Refused('catalog intent ambiguity unsupported')
 preflight_catalog(c.doc['seals'],c.doc['run'])
 for m in MEMBERS:
  v=c.status(m)
  if v['phase']!='OPEN' or v['run']!=c.doc['run'] or type(v.get('child_pid'))is not int:raise Refused('same live cohort required')
  if any(rec['action'] in ('cleanup','retire') for rec in v['operations'].values()):raise Refused('cleanup intent ambiguity unsupported')

def preserve(c,request):
 preflight(c)
 original=canonical(c.doc)
 if digest(c.doc)!=request['incident_sha256'] or len(original)>262144:raise Refused('original coordinator binding')
 path=c.root/'typed-repair-original.json';tmp=path.with_suffix('.tmp')
 if path.exists() or tmp.exists():raise Refused('existing typed repair custody unresolved')
 fs=os.statvfs(c.root)
 if fs.f_bavail*fs.f_frsize<524288 or fs.f_favail<2:raise Refused('repair custody capacity')
 fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as f:f.write(original);f.flush();os.fsync(f.fileno())
 os.replace(tmp,path);fd=os.open(c.root,os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)

def retry_upload(c):
 from repair_cli import owner_resolution
 preflight(c)
 path=c.root/'typed-repair-original.json'
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 try:
  if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_size>262144:raise Refused('original custody bound')
  old=decode(os.read(fd,262145))
 finally:os.close(fd)
 request=c.doc['repair_rpc']['request']
 if digest(old)!=request['incident_sha256'] or old['run']!=c.doc['run']:raise Refused('original custody mismatch')
 c.update(repair_intent={'schema':'typed-upload-repair-v1','phase':'APPLYING','original_sha256':digest(old),'run':c.doc['run']})
 uploads={}
 try:
  for m in MEMBERS:
   op=c.doc['run']+':'+m+':upload:1';v=c.status(m,op);record=v['operations'].get(op)
   if record is None:uploads[m]=c.command(m,'upload')
   elif record['phase']=='DONE':
    receipt=record.get('receipt',{})
    if any(receipt.get(k)!=val for k,val in {'member':m,'boot':c.doc['boots'][m],'run':c.doc['run'],'action':'upload'}.items()):raise Refused('original upload DONE context')
    uploads[m]=receipt['result']
   else:
    result=owner_resolution(c,m,op,'retry-upload');uploads[m]=result['result']
    rs=copy.deepcopy(c.doc.get('repair_resolutions',{}));rs[op]=result;c.update(repair_resolutions=rs)
   require_member(uploads[m],c.doc['run'],m)
   if uploads[m]['seal_sha256']!=digest(c.doc['seals'][m]):raise Refused('typed repaired seal binding')
  proof=c.catalog.create(c.doc['run'],uploads,c.doc['seals'])
  for m in MEMBERS:
   if c.command(m,'verify_remote')!=uploads[m]:raise Refused('typed repaired remote mismatch')
  c.update(phase='REMOTE_VERIFIED',catalog_proof=proof);c.catalog.publish(proof)
  cleaned={}
  for m in MEMBERS:
   cleaned[m]=c.command(m,'cleanup');c.update(phase='CLEANING',cleaned=cleaned)
  intent=copy.deepcopy(c.doc['repair_intent']);intent['phase']='RESOLVED'
  c.update(phase='COMPLETE',cleaned=cleaned,repair_intent=intent)
  return {'phase':'COMPLETE','run':c.doc['run'],'catalog_sha256':proof['catalog_sha256'],'catalog_snapshot':proof['catalog_snapshot'],'original_incident_sha256':digest(old),'schema':'typed-upload-repair-v1'}
 except BaseException:
  intent=copy.deepcopy(c.doc['repair_intent']);intent['phase']='UNRESOLVED'
  c.update(phase='FAILED',repair_intent=intent);raise
