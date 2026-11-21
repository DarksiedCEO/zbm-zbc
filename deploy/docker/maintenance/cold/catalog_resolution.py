"""Typed immutable resolution of an exact existing catalog locator, not discovery."""
import hashlib
from pathlib import Path
import b2_adapter as b
from b2_catalog import Catalog as RemoteCatalog
from distributed_transfer import require_proof,preflight_catalog
from metadata_retirement import read,save
from owner_wire import Refused

def path(catalog,run):
 root,context=catalog.paths(run);return context.parent/('catalog-resolution-'+run+'.json')
def sha(raw):return hashlib.sha256(raw).hexdigest()
def retained(catalog,run):
 root,context=catalog.paths(run)
 for p in (context.with_name(context.name+'.tmp'),root/'catalog-intent.json.tmp',root/'catalog-state.json.tmp',root/'catalog-locator.json.tmp'):
  if p.exists() or p.is_symlink():raise Refused('catalog interrupted artifact unsupported')
 raw=read(context,32768);value=b.decode(raw)
 if set(value)!={'run','uploads','seals'} or value['run']!=run:raise Refused('exact retained catalog context')
 preflight_catalog(value['seals'],run)
 obj=RemoteCatalog(root,value['seals'],value['uploads'],password=catalog.password,**catalog.credentials)
 artifacts={'context':sha(raw)}
 for n in ('catalog-intent.json','catalog-locator.json','catalog-state.json'):artifacts[n]=sha(read(root/n,32768))
 intent=obj.read('catalog-intent.json');loc=obj.read('catalog-locator.json');state=obj.read('catalog-state.json')
 if intent!={'phase':'PENDING','set_id':run,'catalog_sha256':b.digest(obj.catalog)} or not b.hexid(loc.get('snapshot')) or loc!=obj.locator(loc['snapshot']):raise Refused('original catalog intent/locator mismatch')
 known=[{'phase':p,'set_id':run,'catalog_sha256':b.digest(obj.catalog)} for p in ('PENDING','UNKNOWN')]+[{'phase':'PUBLISHED','set_id':run,'locator_sha256':b.digest(loc)}]
 if state not in known:raise Refused('unsupported catalog state')
 proof={'catalog':obj.catalog,'locator':loc,'catalog_snapshot':loc['snapshot'],'catalog_sha256':loc['catalog_sha256']}
 for m in b.MEMBERS:require_proof(proof,value['seals'][m],value['uploads'][m])
 return obj,value,artifacts,proof

def validate(catalog,run):
 p=path(catalog,run)
 if p.with_name(p.name+'.tmp').exists() or p.with_name(p.name+'.tmp').is_symlink():raise Refused('catalog resolution interrupted')
 v=b.decode(read(p,32768));obj,context,artifacts,proof=retained(catalog,run)
 if set(v)!={'schema','run','incident_sha256','artifacts','proof','verification'} or v['schema']!='catalog-resolution-v1' or v['run']!=run or not b.hexid(v['incident_sha256']) or v['artifacts']!=artifacts or v['proof']!=proof or v['verification']!='fresh-exact-catalog-and-five-members':raise Refused('catalog resolution binding')
 return v

def publish(catalog,run,incident,artifacts,proof):
 if retained(catalog,run)[2:]!=(artifacts,proof):raise Refused('catalog originals changed after verification')
 value={'schema':'catalog-resolution-v1','run':run,'incident_sha256':incident,'artifacts':artifacts,'proof':proof,'verification':'fresh-exact-catalog-and-five-members'}
 if not b.hexid(incident) or len(b.canonical(value))>32768:raise Refused('catalog resolution capacity/context')
 p=path(catalog,run)
 if p.exists() or p.is_symlink():
  if validate(catalog,run)!=value:raise Refused('different catalog resolution')
 else:save(p,value)
 return value
