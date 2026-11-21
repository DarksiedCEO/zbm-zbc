"""Same-boot typed upload repair; no init/unlock/delete/application authority."""
import os,stat,fcntl,threading
import b2_adapter as b
from owner_wire import Refused
from backend_quiescence import require_quiescent
from distributed_transfer import require_member

def original_bytes(a):
 fd=os.open(a.root/'transfer.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 try:
  st=os.fstat(fd)
  if not stat.S_ISREG(st.st_mode) or st.st_size>8192:raise b.Refused('TRANSFER_RECORD_BOUND')
  raw=os.read(fd,8193)
  if len(raw)>8192:raise b.Refused('TRANSFER_RECORD_BOUND')
  return raw
 finally:os.close(fd)

def retained(a):
 for name in ('transfer.tmp','cleanup-intent.json','cleanup-intent.json.tmp','cleanup-result.json','cleanup-result.json.tmp','cleanup-failure.json'):
  if (a.root/name).exists() or (a.root/name).is_symlink():raise b.Refused('AMBIGUOUS_TRANSFER_OR_CLEANUP')
 raw=original_bytes(a)
 v=b.decode(raw);common={'schema':'b2-transfer-state-v1','set_id':a.run,'member':a.member,'seal_sha256':b.digest(a.seal)}
 if any(v.get(k)!=x for k,x in common.items()) or v.get('phase') not in ('PENDING','UNCERTAIN','VERIFIED'):raise b.Refused('TRANSFER_CONTEXT')
 fields=set(common)|{'phase'}
 if v['phase']=='UNCERTAIN':
  fields|={'reason','retained','repair_required'}
  if v.get('reason')!='TRANSFER_UNCERTAIN' or v.get('retained')is not True or v.get('repair_required')is not True:raise b.Refused('TRANSFER_UNCERTAINTY_SCHEMA')
 if v['phase']=='VERIFIED':
  fields|={'receipt'};require_member(v.get('receipt'),a.run,a.member)
 if set(v)!=fields:raise b.Refused('TRANSFER_SCHEMA')
 if a.source.is_symlink() or not a.source.is_dir() or b.local_inventory(a.source,a.profile['repository'])!=a.seal['repository_files'] or (a.root/'stage').exists():raise b.Refused('SEAL_CHANGED')
 if 9+2*len(a.seal['manifest']['files'])>40:raise b.Refused('REPAIR_COMMAND_PROFILE')
 b.spool_guard(a.root)
 return raw

def retry(a):
 """Caller holds exclusive owner repair/transfer lock and durable APPLYING."""
 original=retained(a);b.verify_binary();require_quiescent()
 a.budget=b.Budget.start(seconds=120,commands=40,output_bytes=8*b.MIB)
 # Same strict verifier, fixed local sealed repository, no caller-selected path.
 remote=a.remote
 try:a.remote=str(a.source);a.verify(a.seal['snapshot'])
 finally:a.remote=remote
 rows=b.decode(a.command('snapshots','--json')) # success required; failure is NOT absence
 if not isinstance(rows,list) or len(rows)>1:raise b.Refused('REMOTE_SNAPSHOT_SET')
 if not rows and b.decode(original)['phase']=='VERIFIED':raise b.Refused('VERIFIED_REMOTE_MISSING')
 if rows:snapshot=b.destination_snapshot(b.canonical(rows),a.seal['snapshot'])
 else:
  a.command('copy','--from-repo',str(a.source),a.seal['snapshot'])
  snapshot=b.destination_snapshot(a.command('snapshots','--json'),a.seal['snapshot'])
 a.verify(snapshot);require_quiescent()
 if retained(a)!=original:raise b.Refused('ORIGINAL_CHANGED')
 result={'schema':'b2-restic-member-v1','member':a.member,'set_id':a.run,'repository':a.remote,'source_snapshot':a.seal['snapshot'],'snapshot':snapshot,'manifest_sha256':b.digest(a.seal['manifest']),'seal_sha256':b.digest(a.seal),'verification':'full-read-data-and-exact-file-dumps'}
 if b.decode(original)['phase']=='VERIFIED' and b.decode(original)['receipt']!=result:raise b.Refused('ORIGINAL_VERIFIED_MISMATCH')
 return result

def validate_resolution(o,record):
 p=record['repair'];r=p['result'];op=record['identity']['operation']
 if p['boot']!=o.boot or p['evidence'].get('schema')!='typed-upload-repair-v1' or p['evidence'].get('seal_sha256')!=b.digest(o.sealed) or record['incident']!=o.journal.doc['operations'].get(op):raise Refused('typed resolution original context')
 if any(r.get(k)!=v for k,v in {'member':o.member,'boot':o.boot,'run':o.journal.doc['run'],'action':'upload'}.items()):raise Refused('typed resolution result context')
 a=b.Adapter(o.backup,o.sealed,password=o.password,**o.transport_credentials)
 require_member(r.get('result'),a.run,a.member)
 if r['result']['seal_sha256']!=b.digest(a.seal) or r['result']['manifest_sha256']!=b.digest(a.seal['manifest']) or r['result']['source_snapshot']!=a.seal['snapshot']:raise Refused('typed resolution seal binding')
 # Original transfer evidence remains immutable; local seal may legitimately
 # be gone only after separately proved cleanup, so don't rehash it here.
 raw=original_bytes(a)
 if len(raw)>8192 or __import__('hashlib').sha256(raw).hexdigest()!=p['evidence']['original_transfer_sha256']:raise Refused('typed resolution retained evidence')

def submit(manager,body):
 o=manager.owner;op=body['incident'];incident=o.journal.doc['operations'].get(op)
 from repair_state import digest
 if incident is None or incident['action']!='upload' or incident['phase']not in ('PENDING','FAILED','DONE') or digest(incident)!=body['incident_sha256']:raise Refused('typed upload incident binding')
 if o.journal.recovered or o.fenced or o.phase!='OPEN' or o.child is None or o.child.poll()is not None:raise Refused('typed retry requires same live boot')
 a=b.Adapter(o.backup,o.sealed,password=o.password,**o.transport_credentials)
 record=manager.record(op,'retry-upload');prior=record.read()
 if prior['repair'] is not None:
  if prior['repair']['phase']=='RESOLVED':validate_resolution(o,prior)
  return manager.wire(prior) # Never redispatch an existing repair.
 if o.worker and o.worker.is_alive() or manager.worker and manager.worker.is_alive():raise Refused('owner effect active')
 require_quiescent()
 fd=os.open(a.root/'transfer.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
 try:
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);raw=retained(a)
  evidence={'seal_sha256':b.digest(a.seal),'original_transfer_sha256':__import__('hashlib').sha256(raw).hexdigest(),'boot':o.boot,'run':body['run'],'schema':'typed-upload-repair-v1'}
  prepared=record.prepare('retry-upload',evidence,o.boot);record.begin(prepared['repair_id'])
 except BaseException:os.close(fd);raise
 def execute():
  try:
   result=retry(a)
   with o.mutex:record.finish(prepared['repair_id'],{'member':o.member,'boot':o.boot,'run':body['run'],'action':'upload','result':result})
  except BaseException:
   with o.mutex:
    try:record.fail(prepared['repair_id'],'TYPED_UPLOAD_UNRESOLVED')
    except BaseException:pass
  finally:os.close(fd)
 manager.worker=threading.Thread(target=execute,name='typed-upload-repair',daemon=False)
 try:manager.worker.start()
 except BaseException:
  os.close(fd)
  try:record.fail(prepared['repair_id'],'WORKER_START_UNRESOLVED')
  except BaseException:pass
  raise
 return manager.wire(record.read())
