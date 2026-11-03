import ast, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
P=Path(__file__).resolve().parents[1]/'deploy/docker/maintenance/cold/owner.py'
class Refused(ValueError): pass
class TestExistingOnly(unittest.TestCase):
 def test_lock_only_directory_never_seeds(self):
  tree=ast.parse(P.read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Owner')
  fun=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='initialize')
  proc=Mock();proc.Popen.return_value.wait.return_value=0;proc.Popen.return_value.pid=123
  ns={'Refused':Refused,'LOCK':{'finance':'fin.lock'},'subprocess':proc,'os':Mock(),'signal':Mock(),'absent':lambda pid:True}
  exec(compile(ast.Module(body=[fun],type_ignores=[]),str(P),'exec'),ns)
  with tempfile.TemporaryDirectory() as td:
   source=Path(td);(source/'fin.lock').touch()
   runtime=source/'runtime';runtime.mkdir()
   data=source/'data';data.mkdir();(data/'fin.lock').touch()
   owner=SimpleNamespace(member='finance',source=data,runtime=runtime,root=source,env={})
   with self.assertRaises(Refused):ns['initialize'](owner)
if __name__=='__main__':unittest.main()
