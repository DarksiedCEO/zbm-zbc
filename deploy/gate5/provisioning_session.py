"""Offline session simulation only. No Railway/CLI/secret storage implementation."""
import copy,json
import credential_plan as c
class Refused(ValueError):pass
def need(ok):
 if not ok:raise Refused('provisioning simulation refused')
class Snapshot:
 __slots__=('identity','inventory')
 def __init__(self,identity,inventory):self.identity=copy.deepcopy(identity);self.inventory=copy.deepcopy(inventory)
 def __repr__(self):return '<ProvisioningSnapshot redacted>'
class MemoryAdapter:
 """Finite test double; deliberately no extension or provider backend seam."""
 def __init__(self,identity,inventory,*,interrupt_after=None,drift_before=None):
  self._identity=copy.deepcopy(identity);self._inventory=copy.deepcopy(inventory)
  self.interrupt_after=interrupt_after;self.drift_before=drift_before;self.writes=[]
 def __repr__(self):return '<MemoryAdapter synthetic only; values redacted>'
 def read(self):return Snapshot(self._identity,self._inventory)
 def compare_set(self,expected,key,value):
  if self.drift_before==len(self.writes):self._identity['environment_name']='unexpected';self.drift_before=None
  need(self._identity==expected.identity and self._inventory==expected.inventory)
  self._inventory[key]=c.Cell.supplied(value);self.writes.append(key)
  if self.interrupt_after==len(self.writes):raise Refused('SIMULATED_WRITE_OUTCOME_UNKNOWN')
class SyntheticGenerator:
 """Returns supplied synthetic fixtures; no entropy, OpenSSL or secret APIs."""
 def __init__(self,values):self._values=dict(values);self.calls=[]
 def __repr__(self):return '<SyntheticGenerator redacted>'
 def once(self,role):
  need(role in self._values and role not in self.calls);self.calls.append(role);return self._values[role]

def identity(value):
 need(type(value)is dict and set(value)=={'project_id','project_name','environment_id','environment_name','services'})
 need(value['project_name']=='zbm-zbc' and value['environment_name']=='production')
 for k in ('project_id','environment_id'):need(type(value[k])is str and 1<=len(value[k])<=128)
 services=value['services'];required={k[0] for k in c.REQUIRED}
 need(type(services)is dict and required<=set(services))
 need(all(type(k)is str and type(v)is str and 1<=len(v)<=128 for k,v in services.items()))
 need(len(services.values())==len(set(services.values())))

def wire(value):
 if type(value)is c.Secret:return value._value
 need(type(value)is dict)
 def unwrap(x):
  if type(x)is c.Secret:return x._value
  if type(x)is dict:return {k:unwrap(v) for k,v in x.items()}
  if type(x)is list:return [unwrap(v) for v in x]
  need(type(x)is str);return x
 return json.dumps(unwrap(value),sort_keys=True,separators=(',',':'))

def run(adapter,expected_identity,generator=None):
 """Returns names only. Any exception stops; no retries, rollback or real I/O.

Re-entry must construct a NEW generator from still-absent fixture values. A
partially applied logical role is adopted by the full planner, never rotated.
"""
 need(type(adapter)is MemoryAdapter)
 need(generator is None or type(generator)is SyntheticGenerator)
 identity(expected_identity);before=adapter.read();identity(before.identity);need(before.identity==expected_identity)
 first=c.plan(before.inventory);staged=copy.deepcopy(before.inventory);generated={}
 for role in first._generation:
  need(generator is not None and staged[role]==c.Cell.absent())
  value=generator.once(role);c.token(value,role);generated[role]=value;staged[role]=c.Cell.supplied(value)
 proposed=c.plan(staged);need(not proposed._generation)
 changes={k:wire(v) for k,v in proposed._deltas.items()}
 changes.update(generated)
 # Generated source tokens precede all destinations/maps so re-entry can adopt.
 order=sorted(generated)+sorted(k for k in changes if k not in generated and k not in c.CALLERS and k!=c.LEDGER_MAP)+sorted(k for k in changes if k in c.CALLERS)+([c.LEDGER_MAP] if c.LEDGER_MAP in changes else [])
 current=before;lines=[]
 for key in order:
  fresh=adapter.read();identity(fresh.identity);need(fresh.identity==current.identity and fresh.inventory==current.inventory)
  # Recheck full preflight after every observed mutation; generated values not
  # written yet remain only in the hypothetical complete proposal.
  trial=copy.deepcopy(fresh.inventory)
  for role,value in generated.items():
   if trial[role].presence=='absent':trial[role]=c.Cell.supplied(value)
  c.plan(trial)
  adapter.compare_set(fresh,key,changes[key])
  current=Snapshot(fresh.identity,{**fresh.inventory,key:c.Cell.supplied(changes[key])})
  lines.append('.'.join(key)+' simulated set')
 final=adapter.read();need(final.identity==expected_identity and final.inventory==current.inventory)
 remaining=c.plan(final.inventory);need(not remaining._generation and not remaining._deltas)
 return {'state':'SIMULATED_COMPLETE_NOT_APPLIED','names':tuple(lines),'url_names_pending':tuple('.'.join(k) for k in sorted(proposed._urls))}
