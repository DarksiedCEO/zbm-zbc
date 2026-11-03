"""Pure complete-set equality checks; no authentication or deletion authority."""
import hashlib,re
from owner_wire import canonical,MEMBERS

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def require_upload(v,run,member):
 if not isinstance(v,dict) or set(v)!={'member','set_id','source_snapshot','snapshot','manifest_sha256','repository_files'} or v['member']!=member or v['set_id']!=run:raise ValueError('upload context')
 for field in ('source_snapshot','snapshot','manifest_sha256'):
  if not isinstance(v[field],str) or not re.fullmatch('[0-9a-f]{64}',v[field]):raise ValueError('upload digest')
 files=v['repository_files']
 if not isinstance(files,dict) or not 1<=len(files)<=64:raise ValueError('inventory bound')
 for path,record in files.items():
  if not isinstance(path,str) or len(path)>160 or path.startswith('/') or any(x in ('','.','..') for x in path.split('/')):raise ValueError('inventory path')
  if not isinstance(record,dict) or set(record)!={'bytes','sha256'} or type(record['bytes'])is not int or not 0<=record['bytes']<=2*1048576 or not re.fullmatch('[0-9a-f]{64}',record['sha256']):raise ValueError('inventory value')
 if len(canonical(v))>8192:raise ValueError('upload receipt bound')
def require_catalog(catalog,run,member,upload):
 if not isinstance(catalog,dict) or set(catalog)!={'schema','set_id','members'} or type(catalog['schema'])is not int or catalog['schema']!=1 or catalog['set_id']!=run or not isinstance(catalog['members'],dict) or set(catalog['members'])!=set(MEMBERS):raise ValueError('complete catalog context')
 for m in MEMBERS:require_upload(catalog['members'][m],run,m)
 if member not in MEMBERS or catalog['members'][member]!=upload or len(canonical(catalog))>24576:raise ValueError('exact catalog member')
