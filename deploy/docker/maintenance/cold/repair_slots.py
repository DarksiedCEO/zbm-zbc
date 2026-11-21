"""Bounded repair namespaces: four shared incident slots, no automatic removal."""
from pathlib import Path
from owner_wire import Refused,decode,canonical
from metadata_retirement import read,save
from incident_archive import Archive,SLOTS
import re
SLOT_TOTAL=1048576

def inventory(control):
 root=Path(control)/'repair-archives'
 if root.is_symlink():raise Refused('repair archive alias')
 if not root.exists():return []
 found=[]
 for d in root.iterdir():
  if not d.is_dir() or d.is_symlink() or not re.fullmatch('cold-[0-9a-f]{32}',d.name):raise Refused('repair archive namespace')
  entries=list(d.rglob('*'));total=0
  if len(entries)>16:raise Refused('repair archive entries')
  for p in entries:
   if p.is_symlink() or not(p.is_file() or p.is_dir()):raise Refused('repair archive type')
   if p.is_file():total+=len(read(p))
  if total>SLOT_TOTAL:raise Refused('repair archive total bytes')
  found.append(d)
 if len(found)+len(Archive(control).scan())>SLOTS:raise Refused('incident slot total')
 return found

def slot(control,run,*,create=False):
 if type(run)is not str or not re.fullmatch('cold-[0-9a-f]{32}',run):raise Refused('repair run identity')
 control=Path(control);items=inventory(control);dest=control/'repair-archives'/run
 if dest in items:return dest
 if not create:raise Refused('repair archive absent')
 a=Archive(control)
 if len(items)+len(a.scan())>=SLOTS:
  a.full_alert([p.name for p in sorted(items)])
  raise Refused('incident archive full; restore fallback required')
 a.capacity();dest.parent.mkdir(mode=0o700,exist_ok=True);dest.mkdir(mode=0o700)
 from metadata_retirement import sync
 sync(dest.parent);return dest

def coordinator_slot(c):
 items=inventory(c.root);run=c.doc['run']
 exact=[p for p in items if p.name==run]
 if exact:return exact[0]
 linked=[]
 for p in items:
  f=p/'repaired-retirement.json'
  if f.exists():
   v=decode(read(f))
   if v.get('new',{} ) and v['new']['run']==run:linked.append((p,v))
 if len(linked)>1:raise Refused('repair lineage ambiguity')
 if linked:
  p,v=linked[0]
  if c.doc['phase']!='COMPLETE' or v['phase']!='DONE':return p
 if c.doc['phase']!='COMPLETE':raise Refused('no new repaired archive authority')
 return slot(c.root,run,create=True)
