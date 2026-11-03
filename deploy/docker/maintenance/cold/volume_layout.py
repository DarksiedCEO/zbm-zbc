"""One persistent volume; fixed distinct source, control and backup siblings."""
import os
from pathlib import Path
MEMBERS=('ledger','finance','legal','compliance','seo','coordinator')
class Layout:
 def __init__(self,root,member):
  self.root=Path(root)
  if member not in MEMBERS or not self.root.is_absolute() or self.root.is_symlink() or self.root.resolve()!=self.root:raise ValueError('fixed canonical volume')
  self.root.mkdir(mode=0o755,parents=True,exist_ok=True)
  self.source=self.root/member;self.backup=self.root/'backup';self.control=self.root/'control'
  for path in (self.source,self.backup,self.control):
   if path.is_symlink():raise ValueError('volume sibling symlink')
   path.mkdir(mode=0o700,exist_ok=True)
   if not path.is_dir() or path.resolve()!=path or path.stat().st_dev!=self.root.stat().st_dev:raise ValueError('volume sibling filesystem')
  for path in (self.backup,self.control):path.chmod(0o700)
