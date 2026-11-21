"""Conventional original-store recovery. No old PID receipt becomes new authority.

Caller has a durable typed repair intent and fixed owner worker exclusion. This
is not backup completion or a restore from remote data.
"""
import fcntl,hashlib,os,shutil
from pathlib import Path
from owner_wire import Refused,canonical
from cold_capture import source_inventory
from backend_quiescence import require_quiescent,require_fresh_namespace
LOCK={'ledger':'ledger.jsonl.lock','finance':'fin.lock','legal':'legal.lock','compliance':'compliance.lock','seo':'seo.lock'}
def quiesce(o):
 require_quiescent()
 if o.journal.recovered:require_fresh_namespace()
 o.front.close()
 if o.child is not None:
  if o.child.poll() is None:
   o.phase='CLOSED';o.stop() # Actual native clean barrier for surviving child.
  else:
   from owner import absent
   if not absent(o.child.pid):raise Refused('original process group remains')
   o.log_handle.close();o.child=None
 if o.guard is None:
  fd=os.open(o.source/LOCK[o.member],os.O_RDWR|os.O_NOFOLLOW)
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BaseException:os.close(fd);raise
  o.guard=fd
 guard=o.guard_identity();inventory=source_inventory(o.source,o.member)
 if not inventory:raise Refused('original store missing; restore required')
 o.repair_source=inventory;o.phase='REPAIR_LOCKED'
 return {'member':o.member,'boot':o.boot,'run':o.journal.doc['run'],'guard':guard,'source_sha256':hashlib.sha256(canonical(inventory)).hexdigest(),'public':'CLOSED','native_stop_claim':False}
def resume(o):
 if o.phase!='REPAIR_LOCKED' or not hasattr(o,'repair_source'):raise Refused('no current original lock proof')
 o.guard_identity()
 if source_inventory(o.source,o.member)!=o.repair_source:raise Refused('original changed under guard')
 os.close(o.guard);o.guard=None
 o.start(initial=False) # No seed initializer, actual existing-store constructor.
 proof=o.effect('verify',o.journal.doc['run'])
 # Disposable plaintext stage is never an encrypted backup. Only after actual
 # originals passed integrity can this own-run, fixed-path stage be discarded.
 stage=o.layout.backup/o.journal.doc['run']/'stage'
 if stage.exists():
  if stage.is_symlink() or not stage.is_dir():raise Refused('staging alias refused')
  source_inventory(stage,o.member);shutil.rmtree(stage)
 o.phase='REPAIR_VERIFIED_CLOSED'
 return {'member':o.member,'boot':o.boot,'run':o.journal.doc['run'],'integrity':proof,'public':'CLOSED','plaintext_stage_absent':not stage.exists()}
def publish(o):
 if o.phase!='REPAIR_VERIFIED_CLOSED' or o.child is None or o.child.poll()is not None:raise Refused('originals not currently verified')
 o.ready();o.front.open();o.phase='OPEN_REPAIRED'
 # recovered/fenced remain untouched. Normal backup remains locked until a
 # separate run retirement/reset decision; business front alone is published.
 return {'member':o.member,'boot':o.boot,'run':o.journal.doc['run'],'pid':o.child.pid,'public':'OPEN','backup':'REPAIR_REQUIRED'}

def verify_sealed(o):
 if o.phase!='REPAIR_LOCKED':raise Refused('seal recovery requires current source lock')
 o.guard_identity()
 seals=[v['receipt']['result'] for v in o.journal.doc['operations'].values() if v['action']=='seal' and v['phase']=='DONE']
 if len(seals)!=1:raise Refused('no exact durable local seal')
 seal=seals[0]
 if seal['member']!=o.member or seal['set_id']!=o.journal.doc['run']:raise Refused('seal original context')
 import gate3_restic as engine
 from quota_capture import Restic
 root=o.layout.backup/seal['set_id']
 if engine.inventory(root/'sealed')!=seal['repository_files']:raise Refused('retained seal changed')
 r=Restic(root,o.password,str(root/'sealed'),'',o.member);r.verify(root/'sealed',seal['snapshot'],seal['manifest'])
 if engine.inventory(root/'sealed')!=seal['repository_files']:raise Refused('seal changed during verification')
 return {'member':o.member,'boot':o.boot,'run':seal['set_id'],'seal_sha256':hashlib.sha256(canonical(seal)).hexdigest(),'snapshot':seal['snapshot'],'public':'CLOSED'}
