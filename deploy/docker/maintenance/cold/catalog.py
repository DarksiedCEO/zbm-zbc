"""Coordinator-local durable catalog context; no member filesystem access."""
import re
from pathlib import Path
import b2_adapter as b
from b2_catalog import Catalog as RemoteCatalog
from distributed_transfer import save
from owner_wire import Refused
class Catalog:
 def __init__(self,password,credentials):self.password=password;self.credentials=credentials;self.proof=None
 def paths(self,run):
  if not isinstance(run,str) or not re.fullmatch('cold-[0-9a-f]{32}',run):raise Refused('catalog run')
  return Path('/volume/backup')/run,Path('/volume/control')/('catalog-'+run+'.json')
 def create(self,run,uploads,seals):
  root,context=self.paths(run)
  if root.exists() or context.exists():raise Refused('existing catalog intent; resolve retained state')
  root.mkdir(mode=0o700)
  obj=RemoteCatalog(root,seals,uploads,password=self.password,**self.credentials)
  value={'run':run,'uploads':uploads,'seals':seals}
  if len(b.canonical(value))>32768:raise Refused('catalog context bound')
  save(context.parent,context.name,value)
  loc=obj.publish();proof={'catalog':obj.catalog,'locator':loc,'catalog_snapshot':loc['snapshot'],'catalog_sha256':loc['catalog_sha256']}
  return proof
 def publish(self,proof):self.proof=proof
 def read(self,run):
  # Durable exact context reconstructs identity after coordinator replacement;
  # it does not clear the coordinator's interrupted state or perform repair.
  root,context=self.paths(run)
  from catalog_resolution import path,validate
  resolved=path(self,run)
  if resolved.exists() or resolved.is_symlink() or resolved.with_name(resolved.name+'.tmp').exists():return validate(self,run)['proof']
  if context.is_symlink() or not context.is_file() or context.stat().st_size>32768:raise Refused('catalog context unavailable')
  value=b.decode(context.read_bytes())
  if set(value)!={'run','uploads','seals'} or value['run']!=run:raise Refused('catalog context')
  obj=RemoteCatalog(root,value['seals'],value['uploads'],password=self.password,**self.credentials)
  loc=obj.read('catalog-locator.json')
  if loc!=obj.locator(loc.get('snapshot')) or not b.hexid(loc.get('snapshot')) or obj.read('catalog-state.json')!={'phase':'PUBLISHED','set_id':run,'locator_sha256':b.digest(loc)}:raise Refused('catalog unpublished')
  return {'catalog':obj.catalog,'locator':loc,'catalog_snapshot':loc['snapshot'],'catalog_sha256':loc['catalog_sha256']}

 def fresh(self,run):
  expected=self.read(run);root,context=self.paths(run)
  value=b.decode(context.read_bytes())
  obj=RemoteCatalog(root,value['seals'],value['uploads'],password=self.password,**self.credentials)
  loc=obj.recover() # actual remote full catalog/member read; no cached result
  if loc!=expected['locator'] or obj.catalog!=expected['catalog'] or self.read(run)!=expected:raise Refused('fresh catalog changed')
  return expected
