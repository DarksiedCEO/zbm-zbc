"""Same-ID absent-ciphertext reconciliation; never deletes or retries effects."""
import copy,os,stat,fcntl,threading
from owner_wire import Refused,Client
from repaired_owner import immutable
from backend_quiescence import require_quiescent
import typed_cleanup_repair as t

def submit(manager,body,rec,old):
 o=manager.owner;p=old['repair'];e=p['evidence']
 if p['phase'] not in ('APPLYING','UNRESOLVED') or t.present(o.backup/'sealed'):return manager.wire(old)
 if o.worker and o.worker.is_alive() or manager.worker and manager.worker.is_alive():raise Refused('cleanup worker active')
 rec.check();require_quiescent()
 if p['boot']!=o.boot or e.get('schema')!='typed-cleanup-repair-v1' or e.get('boot')!=o.boot or e.get('run')!=body['run'] or e.get('seal_sha256')!=t.b.digest(o.sealed):raise Refused('cleanup terminal evidence identity')
 proof=e['proof'];upload=o.upload_result();t.require_proof(proof,o.sealed,upload)
 if t.artifacts(o,proof)!=e['artifacts']:raise Refused('cleanup original artifacts changed')
 fds=[]
 try:
  for name in ('transfer.lock','cleanup.lock'):
   fd=os.open(o.backup/name,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK);fds.append(fd);s=os.fstat(fd)
   if not stat.S_ISREG(s.st_mode) or s.st_size!=0:raise Refused('cleanup lock type')
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  archive=rec.root/'cleanup-terminal-original.json'
  if t.present(archive.with_name(archive.name+'.tmp')) or archive.is_symlink():raise Refused('ambiguous terminal archive')
  if not archive.exists():immutable(archive,old)
  else:
   saved=t.b.decode(t.raw(archive,65536))
   if saved['identity']!=old['identity'] or saved['incident']!=old['incident'] or saved['repair']['repair_id']!=p['repair_id'] or saved['repair']['evidence']!=e:raise Refused('terminal archive mismatch')
  # Retain first uncertainty before publishing the same intent as active.
  v=copy.deepcopy(old);v['repair'].update(phase='APPLYING',failure=None);rec.commit(v)
 except BaseException:
  for fd in fds:os.close(fd)
  raise
 def work():
  try:
   current=Client('coordinator','coordinator.railway.internal',27002,o.token).call('/status',{'kind':'complete_catalog','run':body['run']})
   if current!=proof:raise Refused('cleanup catalog changed')
   t.Transfer(o.backup,o.password,o.sealed,o.transport_credentials).fresh(upload,proof)
   require_quiescent()
   if o.boot!=p['boot'] or o.journal.recovered or o.fenced or o.phase!='OPEN' or o.child is None or o.child.poll()is not None:raise Refused('cleanup live boot lost')
   if t.present(o.backup/'sealed') or t.artifacts(o,proof)!=e['artifacts'] or o.upload_result()!=upload:raise Refused('cleanup terminal evidence changed')
   with o.mutex:rec.finish(p['repair_id'],{'member':o.member,'boot':o.boot,'run':body['run'],'action':'cleanup','result':t.result(o,proof)})
  except BaseException:
   with o.mutex:
    try:rec.fail(p['repair_id'],'CLEANUP_TERMINAL_UNRESOLVED')
    except BaseException:pass
  finally:
   for fd in fds:os.close(fd)
 manager.worker=threading.Thread(target=work,name='cleanup-terminal-reconcile',daemon=False)
 try:manager.worker.start()
 except BaseException:
  for fd in fds:os.close(fd)
  raise
 return manager.wire(rec.read())
