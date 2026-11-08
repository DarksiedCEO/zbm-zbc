"""Narrow nonsecret dependency route completion; no API/provider startup."""
import json, subprocess, sys, tempfile, unittest
from pathlib import Path
from test_credential_plan import fixture
import credential_plan as c
ROOT=Path(__file__).resolve().parents[2]
CASES={'legal':('LEGAL_COMPLIANCE','compliance',27112),
       'compliance':('COMPLIANCE_LEGAL','legal',27111),
       'finance':('FIN_COMPLIANCE','compliance',27112),
       'seo':('SEO_COMPLIANCE','compliance',27112)}

class DependencyURLs(unittest.TestCase):
 def test_planned_credentials_have_urls_rewritten_and_validated(self):
  planned=c.plan(fixture())
  for role,(prefix,target,port) in CASES.items():
   with self.subTest(role=role):
    name=prefix+'_URL'
    self.assertEqual(planned._urls[(role,name)],'http://${{'+target+'.RAILWAY_PRIVATE_DOMAIN}}:27001')
    # Isolated module namespace avoids the applications' same-named config/store modules.
    code='''import sys,json,tempfile,types
sys.path.insert(0,RELAY)
from owner_relay import configure_environment
sys.path.insert(0,SOURCE)
import config
prefix=PREFIX;role=ROLE
with tempfile.TemporaryDirectory() as directory:
 env={prefix+'_TOKEN':'a'*64,prefix+'_CALLER_TOKEN':'b'*64}
 def validate():
  if role=='finance':return config._thin(env,prefix,'Compliance')
  if role=='seo':return config._compliance(env,types.SimpleNamespace(service_token='c'*64,andre_token='d'*64,caller_tokens={},tenant_tokens={},finance_token=None,finance_caller_token=None))
  app='LEGAL' if role=='legal' else 'COMPLIANCE'
  return config.load({app+'_SERVICE_TOKEN':'c'*64,app+'_DATA_DIR':directory,**env})
 try:validate()
 except RuntimeError as exc:assert prefix+'_URL' in str(exc)
 else:raise AssertionError('missing URL not refused')
 env[prefix+'_URL']=URL
 configure_environment(role,env)
 assert env[prefix+'_URL']=='http://127.0.0.1:'+str(PORT)
 settings=validate()
 if role in ('legal','compliance') and settings.data_dir_lock:settings.data_dir_lock.release()
 print('missing refused; relay URL and native config accepted')
'''
    for key,value in {'RELAY':str(ROOT/'deploy/docker/maintenance/cold'),'SOURCE':str(ROOT/f'services/{role}-py/src'),'PREFIX':prefix,'ROLE':role,'URL':planned._urls[(role,name)],'PORT':port}.items():code=key+'='+repr(value)+'\n'+code
    result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True,timeout=15)
    self.assertEqual(result.returncode,0,result.stderr)
if __name__=='__main__':unittest.main()
