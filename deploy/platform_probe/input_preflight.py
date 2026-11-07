"""Finite controller contract checks against captured official schema; no network."""
import ast,json,re,math
from pathlib import Path
from functools import lru_cache
ROOT=Path(__file__).resolve().parent;E=ROOT/'input-contract'
class Invalid(ValueError):pass
@lru_cache(maxsize=1)
def contracts():
 s=json.loads((E/'schema.json').read_text())['data']['__schema']
 return {t['name']:t for t in s['types']},json.loads((E/'environment.schema.json').read_text())
def base(t):
 while t.get('ofType'):t=t['ofType']
 return t['name']
def typename(t):return t['name'] if t.get('name') else (typename(t['ofType'])+'!' if t['kind']=='NON_NULL' else '['+typename(t['ofType'])+']')
def json_value(v,s):
 if 'anyOf'in s:
  for alt in s['anyOf']:
   try:json_value(v,alt);return
   except Invalid:pass
  raise Invalid('JSON_ANYOF')
 types={'object':dict,'array':list,'string':str,'boolean':bool,'null':type(None),'integer':int,'number':(int,float)}
 if 'type'in s and (not isinstance(v,types[s['type']]) or s['type']in ('integer','number') and type(v)is bool):raise Invalid('JSON_TYPE')
 if 'enum'in s and v not in s['enum']:raise Invalid('JSON_ENUM')
 for key,fn in [('minimum',lambda a,b:a>=b),('maximum',lambda a,b:a<=b),('minLength',lambda a,b:len(a)>=b),('maxLength',lambda a,b:len(a)<=b)]:
  if key in s and not fn(v,s[key]):raise Invalid('JSON_BOUND')
 if 'pattern'in s and re.search(s['pattern'],v)is None:raise Invalid('JSON_PATTERN')
 if isinstance(v,dict):
  if not set(s.get('required',[]))<=set(v):raise Invalid('JSON_REQUIRED')
  for k,x in v.items():
   if 'propertyNames'in s:json_value(k,s['propertyNames'])
   # Fixed rehearsal contract deliberately rejects undocumented object keys.
   child=s.get('properties',{}).get(k,s.get('additionalProperties',False if 'properties'in s else True))
   if child is False:raise Invalid('JSON_FIELD')
   if isinstance(child,dict):json_value(x,child)
 if isinstance(v,list):
  for x in v:json_value(x,s.get('items',{}))
def value(v,t):
 types,env=contracts()
 if t['kind']=='NON_NULL':
  if v is None:raise Invalid('REQUIRED')
  return value(v,t['ofType'])
 if v is None:return
 if t['kind']=='LIST':
  if not isinstance(v,list):raise Invalid('LIST')
  for x in v:value(x,t['ofType'])
  return
 name=t['name'];spec=types[name]
 if spec['kind']=='INPUT_OBJECT':
  if not isinstance(v,dict):raise Invalid('INPUT_OBJECT')
  fields={f['name']:f for f in spec['inputFields']}
  if set(v)-set(fields):raise Invalid('INPUT_FIELD')
  if name=='ProjectCreateInput' and 'name'in v and not 1<=len(v['name'])<=32:raise Invalid('PROJECT_NAME')
  if name=='ServiceCreateInput' and 'name'in v and (not v['name'] or len(v['name'])>32):raise Invalid('SERVICE_NAME')
  if name=='VolumeCreateInput' and not v.get('mountPath','').startswith('/'):raise Invalid('MOUNT_PATH')
  for k,f in fields.items():
   if k not in v and f['type']['kind']=='NON_NULL' and f.get('defaultValue')is None:raise Invalid('INPUT_REQUIRED')
   if k in v:
    if f.get('isDeprecated'):raise Invalid('DEPRECATED_INPUT')
    value(v[k],f['type'])
 elif spec['kind']=='ENUM':
  if v not in [x['name'] for x in spec['enumValues'] if not x.get('isDeprecated')]:raise Invalid('ENUM_OR_DEPRECATED')
 elif name=='EnvironmentConfig':json_value(v,env)
 elif name in ('String','ID'):
  if not isinstance(v,str):raise Invalid('STRING')
 elif name=='Boolean':
  if type(v)is not bool:raise Invalid('BOOL')
 elif name=='Int':
  if type(v)is not int or not -(2**31)<=v<2**31:raise Invalid('INT')
 elif name=='Float':
  if type(v)not in (int,float) or not math.isfinite(v):raise Invalid('FLOAT')
 else:raise Invalid('UNSUPPORTED_SCALAR')
def ref(name):
 if name.endswith('!'):return {'kind':'NON_NULL','ofType':ref(name[:-1])}
 if name.startswith('['):return {'kind':'LIST','ofType':ref(name[1:-1])}
 types,_=contracts()
 if name not in types:raise Invalid('VARIABLE_TYPE')
 return {'kind':types[name]['kind'],'name':name}
def operation(q,variables=None):
 """Restricted grammar used by the frozen controller: no aliases/fragments."""
 types,_=contracts();m=re.fullmatch(r'(query|mutation)\((.*?)\)(\{.*\})',q)
 if not m:raise Invalid('OPERATION_GRAMMAR')
 declarations=dict(re.findall(r'\$(\w+):([\w!\[\]]+)',m[2]));tokens=re.findall(r'\$\w+|\w+|[{}():,]|-?\d+',m[3]);i=0
 if variables is not None:
  if set(variables)!=set(declarations):raise Invalid('VARIABLE_SET')
  for k,v in variables.items():value(v,ref(declarations[k]))
 def selection(parent):
  nonlocal i
  if tokens[i]!='{':raise Invalid('SELECTION')
  i+=1;fields={f['name']:f for f in types[parent].get('fields')or []}
  while tokens[i]!='}':
   name=tokens[i];i+=1
   if name not in fields:raise Invalid('SELECTION_FIELD_'+name)
   f=fields[name]
   if f.get('isDeprecated'):raise Invalid('DEPRECATED_FIELD_'+name)
   args={a['name']:a for a in f['args']};seen=set()
   if tokens[i]=='(':
    i+=1
    while tokens[i]!=')':
     k=tokens[i];i+=1
     if k not in args or k in seen or tokens[i]!=':':raise Invalid('ARGUMENT')
     if args[k].get('isDeprecated'):raise Invalid('DEPRECATED_ARGUMENT')
     seen.add(k);i+=1;x=tokens[i];i+=1
     if x.startswith('$'):
      if declarations.get(x[1:])!=typename(args[k]['type']):
       # A non-null variable is valid for the same nullable argument.
       if declarations.get(x[1:])!=typename(args[k]['type'])+'!':raise Invalid('ARGUMENT_VARIABLE_TYPE')
     else:value(True if x=='true' else False if x=='false' else int(x),args[k]['type'])
     if tokens[i]==',':i+=1
    i+=1
   if any(a['type']['kind']=='NON_NULL' and a.get('defaultValue')is None and k not in seen for k,a in args.items()):raise Invalid('ARGUMENT_REQUIRED')
   b=base(f['type']);compound=types[b]['kind']in ('OBJECT','INTERFACE','UNION')
   if tokens[i]=='{':
    if not compound:raise Invalid('SCALAR_SELECTION')
    selection(b)
   elif compound:raise Invalid('MISSING_SELECTION')
  i+=1
 selection('Mutation' if m[1]=='mutation' else 'Query')
 if i!=len(tokens):raise Invalid('TRAILING')
