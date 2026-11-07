"""Explicit fresh-volume restore boot before normal Owner construction.

Only the existing dedicated authenticated control listener is used. A failed or
interrupted write is retained and requires a new fresh destination, never retry.
"""
import hashlib,os,secrets,signal,threading,time,fcntl
from pathlib import Path
from owner_wire import Server,canonical,Refused
from cold_front import Front
class RestoreBoot:
 def __init__(self,cfg,*,restore,construct,prerequisites,front):
  from owner import validate_config
  validate_config(cfg)
  self.cfg=cfg;self.member=cfg['member'];self.boot=secrets.token_hex(16);self.restore=restore;self.construct=construct;self.prerequisites=prerequisites;self.front=front;self.owner=None;self.lock=threading.RLock();self.worker=None;self.record=None;self.identity=None
 def dispatch(self,path,body):
  with self.lock:
   if body.get('action')!='recovery-restore':
    if self.owner is not None:return self.owner.dispatch(path,body)
    raise Refused('restore boot only')
   base={'action','member','boot'}
   if body.get('member')!=self.member:raise Refused('restore member')
   if path=='/status' and body.get('boot')=='discover' and set(body)==base:return {'member':self.member,'boot':self.boot,'phase':'RESTORE_WAIT' if self.record is None else self.record['state']}
   if body.get('boot')!=self.boot:raise Refused('restore boot changed')
   if path=='/status':
    if set(body)!=base|{'operation'} or body['operation']!=self.identity or self.record is None:raise Refused('restore status identity')
    return dict(self.record)
   if path!='/submit' or set(body)!=base|{'operation','locator'}:raise Refused('restore submit schema')
   from packaged_restore import selection
   locator=body['locator'];selection({'locator':locator,'cutoff_utc':'1970-01-01T00:00:00Z'})
   identity='restore-'+hashlib.sha256(canonical({'member':self.member,'locator':locator})).hexdigest()
   if body['operation']!=identity:raise Refused('restore operation binding')
   if self.record is not None:raise Refused('existing attempt status only')
   self.identity=identity;self.record={'state':'PENDING','operation':identity}
   def work():
    try:
     credentials={'key_id':self.cfg['transport_credentials']['key_id'],'application_key':self.cfg['transport_credentials']['key']}
     receipt=self.restore(self.member,self.cfg['backup_password'],credentials,locator)
     if receipt.get('member')!=self.member or receipt.get('set_id')!=locator['set_id'] or receipt.get('catalog_snapshot')!=locator['snapshot'] or receipt.get('catalog_sha256')!=locator['catalog_sha256'] or receipt.get('application_start')!='NOT_PERFORMED':raise Refused('member restore receipt')
     self.prerequisites(self.cfg)
     self.front.stop() # no business dispatch occurred; release fixed intake for Owner
     owner=self.construct(self.cfg)
     with self.lock:self.owner=owner;self.record={'state':'DONE','operation':identity,'receipt':receipt,'owner_boot':owner.boot}
    except BaseException:
     with self.lock:self.record={'state':'UNKNOWN','operation':identity}
   self.worker=threading.Thread(target=work,name='fixed-member-restore',daemon=False);self.worker.start();return {'state':'PENDING','operation':identity}

def run(cfg):
 from capacity_policy import require_loaded_application_recovery
 require_loaded_application_recovery()
 from member_restore import restore_member
 from owner import Owner
 from cold_config import prerequisites
 from volume_layout import Layout,member_root
 cfg=dict(cfg)
 if cfg.pop('recovery',None)is not True:raise Refused('explicit recovery boot required')
 root=member_root(cfg['member'],cfg['app_env']);layout=Layout(root,cfg['member'])
 def restore_here(member,password,credential,locator):
  return restore_member(member,password,credential,locator,volume_root=str(root))
 fd=os.open(Path('/run/recovery-boot.lock'),os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);os.write(fd,b'recovery boot: interrupted state requires fresh destination\n');os.fsync(fd)
 front=Front({cfg['member']:27000},bind='0.0.0.0',port=27001)
 boot=RestoreBoot(cfg,restore=restore_here,construct=Owner,prerequisites=prerequisites,front=front)
 server=Server(('0.0.0.0',27002),cfg['coord_token'],boot.dispatch)
 def stop(*_):raise KeyboardInterrupt
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 try:server.serve_forever(poll_interval=.1)
 finally:
  server.server_close()
  if boot.worker and boot.worker.is_alive():boot.worker.join(125)
  if boot.worker and boot.worker.is_alive():os._exit(75) # process exit; no clean receipt or retry claim
  if boot.owner:boot.owner.cleanup()
  else:front.stop()
  os.close(fd)
 return 0
