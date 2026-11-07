"""Immutable supervisor-local byte capacity. Not deployment authorization.

Explicit policy expands data-byte envelopes only; file counts, metadata, command
counts, timeouts and per-command output ceiling retain their original bounds.
"""
import hashlib,json,threading
MIB=1048576
MEMBERS=('ledger','finance','legal','compliance','seo')
SETTING='BACKUP_CAPACITY_POLICY'
_lock=threading.Lock();_loaded=None

def parse(raw):
 if not isinstance(raw,str) or len(raw)>2048:raise ValueError('capacity policy input bound')
 def unique(pairs):
  out={}
  for k,v in pairs:
   if k in out:raise ValueError('duplicate capacity policy field')
   out[k]=v
  return out
 try:value=json.loads(raw,object_pairs_hook=unique)
 except (ValueError,TypeError):raise ValueError('invalid capacity policy JSON') from None
 if not isinstance(value,dict) or set(value)!={'version','sources'} or type(value['version'])is not int or value['version']!=1:raise ValueError('capacity policy version')
 rows=value['sources']
 if not isinstance(rows,dict) or set(rows)!=set(MEMBERS) or any(type(v)is not int or not 0<v<=8*MIB for v in rows.values()) or sum(rows.values())>32*MIB:raise ValueError('capacity source limits')
 return json.dumps(value,sort_keys=True,separators=(',',':'))

def initialize(env,*,required=True):
 global _loaded
 raw=env.get(SETTING)
 if raw is None:
  if required:raise ValueError('explicit capacity policy required')
  canonical=None
 else:canonical=parse(raw)
 with _lock:
  if _loaded is not None and _loaded!=canonical:raise ValueError('capacity policy immutable')
  if canonical is not None:_loaded=canonical
 return fingerprint()

def fingerprint():
 raw=_loaded if _loaded is not None else 'legacy-fixture-capacity-v1'
 return hashlib.sha256(raw.encode()).hexdigest()
def configured():return _loaded is not None

def member(member):
 if member not in MEMBERS:raise ValueError('fixed member profile')
 if _loaded is None:
  large=member in ('ledger','compliance')
  return {'source':2*MIB if large else MIB//2,'stage':3*MIB if large else MIB,'repository':4*MIB if large else 2*MIB,'control_reserve':MIB}
 return derive_member(_loaded,member)
def derive_member(canonical_policy,member_name):
 if member_name not in MEMBERS:raise ValueError('fixed member profile')
 source=json.loads(parse(canonical_policy))['sources'][member_name]
 return {'source':source,'stage':source+MIB,'repository':max(2*MIB,2*source+2*MIB),'control_reserve':MIB}
def capture_bytes(member_name):
 if _loaded is None:return 8*MIB
 p=member(member_name);return max(8*MIB,2*(p['stage']+p['repository'])+p['control_reserve'])
def spool_bytes():return 24*MIB if _loaded is None else max(24*MIB,3*max(capture_bytes(m) for m in MEMBERS))
def output_bytes(member_name=None):
 if _loaded is None:return 8*MIB
 source=sum(member(m)['source'] for m in MEMBERS) if member_name is None else member(member_name)['source']
 return source+64*MIB


def require_application_recovery(canonical_policy):
 """Pure qualification boundary, not production sizing or authorization."""
 rows=json.loads(parse(canonical_policy))['sources']
 for name,size in rows.items():
  ceiling=2*MIB if name in ('ledger','compliance') else MIB//2
  if size>ceiling:raise ValueError('native application recovery capacity unqualified')
 return True

def require_loaded_application_recovery():
 if _loaded is not None:require_application_recovery(_loaded)
