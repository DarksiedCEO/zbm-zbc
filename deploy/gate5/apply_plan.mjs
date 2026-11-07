// Offline only: produces targeted staged-tool arguments, never calls Railway.
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {assemble,digest} from './project_assembly.mjs';
const owners=['ledger','legal','finance','seo'];
const fail=x=>{throw new Error(x);};
export function plan(b, images) {
 const conservation=assemble(b,{schema:'gate5-observed-deltas-v1',status:'HOLD',executable:false,baseline_sha256:digest(b),changes:[],new_role_intents:[]});
 const project=b.environment.project,env=b.environment.environment;
 if(project.name!=='zbm-zbc'||env.name!=='production')fail('TARGET_NAMES');
 const operations=[];
 for(const name of owners){
  const s=b.services.find(s=>s.service.name===name);
  if(!s||s.volumes.length!==1||s.volumes[0].mountPath!=='/data')fail('OWNER_VOLUME');
  const image=images.images?.[name];
  if(image?.source_commit!=='f5fa44b6ab9ae9841188b44ec87a1f89fc63c1c3'||!new RegExp('^ghcr.io/darksiedceo/zbm-gate5-'+name+'@sha256:[0-9a-f]{64}$').test(image.image))fail('IMAGE_PIN');
  const identity={projectId:project.id,environmentId:env.id,serviceId:s.service.id,staged:true};
  operations.push({role:name,phase:'closed-owner-rollout',before_sha256:digest(s),
   source:{tool:'connect_service_source',arguments:{...identity,image:image.image}},
   settings:{tool:'update_service',arguments:{...identity,drainingSeconds:45,overlapSeconds:0,restartPolicyType:'NEVER'}},
   preserve:{volume:s.volumes[0],volumeMounts:s.config.volumeMounts,variableNames:s.variableNames,domains:s.domains,tcpProxies:s.tcpProxies},
   schedule_observation:Object.hasOwn(s.config.volumeMounts[s.volumes[0].id],'backupSchedules')?'EXPOSED':'UNKNOWN_NOT_EMPTY'});
 }
 return {schema:'gate5-targeted-stage-plan-v1',status:'HOLD',dry_run:true,provider_writes:0,
  baseline_sha256:conservation.baseline_sha256,image_manifest_sha256:digest(images),operations,
  untouched_services:b.services.filter(s=>!owners.includes(s.service.name)).map(s=>({id:s.service.id,name:s.service.name,sha256:digest(s)})),
  forbidden_operations:['update_volume','delete_volume','delete_service','accept_deploy','whole-project-apply'],
  pid_decision:{status:'FOUNDER_DECISION_REQUIRED',proposal:'Accept platform-managed PID limit for first client; do not claim Docker PID caps are enforced on Railway.',alternative:'HOLD until a supported per-container PID cap is established.'},
  remaining:['fresh baseline/date and rollback evidence at sitting','approved CPU/RAM and monthly cost','new roles and volumes provisioned with explicit approval; rebind their returned IDs','secure variable provisioning and routing phase','explicit cron/first-due approval and readback','portal publication and service approval after B2 proof','founder-approved PID policy'],
  execution_rule:'Stage one approved service phase only, inspect staged diff, then separate founder approval before accept-deploy. Never pass this document itself to an API.'};
}
// Compare actual staged readback: fail on every unexpected config mutation,
// including deletion of an unrecognized field. Source selectors alone normalize.
export function verifyReadback(before,after,op){
 const a=structuredClone(after),e=structuredClone(before);
 if(op.before_sha256!==digest(before)||op.source.arguments.serviceId!==before.service.id)fail('BEFORE_BINDING');
 if(a.service.id!==e.service.id||a.service.name!==e.service.name||digest(a.environment)!==digest(e.environment))fail('IDENTITY_CHANGED');
 const source=a.config.source;
 if(source?.image!==op.source.arguments.image||['repo','branch','commitSha'].some(k=>source[k]!=null))fail('SOURCE_NOT_PINNED');
 // Keep unknown source fields: they must equal the original observations.
 const old=structuredClone(e.config.source),actual=structuredClone(source);
 for(const k of ['repo','branch','commitSha','image']){delete old[k];delete actual[k];}
 if(digest(old)!==digest(actual))fail('UNKNOWN_SOURCE_CHANGED');
 e.config.source=source;
 Object.assign(e.config.deploy,{drainingSeconds:45,overlapSeconds:0,restartPolicyType:'NEVER'});
 if(digest(e.config)!==digest(a.config))fail('UNEXPECTED_CONFIG_CHANGE');
 for(const key of ['volumes','domains','tcpProxies','variableNames'])if(digest(e[key])!==digest(a[key]))fail('PRESERVATION_FAILED');
 return true;
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
 try{
  if(process.argv.length!==4)fail('USAGE_BASELINE_IMAGE_MANIFEST');
  console.log(JSON.stringify(plan(JSON.parse(readFileSync(process.argv[2])),JSON.parse(readFileSync(process.argv[3]))),null,2));
 }catch(e){console.error('APPLY_PLAN_REFUSED: '+(e instanceof SyntaxError?'INVALID_JSON':e.message));process.exitCode=2;}
}
