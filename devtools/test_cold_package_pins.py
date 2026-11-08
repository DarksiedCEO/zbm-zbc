"""Installed startup prerequisite digests must describe this release tree."""
import hashlib,json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class Pins(unittest.TestCase):
 def test_actual_application_sources_match_required_startup_inputs(self):
  p=ROOT/'deploy/docker/maintenance/application-prerequisites.json'
  rows=json.loads(p.read_text())
  self.assertEqual(set(rows),{'finance','legal','compliance','seo'})
  for member,files in rows.items():
   names=[r['path'] for r in files];self.assertEqual(len(names),len(set(names)))
   self.assertIn('startup_profile.py',names);self.assertIn('api.py',names)
   for r in files:
    with self.subTest(member=member,path=r['path']):
     self.assertEqual(set(r),{'path','sha256'})
     p=ROOT/'services'/(member+'-py')/'src'/r['path']
     self.assertFalse(p.is_symlink())
     self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),r['sha256'])
 def test_restic_has_the_recorded_binary_digest(self):
  p=ROOT/'deploy/docker/maintenance/vendor/restic'
  self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),'20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639')
if __name__=='__main__':unittest.main()
