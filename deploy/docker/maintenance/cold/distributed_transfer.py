"""Owner-local typed transfer. No coordinator filesystem access or app handle."""
import os,fcntl,shutil
from pathlib import Path
import b2_adapter as b
from b2_catalog import CATBASE
from capacity_policy import output_bytes

def preflight_catalog(seals,run):
 if not isinstance(seals,dict) or set(seals)!=set(b.MEMBERS):raise b.Refused('FIVE_SEALS_REQUIRED')
 count=0
 for m,s in seals.items():
  if not isinstance(s,dict) or set(s)!={'member','set_id','snapshot','manifest','repository_files'} or s['member']!=m or s['set_id']!=run or not b.hexid(s['snapshot']):raise b.Refused('SEAL_CONTEXT')
  manifest=s['manifest']
  if not isinstance(manifest,dict) or set(manifest)!={'member','set_id','files'} or manifest['member']!=m or manifest['set_id']!=run:raise b.Refused('MANIFEST_CONTEXT')
  profile=b.limits(m);b.files_shape(manifest['files'],32,profile['source']);b.files_shape(s['repository_files'],64,profile['repository']);count+=len(manifest['files'])
 if 20+count>64:raise b.Refused('COMPLETE_VERIFICATION_COMMAND_BOUND')
 if len(b.canonical(seals))>24576:raise b.Refused('SEAL_CONTEXT_BOUND')
 return count

def save(root,name,value):
 raw=b.canonical(value)
 if len(raw)>32768:raise b.Refused('STATE_BOUND')
 root=Path(root);tmp=root/(name+'.tmp');fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 os.replace(tmp,root/name);fd=os.open(root,os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)

def require_member(v,run,member):
 if not isinstance(v,dict) or set(v)!={'schema','set_id','member','repository','source_snapshot','snapshot','manifest_sha256','seal_sha256','verification'}:raise b.Refused('MEMBER_SCHEMA')
 if v['schema']!='b2-restic-member-v1' or v['set_id']!=run or v['member']!=member or v['repository']!=b.BASE+run+'/'+member or v['verification']!='full-read-data-and-exact-file-dumps' or not all(b.hexid(v[k]) for k in ('source_snapshot','snapshot','manifest_sha256','seal_sha256')):raise b.Refused('MEMBER_BINDING')

def require_proof(proof,seal,upload):
 if not isinstance(proof,dict) or set(proof)!={'catalog','locator','catalog_snapshot','catalog_sha256'} or len(b.canonical(proof))>24576:raise b.Refused('PROOF_SCHEMA')
 run=seal['set_id'];member=seal['member'];cat=proof['catalog'];loc=proof['locator']
 if not isinstance(cat,dict) or set(cat)!={'schema','set_id','members','seals_sha256'} or cat['schema']!='b2-restic-catalog-v1' or cat['set_id']!=run or set(cat['members'])!=set(b.MEMBERS) or not b.hexid(cat['seals_sha256']):raise b.Refused('COMPLETE_CATALOG')
 for m in b.MEMBERS:require_member(cat['members'][m],run,m)
 if cat['members'][member]!=upload or upload['seal_sha256']!=b.digest(seal) or upload['manifest_sha256']!=b.digest(seal['manifest']) or upload['source_snapshot']!=seal['snapshot']:raise b.Refused('OWN_SEAL_BINDING')
 expected={'schema':'b2-catalog-locator-v1','set_id':run,'repository':CATBASE+run,'snapshot':proof['catalog_snapshot'],'catalog_sha256':b.digest(cat),'members_sha256':b.digest(cat['members']),'seals_sha256':cat['seals_sha256']}
 if not b.hexid(proof['catalog_snapshot']) or loc!=expected or proof['catalog_sha256']!=b.digest(cat):raise b.Refused('LOCATOR_BINDING')
 return loc

class Transfer:
 def __init__(self,root,password,seal,credentials):
  self.root=Path(root);self.seal=seal;self.credentials=dict(credentials,password=password)
  self.adapter=b.Adapter(self.root,seal,**self.credentials)
 def upload(self):return self.adapter.upload()
 def verify_remote(self,upload):
  require_member(upload,self.seal['set_id'],self.seal['member'])
  if upload['seal_sha256']!=b.digest(self.seal) or upload['manifest_sha256']!=b.digest(self.seal['manifest']) or upload['source_snapshot']!=self.seal['snapshot']:raise b.Refused('OWN_UPLOAD_BINDING')
  b.verify_binary();self.adapter.budget=b.Budget.start(seconds=90,commands=40,output_bytes=output_bytes(self.seal['member']));self.adapter.verify(upload['snapshot']);return upload
 def fresh(self,upload,proof):
  loc=require_proof(proof,self.seal,upload);b.verify_binary()
  a=self.adapter;a.budget=b.Budget.start(seconds=90,commands=40,output_bytes=output_bytes(self.seal['member']));old=a.remote;a.remote=CATBASE+self.seal['set_id']
  try:
   a.command('check','--read-data')
   rows=[b.decode(x) for x in a.command('ls','--json',loc['snapshot']).splitlines()]
   if len(rows)>8 or [r.get('path') for r in rows if r.get('type')=='file']!=['/catalog.json']:raise b.Refused('CATALOG_TREE')
   if b.decode(a.command('dump',loc['snapshot'],'/catalog.json'))!=proof['catalog']:raise b.Refused('REMOTE_CATALOG_MISMATCH')
  finally:a.remote=old
  a.verify(upload['snapshot']);return loc
 def cleanup(self,upload,proof):
  loc=require_proof(proof,self.seal,upload);fd=os.open(self.root/'cleanup.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
  try:
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   if (self.root/'cleanup-intent.json').exists() or (self.root/'cleanup-intent.json.tmp').exists():raise b.Refused('CLEANUP_ALREADY_ATTEMPTED')
   save(self.root,'cleanup-intent.json',{'phase':'PENDING','run':self.seal['set_id'],'member':self.seal['member'],'locator_sha256':b.digest(loc)})
   try:
    self.fresh(upload,proof)
    source=self.root/'sealed'
    if source.is_symlink() or not source.is_dir() or b.local_inventory(source,self.adapter.profile['repository'])!=self.seal['repository_files'] or (self.root/'stage').exists():raise b.Refused('LOCAL_SOURCE_MISMATCH')
    shutil.rmtree(source)
    result={'schema':'b2-cleanup-v1','deleted':True,'run':self.seal['set_id'],'member':self.seal['member'],'catalog_sha256':proof['catalog_sha256'],'catalog_snapshot':proof['catalog_snapshot'],'locator_sha256':b.digest(loc)}
    save(self.root,'cleanup-result.json',{'phase':'DONE','receipt':result});return result
   except BaseException:
    try:save(self.root,'cleanup-failure.json',{'phase':'UNKNOWN','run':self.seal['set_id'],'member':self.seal['member']})
    except BaseException:pass
    raise b.Refused('CLEANUP_UNCERTAIN') from None
  finally:os.close(fd)
