// Pure Railway3.13.1 constructors supplied by caller. No CLI/API/network/apply.
// Returns a PARTIAL definition set, never a project safe to apply by itself.
import {createHash} from 'node:crypto';
export function buildSdkDefinitions(proposal, registryImages, existingVariableNames, sdk) {
  const refuse = code => { throw new Error(code); };
  if (proposal?.schema !== 'gate5-non-sdk-binding-proposal-v1' || proposal.status !== 'HOLD' || proposal.executable !== false) refuse('PROPOSAL_REQUIRED');
  if (!Array.isArray(proposal.blockers) || proposal.blockers.some(x => ['RUNTIME_PRODUCTION_LIMITS_UNRESOLVED','COMPLIANCE_INITIALIZATION_UNRESOLVED'].includes(x))) refuse('IMPLEMENTATION_EVIDENCE_REQUIRED');
  const names = ['ledger','legal','compliance','finance','seo','coordinator','observer','backup-trigger'];
  if (JSON.stringify(Object.keys(proposal.roles).sort()) !== JSON.stringify([...names].sort())) refuse('FIXED_ROLES');
  const policyText=proposal.roles.ledger.disabled_settings.BACKUP_CAPACITY_POLICY;
  let policy;
  try {policy=JSON.parse(policyText);} catch {refuse('CAPACITY_POLICY_SCHEMA');}
  const members=['compliance','finance','ledger','legal','seo'];
  if (!policy || JSON.stringify(Object.keys(policy).sort())!==JSON.stringify(['sources','version']) || policy.version!==1 || !policy.sources || JSON.stringify(Object.keys(policy.sources).sort())!==JSON.stringify(members)) refuse('CAPACITY_POLICY_SCHEMA');
  for (const member of members) {
    const value=policy.sources[member], ceiling=['ledger','compliance'].includes(member)?2097152:524288;
    if (!Number.isSafeInteger(value) || value<1 || value>ceiling) refuse('APPLICATION_RECOVERY_CAPACITY_UNQUALIFIED');
  }
  if (policyText!==JSON.stringify({sources:Object.fromEntries(members.map(m=>[m,policy.sources[m]])),version:1})) refuse('CAPACITY_POLICY_CANONICAL');
  const definitions = [], identities = {};
  for (const name of names) {
    const row=proposal.roles[name], image=registryImages[name];
    const policy=row.disabled_settings.BACKUP_CAPACITY_POLICY;
    if (names.indexOf(name)<6) {
      if (typeof policy!=='string' || policy.length>2048 || createHash('sha256').update(policy).digest('hex')!==proposal.capacity_policy_sha256) refuse('CAPACITY_POLICY_MISMATCH');
    } else if (policy!==undefined) refuse('CAPACITY_POLICY_ROLE');
    if (typeof image !== 'string' || !/^[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}$/.test(image) || !image.endsWith('@'+row.approved_image_digest)) refuse('PINNED_PULLABLE_IMAGE_REQUIRED');
    const prior=existingVariableNames[name];
    if (!Array.isArray(prior) || prior.some(x=>typeof x!=='string'||!/^[A-Z][A-Z0-9_]{0,100}$/.test(x)) || new Set(prior).size!==prior.length) refuse('VARIABLE_NAME_INVENTORY_REQUIRED');
    const env=Object.fromEntries([...new Set([...prior,...row.required_variable_names])].map(k=>[k,sdk.preserve()]));
    Object.assign(env,row.disabled_settings);
    if (env.BACKUP_LAUNCH_ENABLED!=='0') refuse('DISABLED_ONLY');
    const deploy={restartPolicyType:'NEVER',overlapSeconds:0,drainingSeconds:45,multiRegionConfig:{'us-west2':{numReplicas:1}},limitOverride:{containers:{cpu:row.resources_requested_not_applied.vcpus,memoryBytes:row.resources_requested_not_applied.memory_bytes}},cronSchedule:null};
    // Never replace owner image ENTRYPOINT: its secret-file bootstrap is required.
    if (!['ledger','legal','compliance','finance','seo'].includes(name)) deploy.startCommand=row.entrypoint_contract.entrypoint.join(' ');
    const volumeMounts={};
    if (row.volume) {
      if (!/^[0-9a-f-]{36}$/.test(row.volume.id) || !['/data','/volume'].includes(row.volume.mount)) refuse('VOLUME_BINDING_REQUIRED');
      volumeMounts[row.volume.id]={mountPath:row.volume.mount,backupSchedules:[]};
      deploy.requiredMountPath=row.volume.mount;
    }
    definitions.push(sdk.service(name,{source:sdk.image(image),deploy,env,volumeMounts}));
    identities[name]={...row.identity,volume:row.volume,capacity_policy_sha256:policy===undefined?null:proposal.capacity_policy_sha256,pid_limit_requires_separate_supported_binding:row.resources_requested_not_applied.pid_limit};
  }
  // Routing is a separate cutover fragment, not mixed into disabled preparation.
  const routing={};
  for (const route of proposal.central_route_proposals) {
    if (route.proposed_url!=='http://coordinator.railway.internal:27001') refuse('CENTRAL_ROUTE_REQUIRED');
    (routing[route.client]??={})[route.variable_name]=route.proposed_url;
  }
  const gateway=proposal.gateway_private_name_proposal;
  if (gateway.proposed_name!=='coordinator') refuse('GATEWAY_PRIVATE_NAME');
  (routing['stripe-gateway']??={})[gateway.variable_name]=gateway.proposed_name;
  return {status:'HOLD',executable:false,partial_only:true,sdk_version:'3.13.1',definitions,identity_sidecar:identities,cutover_variable_fragments:routing,disabled_schedule_proposal:proposal.schedule_proposal,blockers:['SDK_PRUNES_NULL_CRON_EXISTING_SCHEDULE_REMOVAL_UNPROVEN','NOT_A_COMPLETE_PROJECT_DO_NOT_APPLY','EXISTING_IDENTITY_BINDINGS_REQUIRE_LIVE_PLAN_RECONCILIATION','PID_LIMIT_NOT_SUPPORTED_BY_SDK_DEPLOY_TYPE','NEW_ADMIN_VOLUMES_REQUIRE_SEPARATE_APPROVED_PROVISIONING','CRON_AND_BACKUP_ENABLE_REQUIRE_EXPLICIT_CUTOVER','PRODUCTION_APPROVAL_AND_EVIDENCE_NOT_ESTABLISHED']};
}
