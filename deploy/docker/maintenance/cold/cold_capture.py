"""Bounded application source inventory; no fixture capture backend."""
from pathlib import Path
import gate3_restic as engine
SOURCE_LIMIT=512*1024
def source_inventory(source,member=None):
 from member_limits import limits
 limit=SOURCE_LIMIT if member is None else limits(member)['source']
 source=Path(source);files=[];size=0
 for p in source.rglob('*'):
  if p.is_symlink() or not(p.is_dir() or p.is_file()):raise ValueError('source entry refused')
  if p.is_file():
   files.append(p);size+=p.stat().st_size
   if len(files)>32 or size>limit:raise ValueError('test source envelope exceeded')
 return engine.inventory(source)
