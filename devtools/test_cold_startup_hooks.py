import ast,os,sys,types,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]/'services'
class Hooks(unittest.TestCase):
 def profile(self,member):
  ns={};exec((ROOT/(member+'-py')/'src/startup_profile.py').read_text(),ns);return ns['existing_store_profile']
 def test_profiles(self):
  for m in ('finance','legal','compliance','seo'):
   for value,result in [('normal',False),('existing-store',True)]:
    with self.subTest(member=m,value=value),patch.dict(os.environ,{'ZBM_STARTUP_PROFILE':value}):self.assertEqual(self.profile(m)(),result)
   with patch.dict(os.environ,{'ZBM_STARTUP_PROFILE':'anything-else'}),self.assertRaises(ValueError):self.profile(m)()
 def test_factories_forward_actual_profile(self):
  for m in ('finance','legal','compliance'):
   tree=ast.parse((ROOT/(m+'-py')/'src/api.py').read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_app_from_env')
   for value,wanted in [('normal',False),('existing-store',True)]:
    calls=[];settings=object();service=object();app=types.SimpleNamespace(state=types.SimpleNamespace())
    def build(s,**kw):calls.append((s,kw));return service
    def create(s,c):self.assertIs(s,service);self.assertIs(c,settings);return app
    ns={'existing_store_profile':self.profile(m),'config_mod':types.SimpleNamespace(load=lambda:settings),'build_service':build,'create_app':create,'FastAPI':object}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'<factory>','exec'),ns)
    with patch.dict(os.environ,{'ZBM_STARTUP_PROFILE':value}):self.assertIs(ns['_app_from_env'](),app)
    self.assertEqual(calls,[(settings,{'require_existing_seed':wanted})])
    if m=='compliance':self.assertEqual(app.state.startup_existing_store,wanted)
 def test_seo_existing_mode(self):
  tree=ast.parse((ROOT/'seo-py/src/api.py').read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
  for value,wanted in [('normal',False),('existing-store',True)]:
   calls=[];app=object();svc=types.SimpleNamespace(settings=types.SimpleNamespace(bind_addr='127.0.0.1',port=1));serve=types.SimpleNamespace(run=lambda *x:calls.append(x))
   def build(**kw):self.assertEqual(kw,{'observe_existing_on_start':wanted});return app,svc
   ns={'existing_store_profile':self.profile('seo'),'build':build};exec(compile(ast.Module(body=[fn],type_ignores=[]),'<main>','exec'),ns)
   with patch.dict(os.environ,{'ZBM_STARTUP_PROFILE':value}),patch.dict(sys.modules,{'serve':serve}):ns['main']()
   self.assertEqual(calls,[(app,'127.0.0.1',1)])
if __name__=='__main__':unittest.main()
