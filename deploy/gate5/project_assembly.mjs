// Pure observed-state conservation proposal. NOT a Railway SDK/API/apply payload.
import {createHash} from 'node:crypto';
const fail=code=>{throw new Error(code);};
const object=x=>x!==null&&typeof x==='object'&&!Array.isArray(x);
const own=(x,k)=>Object.hasOwn(x,k);
function stable(x) {
  if(x===null||typeof x==='string'||typeof x==='boolean') return JSON.stringify(x);
  if(typeof x==='number'&&Number.isFinite(x)) return JSON.stringify(x);
  if(Array.isArray(x)) return '['+x.map(stable).join(',')+']';
  if(object(x)&&Object.getPrototypeOf(x)===Object.prototype) return '{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+stable(x[k])).join(',')+'}';
  fail('PLAIN_JSON_REQUIRED');
}
export const digest=x=>createHash('sha256').update(stable(x)).digest('hex');
const copy=x=>JSON.parse(stable(x));
const equal=(a,b)=>stable(a)===stable(b);
const exact=(x,keys)=>object(x)&&equal(Object.keys(x).sort(),[...keys].sort());
const text=x=>typeof x==='string'&&x.length>0&&x.length<=512;
function index(rows,identity) {
  if(!Array.isArray(rows)) fail('IDENTITY_INVENTORY');
  const ids=new Map(),names=new Set();
  for(const row of rows){const x=identity(row);if(!object(x)||!text(x.id)||!text(x.name)||ids.has(x.id)||names.has(x.name))fail('IDENTITY_AMBIGUOUS');ids.set(x.id,row);names.add(x.name);}
  return ids;
}
function names(value) {
  if(!Array.isArray(value)||value.some(x=>typeof x!=='string'||!/^[A-Z][A-Z0-9_]{0,127}$/.test(x))||new Set(value).size!==value.length)fail('VARIABLE_NAMES_REQUIRED');
}
function validate(b) {
  if(!object(b)||b.schema!==1||!text(b.captured_at)||!Array.isArray(b.limitations)||!object(b.environment))fail('OBSERVED_BASELINE_REQUIRED');
  const env=b.environment;
  if(!object(env.project)||!text(env.project.id)||!object(env.environment)||!text(env.environment.id))fail('PROJECT_ENV_IDENTITY');
  if(env.staged!==null)fail('STAGED_BASELINE');
  const summary=index(env.services,x=>x),detail=index(b.services,x=>x.service),volumes=index(env.volumes,x=>x);
  if(!equal([...summary.keys()].sort(),[...detail.keys()].sort()))fail('SERVICE_INVENTORY_IDENTITY');
  if(!Array.isArray(env.buckets))fail('BUCKET_INVENTORY');
  names(env.sharedVariableNames);
  for(const [id,s] of detail) {
    const a=summary.get(id);
    if(s.service.name!==a.name||s.environment?.id!==env.environment.id||s.environment?.name!==env.environment.name)fail('SERVICE_IDENTITY_CONFLICT');
    if(s.staged!==null||!Array.isArray(s.stagedChanges)||s.stagedChanges.length)fail('STAGED_BASELINE');
    if(!object(s.config)||!object(s.domains)||!Array.isArray(s.domains.serviceDomains)||!Array.isArray(s.domains.customDomains)||!Array.isArray(s.tcpProxies))fail('SERVICE_SETTINGS_REQUIRED');
    if(['variables','env','variableValues'].some(k=>own(s.config,k)||own(s,k))||own(env,'sharedVariables'))fail('VARIABLE_VALUES_FORBIDDEN');
    names(s.variableNames);
    if(!Array.isArray(a.volumeMounts)||!Array.isArray(s.volumes))fail('VOLUME_INVENTORY');
    const local=index(s.volumes,x=>x),summ=new Map();
    for(const mount of a.volumeMounts){if(!object(mount)||!text(mount.volumeId)||summ.has(mount.volumeId))fail('VOLUME_IDENTITY');summ.set(mount.volumeId,mount);}
    const config=s.config.volumeMounts??{};
    if(!object(config)||!equal([...local.keys()].sort(),[...summ.keys()].sort())||!equal([...local.keys()].sort(),Object.keys(config).sort()))fail('VOLUME_INVENTORY_CONFLICT');
    for(const [vid,v] of local){const global=volumes.get(vid),m=summ.get(vid);if(!global||global.name!==v.name||m.volumeName!==v.name||m.mountPath!==v.mountPath||config[vid]?.mountPath!==v.mountPath||global.mountedOn?.serviceId!==id||global.mountedOn?.serviceName!==s.service.name||global.mountedOn?.mountPath!==v.mountPath||global.sizeMB!==v.sizeMB||global.region!==v.region)fail('VOLUME_BINDING_CONFLICT');}
  }
  for(const v of volumes.values())if(v.mountedOn!==null&&v.mountedOn!==undefined){const s=detail.get(v.mountedOn.serviceId);if(!s||!s.volumes.some(x=>x.id===v.id))fail('VOLUME_REVERSE_BINDING');}
  return detail;
}
// Known leaf settings only. Unknown fields, domains, identities, mounts, variable
// inventories and whole containers cannot be edited through this proposal seam.
const PATHS=new Set(['source/image','source/repo','source/branch','source/autoUpdates',
 'deploy/drainingSeconds','deploy/overlapSeconds','deploy/healthcheckPath',
 'deploy/healthcheckTimeout','deploy/startCommand','deploy/cronSchedule',
 'deploy/restartPolicyType','deploy/restartPolicyMaxRetries','deploy/numReplicas']);
const NEW_ROLES=new Set(['compliance','coordinator','observer','backup-trigger','client-portal']);
export function assemble(baseline,request) {
  const b=copy(baseline),r=copy(request);
  if(!exact(r,['schema','status','executable','baseline_sha256','changes','new_role_intents'])||r.schema!=='gate5-observed-deltas-v1'||r.status!=='HOLD'||r.executable!==false)fail('DELTA_REQUEST_REQUIRED');
  if(r.baseline_sha256!==digest(b))fail('BASELINE_DIGEST_MISMATCH');
  const byId=validate(b);
  if(!Array.isArray(r.changes)||r.changes.length>128||!Array.isArray(r.new_role_intents)||r.new_role_intents.length>5)fail('DELTA_BOUND');
  const projected=copy(b),projectedById=new Map(projected.services.map(s=>[s.service.id,s]));
  const used=new Set(),deltas=[];
  for(const change of r.changes) {
    if(!exact(change,['service_name','service_id','path','before','after']))fail('DELTA_SCHEMA');
    const s=byId.get(change.service_id);
    if(!s||s.service.name!==change.service_name)fail('DELTA_SERVICE_IDENTITY');
    const path=change.path;
    if(!Array.isArray(path)||path.length!==3||path[0]!=='config'||!PATHS.has(path.slice(1).join('/')))fail('DELTA_PATH_FORBIDDEN');
    const signature=change.service_id+':'+path.join('/');if(used.has(signature))fail('DUPLICATE_DELTA');used.add(signature);
    const parent=s.config[path[1]];
    if(!object(parent))fail('BEFORE_PARENT_NOT_OBSERVED');
    const present=own(parent,path[2]);
    if(!exact(change.before,present?['present','value']:['present'])||change.before.present!==present||(present&&!equal(change.before.value,parent[path[2]])))fail('BEFORE_MISMATCH');
    if(change.after!==null&&!['string','number','boolean'].includes(typeof change.after))fail('LEAF_VALUE_REQUIRED');
    if(typeof change.after==='string'&&change.after.length>4096)fail('LEAF_VALUE_BOUND');
    projectedById.get(change.service_id).config[path[1]][path[2]]=copy(change.after);
    deltas.push({...change,prior_observation:present?'EXACT_OBSERVED_VALUE':'NOT_EXPOSED_UNKNOWN'});
  }
  const proposedNames=new Set(),existingNames=new Set([...byId.values()].map(s=>s.service.name));
  for(const n of r.new_role_intents){if(!exact(n,['name','service_id','mode'])||!NEW_ROLES.has(n.name)||existingNames.has(n.name)||proposedNames.has(n.name)||n.service_id!==null||n.mode!=='unprovisioned-proposal-only')fail('NEW_ROLE_IDENTITY');proposedNames.add(n.name);}
  const obligations=b.services.map(s=>({service_name:s.service.name,service_id:s.service.id,variables:s.variableNames.map(name=>({name,instruction:'preserve-observed-name',observed_kind:'UNKNOWN',value_collected:false}))}));
  return {schema:'gate5-observed-conservation-proposal-v1',status:'HOLD',executable:false,
    format:'NOT_SDK_NOT_API_NOT_APPLY',baseline_sha256:digest(b),request_sha256:digest(r),
    observed_baseline:b,projected_observations:projected,deltas,new_role_intents:r.new_role_intents,
    variable_obligations:obligations,shared_variable_obligations:b.environment.sharedVariableNames.map(name=>({name,instruction:'preserve-observed-name',observed_kind:'UNKNOWN',value_collected:false})),
    conservation:{observed_services:byId.size,observed_volumes:b.environment.volumes.length,observed_baseline_retained:true,resource_deletions:0,changed_fields:deltas.length,
      untouched_services:b.services.filter(s=>!deltas.some(d=>d.service_id===s.service.id)).map(s=>({service_id:s.service.id,sha256:digest(s)})),projected_sha256:digest(projected)},
    blockers:['OBSERVED_SNAPSHOT_NOT_COMPLETE_SDK_GRAPH','VARIABLE_KINDS_VALUES_AND_SEALED_STATUS_UNKNOWN',
      'OMITTED_FIELDS_ARE_UNKNOWN_NOT_DEFAULTS','IDENTITY_LOCK_BINDINGS_AND_APPLY_SEMANTICS_UNPROVEN',
      'PROVIDER_SNAPSHOT_FRESHNESS_AND_ATOMICITY_UNPROVEN','NEW_RESOURCES_NOT_PROVISIONED',
      'EXPLICIT_FIELD_DELTAS_ARE_PROPOSALS_NOT_DEPLOYMENT_APPROVAL','NO_PROVIDER_WRITES_AUTHORIZED']};
}
export function verifyConservation(baseline,request,artifact) {
  if(!equal(assemble(baseline,request),artifact))fail('CONSERVATION_MISMATCH');
  return true;
}
