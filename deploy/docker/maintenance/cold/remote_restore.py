"""Fixed B2/S3 exact-catalog recovery into fresh destinations; never opens apps."""
import os,re,json,hashlib
from pathlib import Path
import b2_adapter as b
from b2_catalog import CATBASE
from member_limits import limits
from bounded_process import Budget,run_fixed

def validate_catalog(loc,cat):
 fields={'schema','set_id','repository','snapshot','catalog_sha256','members_sha256','seals_sha256'}
 if type(loc)is not dict or set(loc)!=fields or loc['schema']!='b2-catalog-locator-v1' or not isinstance(loc['set_id'],str) or not re.fullmatch('cold-[0-9a-f]{32}',loc['set_id']):raise b.Refused('LOCATOR_SCHEMA')
 run=loc['set_id']
 if loc['repository']!=CATBASE+run or not all(b.hexid(loc[k]) for k in ('snapshot','catalog_sha256','members_sha256','seals_sha256')):raise b.Refused('LOCATOR_BINDING')
 if type(cat)is not dict or set(cat)!={'schema','set_id','members','seals_sha256'} or cat['schema']!='b2-restic-catalog-v1' or cat['set_id']!=run or cat['seals_sha256']!=loc['seals_sha256'] or type(cat['members'])is not dict or set(cat['members'])!=set(b.MEMBERS) or b.digest(cat)!=loc['catalog_sha256'] or b.digest(cat['members'])!=loc['members_sha256']:raise b.Refused('CATALOG_BINDING')
 for m,v in cat['members'].items():
  if type(v)is not dict or set(v)!={'schema','set_id','member','repository','source_snapshot','snapshot','manifest_sha256','seal_sha256','verification'} or v['schema']!='b2-restic-member-v1' or v['member']!=m or v['set_id']!=run or v['repository']!=b.BASE+run+'/'+m or v['verification']!='full-read-data-and-exact-file-dumps' or not all(b.hexid(v[k]) for k in ('source_snapshot','snapshot','manifest_sha256','seal_sha256')):raise b.Refused('MEMBER_BINDING')
 return run

def validate_manifest(value,receipt):
 if type(value)is not dict or set(value)!={'member','set_id','files'} or value['member']!=receipt['member'] or value['set_id']!=receipt['set_id'] or b.digest(value)!=receipt['manifest_sha256']:raise b.Refused('MANIFEST_BINDING')
 b.files_shape(value['files'],32,limits(receipt['member'])['source'])
 if 'manifest.json' in value['files']:raise b.Refused('MANIFEST_ALIAS')
 return value

def tree(raw,expected):
 rows=raw.splitlines()
 if len(rows)>128:raise b.Refused('TREE_BOUND')
 paths=[]
 for line in rows:
  row=b.decode(line)
  if type(row)is not dict or row.get('type') not in (None,'dir','file'):raise b.Refused('TREE_TYPE')
  if row.get('type')=='file':paths.append(row.get('path'))
 if len(paths)!=len(set(paths)) or set(paths)!=set(expected):raise b.Refused('TREE_COVERAGE')

def restore(password,token,locator,expected=None,*,_b2_credentials=None):
 if token is not None or expected is not None or _b2_credentials is None:raise b.Refused('FIXED_B2_ONLY')
 from restore_transport import credentials
 credentials(password,_b2_credentials)
 root=Path('/recovery');scratch=root/'scratch';targets=root/'targets'
 if scratch.is_symlink() or not scratch.is_dir() or any(scratch.iterdir()):raise b.Refused('FRESH_SCRATCH')
 for m in b.MEMBERS:
  p=targets/m
  if p.is_symlink() or not p.is_dir() or any(p.iterdir()):raise b.Refused('FRESH_TARGET')
 # Locator validation before any remote request.
 if type(locator)is not dict or not b.hexid(locator.get('snapshot')) or not isinstance(locator.get('set_id'),str) or not re.fullmatch('cold-[0-9a-f]{32}',locator['set_id']) or locator.get('repository')!=CATBASE+locator['set_id']:raise b.Refused('LOCATOR_BINDING')
 b.verify_binary();budget=Budget.start(seconds=120,commands=64,output_bytes=8*b.MIB)
 env={'PATH':'/usr/bin:/bin','HOME':str(scratch),'TMPDIR':str(scratch),'RESTIC_PASSWORD':password,'AWS_ACCESS_KEY_ID':_b2_credentials['key_id'],'AWS_SECRET_ACCESS_KEY':_b2_credentials['application_key'],'AWS_DEFAULT_REGION':'us-west-004','RESTIC_RETRY_LOCK':'0s'}
 def command(member,*args):
  if member not in (*b.MEMBERS,'catalog') or args[0] not in ('check','ls','dump'):raise b.Refused('READ_ONLY_COMMAND')
  repo=(CATBASE+locator['set_id']) if member=='catalog' else b.BASE+locator['set_id']+'/'+member
  cap=limits(member)['source'] if member!='catalog' else b.MIB
  return run_fixed([str(b.BINARY),'--no-cache','--no-lock','-r',repo,*args],env=env,cwd=scratch,budget=budget,output_limit=cap)
 command('catalog','check','--read-data');tree(command('catalog','ls','--json',locator['snapshot']),{'/catalog.json'})
 cat=b.decode(command('catalog','dump',locator['snapshot'],'/catalog.json'));run=validate_catalog(locator,cat)
 manifests={};data={}
 for m in b.MEMBERS:
  receipt=cat['members'][m];snap=receipt['snapshot'];command(m,'check','--read-data')
  manifest=validate_manifest(b.decode(command(m,'dump',snap,'/manifest.json')),receipt)
  tree(command(m,'ls','--json',snap),{'/'+n for n in manifest['files']}|{'/manifest.json'});manifests[m]=manifest
 count=sum(len(x['files']) for x in manifests.values())
 if count>budget.commands_left:raise b.Refused('RESTORE_COMMAND_BOUND')
 for m in b.MEMBERS:
  data[m]={}
  for name,meta in manifests[m]['files'].items():
   raw=command(m,'dump',cat['members'][m]['snapshot'],'/'+name)
   if len(raw)!=meta['bytes'] or hashlib.sha256(raw).hexdigest()!=meta['sha256']:raise b.Refused('CONTENT_MISMATCH')
   data[m][name]=raw
 # Advisory free-space/inode refusal before writes; exclusive empty mounts still
 # required. This is not a quota or a guarantee against concurrent consumption.
 needs={}
 for m in b.MEMBERS:
  dest=targets/m;device=dest.stat().st_dev
  dirs={'.'}
  for name in data[m]:dirs.update(str(p) for p in Path(name).parents)
  amount=sum(len(raw) for raw in data[m].values())
  old=needs.setdefault(device,[dest,0,0]);old[1]+=amount;old[2]+=len(data[m])+len(dirs)
 for dest,amount,entries in needs.values():
  fs=os.statvfs(dest)
  if fs.f_bavail*fs.f_frsize<amount+1048576 or fs.f_favail<entries+16:raise b.Refused('DESTINATION_HEADROOM')
 # All five exact snapshots/content verified before any application destination write.
 for m in b.MEMBERS:
  target=targets/m/m;target.mkdir(mode=0o700)
  for name,raw in data[m].items():
   p=target/name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
   fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
   with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  from cold_capture import source_inventory
  if source_inventory(target,m)!=manifests[m]['files']:raise b.Refused('RESTORED_BYTES')
  # Sync directory entries before ownership handoff.
  for directory in [target,*[p for p in target.rglob('*') if p.is_dir()]]:
   fd=os.open(directory,os.O_RDONLY);os.fsync(fd);os.close(fd)
 for m in b.MEMBERS:
  target=targets/m/m
  # Materialize while root owns all directories, then deepest-first UID handoff.
  paths=sorted([target,*target.rglob('*')],key=lambda p:len(p.parts),reverse=True)
  for p in paths:os.chown(p,10001,10001)
 return {'schema':'local-remote-restore-v1','set_id':run,'catalog_snapshot':locator['snapshot'],'catalog_sha256':locator['catalog_sha256'],'members':{m:{'snapshot':cat['members'][m]['snapshot'],'manifest_sha256':b.digest(manifests[m])} for m in b.MEMBERS},'application_start':'NOT_PERFORMED'}
if __name__=='__main__':
 from packaged_restore import main
 raise SystemExit(main())
