"""Offline boundary checks; --smoke adds one internal-network exact-image Docker run."""
import ast,json,os,shlex,subprocess,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import controller as c

def boundaries():
 for f in ROOT.glob('*.py'):compile(f.read_text(),str(f),'exec')
 assert c.old.ALLOW_EXECUTION is False
 assert c.projected_cost(0,None,.03)>.33
 assert c.projected_cost(2700,None,.03)<.50
 assert c.projected_cost(3600,None,.03)>.50
 assert c.projected_cost(0,.5,.03)>.5
 assert len(c.pins())==6
 for r in c.ROLES:assert len(c.start_command(r))<30000
 assert 'deploymentRemove' not in (ROOT/'controller.py').read_text()
 assert 'deploymentRemove' not in (ROOT/'platform_helpers.py').read_text()
 env=c.synthetic_environment()
 assert set(env)==set(c.ROLES)
 assert all(env[m]['BACKUP_VOLUME']=='/data' for m in c.MEMBERS)
 assert len({env[m]['BACKUP_COORD_TOKEN'] for m in c.MEMBERS})==1
 assert not any(k.startswith('FIN_STRIPE_') for k in env['finance'])
 from input_preflight import operation
 for name in ('controller.py','platform_helpers.py'):
  for node in ast.walk(ast.parse((ROOT/name).read_text())):
   if isinstance(node,ast.Constant) and isinstance(node.value,str) and node.value.startswith(('query(','mutation(')):operation(node.value)
 print('OFFLINE_BOUNDARIES_PASS',flush=True)

def smoke():
 ident='zbm-smoke-'+uuid.uuid4().hex[:10];out=ROOT/'evidence'/ident;out.mkdir(parents=True)
 names=[];volumes=[];network=False
 def run(args,timeout=30,check=True):
  p=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=timeout)
  if check and p.returncode:raise RuntimeError('docker '+args[0]+' exit '+str(p.returncode))
  return p
 try:
  run(['network','create','--internal',ident]);network=True
  envs=c.synthetic_environment();pins=c.pins()
  for role in c.ROLES:
   name=ident+'-'+role
   if role in c.MEMBERS:
    run(['volume','create',name]);volumes.append(name)
   args=['run','-d','--pull=never','--name',name,'--network',ident,'--network-alias',role+'.railway.internal','--memory','256m' if role in ('ledger','admin') else '512m','--memory-swap','256m' if role in ('ledger','admin') else '512m','--cpus','1','--pids-limit','64','--user','0','--entrypoint','/usr/local/bin/python']
   if role in c.MEMBERS:args+=['--mount','type=volume,source='+name+',target=/data']
   for k,v in envs[role].items():args+=['--env',k+'='+v]
   args+=[pins[role],*shlex.split(c.start_command(role))[1:]]
   run(args);names.append(name)
  end=time.monotonic()+600
  while time.monotonic()<end:
   for name in names:
    v=json.loads(run(['inspect',name]).stdout)[0]['State']
    if not v['Running'] and (name!=ident+'-admin' or v['ExitCode']!=0):raise RuntimeError('container exited '+name+' '+str(v['ExitCode']))
   a=json.loads(run(['inspect',ident+'-admin']).stdout)[0]['State']
   if not a['Running']:
    logs=run(['logs',ident+'-admin']).stdout
    if not any(json.loads(x).get('probe')=='COMPLETE' and json.loads(x).get('result')=='PASS' for x in logs.splitlines() if x.startswith('{')):raise RuntimeError('completion not proven')
    print('LOCAL_CONNECTED_SMOKE_PASS',flush=True);return
   time.sleep(2)
  raise RuntimeError('smoke timeout')
 finally:
  for name in names:
   for cmd in ('logs','inspect'):
    r=run([cmd,name],check=False);(out/(name+'-'+cmd+'.txt')).write_text(r.stdout+r.stderr)
   run(['rm','-f',name],check=False)
  for name in volumes:run(['volume','rm',name],check=False)
  if network:run(['network','rm',ident],check=False)
  remaining=run(['ps','-a','--filter','name='+ident,'--format','{{.Names}}']).stdout.strip()
  if remaining:raise RuntimeError('cleanup unproven')
  (out/'cleanup.json').write_text(json.dumps({'containers_absent':True,'volume_removal_requested':volumes,'network_removal_requested':network}))
if __name__=='__main__':
 boundaries()
 if sys.argv[1:]==['--smoke']:smoke()
 elif sys.argv[1:]:raise SystemExit('Only --smoke allowed')
