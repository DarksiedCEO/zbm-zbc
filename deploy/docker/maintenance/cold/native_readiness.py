"""Nonsecret pre-serve Compliance observation, bound to exact owned child."""
import json,re
PREFIX='PYTHON_NATIVE_READY '
FIELDS={'pid','member','epoch','instance','integrity_ok','ledger_valid'}
def validate(v,pid,instance,member,epoch):
 if type(v)is not dict or set(v)!=FIELDS:raise ValueError('readiness schema')
 if type(v['pid'])is not int or v['pid']<=0 or v['pid']!=pid:raise ValueError('readiness pid')
 if member!='compliance' or v['member']!=member or v['epoch']!=epoch or v['instance']!=instance:raise ValueError('readiness context')
 if type(epoch)is not str or re.fullmatch(r'owner-[1-9][0-9]{0,9}',epoch)is None or type(instance)is not str or re.fullmatch('[0-9a-f]{32}',instance)is None:raise ValueError('readiness identity')
 if v['integrity_ok']is not True or v['ledger_valid']is not True:raise ValueError('readiness refused')
 return v

def from_observation(observation,pid,instance,member,epoch):
 if type(observation)is not dict or type(observation.get('integrity'))is not dict:raise ValueError('observation missing')
 v={'pid':pid,'instance':instance,'member':member,'epoch':epoch,'integrity_ok':observation['integrity'].get('ok'),'ledger_valid':observation.get('ledger_valid')}
 return validate(v,pid,instance,member,epoch)
def encode(v):
 raw=PREFIX+json.dumps(v,sort_keys=True,separators=(',',':'))
 if len(raw.encode())>1024:raise ValueError('readiness bound')
 return raw

def parse(text,pid,instance,member,epoch):
 if type(text)is not str or len(text.encode())>262144:raise ValueError('child log bound')
 rows=[line[len(PREFIX):] for line in text.splitlines() if line.startswith(PREFIX)]
 if len(rows)!=1 or len(rows[0].encode())>1024:raise ValueError('missing or repeated readiness')
 def unique(pairs):
  d={}
  for k,v in pairs:
   if k in d:raise ValueError('duplicate readiness key')
   d[k]=v
  return d
 return validate(json.loads(rows[0],object_pairs_hook=unique),pid,instance,member,epoch)
