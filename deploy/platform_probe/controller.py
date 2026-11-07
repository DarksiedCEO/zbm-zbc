"""One-shot no-rebuild platform probe. Live entrypoint intentionally requires approval.json."""
import base64,json,os,re,secrets,shlex,sys,time,uuid,subprocess,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parent
import platform_helpers as old
MEMBERS=('ledger','legal','compliance','finance','seo');ROLES=(*MEMBERS,'admin')
Stop=old.Stop
SOURCE_FILES=('controller.py','bootstrap.py','admin_probe.py','platform_helpers.py','watchdog.py','fixture_config.py','input_preflight.py','image-pins.json','input-contract/schema.json','input-contract/environment.schema.json')
PINS_PATH=ROOT/'image-pins.json'
PREFIX=dict(ledger='LEDGER',legal='LEGAL',compliance='COMPLIANCE',finance='FIN',seo='SEO')
ENTRYPOINT={m:['/usr/local/bin/zbm-entrypoint','--port-var',PREFIX[m]+'_PORT','--state-file-var' if m=='ledger' else '--state-dir-var',PREFIX[m]+'_LOG_PATH' if m=='ledger' else PREFIX[m]+'_DATA_DIR'] for m in MEMBERS}
FILES={'ledger':['LEDGER_CALLERS_FILE'],'legal':[],'compliance':[],'finance':['FIN_STRIPE_SECRET_KEY_FILE','FIN_STRIPE_WEBHOOK_SECRET_FILE','FIN_RECEIPT_RESEND_KEY_FILE'],'seo':['SEO_LOG_HASH_KEY_FILE','SEO_GSC_SERVICE_ACCOUNT_FILE']}
for m in MEMBERS:
 for f in FILES[m]:ENTRYPOINT[m]+= ['--secret-file',f]
 ENTRYPOINT[m]+=['--',*(['/usr/local/bin/ledger-server'] if m=='ledger' else ['/opt/venv/bin/python','-m','api'])]

def pins():
 rows=json.loads(PINS_PATH.read_text())
 if set(rows)!=set(ROLES) or any(not re.fullmatch('ghcr.io/darksiedceo/zbm-gate5-'+r+'@sha256:[0-9a-f]{64}',v) for r,v in rows.items()):raise Stop('PIN_PREFLIGHT')
 return rows
def start_command(role):
 if role not in ROLES:raise Stop('ROLE')
 code=(ROOT/('admin_probe.py' if role=='admin' else 'bootstrap.py')).read_bytes()
 return '/usr/local/bin/python -I -B -c '+shlex.quote('import base64;exec(compile(base64.b64decode('+repr(base64.b64encode(code).decode())+'),"platform-probe","exec"))')
def synthetic_environment():
 from fixture_config import build
 pw='synthetic-restic-'+secrets.token_hex(24)
 _,envs,token,_,_=build({'key_id':'synthetic-only-id','application_key':'synthetic-only-key','password':pw})
 for m in MEMBERS:
  p=PREFIX[m];envs[m]={k:v for k,v in envs[m].items() if not k.startswith('COLD_')}
  envs[m][p+'_LOG_PATH' if m=='ledger' else p+'_DATA_DIR']='/data/'+m+('/ledger.jsonl' if m=='ledger' else '')
  envs[m].update(RAILWAY_RUN_UID='0',BACKUP_LAUNCH_ENABLED='1',BACKUP_MEMBER=m,BACKUP_VOLUME='/data',BACKUP_COORD_TOKEN=token,BACKUP_RESTIC_PASSWORD=pw,BACKUP_B2_KEY_ID='synthetic-only-id',BACKUP_B2_APPLICATION_KEY='synthetic-only-key',BACKUP_CAPACITY_POLICY=json.dumps({'version':1,'sources':{x:524288 for x in MEMBERS}}),PROBE_MEMBER=m,PROBE_ENTRYPOINT=json.dumps(ENTRYPOINT[m]),PORT='27001')
  envs[m][p+'_PORT']='27001';envs[m][p+'_BIND_ADDR']='0.0.0.0'
 envs['compliance'].update(COMPLIANCE_SCHEDULER_ENABLED='0',COMPLIANCE_WATCHER_ENABLED='0')
 envs['finance']={k:v for k,v in envs['finance'].items() if not k.startswith('FIN_STRIPE_')};envs['finance']['FIN_CARD_PREPAYMENTS']='0'
 envs['seo']['SEO_LOG_HASH_KEY']=secrets.token_hex(32)
 envs['admin']={'BACKUP_COORD_TOKEN':token,'BACKUP_LAUNCH_ENABLED':'0'}
 return envs

def load_scope():
 policy=json.loads((ROOT/'approval.json').read_text())
 workspace=policy['workspace_id'];protected=policy['protected_project_ids']
 uuid.UUID(workspace)
 if len(protected)!=2 or len(set(protected))!=2:raise Stop('PROTECTED_SCOPE')
 for ident in protected:uuid.UUID(ident)
 old.WORKSPACE=workspace;old.DENY=set(protected)
 return policy

def projected_cost(elapsed,posted,prior):
 # Conservative all-role allocation, 49h five-volume tail, 1GB egress reserve.
 return prior+max(posted or 0,max(0,elapsed)/3600*(.1667+.0373))+.2552+.05
class Controller(old.Controller):
 def __init__(self,*a,**kw):
  super().__init__(*a,**kw);self.run='zbm-probe-'+uuid.uuid4().hex[:12];self.out=ROOT/'evidence'/self.run;self.policy=None
 def preflight(self):
  self.policy=load_scope()
  if self.policy.get('approved') is not True or self.policy.get('soft_stop_usd')!=.5 or self.policy.get('ceiling_usd')!=1 or self.policy.get('no_retry') is not True:raise Stop('APPROVAL_CLOSED')
  if self.policy.get('prior_reserved_usd',0)<.03:raise Stop('PRIOR_RESERVE')
  ids=self.policy.get('prior_project_ids',[])
  if len(ids)!=6 or len(set(ids))!=6 or set(ids)&old.DENY:raise Stop('PRIOR_IDS')
  for ident in ids:uuid.UUID(ident)
  self.images=pins();self.envs=synthetic_environment()
  for f,h in self.policy['source_sha256'].items():
   if f not in SOURCE_FILES or hashlib.sha256((ROOT/f).read_bytes()).hexdigest()!=h:raise Stop('SOURCE_CHANGED')
  if set(self.policy['source_sha256'])!=set(SOURCE_FILES):raise Stop('SOURCE_PINS')
  if projected_cost(3600,0,self.policy['prior_reserved_usd'])>=1:raise Stop('COST_RESERVATION')
  for role in ROLES:
   if len(start_command(role))>30000:raise Stop('START_COMMAND_BOUND')
 def cost_check(self):
  if not self.p:return
  now=self.clock()
  if self.last_cost_check is not None and now-self.last_cost_check<30:return
  self.last_cost_check=now
  d=json.loads(self.call(['usage','projects','--workspace',old.WORKSPACE,'--limit','100','--json'],30))
  prior,current=old.billed(d,self.p,self.policy)
  estimate=projected_cost(now-self.started,current,prior)
  self.save('COST_OBSERVED',current_usd=current,prior_accounted_usd=prior,projected_with_reserves_usd=estimate,missing_current=current is None)
  if estimate>=.5:raise Stop('SOFT_COST_TEARDOWN')
 def provision(self):
  fd=os.open(ROOT/'one-shot.claim',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as f:f.write(self.run);f.flush();os.fsync(f.fileno())
  self.started=self.clock();self.save('ATTEMPT_STARTED')
  d=self.api('mutation($i:ProjectCreateInput!){projectCreate(input:$i){id name workspaceId createdAt environments{edges{node{id name}}}}}',{'i':{'name':self.run,'workspaceId':old.WORKSPACE,'defaultEnvironmentName':'synthetic-probe','isPublic':False,'prDeploys':False}})['projectCreate']
  if d['id'] in old.DENY or d['name']!=self.run or d['workspaceId']!=old.WORKSPACE:raise Stop('PROJECT_IDENTITY')
  self.p=d['id'];self.verified_created={k:d[k] for k in ('id','name','workspaceId','createdAt')};self.save('PROJECT_CREATED')
  es=d['environments']['edges']
  if len(es)!=1 or es[0]['node']['name']!='synthetic-probe':raise Stop('ENVIRONMENT_IDENTITY')
  self.e=es[0]['node']['id'];self.verified_created['environment']=self.e;self.save('ENVIRONMENT_BOUND')
  for role in ROLES:
   self.assert_scope();d=self.api('mutation($i:ServiceCreateInput!){serviceCreate(input:$i){id name projectId}}',{'i':{'name':role,'projectId':self.p}})['serviceCreate']
   if d['name']!=role or d['projectId']!=self.p:raise Stop('SERVICE_IDENTITY')
   self.ids[role]=d['id'];self.save('SERVICE_CREATED',role=role);self.set_limits(role)
  for role in MEMBERS:
   self.assert_scope();d=self.api('mutation($i:VolumeCreateInput!){volumeCreate(input:$i){id}}',{'i':{'projectId':self.p,'environmentId':self.e,'serviceId':self.ids[role],'mountPath':'/data','region':old.REGION}})['volumeCreate']
   self.volumes[role]=d['id'];self.save('VOLUME_CREATED',role=role);self.sleep(31)
  self.verify_volumes()
 def verify_volumes(self):
  end=self.clock()+120
  while True:
   d=self.api('query($p:String!,$e:String!){project(id:$p){id volumes{edges{node{id projectId}}}} environment(id:$e){id projectId volumeInstances{edges{node{volumeId environmentId serviceId mountPath region sizeMB deletedAt isPendingDeletion}}}}}',{'p':self.p,'e':self.e})
   if d['project']['id']!=self.p or d['environment']['projectId']!=self.p or d['environment']['id']!=self.e:raise Stop('VOLUME_SCOPE')
   rows=[x['node'] for x in d['environment']['volumeInstances']['edges']]
   if len({x['volumeId'] for x in rows})!=len(rows) or set(x['volumeId'] for x in rows)-set(self.volumes.values()):raise Stop('VOLUME_MISMATCH')
   for r in rows:
    role=next(k for k,v in self.volumes.items() if v==r['volumeId'])
    if (r['serviceId'],r['mountPath'],r['region'],r['sizeMB'],r['environmentId'])!=(self.ids[role],'/data',old.REGION,5000,self.e) or r['deletedAt'] or r['isPendingDeletion']:raise Stop('VOLUME_MISMATCH')
   if len(rows)==5:return
   if self.clock()>=end:raise Stop('VOLUME_READINESS_TIMEOUT')
   self.sleep(3)
 def configure_image(self,role):
  self.assert_scope();deploy={'restartPolicyType':'NEVER','multiRegionConfig':{old.REGION:{'numReplicas':1}},'overlapSeconds':0,'drainingSeconds':45,'startCommand':start_command(role)}
  patch={'services':{self.ids[role]:{'source':{'image':self.images[role]},'variables':{k:{'value':v} for k,v in self.envs[role].items()},'deploy':deploy}}}
  s=self.api('mutation($e:String!,$i:EnvironmentConfig!){environmentStageChanges(environmentId:$e,input:$i,merge:false){id}}',{'e':self.e,'i':patch})['environmentStageChanges']['id']
  self.api('mutation($e:String!){environmentPatchCommitStaged(environmentId:$e,skipDeploys:true)}',{'e':self.e});end=self.clock()+120
  while True:
   r=self.api('query($id:String!){environmentPatch(id:$id){id environmentId status appliedAt lastAppliedError}}',{'id':s})['environmentPatch']
   if r['id']!=s or r['environmentId']!=self.e or r['status']=='FAILED' or r['lastAppliedError']:raise Stop('PATCH_REFUSED')
   if r['status']=='COMMITTED' and r['appliedAt']:break
   if self.clock()>=end:raise Stop('PATCH_TIMEOUT')
   self.sleep(3)
  e=self.api('query($e:String!){environment(id:$e){id projectId config(decryptVariables:false)}}',{'e':self.e})['environment']
  if e['id']!=self.e or e['projectId']!=self.p:raise Stop('CONFIG_SCOPE')
  actual=e['config'].get('services',{}).get(self.ids[role],{})
  if actual.get('source',{}).get('image')!=self.images[role] or any(actual.get('deploy',{}).get(k)!=v for k,v in deploy.items()):raise Stop('CONFIG_MISMATCH')
  self.save('IMAGE_CONFIGURED',role=role,digest=self.images[role])
 def deploy_once(self,role):
  if role in self.deployments:raise Stop('NO_RETRY')
  self.assert_scope();before={x['node']['id'] for x in self.listing(role)}
  if before:raise Stop('PREEXISTING_DEPLOYMENT')
  ok=self.api('mutation($e:String!,$s:String!){serviceInstanceDeploy(environmentId:$e,serviceId:$s)}',{'e':self.e,'s':self.ids[role]})['serviceInstanceDeploy']
  if ok is not True:raise Stop('DEPLOY_REFUSED')
  for _ in range(30):
   rows=[x['node'] for x in self.listing(role)]
   if len(rows)>1:raise Stop('DEPLOYMENT_AMBIGUOUS')
   if rows:self.deployments[role]=rows[0]['id'];self.save('DEPLOYMENT_SUBMITTED',role=role,deployment=rows[0]['id']);return
   self.sleep(2)
  raise Stop('DEPLOYMENT_NOT_FOUND')
 def execute(self):
  for role in ROLES:self.configure_image(role)
  for role in MEMBERS:self.deploy_once(role);self.wait_status(role)
  self.deploy_once('admin');end=self.clock()+900
  while self.clock()<end:
   for role in ROLES:
    d=self.deployment(role)
    if d['status'] in ('FAILED','CRASHED','SKIPPED'):raise Stop('WORKLOAD_FAILURE')
   rows=self.logs('admin')
   if any(r.get('result')=='FAIL' for r in rows):raise Stop('PROBE_FAILED')
   if any(r.get('probe')=='COMPLETE' and r.get('result')=='PASS' for r in rows):self.save('PROBE_COMPLETE',results=rows);return
   self.sleep(3)
  raise Stop('PROBE_TIMEOUT')

def main():
 c=Controller();c.preflight();old.ALLOW_EXECUTION=True
 c.out.mkdir(parents=True,exist_ok=True)
 w=subprocess.Popen([sys.executable,'-B',str(ROOT/'watchdog.py'),str(c.out),str(os.getpid())],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 for _ in range(50):
  if (c.out/'watchdog.ready').exists():break
  if w.poll() is not None:raise Stop('WATCHDOG_EXITED')
  time.sleep(.1)
 else:raise Stop('WATCHDOG_NOT_READY')
 try:
  c.provision();c.execute()
 finally:
  if c.p:
   if not c.teardown():raise Stop('DELETE_NOT_PROVEN')
  (c.out/'finished').write_text('closed')
  w.wait(timeout=10)
if __name__=='__main__':main()
