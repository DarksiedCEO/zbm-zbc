import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {buildSdkDefinitions} from '../../deploy/gate5/binding_sdk.mjs';
// Optional local read-only SDK path; no installation or provider access.
const base=process.argv[2];assert.ok(base,'installed railway package path required');
assert.equal(JSON.parse(readFileSync(base+'/package.json','utf8')).version,'3.13.1');
const sdk=await import(pathToFileURL(base+'/dist/iac/index.js'));
const names=['ledger','legal','compliance','finance','seo','coordinator','observer','backup-trigger'];
const p={schema:'gate5-non-sdk-binding-proposal-v1',status:'HOLD',executable:false,blockers:[],roles:{},central_route_proposals:[{client:'client-portal',variable_name:'PORTAL_LEGAL_URL',proposed_url:'http://coordinator.railway.internal:27001'}],gateway_private_name_proposal:{proposed_name:'coordinator',variable_name:'STRIPE_GATEWAY_FINANCE_PRIVATE_NAME'},schedule_proposal:{enabled:false,cron_utc:'0 11 * * *'}};
const policy=JSON.stringify({sources:{compliance:524288,finance:524288,ledger:524288,legal:524288,seo:524288},version:1});
p.capacity_policy_sha256=createHash('sha256').update(policy).digest('hex');
const images={},vars={};
for(const [i,n] of names.entries()) {
 p.roles[n]={approved_image_digest:'sha256:'+'b'.repeat(64),identity:{service_id:null,mode:'synthetic-test'},volume:n==='backup-trigger'?null:{id:'11111111-1111-4111-8111-'+String(i+1).padStart(12,'0'),mount:i<5?'/data':'/volume'},required_variable_names:['BACKUP_COORD_TOKEN','BACKUP_LAUNCH_ENABLED'],disabled_settings:{BACKUP_LAUNCH_ENABLED:'0'},resources_requested_not_applied:{vcpus:1,memory_bytes:268435456,pid_limit:128},entrypoint_contract:{entrypoint:['/opt/venv/bin/python','-I','-B','/opt/zbm-maintenance/admin_launcher.py',n==='backup-trigger'?'trigger':n]}};
 if(i<6) p.roles[n].disabled_settings.BACKUP_CAPACITY_POLICY=policy;
 images[n]='ghcr.io/synthetic/gate5@'+p.roles[n].approved_image_digest;vars[n]=['EXISTING_SERVICE_TOKEN'];
}
const out=buildSdkDefinitions(p,images,vars,sdk);assert.equal(out.executable,false);assert.equal(out.definitions.length,8);
for(const node of out.definitions) {
 assert.equal(node.deploy.cronSchedule,undefined);assert.equal(node.variables.BACKUP_LAUNCH_ENABLED.value,'0');assert.equal(node.variables.EXISTING_SERVICE_TOKEN.type,'preserve');
 assert.equal(node.deploy.limitOverride.containers.memoryBytes,268435456);
}
assert.equal(out.definitions[0].deploy.startCommand,undefined);
assert.match(out.definitions[7].deploy.startCommand,/admin_launcher.py trigger$/);
assert.equal(out.cutover_variable_fragments['client-portal'].PORTAL_LEGAL_URL,'http://coordinator.railway.internal:27001');
assert.throws(()=>buildSdkDefinitions({...p,blockers:['RUNTIME_PRODUCTION_LIMITS_UNRESOLVED']},images,vars,sdk),/IMPLEMENTATION/);
assert.throws(()=>buildSdkDefinitions(p,{...images,ledger:'ghcr.io/synthetic/gate5:latest'},vars,sdk),/PINNED/);
assert.throws(()=>buildSdkDefinitions(p,images,{},sdk),/VARIABLE/);
const bad=structuredClone(p);bad.roles.ledger.disabled_settings.BACKUP_CAPACITY_POLICY+=' ';
assert.throws(()=>buildSdkDefinitions(bad,images,vars,sdk),/CAPACITY_POLICY_/);
console.log('PASS: installed Railway3.13.1 constructors; disabled nodes, preserved names, fixed image/volume/routing and refusal checks. No apply.');
