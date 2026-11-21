import _runtime_path
import tempfile,unittest,stat
from pathlib import Path
from volume_layout import Layout,member_root,PREFIX
class ExistingLayout(unittest.TestCase):
 def test_existing_stores_never_moved_or_modified(self):
  with tempfile.TemporaryDirectory() as tmp:
   for member in PREFIX:
    root=Path(tmp).resolve()/member;source=root/member;source.mkdir(parents=True)
    file=source/('ledger.jsonl' if member=='ledger' else 'retained.jsonl');file.write_bytes(b'existing immutable fixture\n');inode=file.stat().st_ino;mode=source.stat().st_mode
    layout=Layout(root,member)
    self.assertEqual(layout.source,source);self.assertEqual(file.read_bytes(),b'existing immutable fixture\n');self.assertEqual(file.stat().st_ino,inode);self.assertEqual(source.stat().st_mode,mode)
    self.assertEqual(layout.backup.parent,root);self.assertEqual(layout.control.parent,root)
    self.assertEqual(stat.S_IMODE(layout.control.stat().st_mode),0o700)
 def test_roots_only_exact_known_sources(self):
  for member,prefix in PREFIX.items():
   key=prefix+('_LOG_PATH' if member=='ledger' else '_DATA_DIR');suffix='/'+member+('/ledger.jsonl' if member=='ledger' else '')
   for root in ('/data','/volume'):self.assertEqual(member_root(member,{key:root+suffix}),Path(root))
   for bad in ('/data/../volume'+suffix,'/data/control','/other'+suffix):
    with self.assertRaises(ValueError):member_root(member,{key:bad})
 def test_symlink_source_rejected_without_target_mutation(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();target=root/'outside';target.mkdir();(target/'keep').write_text('keep');(root/'finance').symlink_to(target)
   with self.assertRaises(ValueError):Layout(root,'finance')
   self.assertEqual((target/'keep').read_text(),'keep')
if __name__=='__main__':unittest.main()
