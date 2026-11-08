import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {plan,verifyReadback} from '../../deploy/gate5/apply_plan.mjs';
const names=['ledger','legal','finance','seo'];
const services=names.map((name,i)=>({service:{name,id:'s'+i},environment:{id:'env',name:'production'},staged:null,stagedChanges:[],config:{source:{repo:'example/repo',branch:'main'},deploy:{cronSchedule:'0 1 * * *',future:{keep:true}},volumeMounts:{['v'+i]:{mountPath:'/data',backupSchedules:['daily']}}},volumes:[{id:'v'+i,name:name+'-data',mountPath:'/data',sizeMB:5000,region:'us-west2'}],variableNames:['TOKEN'],domains:{serviceDomains:[],customDomains:[]},tcpProxies:[]}));
const b={schema:1,captured_at:'2026-11-07T09:00:00Z',limitations:[],services,environment:{project:{id:'p',name:'zbm-zbc'},environment:{id:'env',name:'production'},staged:null,buckets:[],sharedVariableNames:[],services:services.map(s=>({...s.service,volumeMounts:s.volumes.map(v=>({volumeId:v.id,volumeName:v.name,mountPath:v.mountPath}))})),volumes:services.map(s=>({...s.volumes[0],mountedOn:{serviceId:s.service.id,serviceName:s.service.name,mountPath:'/data'}}))}};
const images=JSON.parse(readFileSync(new URL('../../deploy/gate5/release_images.json',import.meta.url)));
const out=plan(b,images);assert.equal(out.operations.length,4);assert.equal(out.provider_writes,0);
for(const op of out.operations){assert.equal(op.source.arguments.staged,true);assert.equal(op.settings.arguments.staged,true);assert.equal(Object.hasOwn(op.settings.arguments,'cronSchedule'),false);}
const before=b.services[0],op=out.operations[0],after=structuredClone(before);
after.config.source={image:op.source.arguments.image,repo:null,branch:null};Object.assign(after.config.deploy,{drainingSeconds:45,overlapSeconds:0,restartPolicyType:'NEVER'});
assert.equal(verifyReadback(before,after,op),true);
for(const mutate of [a=>a.config.volumeMounts.v0.backupSchedules=[],a=>a.config.deploy.cronSchedule=null,a=>delete a.config.deploy.future,a=>a.volumes[0].sizeMB=6000,a=>a.variableNames=[],a=>a.config.source.branch='main']){const a=structuredClone(after);mutate(a);assert.throws(()=>verifyReadback(before,a,op));}
const bad=structuredClone(b);bad.environment.project.name='zbestmedia-backend';assert.throws(()=>plan(bad,images),/TARGET_NAMES/);
console.log('PASS: four scoped staged payloads; schedule, volume, unknown-setting and identity guards; no provider calls.');
