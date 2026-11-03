"""Persistent recovery role; fixed nonsecret selection, no restart adoption."""
import os,stat,fcntl,hashlib,json,threading,time,signal
from pathlib import Path
from owner_wire import canonical,decode,Refused,Server,keycheck
from recovery_gateway import Inbox
import recovery_controller as controller

def construct_coordinator(root,cfg):
 from coordinator import Coordinator
 # A new maintenance namespace must never reuse the selected remote backup set.
 return Coordinator(root,cfg['token'],cfg['password'],transport_credentials=cfg['credentials'],serve_control=False)

def request(value):
 from packaged_restore import selection
 import re
 if type(value)is not dict or set(value)!={'kind','selection','scope'} or value['kind']!='recover':raise Refused('fixed recovery request')
 selection(value['selection']);scope=value['scope'];keys={'account','invoice','client','session','payment_intent','acceptance','tenant','audit','mode'}
 if type(scope)is not dict or set(scope)!=keys or scope.get('mode')not in ('TEST','LIVE') or any(type(v)is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',v) for v in scope.values()) or len(canonical(value))>4096:raise Refused('fixed recovery scope')
 return decode(canonical(value))

def read(path):
 fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
 try:
  s=os.fstat(fd)
  if not stat.S_ISREG(s.st_mode) or s.st_size>4096:raise Refused('recovery record bound')
  return decode(os.read(fd,4097))
 finally:os.close(fd)

def immutable(path,value):
 raw=canonical(value)
 if len(raw)>4096 or path.exists() or path.is_symlink() or path.with_suffix('.tmp').exists():raise Refused('recovery record already exists')
 tmp=path.with_suffix('.tmp');fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 os.replace(tmp,path);fd=os.open(path.parent,os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)

class Service:
 def __init__(self,root,cfg):
  keycheck(cfg['token']);self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
  if self.root.is_symlink() or self.root.resolve()!=self.root:raise Refused('recovery custody')
  self.fd=os.open(self.root/'recovery-service.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
  try:fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BaseException:os.close(self.fd);raise Refused('recovery service already active') from None
  self.cfg=cfg;self.gateway_auth=cfg['gateway_auth'];self.lock=threading.RLock();self.inbox=Inbox();self.co=None;self.worker=None;self.stopped=False;self.value=None;self.identity=None;self.state='WAIT'
  p=self.root/'recovery-selection.json'
  if p.exists() or p.is_symlink():
   self.value=request(read(p));self.identity=hashlib.sha256(canonical(self.value)).hexdigest();self.state='LOCKED'
  if p.with_suffix('.tmp').exists() or p.with_suffix('.tmp').is_symlink() or (self.root/'run.json').exists() or (self.root/'run.tmp').exists():self.state='LOCKED'
 def status(self):
  with self.lock:return {'state':self.state,'request_sha256':self.identity}
 def is_open(self):
  with self.lock:return self.state=='OPEN' and not self.stopped and self.co is not None
 def dispatch(self,path,body):
  with self.lock:
   if path=='/status' and body=={'kind':'recovery_status'}:return self.status()
   if path=='/status' and body.get('kind')=='recovery-bootstrap-permit' and self.state=='RUNNING' and self.co is not None:
    return self.co.catalog_status(path,body)
   if path!='/submit' or body.get('kind')!='recover':
    if self.state=='OPEN' and self.co is not None:return self.co.catalog_status(path,body)
    raise Refused('fixed recovery control')
   value=request(body);identity=hashlib.sha256(canonical(value)).hexdigest()
   if self.identity is not None:
    if identity!=self.identity:raise Refused('immutable recovery selection')
    return self.status()
   if self.state!='WAIT' or self.stopped:raise Refused('recovery restart unresolved')
   immutable(self.root/'recovery-selection.json',value);self.value=value;self.identity=identity;self.state='RUNNING'
   self.worker=threading.Thread(target=self.work,name='fixed-recovery-run',daemon=False);self.worker.start();return self.status()
 def command_value(self):
  return {'schema':'fixed-application-recovery-v1','coordination_token':self.cfg['token'],'stripe_key':self.cfg['stripe_key'],'scope':self.value['scope'],'restore':{'schema':'fixed-b2-data-restore-v1','selection':self.value['selection'],'password':self.cfg['password'],'credentials':{'key_id':self.cfg['credentials']['key_id'],'application_key':self.cfg['credentials']['key']}}}
 def fail(self):
  with self.lock:self.state='LOCKED';self.stopped=True;self.inbox.close();co=self.co
  if co is not None:
   try:co.front.close()
   except Exception:pass
  if self.value is not None:
   try:controller.retain_locked(self.command_value(),root=self.root)
   except Exception:print('RECOVERY_ALERT_UNCONFIRMED',flush=True)
 def deliveries(self,inventory):
  # Only actual closed native committed evidence can remove an event from wait.
  from provider_recovery import ProviderInterval,Refused as ProviderRefused
  raw=controller.observation(self.co,'finance','finance-log',1);missing=set()
  for eid,event in inventory['events'].items():
   try:ProviderInterval.require_deliveries(None,{'events':{eid:event}},raw)
   except ProviderRefused:missing.add(eid)
  self.inbox.install(inventory,missing);end=time.monotonic()+180
  while missing:
   eid,body,signature=self.inbox.take(end)
   yield body,signature # execute sends unchanged signature, then waits exact DONE
   self.inbox.done(eid);missing.discard(eid)
 def work(self):
  timer=threading.Timer(900,self.fail);timer.daemon=True;timer.start()
  try:
   co=construct_coordinator(self.root,self.cfg)
   with self.lock:
    self.co=co
    if self.stopped:raise Refused('recovery stopped')
   original_publish=co.publish
   def publish():
    with self.lock:
     if self.stopped or self.state not in ('RUNNING','OPEN'):raise Refused('publication stopped')
     original_publish()
   co.publish=publish
   result=controller.execute(self.command_value(),restore=lambda v:controller.restore_group(v,self.cfg['token']),coordinator_factory=lambda *a:co,provider_factory=__import__('provider_recovery').transport_from_key,deliveries=self.deliveries)
   with self.lock:
    if self.stopped:raise Refused('recovery stopped')
    immutable(self.root/'recovery-service-result.json',{'request_sha256':self.identity,'state':'OPEN','proof_sha256':hashlib.sha256(canonical(result)).hexdigest(),'maintenance_run':co.doc['run']})
    self.state='OPEN'
  except BaseException:self.fail()
  finally:timer.cancel()
 def close(self):
  self.fail()
  if self.worker and self.worker.is_alive():self.worker.join(3)
  if self.co is not None:
   self.co.front.stop()
   if self.co.catalog_server is not None:self.co.catalog_server.shutdown();self.co.catalog_server.server_close()
  if self.fd is not None:os.close(self.fd);self.fd=None

def run(cfg):
 import recovery_gateway
 if os.geteuid()!=0:raise Refused('supervisor root required')
 service=Service(Path('/volume/control'),cfg);control=Server(('0.0.0.0',27002),cfg['token'],service.dispatch);gateway=recovery_gateway.server(service)
 threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (control,gateway)]
 for t in threads:t.start()
 stop=threading.Event()
 for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
 try:
  if service.state=='LOCKED':service.fail()
  stop.wait()
 finally:
  service.close()
  for s in (control,gateway):s.shutdown();s.server_close()
  for t in threads:t.join(3)
  if service.worker and service.worker.is_alive():os._exit(75)
 return 0
