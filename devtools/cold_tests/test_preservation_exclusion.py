import _runtime_path
import unittest
from unittest.mock import patch
import b2_adapter as b
from b2_catalog import Catalog, CATBASE


class PreservationExclusion(unittest.TestCase):
 def test_protected_and_confused_paths_never_execute(self):
  base=b.BASE.removesuffix('sets/')
  paths=[base+'preservation/preserve-123',base,base+'sets/../preservation/x',
         base+'sets/%2e%2e/preservation/x',base+'preservation-extra/x',
         b.BASE+'cold-'+'a'*32+'/ledger/../../preservation/x']
  for repo in paths:
   for op in ('init','copy','backup','forget','prune','check','dump','restore'):
    with self.subTest(repo=repo,op=op),patch.object(b,'run_fixed') as execute:
     adapter=object.__new__(b.Adapter);adapter.remote=repo
     with self.assertRaises(b.Refused):adapter.command(op)
     execute.assert_not_called()
    with self.subTest(catalog=repo,op=op),patch.object(b,'run_fixed') as execute:
     catalog=object.__new__(Catalog);catalog.remote=repo
     with self.assertRaises(b.Refused):catalog.catcommand(op,cwd='/unused')
     execute.assert_not_called()

 def test_remote_deletion_refused_even_in_operational_repositories(self):
  for repo in (b.BASE+'cold-'+'a'*32+'/ledger',CATBASE+'cold-'+'a'*32):
   for op in ('forget','prune','unlock','repair','key','tag'):
    with self.subTest(repo=repo,op=op),self.assertRaises(b.Refused):
     b.operational_command(repo,(op,'--prune'))

 def test_repository_override_cannot_bypass_exclusion(self):
  repo=b.BASE+'cold-'+'a'*32+'/ledger'
  for option in ('-r','-rs3:other','--repo','--repo=s3:other','--repository-file'):
   with self.subTest(option=option),self.assertRaises(b.Refused):
    b.operational_command(repo,('check',option))

 def test_fixed_member_and_catalog_paths_remain_usable(self):
  for member in b.MEMBERS:
   b.operational_command(b.BASE+'cold-'+'a'*32+'/'+member,('copy','--from-repo','/owned/sealed','1'*64))
  b.operational_command(CATBASE+'cold-'+'a'*32,('backup','--json','.'))


if __name__=='__main__':unittest.main()
