"""Offline desired-state checks only. No provider, subprocess or apply capability."""
import hashlib,importlib.util,json,re,sys
from pathlib import Path
MEMBERS=('ledger','legal','compliance','finance','seo')
ROLES=MEMBERS+('coordinator','observer','backup-trigger')
PREFIX={'ledger':'LEDGER','legal':'LEGAL','compliance':'COMPLIANCE','finance':'FIN','seo':'SEO'}
CONSUMERS={'client-portal':['PORTAL_LEGAL_URL'],'stripe-gateway':['STRIPE_GATEWAY_FINANCE_URL'],'orchestrator':['LEDGER_SERVICE_URL'],'detection':['DETECTION_LEDGER_URL','DETECTION_COMPLIANCE_URL']}
OWNER_KEYS=['BACKUP_LAUNCH_ENABLED','BACKUP_MEMBER','BACKUP_VOLUME','BACKUP_RECOVERY_MODE','BACKUP_COORD_TOKEN','BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_CAPACITY_POLICY','PORT','RAILWAY_RUN_UID']
ADMIN=['/opt/venv/bin/python','-I','-B','/opt/zbm-maintenance/admin_launcher.py']
def contract(role):
 if role in MEMBERS:
  command=['/usr/local/bin/ledger-server'] if role=='ledger' else ['/opt/venv/bin/python','-m','api']
  return {'entrypoint':['/usr/local/bin/zbm-entrypoint'],'handoff':['/usr/local/bin/python3','-I','/opt/zbm-maintenance/launcher.py']+command,'mount':'/data','source':'/data/'+role+('/ledger.jsonl' if role=='ledger' else ''),'control':'/data/control','backup':'/data/backup','ports':{'native_loopback':27000,'business_private':27001,'control_private':27002,'dependency_private':27003},'root_supervisor':True,'variable_names':OWNER_KEYS+[PREFIX[role]+('_LOG_PATH' if role=='ledger' else '_DATA_DIR')],'public_domain':False}
 keys=['BACKUP_LAUNCH_ENABLED','BACKUP_COORD_TOKEN']
 if role=='coordinator':keys+=['BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_CAPACITY_POLICY']
 if role=='observer':keys+=['BACKUP_FIRST_DUE_UTC_SECONDS','BACKUP_ALERTS_ENABLED','BACKUP_RESEND_API_KEY','BACKUP_ALERT_FROM']
 return {'entrypoint':ADMIN+[role if role!='backup-trigger' else 'trigger'],'handoff':None,'mount':None if role=='backup-trigger' else '/volume','source':None,'control':None if role=='backup-trigger' else '/volume/control','backup':None if role=='backup-trigger' else '/volume/backup','ports':{'business_private':27001,'control_private':27002} if role=='coordinator' else ({'monitor_private':27003} if role=='observer' else {}),'root_supervisor':role!='backup-trigger','variable_names':keys,'public_domain':False}
def template():
 return {'schema':'gate5-topology-plan-v1','executable':False,'source_sha':None,'roles':{r:{'contract':contract(r),'image_digest':None,'volume_id':None,'resources':None} for r in ROLES},'start_order':list(MEMBERS),'stop_order':list(reversed(MEMBERS)),'routing':{r:{k:'http://coordinator.railway.internal:27001' for k in keys} for r,keys in CONSUMERS.items()},'stripe_private_name':'coordinator','external_inventory_complete':False,'external_inventory_evidence':None,'additional_external_consumers':[],'coordination':{'new_dedicated_token':True,'distribution':'direct-service-local-copies','secret_references':False,'reference_kind':'non-secret-URLs-only'},'schedule':{'policy':'fixed-UTC-no-DST-adjustment','timezone':'UTC','cadence_seconds':86400,'grace_seconds':900,'cron_utc':None,'first_due_utc_seconds':None},'capture_limits':None,'evidence':{k:None for k in ('source_review','resources_approval','volume_bindings','compliance_initialized','dns_connectivity','routing','schedule','no_directory_collisions')}}
def hexid(value,n):return isinstance(value,str) and re.fullmatch('[0-9a-f]{'+str(n)+'}',value) is not None and value!='0'*n
def capacity_settings(limits):
 """Use runtime's pure policy parser/derivation; never initialize process policy."""
 if not isinstance(limits,dict) or set(limits)!=set(MEMBERS):raise ValueError('capacity members')
 spec=importlib.util.spec_from_file_location('gate5_capacity_policy',Path(__file__).parents[1]/'docker/maintenance/cold/capacity_policy.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 raw=module.parse(json.dumps({'version':1,'sources':{m:limits[m]['source'] for m in MEMBERS}},allow_nan=False))
 if any(limits[m]!=module.derive_member(raw,m) for m in MEMBERS):raise ValueError('capacity derived limits')
 module.require_application_recovery(raw)
 return raw,hashlib.sha256(raw.encode()).hexdigest()
def uuid(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',value) is not None and value!='00000000-0000-0000-0000-000000000000'
def inputs_digest(plan):
 return hashlib.sha256(json.dumps({k:v for k,v in plan.items() if k not in ('evidence','external_inventory_evidence')},sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def evidence(value,sha,digest):return isinstance(value,dict) and set(value)=={'source_sha','artifact_sha256','inputs_sha256'} and value['source_sha']==sha and value['inputs_sha256']==digest and hexid(value['artifact_sha256'],64)
def validate(plan):
 errors=[];base=template()
 def require(ok,label):
  if not ok:errors.append(label)
 if not isinstance(plan,dict) or set(plan)!=set(base):return ['PLAN_SCHEMA']
 require(plan['schema']==base['schema'] and plan['executable'] is False,'PLAN_ONLY')
 try:digest=inputs_digest(plan)
 except (ValueError,TypeError):return ['NONFINITE_OR_INVALID_INPUT']
 sha=plan['source_sha'];require(hexid(sha,40),'SOURCE_SHA')
 for k in ('start_order','stop_order','routing','stripe_private_name','coordination'):require(plan[k]==base[k],k.upper())
 require(plan['external_inventory_complete'] is True,'EXTERNAL_INVENTORY_INCOMPLETE')
 require(evidence(plan['external_inventory_evidence'],sha,digest),'EXTERNAL_INVENTORY_EVIDENCE')
 extra=plan['additional_external_consumers'];require(isinstance(extra,list),'EXTERNAL_CONSUMERS_SCHEMA')
 if isinstance(extra,list):
  for row in extra:
   require(isinstance(row,dict) and set(row)=={'consumer','variable_name','url'} and isinstance(row.get('consumer'),str) and re.fullmatch('[a-z][a-z0-9-]{0,62}',row['consumer']) and isinstance(row.get('variable_name'),str) and re.fullmatch('[A-Z][A-Z0-9_]{0,80}',row['variable_name']) and row.get('url')=='http://coordinator.railway.internal:27001','EXTERNAL_ROUTE')
 roles=plan['roles']
 if not isinstance(roles,dict) or set(roles)!=set(ROLES):return errors+['ROLES_SCHEMA']
 volumes=[]
 for r,row in roles.items():
  if not isinstance(row,dict) or set(row)!=set(base['roles'][r]):errors.append(r+':SCHEMA');continue
  require(row['contract']==contract(r),r+':FIXED_CONTRACT')
  d=row['image_digest'];require(isinstance(d,str) and d.startswith('sha256:') and hexid(d[7:],64),r+':IMAGE_DIGEST')
  if r=='backup-trigger':require(row['volume_id'] is None,r+':NO_VOLUME')
  else:require(uuid(row['volume_id']),r+':VOLUME_ID');volumes.append(row['volume_id'])
  rs=row['resources'];require(isinstance(rs,dict) and set(rs)=={'memory_bytes','vcpus','pid_limit'} and type(rs.get('memory_bytes'))is int and rs['memory_bytes']>0 and type(rs.get('vcpus')) in (int,float) and 0<rs['vcpus']<=64 and type(rs.get('pid_limit'))is int and rs['pid_limit']>0,r+':APPROVED_RESOURCES')
 require(len({v for v in volumes if isinstance(v,str)})==len(volumes),'DISTINCT_VOLUMES')
 limits=plan['capture_limits'];require(isinstance(limits,dict) and set(limits)==set(MEMBERS),'PRODUCTION_CAPTURE_LIMITS')
 if isinstance(limits,dict):
  for m in MEMBERS:
   row=limits.get(m);require(isinstance(row,dict) and set(row)=={'source','stage','repository','control_reserve'} and all(type(v)is int and v>0 for v in row.values()),m+':CAPTURE_LIMITS')
 try:capacity_settings(limits)
 except (ValueError,KeyError,TypeError):errors.append('RUNTIME_CAPACITY_POLICY')
 schedule=plan['schedule']
 if not isinstance(schedule,dict) or set(schedule)!=set(base['schedule']):errors.append('SCHEDULE_SCHEMA')
 else:
  for k in ('policy','timezone','cadence_seconds','grace_seconds'):require(schedule[k]==base['schedule'][k],'SCHEDULE_'+k.upper())
  cron=schedule['cron_utc'];require(isinstance(cron,str) and re.fullmatch(r'(?:[0-9]|[1-5][0-9]) (?:[0-9]|1[0-9]|2[0-3]) \* \* \*',cron) is not None,'SCHEDULE_CRON')
  due=schedule['first_due_utc_seconds'];require(type(due)is int and 0<due<253402214399,'SCHEDULE_DUE')
  if type(due)is int and isinstance(cron,str) and re.fullmatch(r'\d+ \d+ \* \* \*',cron):require(due%86400==int(cron.split()[1])*3600+int(cron.split()[0])*60,'SCHEDULE_ALIGNMENT')
 ev=plan['evidence'];require(isinstance(ev,dict) and set(ev)==set(base['evidence']),'EVIDENCE_SCHEMA')
 if isinstance(ev,dict):
  for k in base['evidence']:require(evidence(ev.get(k),sha,digest),'EVIDENCE_'+k.upper())
 return errors
if __name__=='__main__':
 try:
  if len(sys.argv)!=2:raise ValueError()
  path=Path(sys.argv[1]);raw=path.read_bytes()
  if len(raw)>65536:raise ValueError()
  result=validate(json.loads(raw));print(json.dumps({'status':'BLOCKED' if result else 'PLAN_INPUTS_COMPLETE_NOT_APPLY_AUTHORITY','blockers':result}));raise SystemExit(2 if result else 0)
 except (ValueError,OSError,TypeError,KeyError):print('{"status":"BLOCKED","blockers":["INVALID_INPUT"]}');raise SystemExit(2)
