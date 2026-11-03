"""Native one-member owner; control state never accounts for business requests."""
import os,sys,json,secrets,time,threading,subprocess,signal,http.client,hashlib,fcntl,re
from pathlib import Path
from owner_wire import Client,Server,canonical,decode,Refused,MEMBERS,keycheck
from owner_state import Journal
from clean_receipt import python_receipt,ledger_receipt
from cold_front import Front
from cold_capture import source_inventory
import gate3_restic as engine
PREFIX={'ledger':'LEDGER','finance':'FIN','legal':'LEGAL','compliance':'COMPLIANCE','seo':'SEO'}
LOCK={'ledger':'ledger.jsonl.lock','finance':'fin.lock','legal':'legal.lock','compliance':'compliance.lock','seo':'seo.lock'}
def absent(pid):
 try:os.killpg(pid,0)
 except ProcessLookupError:return True
 return False
def validate_config(config):
 if not isinstance(config,dict) or set(config)!={'member','app_env','coord_token','backup_password','runtime_inputs','transport_credentials'}:raise Refused('fixed owner configuration')
 m=config['member']
 if m not in MEMBERS:raise Refused('member')
 keycheck(config['coord_token']);password=config['backup_password']
 if not isinstance(password,str) or not 32<=len(password)<=128:raise Refused('backup credential shape')
 supplied=config['app_env'];prefix=PREFIX[m]
 if not isinstance(supplied,dict) or any(not isinstance(k,str) or not isinstance(v,str) or len(v)>16384 or any(word in k for word in ('RESTIC','COORD_TOKEN','BACKUP_PASSWORD')) or not(k.startswith(prefix+'_') or k.startswith('LEDGER_') or k in ('COLD_LEGAL_PORT','COLD_LEGAL_HOST','COLD_STRIPE_HOST')) for k,v in supplied.items()):raise Refused('child environment allowlist')
 if not isinstance(config['runtime_inputs'],dict) or any(name not in ('seo-key','stripe-key','stripe-whsec','stripe-port') or not isinstance(value,str) or len(value)>1024 for name,value in config['runtime_inputs'].items()):raise Refused('runtime input')
 if not isinstance(config['transport_credentials'],dict) or set(config['transport_credentials'])!={'key_id','key'} or any(not isinstance(x,str) or not 1<=len(x)<=4096 or '\x00' in x for x in config['transport_credentials'].values()):raise Refused('transport credential shape')
 return config
class Owner:
 def __init__(self,config):
  validate_config(config)
  self.member=config['member']
  if self.member not in MEMBERS:raise Refused('member')
  self.boot=secrets.token_hex(16);self.mutex=threading.RLock();self.worker=None;self.child=None;self.guard=None;self.front=None;self.phase='CLOSED';self.seq=0;self.fenced=False
  from volume_layout import Layout
  self.layout=Layout('/volume',self.member);self.root=self.layout.control;self.source=self.layout.source
  self.journal=Journal(self.root/'owner-state.json',self.member,self.boot)
  self.password=config['backup_password'];self.token=config['coord_token'];self.transport_credentials=dict(config['transport_credentials'])
  if not isinstance(self.password,str) or not 32<=len(self.password)<=128:raise Refused('backup credential shape')
  supplied=config['app_env'];prefix=PREFIX[self.member]
  if not isinstance(supplied,dict) or any(not isinstance(v,str) or len(v)>16384 or not(k.startswith(prefix+'_') or k.startswith('LEDGER_') or k in ('COLD_LEGAL_PORT','COLD_LEGAL_HOST','COLD_STRIPE_HOST')) for k,v in supplied.items()):raise Refused('child environment allowlist')
  self.env={'PATH':'/opt/venv/bin:/usr/local/bin:/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1',**supplied}
  self.env[prefix+'_BIND_ADDR']='127.0.0.1';self.env[prefix+'_PORT']='27000'
  self.env[prefix+'_LOG_PATH' if self.member=='ledger' else prefix+'_DATA_DIR']=str(self.source/'ledger.jsonl') if self.member=='ledger' else str(self.source)
  self.runtime=Path('/run/cold');self.runtime.mkdir(mode=0o777,exist_ok=True);self.runtime.chmod(0o777)
  if config['runtime_inputs']:raise Refused('packaged owner requires pre-materialized application secrets')
  self.scheduler_control=None
  self.phase='RECOVERY_REQUIRED' if self.journal.recovered else 'CLOSED'
  self.front=Front({self.member:27000},bind='0.0.0.0',port=27001)
  from owner_repair import OwnerRepairs
  self.repairs=OwnerRepairs(self)
  from owner_relay import install_dependency_relay
  install_dependency_relay(self)
  from recovery_owner import install as install_recovery
  install_recovery(self)
 def req(self,path,headers=None):
  c=http.client.HTTPConnection('127.0.0.1',27000,timeout=3)
  try:c.request('GET',path,headers=headers or {});r=c.getresponse();data=r.read(1048577);assert len(data)<=1048576;return r.status,decode(data)
  finally:c.close()
 def ready(self):
  end=time.monotonic()+35
  while self.child.poll()is None and time.monotonic()<end:
   try:healthy=self.req('/health')[0]==200
   except Exception:healthy=False
   if healthy:
    if self.member=='compliance':self.current_readiness()
    return
   time.sleep(.1)
  raise Refused('native child not ready')
 def current_readiness(self):
  from native_readiness import parse
  child=self.child;nonce=self.child_nonce;epoch=self.epoch
  if child is None or child.poll() is not None:raise Refused('readiness child absent')
  self.log_handle.flush();fd=self.log_handle.fileno()
  if os.fstat(fd).st_size>262144:raise Refused('native log envelope')
  text=os.pread(fd,262145,0).decode('utf-8')
  proof=parse(text,child.pid,nonce,self.member,epoch)
  if self.child is not child or child.poll() is not None or self.child_nonce!=nonce or self.epoch!=epoch:raise Refused('readiness child changed')
  return proof
 def initialize(self):
  # Packaged recovery never creates seed state in a lost/empty store.
  # Existing-store application startup performs the complete integrity checks.
  if not any(p.name!=LOCK[self.member] for p in self.source.iterdir()):
   raise Refused('existing application data required; restore first')
 def start(self,initial=False):
  if self.relay.boots is None:raise Refused("relay roster not bound")
  self.relay_mode="STARTUP"
  if self.child is not None:raise Refused('duplicate child')
  if initial:self.initialize()
  os.chown(self.source,10001,10001)
  self.seq+=1;self.child_nonce=secrets.token_hex(16);self.epoch='owner-'+str(self.seq)
  self.log=self.root/('child-'+str(self.seq)+'.log');self.log_handle=self.log.open('w+b')
  cmd=['/usr/bin/setpriv','--reuid=10001','--regid=10001','--clear-groups','--no-new-privs','--bounding-set=-all','--inh-caps=-all','--ambient-caps=-all','--']+(['/opt/zbm-maintenance/bin/ledger-server'] if self.member=='ledger' else ['/opt/venv/bin/python','-B','/opt/zbm-maintenance/cold/native_entrypoint.py',self.member,self.epoch])
  child_env=dict(self.env,ZBM_STARTUP_PROFILE='existing-store',COLD_INSTANCE=self.child_nonce);child_fd=None;parent_fd=None
  if self.member=='compliance' and self.env.get('COMPLIANCE_SCHEDULER_ENABLED','').strip()=='1':
   import socket
   from scheduler_channel import Parent
   parent_fd,child_fd=socket.socketpair();child_env['COLD_SCHEDULER_FD']=str(child_fd.fileno())
   self.scheduler_control=Parent(parent_fd,self.epoch,self.child_nonce)
  try:self.child=subprocess.Popen(cmd,env=child_env,stdout=self.log_handle,stderr=subprocess.STDOUT,start_new_session=True,pass_fds=(child_fd.fileno(),) if child_fd else ())
  except BaseException:
   if self.scheduler_control is not None:self.scheduler_control.close();self.scheduler_control=None
   raise
  finally:
   if child_fd is not None:child_fd.close()
  self.ready()
  from native_privilege import owned_status
  privileges=owned_status(self.child)
  self.phase='READY_CLOSED';return {'pid':self.child.pid,'instance':self.child_nonce,'ready':True,'public':'CLOSED','privileges':privileges}
 def stop(self):
  if self.phase!='CLOSED' or self.child is None:raise Refused('stop requires closed owned child')
  child=self.child;pid=child.pid
  if type(pid)is not int or pid<=0 or child.poll()is not None or os.getpgid(pid)!=pid:raise Refused('owned live process group required')
  os.killpg(pid,signal.SIGTERM);code=child.wait(40)
  if not absent(self.child.pid):raise Refused('owned process group survives')
  self.log_handle.flush()
  if self.log.stat().st_size>262144:raise Refused('native log envelope')
  text=self.log.read_text();pid=self.child.pid
  if self.member=='ledger':
   if code!=0:raise Refused('Ledger nonclean exit')
   proof=ledger_receipt(text,pid)
  else:
   if code!=143:raise Refused('Python nonclean exit')
   proof=python_receipt(text,pid,self.child_nonce,self.member,self.epoch)
  self.log_handle.close();self.child=None
  if self.scheduler_control is not None:self.scheduler_control.close();self.scheduler_control=None
  fd=os.open(self.source/LOCK[self.member],os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);self.guard=fd
  if self.member=='ledger' and decode((self.source/'ledger.jsonl.head').read_bytes())!=proof['head']:raise Refused('locked head mismatch')
  if self.relay.unknown or self.relay.active:raise Refused('dependency outcome unresolved')
  self.relay_mode='CLOSED'
  self.phase='STOPPED_LOCKED';self.clean=proof;self.stopped_inventory=source_inventory(self.source,self.member)
  return {'pid':pid,'native':proof,'source_sha256':hashlib.sha256(canonical(self.stopped_inventory)).hexdigest(),'guard':self.guard_identity()}
 def guard_identity(self):
  if self.guard is None or self.child is not None:raise Refused('no stopped guard')
  fd=os.fstat(self.guard);path=(self.source/LOCK[self.member]).stat()
  if (fd.st_dev,fd.st_ino)!=(path.st_dev,path.st_ino):raise Refused('guard replaced')
  return {'device':fd.st_dev,'inode':fd.st_ino}
 def seal(self,run):
  from incident_archive import checked_capacity
  checked_capacity(self.root,self.member,Client('observer','observer.railway.internal',27003,self.token))
  if self.phase!='STOPPED_LOCKED':raise Refused('seal state')
  self.guard_identity()
  if source_inventory(self.source,self.member)!=self.stopped_inventory:raise Refused('stopped source changed')
  from quota_capture import seal
  manifest={'member':self.member,'set_id':run,'files':self.stopped_inventory}
  self.sealed,self.backup,self.quota_proof=seal(self.source,self.layout.backup,run,self.password,manifest)
  if source_inventory(self.source,self.member)!=self.stopped_inventory:raise Refused('source changed during seal')
  self.phase='SEALED_LOCKED';return self.sealed
 def upload_result(self):
  done=[v['receipt'] for v in self.journal.doc['operations'].values() if v['action']=='upload' and v['phase']=='DONE']
  done.extend(v for v in self.repairs.resolved().values() if v['action']=='upload')
  if len(done)!=1:raise Refused('no exact original or repaired upload')
  return done[0]['result']
 def effect(self,action,run):
  if action in ('upload','verify_remote','cleanup','retire'):
   if self.phase!='OPEN' or self.child is None or self.child.poll() is not None:raise Refused('transfer requires reopened application')
   from distributed_transfer import Transfer
   from transfer_contract import digest
   if action=='retire':
    done=[v['receipt']['result'] for v in self.journal.doc['operations'].values() if v['action']=='cleanup' and v['phase']=='DONE']
    done.extend(v['result'] for v in self.repairs.resolved().values() if v['action']=='cleanup')
    if len(done)!=1:return (_ for _ in ()).throw(Refused('no exact cleanup'))
    return {'run':run,'catalog_sha256':done[0]['catalog_sha256'],'catalog_snapshot':done[0]['catalog_snapshot']}
   t=Transfer(self.backup,self.password,self.sealed,self.transport_credentials)
   if action=='upload':return t.upload()
   done=[self.upload_result()]
   if action=='verify_remote':return t.verify_remote(done[0])
   proof=Client('coordinator','coordinator.railway.internal',27002,self.token).call('/status',{'kind':'complete_catalog','run':run})
   return t.cleanup(done[0],proof)
  if action=='start':return self.start(initial=True)
  if action=='publish':
   if self.phase!='VERIFIED_CLOSED':raise Refused('not verified')
   self.relay_mode='NORMAL'
   if self.scheduler_control is not None:self.scheduler_control.command('resume')
   self.front.open();self.phase='OPEN';return {'public':'OPEN','pid':self.child.pid}
  if action=='close':
   if not self.journal.doc.get('operations'):raise Refused('no run')
   if self.phase=='OPEN':
    source_inventory(self.source,self.member)
    from quota_capture import preflight
    preflight(self.layout.backup,{'member':self.member,'set_id':run,'files':source_inventory(self.source,self.member)})
   if self.phase not in ('OPEN','READY_CLOSED','VERIFIED_CLOSED'):raise Refused('close state')
   self.front.close();self.relay_mode='DRAIN'
   if self.scheduler_control is not None:self.scheduler_control.command('pause')
   self.phase='CLOSED';return {'public':'CLOSED','inflight':0}
  if action=='stop':return self.stop()
  if action=='seal':return self.seal(run)
  if action=='restart':
   if self.phase!='SEALED_LOCKED' or not any(x['action']=='seal' and x['phase']=='DONE' for x in self.journal.doc['operations'].values()):raise Refused('no durable seal')
   self.guard_identity()
   if engine.inventory(self.backup/'sealed')!=self.sealed['repository_files'] or (self.backup/'stage').exists():raise Refused('sealed state changed')
   os.close(self.guard);self.guard=None;return self.start()
  if action=='verify':
   self.ready();h={'Authorization':'Bearer '+self.env[PREFIX[self.member]+'_SERVICE_TOKEN']}
   if self.member=='ledger':code,v=self.req('/ledger/verify',h);ok=code==200 and v.get('valid')is True
   elif self.member=='compliance':v=self.current_readiness();ok=v['integrity_ok']is True and v['ledger_valid']is True
   else:
    h['X-Andre-Approval-Token']=self.env.get(PREFIX[self.member]+'_ANDRE_APPROVAL_TOKEN','')
    if self.member=='seo':h['X-SEO-Caller-Token']=json.loads(self.env['SEO_CALLER_TOKENS'])['dashboard']
    code,v=self.req({'finance':'/fin/v1/integrity','legal':'/legal/v1/integrity','seo':'/seo/v1/audit/integrity'}[self.member],h)
    ok=code==200 and (v.get('status')=='green' if self.member!='seo' else v['integrity']['ok']is True and v['ledger_valid']is True)
   if not ok:raise Refused('actual integrity refusal')
   self.phase='VERIFIED_CLOSED';return {'pid':self.child.pid,'integrity':v,'public':'CLOSED'}
  raise Refused('unsupported action')
 def view(self,operation=None):
  if self.relay.unknown:self.fenced=True
  if self.child is not None and self.child.poll() is not None and self.phase in ('READY_CLOSED','VERIFIED_CLOSED','OPEN','OPEN_REPAIRED'):
   self.fenced=True;self.phase='UNKNOWN'
   with self.front.cv:self.front.opened=False
  if self.guard is not None:self.guard_identity()
  resources={n:(Path('/sys/fs/cgroup')/n).read_text() for n in ('memory.current','memory.peak','memory.events','memory.swap.current','pids.current','pids.events') if (Path('/sys/fs/cgroup')/n).exists()}
  return {'relay':{'mode':self.relay_mode,'active':self.relay.active,'unknown':self.relay.unknown,'counts':dict(self.relay.counts)},'backup_failure':getattr(self,'backup_failure',None),'last_success':self.journal.doc.get('last_success'),'failure':getattr(self,'failure',None),'resources':resources,'member':self.member,'boot':self.boot,'phase':self.phase,'run':self.journal.doc['run'],'child_pid':self.child.pid if self.child else None,'fenced':self.fenced or self.journal.recovered,'operations':{k:{kk:vv for kk,vv in v.items() if kk!='receipt' or k==operation} for k,v in self.visible_operations().items()}}
 def visible_operations(self):
  ops=dict(self.journal.doc['operations']);old=self.journal.doc.get('last_retirement')
  if old:ops[old['operation']]=old['record']
  return ops
 def dispatch(self,path,body):
  with self.mutex:
   if not isinstance(body,dict) or body.get('member')!=self.member:raise Refused('member binding')
   if path in ('/rearm','/rearm-status'):
    from owner_rearm import dispatch
    return dispatch(self,path,body)
   if path=='/rearm-close':
    from owner_rearm import load
    v=load(self)
    if set(body)!={'member','boot','identity'} or body['boot']!=self.boot or body['identity']!=v['identity']:raise Refused('rearm close context')
    self.front.close();self.fenced=True;return {'public':'CLOSED'}
   if path in ('/repair','/repair-status') and body.get('command') in ('verify-remote',):raise Refused('legacy transfer repair unsupported for member-v1')
   if path=='/submit' and body.get('action') in ('repaired-archive','repaired-ready'):
    from repaired_owner import dispatch
    return dispatch(self,body)
   if path=='/submit' and body.get('action')=='metadata-retire':
    if set(body)!={'member','boot','run','action','advance_sha256'} or body['boot']!=self.boot:raise Refused('metadata command context')
    permission=Client('coordinator','coordinator.railway.internal',27002,self.token).call('/status',{'kind':'advance-permit','member':self.member,'boot':self.boot,'run':body['run']})
    if permission['advance_sha256']!=body['advance_sha256']:raise Refused('advance intent changed')
    from metadata_retirement import retire_owner
    return retire_owner(self,permission)
   if path=='/submit' and body.get('action')=='recovery-bootstrap-retire':
    if set(body)!={'action','member','boot','run','proof_sha256'} or body['member']!=self.member or body['boot']!=self.boot or self.phase!='VERIFIED_CLOSED' or self.front.opened or self.child is None or self.child.poll()is not None or self.fenced or self.journal.recovered:raise Refused('closed live recovery retirement required')
    if (self.worker and self.worker.is_alive()) or (self.repairs.worker and self.repairs.worker.is_alive()):raise Refused('recovery work still active')
    from recovery_bootstrap import digest
    proof=Client('coordinator','coordinator.railway.internal',27002,self.token).call('/status',{'kind':'recovery-bootstrap-permit','member':self.member,'boot':self.boot,'run':body['run']})
    if digest(proof)!=body['proof_sha256']:raise Refused('bootstrap permit changed')
    self.journal.retire_recovery(proof)
    return {'member':self.member,'boot':self.boot,'proof_sha256':digest(proof),'state':'RECOVERY_BOOTSTRAP_RETAINED'}
   if path=='/relay-bind':
    if set(body)!={'member','boot','boots'} or body['boot']!=self.boot or self.phase!='CLOSED' or self.child is not None or self.journal.recovered:raise Refused('relay binding phase')
    self.relay.bind(body['boots']);return {'member':self.member,'boot':self.boot,'relay_bound':True}
   if path=='/repair':return self.repairs.submit(body)
   if path=='/repair-status':
    if set(body)!={'member','boot','incident','command'} or body['boot']!=self.boot:raise Refused('repair status context')
    return self.repairs.status(body['incident'],body['command'])
   if path=='/status':
    if body.get('inspect')=='remote_archive':
     run=body.get('run')
     if body.get('boot')!=self.boot or not isinstance(run,str) or not re.fullmatch(r'cold-[0-9a-f]{32}',run):raise Refused('archive context')
     root=self.layout.backup/run/'remote'/'restic'/'sets'/run/self.member
     if not root.is_dir():raise Refused('archive missing')
     return {'member':self.member,'boot':self.boot,'run':run,'files':engine.inventory(root)}
    if body.get('inspect')=='ciphertext':
     if not hasattr(self,'backup'):raise Refused('no retained capture')
     return {'member':self.member,'boot':self.boot,'run':self.journal.doc['run'],'files':engine.inventory(self.backup/'sealed')}
    if body.get('boot')not in ('discover',self.boot):raise Refused('boot binding')
    if body.get('inspect')=='capacity':
     from incident_archive import checked_capacity
     checked_capacity(self.root,self.member,Client('observer','observer.railway.internal',27003,self.token))
     inventory=source_inventory(self.source,self.member);fs=os.statvfs(self.layout.backup)
     from quota_capture import preflight
     preflight(self.layout.backup,{'member':self.member,'set_id':'cold-'+'0'*32,'files':inventory})
     entries=list(self.layout.backup.rglob('*'))
     if any(p.is_symlink() for p in entries) or len(entries)>8192 or sum(p.stat().st_size for p in entries if p.is_file())>8*1048576:raise Refused('retained two-cycle backup envelope')
     if fs.f_bavail*fs.f_frsize<4*1048576 or fs.f_favail<512:raise Refused('backup headroom')
     result=self.view(body.get('operation'));result['capacity']={'source_bytes':sum(x['bytes'] for x in inventory.values()),'source_files':len(inventory),'available_bytes':fs.f_bavail*fs.f_frsize,'available_inodes':fs.f_favail};return result
    result=self.view(body.get('operation'));result['quota_proof']=getattr(self,'quota_proof',None);return result
   if set(body)!={'member','boot','run','action','operation'} or body['boot']!=self.boot:raise Refused('exact command context')
   if not isinstance(body['run'],str) or not re.fullmatch(r'cold-[0-9a-f]{32}',body['run']) or body['operation']not in tuple(body['run']+':'+self.member+':'+body['action']+':'+str(i) for i in (1,2)):raise Refused('run operation identity')
   if self.fenced or self.journal.recovered:raise Refused('owner fenced')
   if body['action']not in ('start','verify','publish','close','stop','seal','restart','upload','verify_remote','cleanup','retire'):raise Refused('fixed action')
   if self.worker and self.worker.is_alive() or self.repairs.worker and self.repairs.worker.is_alive():raise Refused('operation active; poll status')
   if not self.journal.begin(body['run'],body['operation'],body['action'],resolved=self.repairs.resolved()):return self.view(body.get('operation'))
   def invoke():
    try:
     receipt=self.effect(body['action'],body['run'])
     with self.mutex:
      if body['action']=='retire':self.journal.retire(receipt,body['operation'],resolved=self.repairs.resolved())
      else:self.journal.finish(body['operation'],{'member':self.member,'boot':self.boot,'run':body['run'],'action':body['action'],'result':receipt})
    except BaseException as exc:
     if body['action'] in ('upload','verify_remote','cleanup','retire'):
      # Backup uncertainty after reopen cannot mutate business admission.
      with self.mutex:
       allowed={'MEMBER_SCHEMA','MEMBER_BINDING','PROOF_SCHEMA','COMPLETE_CATALOG','OWN_SEAL_BINDING','LOCATOR_BINDING','CLEANUP_ALREADY_ATTEMPTED','CLEANUP_UNCERTAIN','LOCAL_SOURCE_MISMATCH','OWN_UPLOAD_BINDING','catalog durable binding','catalog context unavailable','catalog unpublished','control outcome unavailable','fixed catalog status','operation pending or exhausted','receipt bound'}
       code=str(exc) if str(exc) in allowed else 'UNCLASSIFIED_REFUSAL'
       self.backup_failure={'refusal_code':code,'run':body['run'],'action':body['action'],'type':type(exc).__name__,'retry':'operator resolution required'}
       try:self.journal.fail_backup(body['operation'],self.backup_failure)
       except BaseException:pass  # Original PENDING remains; no false completion.
      return
     with self.mutex:self.fenced=True;self.phase='UNKNOWN';self.failure={'type':type(exc).__name__,'reason':str(exc)[:160] if isinstance(exc,Refused) else 'effect incomplete'}
     try:self.front.close()
     except BaseException:pass
   self.worker=threading.Thread(target=invoke,name='native-owner-operation',daemon=False);self.worker.start();return self.view(body.get('operation'))
 def cleanup(self):
  # Fixture/host termination is not a clean stop receipt or repair authority.
  try:self.front.close()
  except BaseException:pass
  if self.child and self.child.poll()is None:
   self.child.terminate()
   try:self.child.wait(40)
   except subprocess.TimeoutExpired:os.killpg(self.child.pid,signal.SIGKILL);self.child.wait(3)
  if self.scheduler_control is not None:self.scheduler_control.close();self.scheduler_control=None
  if self.guard is not None:os.close(self.guard);self.guard=None
  from owner_relay import close_dependency_relay
  close_dependency_relay(self)
  self.front.stop();self.repairs.close();self.journal.close()
def main():
 raw=sys.stdin.buffer.readline(65537)
 if len(raw)>65536:raise Refused('input bound')
 config=decode(raw);owner=Owner(config);server=Server(('0.0.0.0',27002),owner.token,owner.dispatch)
 def stop(*_):raise KeyboardInterrupt
 signal.signal(signal.SIGTERM,stop)
 try:server.serve_forever(poll_interval=.1)
 finally:server.server_close();owner.cleanup()
if __name__=='__main__':main()
