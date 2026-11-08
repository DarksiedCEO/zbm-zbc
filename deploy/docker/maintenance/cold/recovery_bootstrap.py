"""One evidence-preserving recovery bootstrap transition; not backup completion.

Partial transition is closed and unsupported: a fresh-volume restore is required.
No incident adoption, journal reset, or synthetic cleanup proof.
"""
import copy,hashlib,secrets
from owner_wire import Refused,canonical,decode,MEMBERS
from metadata_retirement import read,save

def digest(v):return hashlib.sha256(canonical(v)).hexdigest()
def owner_operations(doc,m):
 run=doc['run'];expected={f'{run}:{m}:start:1',f'{run}:{m}:verify:1',f'{run}:{m}:verify:2'}
 records={k:v for k,v in doc['receipts'].items() if v.get('body',{}).get('member')==m}
 if set(records)!=expected:raise Refused('exact bootstrap operations required')
 result={}
 for op,v in records.items():
  b=v['body'];r=v.get('receipt',{})
  if v.get('phase')!='DONE' or any(r.get(k)!=b[k] for k in ('member','boot','run','action')) or b['boot']!=doc['boots'][m] or b['run']!=run:raise Refused('bootstrap completion binding')
  result[op]={'run':run,'action':b['action'],'phase':'DONE','receipt':r}
 return result

def permit(c,body):
 if set(body)!={'kind','member','boot','run'} or body['kind']!='recovery-bootstrap-permit':raise Refused('bootstrap permit schema')
 if c.doc.get('phase')!='RECOVERY_RETIRING_CLOSED' or decode(read(c.path))!=c.doc:raise Refused('bootstrap durable phase')
 a=decode(read(c.root/'recovery-bootstrap.json'));intent=c.doc.get('recovery_bootstrap')
 if not intent or digest(a)!=intent['archive_sha256'] or a['old']['run']!=body['run'] or body['member']not in MEMBERS or a['old']['boots'][body['member']]!=body['boot']:raise Refused('bootstrap permit identity')
 m=body['member']
 return {'schema':'recovery-bootstrap-v1','member':m,'boot':body['boot'],'run':body['run'],'next_run':a['next_run'],'operations_sha256':digest(owner_operations(a['old'],m)),'recovery_sha256':digest(a['old']['recovery'])}

def advance(c):
 if c.front.opened or c.doc.get('phase')!='RECOVERY_RECONCILED_CLOSED' or c.doc.get('schedule') is not None or c.doc.get('recovery_bootstrap') is not None:raise Refused('closed reconciled recovery required')
 proof=c.doc.get('recovery')
 if type(proof)is not dict or set(proof)!={'selected_catalog','provider','applications','mode','operator_cutoff_used'} or proof['operator_cutoff_used'] is not False:raise Refused('exact recovery evidence required')
 if decode(read(c.path))!=c.doc or set(c.doc['boots'])!=set(MEMBERS):raise Refused('bootstrap durable cohort')
 expected=set()
 for m in MEMBERS:
  ops=owner_operations(c.doc,m);expected.update(ops);v=c.status(m)
  if v.get('phase')!='VERIFIED_CLOSED' or v.get('run')!=c.doc['run'] or type(v.get('child_pid'))is not int or set(v.get('operations',{}))!=set(ops):raise Refused('live closed bootstrap cohort')
  for op,record in ops.items():
   if c.status(m,op)['operations'].get(op)!=record:raise Refused('fresh bootstrap receipt mismatch')
 if set(c.doc['receipts'])!=expected:raise Refused('unexpected bootstrap operation')
 path=c.root/'recovery-bootstrap.json'
 if path.exists() or path.is_symlink() or path.with_name(path.name+'.tmp').exists() or path.with_name(path.name+'.tmp').is_symlink():raise Refused('bootstrap archive already exists')
 a={'schema':'recovery-bootstrap-archive-v1','old':copy.deepcopy(c.doc),'next_run':'cold-'+secrets.token_hex(16)}
 if a['next_run'] in (c.doc['run'],proof['selected_catalog']['set_id']):raise Refused('fresh recovery namespace collision')
 # save refuses existing/temp; archive precedes all owner mutation.
 save(path,a)
 intent={'archive_sha256':digest(a),'old_run':c.doc['run'],'next_run':a['next_run']}
 c.update(phase='RECOVERY_RETIRING_CLOSED',recovery_bootstrap=intent)
 for m in MEMBERS:
  body={'kind':'recovery-bootstrap-permit','member':m,'boot':c.doc['boots'][m],'run':c.doc['run']};p=permit(c,body)
  result=c.clients[m].call('/submit',{'action':'recovery-bootstrap-retire','member':m,'boot':body['boot'],'run':body['run'],'proof_sha256':digest(p)})
  if result!={'member':m,'boot':body['boot'],'proof_sha256':digest(p),'state':'RECOVERY_BOOTSTRAP_RETAINED'}:raise Refused('bootstrap retirement unknown')
 c.update(run=a['next_run'],receipts={},barrier=None,seals={},phase='RECOVERY_READY_TO_PUBLISH',recovery_bootstrap={**intent,'state':'RETAINED'})
 c.counts={}
