"""Single temporary-project rehearsal. Root executes only after bounded review."""
import json,os,time,uuid,subprocess,hashlib,signal,shlex
from pathlib import Path
ROOT=Path(__file__).resolve().parent;PACKAGE=ROOT;CLI=os.environ.get('RAILWAY_CLI','/opt/homebrew/bin/railway')
WORKSPACE=None
DENY=set()
MEMBERS=('ledger','finance','legal','compliance','seo');ROLES=(*MEMBERS,'coordinator','observer','provider');SERVICES=(*ROLES,'trigger')
REGION='us-west2'
ALLOW_EXECUTION=False
class Stop(RuntimeError):pass

def money(v):return type(v)in (int,float) and __import__('math').isfinite(v) and v>=0
def billed(d,current,policy):
 if d.get('workspace',{}).get('id')!=WORKSPACE or d.get('truncated') is not False or not isinstance(d.get('projects'),list):raise Stop('USAGE_SCHEMA')
 ids=set(policy['prior_project_ids']);values={}
 if current in ids:raise Stop('COST_PROJECT_OVERLAP')
 for row in d['projects']:
  if not isinstance(row,dict):raise Stop('USAGE_SCHEMA')
  ident=row.get('id')
  if ident not in ids and (current is None or ident!=current):continue
  if ident in values or not money(row.get('currentUsageDollars')):raise Stop('COST_INVALID')
  values[ident]=row['currentUsageDollars']
 return max(sum(values.get(i,0) for i in ids),policy['prior_reserved_usd']),values.get(current) if current is not None else None
class Controller:
 def __init__(self,call=None,clock=time.time,sleep=time.sleep):
   self.call=call or self.cli;self.clock=clock;self.sleep=sleep;self.p=None;self.e=None;self.ids={};self.volumes={};self.started=None;self.delete_requested=False;self.phase={};self.deployments={};self.trigger_seconds=0;self.trigger_started=None;self.verified_created=None;self.last_cost_check=None;self.ev=[];self.run='zbm-temp-20261105-'+uuid.uuid4().hex[:8]
   self.out=ROOT/'evidence'/self.run
 def cli(self,args,timeout=30):
   if not ALLOW_EXECUTION:raise Stop('EXECUTION_GUARD_CLOSED')
   p=subprocess.run([CLI,*args],capture_output=True,timeout=timeout,env={'PATH':'/opt/homebrew/bin:/usr/bin:/bin','HOME':str(Path.home()),'CI':'true'},cwd=PACKAGE)
   self.out.mkdir(parents=True,exist_ok=True)
   n=str(os.getpid())+'-'+uuid.uuid4().hex
   def safe(raw):
    import re
    v=raw.decode('utf-8','replace')[:65536]
    v=re.sub(r'(?i)(bearer\s+|token[=:]\s*)[^\s]+',r'\1[REDACTED]',v)
    return v
   (self.out/('cli-'+n+'.json')).write_text(json.dumps({'operation':args[0],'exit':p.returncode,'stdout':safe(p.stdout),'stderr':safe(p.stderr)}))
   if p.returncode:raise Stop('CLI_'+args[0].upper()+'_EXIT_'+str(p.returncode))
   return p.stdout.decode()
 def save(self,event,**fields):
   self.ev.append({'event':event,'at':self.clock(),**fields});self.out.mkdir(parents=True,exist_ok=True)
   p=self.out/'checkpoint.json';t=p.with_suffix('.new');t.write_text(json.dumps({'run':self.run,'verified_created':self.verified_created,'project':self.p,'environment':self.e,'services':self.ids,'volumes':self.volumes,'started':self.started,'events':self.ev},indent=2));os.replace(t,p)
 def budget(self):
   if self.started is not None and self.clock()-self.started>=2700:raise Stop('TEARDOWN_DEADLINE')
   self.cost_check()
   elapsed=self.trigger_seconds+(self.clock()-self.trigger_started if self.trigger_started is not None else 0)
   if elapsed>=3600:raise Stop('TRIGGER_TOTAL_DEADLINE')
 def api(self,q,v,teardown=False):
   from input_preflight import operation
   operation(q,v)
   if not teardown:self.budget()
   raw=self.call(['api',q,'--variables',json.dumps(v),'--compact'],30);d=json.loads(raw)
   if d.get('errors') or not isinstance(d.get('data'),dict):raise Stop('API_REFUSED_SEE_PRIVATE_CLI_EVIDENCE')
   return d['data']
 def assert_scope(self):
   if self.p in DENY or self.p is None:raise Stop('PROJECT_SCOPE')
   q='query($id:String!){project(id:$id){id name workspaceId deletedAt environments{edges{node{id}}} services{edges{node{id name}}}}}'
   d=self.api(q,{'id':self.p})['project']
   if d['id']!=self.p or d['name']!=self.run or d['workspaceId']!=WORKSPACE or d['deletedAt'] is not None:raise Stop('PROJECT_IDENTITY')
   if [x['node']['id'] for x in d['environments']['edges']] != [self.e]:raise Stop('ENV_IDENTITY')
   actual={x['node']['name']:x['node']['id'] for x in d['services']['edges']}
   if actual!=self.ids:raise Stop('SERVICE_IDENTITY')
 def set_limits(self,role):
   self.assert_scope();size=(512 if role in ('finance','legal','compliance','seo') else 256)*1048576
   self.api('mutation($i:ServiceInstanceLimitsUpdateInput!){serviceInstanceLimitsUpdate(input:$i)}',{'i':{'environmentId':self.e,'serviceId':self.ids[role],'memoryGB':size/1e9,'vCPUs':1}})
   d=self.api('query($e:String!,$s:String!){serviceInstanceLimits(environmentId:$e,serviceId:$s)}',{'e':self.e,'s':self.ids[role]})['serviceInstanceLimits']
   memory=d.get('containers',{}).get('memoryBytes');cpu=d.get('containers',{}).get('cpu')
   if type(memory)not in (int,float) or not __import__('math').isfinite(memory) or not __import__('math').isclose(memory,size,rel_tol=0.0,abs_tol=1e-6) or type(cpu)not in (int,float) or not __import__('math').isfinite(cpu) or cpu!=1:raise Stop('RESOURCE_LIMIT_MISMATCH')
   self.save('LIMITS_VERIFIED',role=role,memory_bytes=size,cpu=1)
 def listing(self,role):
   return self.api('query($i:DeploymentListInput!){deployments(input:$i,first:10){edges{node{id status deploymentStopped projectId environmentId serviceId}}}}',{'i':{'projectId':self.p,'environmentId':self.e,'serviceId':self.ids[role]}})['deployments']['edges']
 def deployment(self,role):
   d=self.api('query($id:String!){deployment(id:$id){id status deploymentStopped projectId environmentId serviceId}}',{'id':self.deployments[role]})['deployment']
   if (d['projectId'],d['environmentId'],d['serviceId'])!=(self.p,self.e,self.ids[role]):raise Stop('DEPLOYMENT_SCOPE')
   return d
 def wait_status(self,role,acceptable=('SUCCESS',),timeout=300):
   end=self.clock()+timeout
   while self.clock()<end:
    d=self.deployment(role)
    if d['status'] in ('FAILED','CRASHED','SKIPPED'):raise Stop('DEPLOYMENT_FAILED')
    if d['status'] in acceptable:return d
    self.sleep(3)
   raise Stop('DEPLOYMENT_WAIT_TIMEOUT')
 def logs(self,role):
   self.assert_scope();self.deployment(role)
   raw=self.call(['logs',self.deployments[role],'--project',self.p,'--environment',self.e,'--service',self.ids[role],'--lines','300','--json'],30)
   rows=[]
   for line in raw.splitlines():
    try:
     outer=json.loads(line)
     if not isinstance(outer,dict):continue
     message=outer.get('message')
     if isinstance(message,str) and message.strip():
      value=json.loads(message)
     elif message=='' or 'message' not in outer:
      # Railway lifts structured application fields beside its empty envelope.
      # Strip only the observed provider envelope; unknown application keys remain.
      value={k:v for k,v in outer.items() if k not in ('message','timestamp','level')}
     else:continue
     if isinstance(value,dict) and value:rows.append(value)
    except (ValueError,TypeError):pass
   return rows
 def teardown(self):
   if not self.p or self.p in DENY:return
   if not self.verified_created or self.verified_created.get('id')!=self.p or self.verified_created.get('name')!=self.run or self.verified_created.get('workspaceId')!=WORKSPACE:raise Stop('UNVERIFIED_DELETE_TARGET')
   if not self.delete_requested:
    self.out.mkdir(parents=True,exist_ok=True)
    try:
     fd=os.open(self.out/'delete.claim',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    except FileExistsError:self.delete_requested=True
    else:
     self.delete_requested=True
     if self.api('mutation($id:String!){projectDelete(id:$id)}',{'id':self.p},teardown=True).get('projectDelete') is not True:raise Stop('PROJECT_DELETE_REFUSED')
     self.save('PROJECT_DELETE_ACCEPTED')
   end=min(self.started+3600,self.clock()+600)
   while self.clock()<end:
    d=self.api('query($id:String!){workspace(workspaceId:$id){projects{edges{node{id}}}}}',{'id':WORKSPACE},teardown=True)
    if self.p not in [x['node']['id'] for x in d['workspace']['projects']['edges']]:self.save('PROJECT_ABSENT_FROM_WORKSPACE');return True
    self.sleep(5)
   self.save('DELETE_NOT_CONFIRMED');return False
