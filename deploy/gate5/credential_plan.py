"""Pure custody/preflight proposal. No generation, serialization or apply capability.

Private fields contain sensitive in-memory objects: opaque repr is accidental
logging protection, not isolation from code running in the same Python process.
"""
import hashlib,json,re
from dataclasses import dataclass

class Refused(ValueError):pass
def need(ok):
 if not ok:raise Refused('credential preflight refused')
@dataclass(frozen=True,repr=False)
class Cell:
 presence:str
 custody:str
 _value:object=None
 def __repr__(self):return '<CredentialCell redacted>'
 @classmethod
 def absent(cls):return cls('absent','none')
 @classmethod
 def supplied(cls,value):return cls('present','provided',value)
class Secret:
 __slots__=('_value',)
 def __init__(self,value):self._value=value
 def __repr__(self):return '<Credential redacted>'
class Plan:
 __slots__=('_deltas','_generation','_urls')
 def __init__(self,deltas,generation,urls):self._deltas=deltas;self._generation=tuple(generation);self._urls=dict(urls)
 def __repr__(self):return '<CredentialPlan HOLD; values redacted; no apply capability>'
 def summary(self):
  return {'state':'HOLD_GENERATION_REQUIRED' if self._generation else 'HOLD_NO_WRITER',
          'generation_needed':tuple('.'.join(k) for k in self._generation),
          'proposed_names':tuple('.'.join(k) for k in sorted(self._deltas)),
          'url_reference_names':tuple('.'.join(k) for k in sorted(self._urls))}
 def __reduce_ex__(self,protocol):raise TypeError('credential plan serialization refused')

GENERATED={('legal','LEGAL_LINK_TAG_KEY'),('legal','LEGAL_COMPLIANCE_CALLER_TOKEN'),('finance','FIN_COMPLIANCE_CALLER_TOKEN'),('seo','SEO_COMPLIANCE_CALLER_TOKEN'),('compliance','COMPLIANCE_SERVICE_TOKEN'),('compliance','COMPLIANCE_ANDRE_APPROVAL_TOKEN'),('compliance','COMPLIANCE_ONBOARDING_CALLER_TOKEN'),('compliance','COMPLIANCE_LEGAL_CALLER_TOKEN'),('compliance','LEDGER_SERVICE_TOKEN'),('client-portal','PORTAL_LEGAL_CALLER_TOKEN'),('client-portal','PORTAL_EVIDENCE_KEY'),('coordinator','BACKUP_COORD_TOKEN')}
DIRECT={
 ('legal','LEGAL_SERVICE_TOKEN'):(('finance','FIN_LEGAL_TOKEN'),('compliance','COMPLIANCE_LEGAL_TOKEN'),('client-portal','PORTAL_LEGAL_SERVICE_TOKEN')),
 ('finance','FIN_SERVICE_TOKEN'):(('seo','SEO_FINANCE_TOKEN'),('stripe-gateway','STRIPE_GATEWAY_FINANCE_TOKEN')),
 ('compliance','COMPLIANCE_SERVICE_TOKEN'):(('legal','LEGAL_COMPLIANCE_TOKEN'),('finance','FIN_COMPLIANCE_TOKEN'),('seo','SEO_COMPLIANCE_TOKEN')),
 ('legal','LEGAL_LINK_TAG_KEY'):(('client-portal','PORTAL_LINK_TAG_KEY'),),
 ('detection','ZBM_SERVICE_TOKEN'):(('orchestrator','DETECTION_SERVICE_TOKEN'),)}
CALLERS={
 ('finance','FIN_CALLER_TOKENS'):{'rail_gateway':('stripe-gateway','STRIPE_GATEWAY_CALLER_TOKEN'),'seo_02':('seo','SEO_FINANCE_CALLER_TOKEN'),'onboarding':('finance','FIN_ONBOARDING_CALLER_TOKEN')},
 ('compliance','COMPLIANCE_CALLER_TOKENS'):{'onboarding':('compliance','COMPLIANCE_ONBOARDING_CALLER_TOKEN'),'finance_31':('finance','FIN_COMPLIANCE_CALLER_TOKEN'),'legal_37':('legal','LEGAL_COMPLIANCE_CALLER_TOKEN'),'seo_02':('seo','SEO_COMPLIANCE_CALLER_TOKEN')},
 ('legal','LEGAL_CALLER_TOKENS'):{'finance_31':('finance','FIN_LEGAL_CALLER_TOKEN'),'onboarding':('legal','LEGAL_ONBOARDING_CALLER_TOKEN'),'compliance_38':('compliance','COMPLIANCE_LEGAL_CALLER_TOKEN'),'client_portal':('client-portal','PORTAL_LEGAL_CALLER_TOKEN')},
 ('seo','SEO_CALLER_TOKENS'):{}}
OWNERS=('ledger','legal','compliance','finance','seo')
for name in ('BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY'):
 DIRECT[('coordinator',name)]=tuple((s,name) for s in OWNERS)
DIRECT[('coordinator','BACKUP_COORD_TOKEN')]=tuple((s,'BACKUP_COORD_TOKEN') for s in (*OWNERS,'observer','backup-trigger'))
LEDGER={('finance','LEDGER_SERVICE_TOKEN'):('finance-py','finance'),('legal','LEDGER_SERVICE_TOKEN'):('legal-py','legal'),('seo','LEDGER_SERVICE_TOKEN'):('seo-py','seo'),('orchestrator','LEDGER_SERVICE_TOKEN'):('orchestrator-go','revenue_recovery'),('detection','DETECTION_LEDGER_TOKEN'):('detection-py','rr_store'),('compliance','LEDGER_SERVICE_TOKEN'):('compliance-py','compliance')}
LEDGER_MAP=('ledger','LEDGER_CALLERS')
FORBIDDEN={('backup-trigger',n) for n in ('BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_RESEND_API_KEY','BACKUP_RECOVERY_STRIPE_KEY','FIN_SERVICE_TOKEN','FIN_RAIL_GATEWAY_CALLER_TOKEN')}
FORBIDDEN|={('observer',n) for n in ('BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_RECOVERY_STRIPE_KEY')}
FORBIDDEN|={('coordinator',n) for n in ('BACKUP_RECOVERY_STRIPE_KEY','FIN_SERVICE_TOKEN','FIN_RAIL_GATEWAY_CALLER_TOKEN','BACKUP_RESEND_API_KEY')}
SINGLE={('observer','BACKUP_RESEND_API_KEY')}
ROLES=GENERATED|set(DIRECT)|set(LEDGER)|SINGLE|{r for m in CALLERS.values() for r in m.values()}
REQUIRED=ROLES|set(CALLERS)|{LEDGER_MAP}|FORBIDDEN|{d for dests in DIRECT.values() for d in dests}
CENTRAL='http://${{coordinator.RAILWAY_PRIVATE_DOMAIN}}:27001'
URLS={('client-portal','PORTAL_LEGAL_URL'):CENTRAL,('stripe-gateway','STRIPE_GATEWAY_FINANCE_URL'):CENTRAL,('orchestrator','LEDGER_SERVICE_URL'):CENTRAL,('detection','DETECTION_LEDGER_URL'):CENTRAL,('detection','DETECTION_COMPLIANCE_URL'):CENTRAL,('stripe-gateway','STRIPE_GATEWAY_FINANCE_PRIVATE_NAME'):'coordinator'}

# Application credential triples require URLs before the owner can rewrite them
# to its fixed authenticated loopback dependency relays.
URLS.update({
 ('legal','LEGAL_COMPLIANCE_URL'):'http://${{compliance.RAILWAY_PRIVATE_DOMAIN}}:27001',
 ('compliance','COMPLIANCE_LEGAL_URL'):'http://${{legal.RAILWAY_PRIVATE_DOMAIN}}:27001',
 ('finance','FIN_COMPLIANCE_URL'):'http://${{compliance.RAILWAY_PRIVATE_DOMAIN}}:27001',
 ('seo','SEO_COMPLIANCE_URL'):'http://${{compliance.RAILWAY_PRIVATE_DOMAIN}}:27001',
})

def text(cell):
 need(type(cell)is Cell)
 if cell.presence=='absent':need(cell.custody=='none' and cell._value is None);return None
 need(cell.presence=='present' and cell.custody=='provided' and type(cell._value)is str and 0<len(cell._value)<=65536)
 return cell._value

def decode(raw):
 def unique(pairs):
  result={}
  for key,value in pairs:need(key not in result);result[key]=value
  return result
 try:value=json.loads(raw,object_pairs_hook=unique)
 except (ValueError,TypeError,RecursionError):raise Refused('credential map refused') from None
 need(type(value)is dict and len(value)<=128);return value

def token(value,role):
 need(type(value)is str and '${{' not in value)
 if role[1] in ('BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_RESEND_API_KEY','BACKUP_RESTIC_PASSWORD'):
  need(1<=len(value)<=4096 and all(33<=ord(c)<=126 for c in value))
  if role[1]=='BACKUP_RESTIC_PASSWORD':need(32<=len(value)<=128)
  if role[1]=='BACKUP_RESEND_API_KEY':need(len(value)>=32)
 else:need(re.fullmatch('[0-9a-f]{64}',value)is not None)
 return value

def grant(caller,department):return {'caller':caller,'departments':[department],'scope':'write'}
def plan(inventory):
 """All required slots explicitly known. Unmentioned application settings untouched.

No generation occurs. Missing eligible roles return zero deltas, requiring a
new complete preflight after future authorized generation supplies their values.
"""
 need(type(inventory)is dict and REQUIRED<=set(inventory))
 need(all(type(k)is tuple and len(k)==2 and all(type(x)is str for x in k) for k in inventory))
 for k in inventory:
  if k[0]=='backup-trigger' and k!=('backup-trigger','BACKUP_COORD_TOKEN'):need(text(inventory[k])is None)
 raw={k:text(inventory[k]) for k in REQUIRED}
 for k in FORBIDDEN:need(raw[k]is None)
 maps={}
 for k in CALLERS:
  if raw[k] is None:need(k==('compliance','COMPLIANCE_CALLER_TOKENS'));maps[k]={}
  else:maps[k]=decode(raw[k])
  for name,value in maps[k].items():
   need(type(name)is str and re.fullmatch('[a-zA-Z0-9_-]{1,128}',name)is not None);token(value,k)
  if k[0]in ('legal','compliance'):need(set(maps[k])<=set(CALLERS[k]))
 need(raw[LEDGER_MAP]is not None);ledger=decode(raw[LEDGER_MAP]);need(bool(ledger));seen=set()
 allowed={caller:department for caller,department in LEDGER.values()}
 for digest,g in ledger.items():
  need(type(digest)is str and re.fullmatch('[0-9a-f]{64}',digest)is not None and type(g)is dict)
  caller=g.get('caller');need(type(caller)is str and caller in allowed and caller not in seen and g==grant(caller,allowed[caller]));seen.add(caller)
 values={};generation=[]
 for role in sorted(ROLES):
  found=[]
  for slot in (role,*DIRECT.get(role,())):
   if raw[slot]is not None:found.append(token(raw[slot],role))
  for mk,bindings in CALLERS.items():
   for name,source in bindings.items():
    if source==role and name in maps[mk]:found.append(token(maps[mk][name],role))
  need(len(set(found))<=1)
  if found:values[role]=found[0]
  else:need(role in GENERATED);generation.append(role)
 # Unknown retained Finance/SEO grants participate in credential reuse checks.
 distinct=list(values.values())
 for mk,bindings in CALLERS.items():distinct.extend(v for k,v in maps[mk].items() if k not in bindings)
 need(len(distinct)==len(set(distinct)))
 for role,(caller,department) in LEDGER.items():
  if role not in values:need(caller not in seen);continue
  digest=hashlib.sha256(values[role].encode()).hexdigest()
  expected=grant(caller,department)
  if caller=='compliance-py':
   need((caller not in seen and digest not in ledger) or ledger.get(digest)==expected)
  else:need(ledger.get(digest)==expected)
 # Digest collision against every other logical plaintext role also refuses.
 for role,value in values.items():
  digest=hashlib.sha256(value.encode()).hexdigest()
  if digest in ledger:need(role in LEDGER and ledger[digest]==grant(*LEDGER[role]))
 if generation:return Plan({},sorted(generation),URLS)
 deltas={}
 for role,value in values.items():
  for slot in (role,*DIRECT.get(role,())):
   if raw[slot]is None:deltas[slot]=Secret(value)
 for mk,bindings in CALLERS.items():
  updated=dict(maps[mk]);updated.update({name:values[role] for name,role in bindings.items()})
  if updated!=maps[mk]:deltas[mk]={name:Secret(value) for name,value in updated.items()}
 role=('compliance','LEDGER_SERVICE_TOKEN');digest=hashlib.sha256(values[role].encode()).hexdigest()
 if digest not in ledger:
  updated={k:dict(v,departments=list(v['departments'])) for k,v in ledger.items()};updated[digest]=grant(*LEDGER[role]);deltas[LEDGER_MAP]=updated
 return Plan(deltas,(),URLS)
