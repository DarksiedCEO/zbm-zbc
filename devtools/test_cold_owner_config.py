"""Explicit recovery selection must not alias public flags with credentials."""
from pathlib import Path
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy/docker/maintenance'))
from cold_config import config,Refused
class ConfigTests(unittest.TestCase):
 def env(self):
  return {'BACKUP_LAUNCH_ENABLED':'1','BACKUP_MEMBER':'finance','BACKUP_COORD_TOKEN':'a'*64,'BACKUP_RESTIC_PASSWORD':'b'*64,'BACKUP_B2_KEY_ID':'synthetic-id','BACKUP_B2_APPLICATION_KEY':'synthetic-key','BACKUP_VOLUME':'/volume','FIN_DATA_DIR':'/volume/finance','FIN_STRIPE_LIVE':'1'}
 def test_public_live_flag_is_not_a_reused_credential(self):
  self.assertEqual(config(self.env(),['/opt/venv/bin/python','-m','api'])['app_env']['FIN_STRIPE_LIVE'],'1')
 def test_recovery_is_explicit_and_strict(self):
  for value,wanted in ((None,False),('0',False),('1',True)):
   env=self.env()
   if value is not None:env['BACKUP_RECOVERY_MODE']=value
   self.assertEqual(config(env,['/opt/venv/bin/python','-m','api'])['recovery'],wanted)
  for value in ('true','yes','2',''):
   with self.assertRaises(Refused):config(dict(self.env(),BACKUP_RECOVERY_MODE=value),['/opt/venv/bin/python','-m','api'])
 def test_actual_secret_alias_still_refuses(self):
  for key in ('BACKUP_COORD_TOKEN','BACKUP_RESTIC_PASSWORD','BACKUP_B2_APPLICATION_KEY'):
   env=self.env();env['FIN_SERVICE_TOKEN']=env[key]
   with self.assertRaises(ValueError):config(env,['/opt/venv/bin/python','-m','api'])
 def test_normal_launcher_strips_public_selector(self):
  import ast,types
  from unittest.mock import patch
  path=Path(__file__).resolve().parents[1]/'deploy/docker/maintenance/launcher.py'
  tree=ast.parse(path.read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
  cfg=config(self.env(),['/opt/venv/bin/python','-m','api']);seen=[]
  owner=types.SimpleNamespace(token='synthetic',dispatch=lambda *a:None,cleanup=lambda:seen.append('cleanup'))
  def construct(c):
   self.assertNotIn('recovery',c);seen.append('owner');return owner
  server=types.SimpleNamespace(serve_forever=lambda **k:None,server_close=lambda:None)
  ns={'os':types.SimpleNamespace(getuid=lambda:0,environ={}), 'sys':types.SimpleNamespace(argv=['launcher'],path=[]),'signal':types.SimpleNamespace(SIGTERM=15,SIGINT=2,signal=lambda *a:None),'config':lambda *a:dict(cfg),'prerequisites':lambda c:seen.append('prerequisites')}
  exec(compile(ast.Module(body=[fn],type_ignores=[]),'<launcher>','exec'),ns)
  with patch.dict(sys.modules,{'owner':types.SimpleNamespace(Owner=construct),'owner_wire':types.SimpleNamespace(Server=lambda *a:server)}):self.assertEqual(ns['main'](),0)
  self.assertEqual(seen,['prerequisites','owner','cleanup'])
if __name__=='__main__':unittest.main()
