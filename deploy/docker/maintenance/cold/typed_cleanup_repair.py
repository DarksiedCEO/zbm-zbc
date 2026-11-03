"""Same-boot typed cleanup reconciliation; one intent, no automatic retry.

Original cleanup/transfer artifacts and operation incident are immutable. Only
remaining exact sealed ciphertext may be removed, after fresh remote evidence.
"""
import os,stat,fcntl,threading,hashlib,re
from pathlib import Path
import b2_adapter as b
from owner_wire import Refused,Client
from distributed_transfer import Transfer,require_proof
from backend_quiescence import require_quiescent

def raw(path,limit=32768):
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 try:
  s=os.fstat(fd)
  if not stat.S_ISREG(s.st_mode) or s.st_size>limit:raise Refused('cleanup retained file bound')
  value=os.read(fd,limit+1)
  if len(value)>limit:raise Refused('cleanup retained file bound')
  return value
 finally:os.close(fd)
def h(value):return hashlib.sha256(value).hexdigest()
def present(p):return p.exists() or p.is_symlink()
def result(o,proof):
 return {'schema':'b2-cleanup-v1','deleted':True,'run':o.sealed['set_id'],'member':o.member,'catalog_sha256':proof['catalog_sha256'],'catalog_snapshot':proof['catalog_snapshot'],'locator_sha256':b.digest(proof['locator'])}
def artifacts(o,proof):
 root=o.backup;expected={'phase':'PENDING','run':o.sealed['set_id'],'member':o.member,'locator_sha256':b.digest(proof['locator'])}
 for name in ('cleanup-intent.json.tmp','cleanup-result.json.tmp','cleanup-failure.json.tmp','transfer.tmp','stage'):
  if present(root/name):raise Refused('interrupted or plaintext cleanup evidence')
 intent=raw(root/'cleanup-intent.json')
 if b.decode(intent)!=expected:raise Refused('original cleanup intent mismatch')
 values={'cleanup-intent.json':h(intent),'transfer.json':h(raw(root/'transfer.json',8192))}
 if present(root/'cleanup-failure.json'):
  v=raw(root/'cleanup-failure.json')
  if b.decode(v)!={'phase':'UNKNOWN','run':o.sealed['set_id'],'member':o.member}:raise Refused('cleanup failure identity')
  values['cleanup-failure.json']=h(v)
 if present(root/'cleanup-result.json'):
  v=raw(root/'cleanup-result.json')
  if b.decode(v)!={'phase':'DONE','receipt':result(o,proof)}:raise Refused('original cleanup result mismatch')
  values['cleanup-result.json']=h(v)
 return values

def subset(o):
 source=o.backup/'sealed';expected=o.sealed['repository_files']
 if not present(source):return {'files':{},'dirs':[],'present':False}
 if source.is_symlink() or not source.is_dir():raise Refused('sealed directory type')
 dirs=set();files={};count=0;total=0
 allowed={'data','index','keys','locks','snapshots'}
 for name in expected:
  allowed.update(str(p) for p in Path(name).parents if str(p)!='.')
 for base,ds,fs in os.walk(source,followlinks=False):
  for name in ds+fs:
   p=Path(base)/name;n=p.relative_to(source).as_posix();s=p.lstat();count+=1
   if count>4096:raise Refused('sealed subset entry bound')
   if stat.S_ISDIR(s.st_mode):
    if n not in allowed and re.fullmatch('data/[0-9a-f]{2}',n)is None:raise Refused('unknown sealed directory')
    dirs.add(n)
   elif stat.S_ISREG(s.st_mode):
    if n not in expected or s.st_size!=expected[n]['bytes']:raise Refused('unknown or changed sealed file')
    total+=s.st_size
    # Stream exact bounded file content, never load a pack into memory.
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);digest=hashlib.sha256();size=0
    try:
     if not stat.S_ISREG(os.fstat(fd).st_mode):raise Refused('sealed file type')
     while True:
      block=os.read(fd,65536)
      if not block:break
      size+=len(block)
      if size>expected[n]['bytes']:raise Refused('sealed file grew')
      digest.update(block)
    finally:os.close(fd)
    if size!=expected[n]['bytes'] or digest.hexdigest()!=expected[n]['sha256']:raise Refused('sealed hash changed')
    files[n]=expected[n]
   else:raise Refused('sealed symlink/special file')
 # The trusted Adapter constructor validates the full original repository cap.
 return {'files':files,'dirs':sorted(dirs),'present':True}

def finish(o,proof,upload,evidence):
 t=Transfer(o.backup,o.password,o.sealed,o.transport_credentials)
 require_proof(proof,o.sealed,upload);require_quiescent()
 if artifacts(o,proof)!=evidence['artifacts'] or subset(o)!=evidence['remaining']:raise Refused('cleanup evidence changed')
 # Executes remote catalog check/dump and exact member check/dumps NOW, never a
 # cached DONE. Missing/changed remote data aborts before any local unlink.
 t.fresh(upload,proof);require_quiescent()
 if artifacts(o,proof)!=evidence['artifacts'] or subset(o)!=evidence['remaining']:raise Refused('cleanup evidence changed after remote read')
 tree=evidence['remaining'];source=o.backup/'sealed'
 if tree['present']:
  for name in sorted(tree['files']):(source/name).unlink()
  for name in sorted(tree['dirs'],key=lambda n:(n.count('/'),n),reverse=True):(source/name).rmdir()
  source.rmdir();fd=os.open(o.backup,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
 if artifacts(o,proof)!=evidence['artifacts']:raise Refused('original cleanup artifacts changed')
 return result(o,proof)

def validate_resolution(o,record):
 p=record['repair'];e=p['evidence'];r=p['result'];op=record['identity']['operation']
 if p['boot']!=o.boot or e.get('schema')!='typed-cleanup-repair-v1' or e.get('seal_sha256')!=b.digest(o.sealed) or record['incident']!=o.journal.doc['operations'].get(op):raise Refused('cleanup resolution original binding')
 proof=e['proof'];require_proof(proof,o.sealed,proof['catalog']['members'][o.member])
 if artifacts(o,proof)!=e['artifacts'] or present(o.backup/'sealed'):raise Refused('cleanup resolution retained evidence')
 expected={'member':o.member,'boot':o.boot,'run':o.journal.doc['run'],'action':'cleanup','result':result(o,proof)}
 if r!=expected:raise Refused('cleanup resolution result binding')

def submit(manager,body):
 o=manager.owner;op=body['incident'];incident=o.journal.doc['operations'].get(op)
 from repair_state import digest
 if incident is None or incident.get('action')!='cleanup' or incident.get('phase')not in ('PENDING','FAILED') or digest(incident)!=body['incident_sha256']:raise Refused('typed cleanup incident binding')
 if o.journal.recovered or o.fenced or o.phase!='OPEN' or o.child is None or o.child.poll()is not None:raise Refused('typed cleanup requires original live boot')
 rec=manager.record(op,'finish-cleanup');old=rec.read()
 if old['repair']is not None:
  if old['repair']['phase']=='RESOLVED':
   validate_resolution(o,old);return manager.wire(old)
  from cleanup_terminal_recovery import submit as reconcile
  return reconcile(manager,body,rec,old)
 if o.worker and o.worker.is_alive() or manager.worker and manager.worker.is_alive():raise Refused('owner effect active')
 require_quiescent();fds=[]
 try:
  for name in ('transfer.lock','cleanup.lock'):
   fd=os.open(o.backup/name,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK);fds.append(fd);st=os.fstat(fd)
   if not stat.S_ISREG(st.st_mode) or st.st_size!=0:raise Refused('cleanup lock type')
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  proof=Client('coordinator','coordinator.railway.internal',27002,o.token).call('/status',{'kind':'complete_catalog','run':body['run']})
  upload=o.upload_result();require_proof(proof,o.sealed,upload)
  # Validate full source profile even when no local ciphertext remains.
  Transfer(o.backup,o.password,o.sealed,o.transport_credentials)
  evidence={'schema':'typed-cleanup-repair-v1','boot':o.boot,'run':body['run'],'seal_sha256':b.digest(o.sealed),'proof':proof,'artifacts':artifacts(o,proof),'remaining':subset(o)}
  prepared=rec.prepare('finish-cleanup',evidence,o.boot);rec.begin(prepared['repair_id'])
 except BaseException:
  for fd in fds:os.close(fd)
  raise
 def execute():
  try:
   value=finish(o,proof,upload,evidence)
   with o.mutex:rec.finish(prepared['repair_id'],{'member':o.member,'boot':o.boot,'run':body['run'],'action':'cleanup','result':value})
  except BaseException:
   with o.mutex:
    try:rec.fail(prepared['repair_id'],'TYPED_CLEANUP_UNRESOLVED')
    except BaseException:pass
  finally:
   for fd in fds:os.close(fd)
 manager.worker=threading.Thread(target=execute,name='typed-cleanup-repair',daemon=False)
 try:manager.worker.start()
 except BaseException:
  for fd in fds:os.close(fd)
  try:rec.fail(prepared['repair_id'],'WORKER_START_UNRESOLVED')
  except BaseException:pass
  raise
 return manager.wire(rec.read())
