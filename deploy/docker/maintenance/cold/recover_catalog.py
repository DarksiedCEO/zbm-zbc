"""One sameboot locator-present catalog repair, followed by exact cleanup."""
import copy
from owner_wire import Refused,MEMBERS
from repair_state import digest
from metadata_retirement import read,save
import catalog_resolution as cr

def preflight(c):
 if c.doc.get('phase')!='FAILED' or c.doc.get('alert',{}).get('stage')not in ('catalog','verify_remote','publish') or c.doc.get('repair_intent') is not None or c.doc.get('cleaned'):raise Refused('recover catalog exact failed stage')
 run=c.doc['run'];obj,value,artifacts,proof=cr.retained(c.catalog,run)
 if value['seals']!=c.doc['seals'] or c.doc.get('catalog_proof')not in (None,proof):raise Refused('catalog source context changed')
 members(c,value)
 return obj,value,artifacts,proof

def members(c,value):
 run=c.doc['run']
 for m in MEMBERS:
  op=run+':'+m+':upload:1';v=c.status(m,op)
  if v['boot']!=c.doc['boots'][m] or v['phase']!='OPEN' or v['run']!=run or type(v.get('child_pid'))is not int:raise Refused('catalog same live boot required')
  ops=v['operations'];receipt={'member':m,'boot':v['boot'],'run':run,'action':'upload','result':value['uploads'][m]}
  if op not in ops or ops[op].get('phase')!='DONE' or ops[op].get('receipt')!=receipt:raise Refused('all original uploads DONE required')
  if any(r['phase']!='DONE' or r['action']in ('cleanup','retire') for r in ops.values()):raise Refused('unknown owner backend or cleanup unsupported')

def preserve(c,request):
 preflight(c);p=c.root/'catalog-repair-original.json'
 if p.exists() or p.is_symlink() or p.with_name(p.name+'.tmp').exists():raise Refused('catalog repair custody exists')
 if digest(c.doc)!=request['incident_sha256']:raise Refused('catalog original incident mismatch')
 save(p,c.doc)

def recover(c):
 obj,value,artifacts,proof=preflight(c);old=cr.b.decode(read(c.root/'catalog-repair-original.json'));incident=digest(old)
 if incident!=c.doc['repair_rpc']['request']['incident_sha256'] or old['run']!=c.doc['run']:raise Refused('catalog original custody mismatch')
 c.update(repair_intent={'schema':'catalog-repair-v1','phase':'APPLYING','original_sha256':incident,'run':old['run']})
 try:
  # Fresh backend calls; existing verify_remote DONE never substitutes for this.
  if obj.recover()!=proof['locator']:raise Refused('fresh catalog locator changed')
  if cr.retained(c.catalog,old['run'])[2:]!=(artifacts,proof):raise Refused('catalog originals changed during read')
  members(c,value) # same live identities again after the bounded remote read
  cr.publish(c.catalog,old['run'],incident,artifacts,proof)
  c.update(phase='REMOTE_VERIFIED',catalog_proof=proof);c.catalog.publish(proof)
  cleaned={}
  for m in MEMBERS:
   got=c.command(m,'cleanup');expected={'schema':'b2-cleanup-v1','deleted':True,'run':old['run'],'member':m,'catalog_sha256':proof['catalog_sha256'],'catalog_snapshot':proof['catalog_snapshot'],'locator_sha256':digest(proof['locator'])}
   if got!=expected:raise Refused('recovered catalog cleanup mismatch')
   cleaned[m]=got;c.update(phase='CLEANING',cleaned=cleaned)
  intent=copy.deepcopy(c.doc['repair_intent']);intent['phase']='RESOLVED';c.update(phase='COMPLETE',repair_intent=intent,cleaned=cleaned)
  return {'schema':'catalog-repair-v1','phase':'COMPLETE','run':old['run'],'catalog_sha256':proof['catalog_sha256'],'original_incident_sha256':incident}
 except BaseException:
  intent=copy.deepcopy(c.doc['repair_intent']);intent['phase']='UNRESOLVED';c.update(phase='FAILED',repair_intent=intent);raise
