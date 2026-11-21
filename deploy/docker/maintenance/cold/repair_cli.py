"""Fixed authenticated cold repair orchestration; no caller health/path inputs.

The coordinator constructs this adapter over its retained fixed roster. This
module is also the CLI command contract; secret custody remains parent stdin.
"""
import copy,time
from owner_wire import Refused,MEMBERS
from repair_state import digest

def owner_resolution(c,member,operation,command):
 v=c.status(member,operation);record=v['operations'].get(operation)
 if record is None:raise Refused('original owner incident missing')
 body={'member':member,'boot':c.doc['boots'][member],'run':c.doc['run'],'incident':operation,'incident_sha256':digest(record),'command':command}
 # Fixed identity is journaled by owner before dispatch. A lost submit response
 # may only poll the same incident; it cannot create another repair identity.
 try:c.clients[member].call('/repair',body)
 except Refused:pass
 end=time.monotonic()+110
 while time.monotonic()<end:
  out=c.clients[member].call('/repair-status',{'member':member,'boot':body['boot'],'incident':operation,'command':command})
  if out.get('identity')!={'member':member,'run':body['run'],'operation':operation} or out.get('incident_sha256')!=body['incident_sha256']:raise Refused('repair original binding mismatch')
  p=out.get('repair')
  if p is not None:
   if p.get('boot')!=body['boot'] or p.get('command')!=command:raise Refused('repair boot/command mismatch')
   if p.get('phase')=='RESOLVED':
    result=p.get('result')
    if not isinstance(result,dict) or any(result.get(k)!=body[k] for k in ('member','boot','run')) or result.get('action')!=record['action']:raise Refused('repair completion binding')
    return result
   if p.get('phase')=='UNRESOLVED':raise Refused('repair remains unresolved')
  time.sleep(.05)
 raise Refused('repair completion unknown')

def retry_upload(c):
 if c.doc['phase']!='FAILED' or c.doc.get('repair_intent') is not None:raise Refused('no fresh supported upload incident')
 for member in MEMBERS:
  if c.status(member)['phase']!='OPEN':raise Refused('original business recovery required')
 original=digest(c.doc)
 c.update(repair_intent={'command':'retry-upload','run':c.doc['run'],'incident_sha256':original,'phase':'APPLYING'})
 uploads={}
 try:
  for member in MEMBERS:
   op=c.doc['run']+':'+member+':upload:1'
   v=c.status(member,op);rec=v['operations'].get(op)
   if rec is None:uploads[member]=c.command(member,'upload')
   elif rec['phase']=='DONE':uploads[member]=rec['receipt']['result']
   else:
    result=owner_resolution(c,member,op,'retry-upload');uploads[member]=result['result']
    rs=copy.deepcopy(c.doc.get('repair_resolutions',{}));rs[op]=result;c.update(repair_resolutions=rs)
  proof=c.catalog.create(c.doc['run'],uploads)
  for member in MEMBERS:
   if c.command(member,'verify_remote')!=uploads[member]:raise Refused('fresh repaired remote mismatch')
  c.update(phase='REMOTE_VERIFIED',catalog_proof=proof);c.catalog.publish(proof)
  cleaned={}
  for member in MEMBERS:
   cleaned[member]=c.command(member,'cleanup');c.update(phase='CLEANING',cleaned=cleaned)
  intent=copy.deepcopy(c.doc['repair_intent']);intent['phase']='RESOLVED'
  c.update(phase='COMPLETE',cleaned=cleaned,repair_intent=intent)
  return {'phase':'COMPLETE','run':c.doc['run'],'catalog_sha256':proof['catalog_sha256'],'original_incident_sha256':original}
 except BaseException:
  intent=copy.deepcopy(c.doc['repair_intent']);intent['phase']='UNRESOLVED'
  c.update(phase='FAILED',repair_intent=intent)
  # Business publication remains untouched, even if repairing transfer fails.
  raise

def originals_stage(c,member,incident,command,boot):
 op,record=incident
 body={'member':member,'boot':boot,'run':c.doc['run'],'incident':op,'incident_sha256':digest(record),'command':command}
 try:c.clients[member].call('/repair',body)
 except Refused:pass
 end=time.monotonic()+110
 while time.monotonic()<end:
  v=c.clients[member].call('/repair-status',{'member':member,'boot':boot,'incident':op,'command':command})
  if v.get('identity')!={'member':member,'run':c.doc['run'],'operation':op} or v.get('incident_sha256')!=digest(record):raise Refused('original repair incident mismatch')
  r=v.get('repair')
  if r:
   if r['boot']!=boot or r['command']!=command:raise Refused('original repair current boot mismatch')
   if r['phase']=='UNRESOLVED':raise Refused('originals recovery requires further evidence')
   if r['phase']=='RESOLVED':
    result=r['result']
    if result.get('member')!=member or result.get('boot')!=boot or result.get('run')!=c.doc['run']:raise Refused('original repair result context')
    return result
  time.sleep(.05)
 raise Refused('original repair completion unknown')

def resume_originals(c,*,sealed=False):
 """One explicit command, all owners quiesced before Ledger-first recovery.

No generic recovered bit or old operation is cleared. Backup remains locked;
this command only restores verified original application availability.
 """
 from coordinator import STOP,START
 if c.doc.get('original_recovery') is not None:raise Refused('existing recovery intent unresolved; no automatic replay')
 c.front.close();boots={};incidents={}
 for m in MEMBERS:
  v=c.clients[m].call('/status',{'member':m,'boot':'discover'})
  if v.get('member')!=m or v.get('run')!=c.doc['run'] or not v.get('operations'):raise Refused('original owner/run unavailable')
  # Select immutable last recorded operation by insertion order from the owner's
  # canonical wire map is unsafe: keys are sorted. Select from coordinator's
  # retained exact operation identities instead, or sole unresolved owner op.
  unresolved=[k for k,r in v['operations'].items() if r['phase'] in ('FAILED','PENDING')]
  candidates=unresolved or [k for k in c.doc['receipts'] if k in v['operations']]
  if not candidates:raise Refused('no original incident identity')
  op=candidates[-1]
  exact=c.clients[m].call('/status',{'member':m,'boot':v['boot'],'operation':op})
  record=exact['operations'][op]
  boots[m]=v['boot'];incidents[m]=(op,record)
 intent={'phase':'APPLYING','run':c.doc['run'],'original_sha256':digest(c.doc),'old_boots':c.doc['boots'],'new_boots':boots,'incidents':{m:digest(r) for m,(_,r) in incidents.items()},'receipts':{}}
 c.update(original_recovery=intent)
 try:
  for action,order in ([('quiesce-originals',STOP)]+([('verify-sealed',MEMBERS)] if sealed else [])+[('resume-originals',START)]):
   for m in order:
    result=originals_stage(c,m,incidents[m],action,boots[m]);intent=copy.deepcopy(c.doc['original_recovery']);intent['receipts'][m+':'+action]=result;c.update(original_recovery=intent)
   if action=='verify-sealed':
    if set(c.doc.get('seals',{}))!=set(MEMBERS):raise Refused('no complete original seal set')
    for m in MEMBERS:
     if c.doc['original_recovery']['receipts'][m+':verify-sealed']['seal_sha256']!=digest(c.doc['seals'][m]):raise Refused('whole seal context mismatch')
  for m in MEMBERS:
   v=c.clients[m].call('/status',{'member':m,'boot':boots[m]})
   if v['phase']!='REPAIR_VERIFIED_CLOSED' or v['boot']!=boots[m] or type(v['child_pid'])is not int:raise Refused('not all original applications verified')
  for m in MEMBERS:originals_stage(c,m,incidents[m],'publish-originals',boots[m])
  for m in MEMBERS:
   v=c.clients[m].call('/status',{'member':m,'boot':boots[m]})
   if v['phase']!='OPEN_REPAIRED' or v['boot']!=boots[m] or type(v['child_pid'])is not int:raise Refused('original publication incomplete')
  intent=copy.deepcopy(c.doc['original_recovery']);intent['phase']='RESOLVED'
  c.update(original_recovery=intent,phase='OPEN_REPAIRED');c.front.open()
  return {'run':c.doc['run'],'phase':'OPEN_REPAIRED','backup':'REPAIR_REQUIRED','original_incident_sha256':intent['original_sha256']}
 except BaseException:
  c.front.close();intent=copy.deepcopy(c.doc['original_recovery']);intent['phase']='UNRESOLVED';c.update(original_recovery=intent,phase='RECOVERY_REQUIRED');raise

def finish_cleanup(c):
 """Resume cleanup only from this retained complete catalog; never invent one."""
 proof=c.doc.get('catalog_proof')
 if proof is None:raise Refused('no retained catalog; upload repair required')
 if c.catalog.read(c.doc['run'])!=proof:raise Refused('catalog ownership unavailable')
 cleaned=copy.deepcopy(c.doc.get('cleaned',{}))
 # Fresh verification of every member precedes every subsequent deletion stage.
 for m in MEMBERS:
  op=c.doc['run']+':'+m+':upload:1'
  result=owner_resolution(c,m,op,'verify-remote')
  if result['result']!=proof['catalog']['members'][m]:raise Refused('fresh cleanup remote proof mismatch')
 c.update(phase='REMOTE_VERIFIED');c.catalog.publish(proof)
 for m in MEMBERS:
  op=c.doc['run']+':'+m+':cleanup:1';v=c.status(m,op);rec=v['operations'].get(op)
  if rec is None:cleaned[m]=c.command(m,'cleanup')
  elif rec['phase']=='DONE':cleaned[m]=rec['receipt']['result']
  else:
   result=owner_resolution(c,m,op,'finish-cleanup');cleaned[m]=result['result']
   resolutions=copy.deepcopy(c.doc.get('repair_resolutions',{}));resolutions[op]=result;c.update(repair_resolutions=resolutions)
  c.update(phase='CLEANING',cleaned=cleaned)
 c.update(phase='COMPLETE',cleaned=cleaned);return {'run':c.doc['run'],'phase':'COMPLETE','catalog_sha256':proof['catalog_sha256']}

def operator_rearm(client):
 """One exact submit, then authenticated evidence reads. Never resubmit on loss."""
 import hashlib,re
 from owner_wire import canonical
 body={'command':'rearm-idle'}
 try:client.call('/repair',body)
 except Refused:pass
 end=time.monotonic()+600;identity=None
 while time.monotonic()<end:
  try:value=client.call('/repair-status',body)
  except Refused:
   time.sleep(.2);continue # bounded read-only polling; never another submit
  if type(value)is not dict or value.get('schema')!='idle-rearm-v1':raise Refused('rearm evidence unavailable; restore fallback')
  base={k:value.get(k) for k in ('schema','run','old_boots','boots','last_success','original_sha256')}
  if value.get('identity')!=hashlib.sha256(canonical(base)).hexdigest():raise Refused('rearm evidence identity')
  for name in ('boots','old_boots'):
   roster=value.get(name)
   if type(roster)is not dict or set(roster)!=set(MEMBERS) or any(type(b)is not str or re.fullmatch('[0-9a-f]{32}',b)is None for b in roster.values()):raise Refused('rearm roster evidence')
  if identity is None:identity=value['identity']
  if value['identity']!=identity:raise Refused('rearm identity changed')
  phase=value.get('phase')
  if phase=='DONE':
   if any(type(value.get(k))is not dict or set(value[k])!=set(MEMBERS) for k in ('locks','verified')):raise Refused('incomplete rearm evidence')
   observed=value.get('observer',{});monitor=observed.get('monitoring',{});prior=observed.get('current',{})
   if monitor.get('state')!='ARMED' or type(monitor.get('expected_due'))is not int or type(prior.get('due'))is not int or monitor['expected_due']<=prior['due']:raise Refused('rearm observer due evidence')
   return value
  if phase not in ('PREPARED','LOCKED','VERIFIED'):raise Refused('rearm unresolved; restore fallback required')
  time.sleep(.2)
 raise Refused('rearm status deadline; original intent retained; restore fallback required')

def operator_command(client,command):
 if command=='rearm-idle':return operator_rearm(client)
 if command=='finish-repaired-run':
  from repaired_retirement import operator_finish
  return operator_finish(client)
 if command=='finish-advance':
  from schedule_advance import operator_finish
  return operator_finish(client)
 if command not in ('inspect','retry-upload','resume-originals','resume-sealed','finish-cleanup','recover-catalog'):raise Refused('fixed command required')
 status=client.call('/repair-status',{'kind':'repair_inspect'})
 if command=='inspect':return status
 body={'command':command,'run':status['run'],'incident_sha256':status['incident_sha256']}
 if command=='finish-cleanup' and status.get('repair')is not None:
  prior=status['repair'].get('request',{})
  if set(prior)!={'command','run','incident_sha256'} or prior['command']!=command or prior['run']!=status['run']:raise Refused('different retained cleanup command')
  body=copy.deepcopy(prior)
 client.call('/repair',body) # Lost acknowledgement requires inspect, never new identity.
 end=time.monotonic()+600
 while time.monotonic()<end:
  status=client.call('/repair-status',{'kind':'repair_inspect'});r=status.get('repair')
  if r and r.get('request')==body and r.get('phase')in ('RESOLVED','UNRESOLVED'):
   if r['phase']!='RESOLVED':raise Refused('repair unresolved')
   return status
  time.sleep(.2)
 raise Refused('repair status deadline; original intent retained')

def main():
 """Local operator command: token arrives through bounded stdin, never argv."""
 import sys
 from owner_wire import Client,decode,canonical
 if len(sys.argv)!=2:raise Refused('fixed command required')
 raw=sys.stdin.buffer.readline(1025)
 if len(raw)>1024:raise Refused('credential input bound')
 value=decode(raw)
 if set(value)!={'coord_token'}:raise Refused('fixed credential input')
 client=Client('coordinator','127.0.0.1',27002,value['coord_token'])
 print(canonical(operator_command(client,sys.argv[1])).decode())
if __name__=='__main__':
 try:main()
 except Exception as exc:
  import sys
  print('REPAIR_REFUSED: '+type(exc).__name__,file=sys.stderr);raise SystemExit(2)
