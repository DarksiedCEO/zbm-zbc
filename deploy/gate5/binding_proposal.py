"""Offline NON-Railway desired-state proposal. Never an SDK payload/apply tool."""
import hashlib,importlib.util,json,sys
from pathlib import Path
_spec=importlib.util.spec_from_file_location('gate5_topology',Path(__file__).with_name('topology_validator.py'))
t=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(t)
EXISTING=('ledger','legal','finance','seo')
CONTEXT_KEYS={'project_id','environment_id','existing_services','existing_volumes','production_limits_binding','compliance_initialization_binding'}
class Refused(ValueError):pass
def require(ok,code):
 if not ok:raise Refused(code)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def binding_matches(binding,plan,kind):
 if not isinstance(binding,dict) or set(binding)!={'source_sha','inputs_sha256','artifact_sha256','kind'}:return False
 return binding['source_sha']==plan['source_sha'] and binding['inputs_sha256']==t.inputs_digest(plan) and t.hexid(binding['artifact_sha256'],64) and binding['kind']==kind

def propose(plan,context):
 require(not t.validate(plan),'TOPOLOGY_INPUTS_BLOCKED')
 require(isinstance(context,dict) and set(context)==CONTEXT_KEYS,'CONTEXT_SCHEMA')
 require(t.uuid(context['project_id']) and t.uuid(context['environment_id']),'PROJECT_ENVIRONMENT_IDENTITY')
 services=context['existing_services'];volumes=context['existing_volumes']
 required_services=set(EXISTING)|{'stripe-gateway','orchestrator','detection'}
 require(isinstance(services,dict) and required_services<=set(services)<=required_services|{'client-portal'},'EXISTING_SERVICE_INVENTORY')
 require(all(t.uuid(v) for v in services.values()) and len(set(services.values()))==len(services),'DISTINCT_SERVICE_IDENTITIES')
 require(isinstance(volumes,dict) and set(volumes)==set(EXISTING),'EXISTING_VOLUME_INVENTORY')
 for m in EXISTING:
  row=volumes[m]
  require(isinstance(row,dict) and set(row)=={'id','mount','source'},'VOLUME_INVENTORY_SCHEMA')
  require(row['id']==plan['roles'][m]['volume_id'] and row['mount']==plan['roles'][m]['contract']['mount'] and row['source']==plan['roles'][m]['contract']['source'],'EXISTING_VOLUME_CHANGE_REFUSED')
 blockers=['SDK_FORMAT_NOT_QUALIFIED','FOUNDER_APPLY_AUTHORITY_NOT_GRANTED','OBSERVED_PLATFORM_READINESS_NOT_ESTABLISHED']
 if not binding_matches(context['production_limits_binding'],plan,'runtime-production-limits-implemented'):blockers.append('RUNTIME_PRODUCTION_LIMITS_UNRESOLVED')
 if not binding_matches(context['compliance_initialization_binding'],plan,'compliance-existing-store-initialized'):blockers.append('COMPLIANCE_INITIALIZATION_UNRESOLVED')
 if 'client-portal' not in services:blockers.append('CLIENT_PORTAL_NOT_PROVISIONED')
 roles={}
 capacity_raw,capacity_sha=t.capacity_settings(plan['capture_limits'])
 for name,row in plan['roles'].items():
  c=row['contract'];owner=name in t.MEMBERS
  settings={'BACKUP_LAUNCH_ENABLED':'0'}
  if owner or name=='coordinator':settings['BACKUP_CAPACITY_POLICY']=capacity_raw
  if owner:settings.update(BACKUP_MEMBER=name,BACKUP_VOLUME='/data',BACKUP_RECOVERY_MODE='0',PORT='27001',RAILWAY_RUN_UID='0');settings[t.PREFIX[name]+('_LOG_PATH' if name=='ledger' else '_DATA_DIR')]=c['source']
  if name=='observer':settings.update(BACKUP_ALERTS_ENABLED='0',BACKUP_FIRST_DUE_UTC_SECONDS=str(plan['schedule']['first_due_utc_seconds']))
  roles[name]={'identity':{'service_id':services.get(name),'mode':'preserve-existing' if name in services else 'unprovisioned-proposal-only'},'approved_image_digest':row['image_digest'],'entrypoint_contract':c,'volume':None if c['mount'] is None else {'id':row['volume_id'],'mount':c['mount'],'mode':'preserve-no-move' if name in EXISTING else 'requires-separate-provisioning-evidence'},'disabled_settings':settings,'preserve_existing_application_settings':True,'required_variable_names':list(c['variable_names']),'resources_requested_not_applied':row['resources'],'public_domains':[]}
 routes=[]
 for client,values in plan['routing'].items():
  for name,url in values.items():routes.append({'client':client,'service_id':services.get(client),'identity_mode':'preserve-existing' if client in services else 'unprovisioned-proposal-only','variable_name':name,'proposed_url':url,'apply_during_authorized_cutover_only':True})
 routes.extend({'client':x['consumer'],'service_id':None,'variable_name':x['variable_name'],'proposed_url':x['url'],'apply_during_authorized_cutover_only':True} for x in plan['additional_external_consumers'])
 return {'schema':'gate5-non-sdk-binding-proposal-v1','status':'HOLD','executable':False,'format':'NOT_RAILWAY_SDK_OR_API_PAYLOAD','source_sha':plan['source_sha'],'topology_inputs_sha256':t.inputs_digest(plan),'binding_context_sha256':digest(context),'project_id':context['project_id'],'environment_id':context['environment_id'],'blockers':blockers,'roles':roles,'central_route_proposals':routes,'gateway_private_name_proposal':{'service_id':services['stripe-gateway'],'variable_name':'STRIPE_GATEWAY_FINANCE_PRIVATE_NAME','proposed_name':'coordinator'},'schedule_proposal':{'service':'backup-trigger','enabled':False,**plan['schedule']},'capture_limits_requested_not_applied':plan['capture_limits'],'capacity_policy_sha256':capacity_sha,'start_order':plan['start_order'],'stop_order':plan['stop_order'],'secret_policy':plan['coordination']}

def diff(before,after):
 """Fixed-shape semantic diff; never accept identity/mount/source migration."""
 for obj in (before,after):require(isinstance(obj,dict) and obj.get('schema')=='gate5-non-sdk-binding-proposal-v1' and obj.get('executable') is False and obj.get('status')=='HOLD','PROPOSAL_SCHEMA')
 for key in ('project_id','environment_id'):require(before[key]==after[key],'IDENTITY_CHANGE_REFUSED')
 for m in EXISTING:
  for key in ('identity','volume'):require(before['roles'][m][key]==after['roles'][m][key],'EXISTING_IDENTITY_OR_MOUNT_CHANGE_REFUSED')
  require(before['roles'][m]['entrypoint_contract']['source']==after['roles'][m]['entrypoint_contract']['source'],'EXISTING_SOURCE_CHANGE_REFUSED')
 return {'schema':'gate5-proposal-diff-v1','apply':False,'changed_sections':[k for k in sorted(set(before)|set(after)) if before.get(k)!=after.get(k)]}

def load(path):
 raw=Path(path).read_bytes();require(len(raw)<=131072,'INPUT_BOUND')
 def unique(pairs):
  out={}
  for k,v in pairs:require(k not in out,'DUPLICATE_JSON_KEY');out[k]=v
  return out
 return json.loads(raw,object_pairs_hook=unique)
if __name__=='__main__':
 try:
  require(len(sys.argv)==3,'TWO_INPUT_FILES_REQUIRED');print(json.dumps(propose(load(sys.argv[1]),load(sys.argv[2])),sort_keys=True,indent=2))
 except (Refused,ValueError,OSError,TypeError,KeyError) as exc:
  print(json.dumps({'status':'HOLD','executable':False,'category':str(exc) if isinstance(exc,Refused) else 'INVALID_INPUT'}));raise SystemExit(2)
