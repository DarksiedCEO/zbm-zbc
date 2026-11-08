import copy, hashlib, json, sys, tempfile, unittest, uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy/gate5'))
import provision_operator as p
from test_credential_plan import fixture
import credential_plan as c

class Fake:
 def __init__(self):
  inv=fixture();roles=sorted({s for s,n in c.REQUIRED|set(c.URLS)})
  self.binding={**p.AUTHORIZED_TARGET,'services':{s:{'id':str(uuid.uuid4()),'complete_names':True,'names':sorted(n for (r,n),v in inv.items() if r==s and v.presence=='present')} for s in roles}}
  self.data={s:{'RAILWAY_PROJECT_ID':self.binding['project_id'],'RAILWAY_ENVIRONMENT_ID':self.binding['environment_id'],'RAILWAY_PROJECT_NAME':self.binding['project_name'],'RAILWAY_ENVIRONMENT_NAME':self.binding['environment_name'],'RAILWAY_SERVICE_ID':self.binding['services'][s]['id'],'RAILWAY_SERVICE_NAME':s,**{n:v._value for (r,n),v in inv.items() if r==s and v.presence=='present'}} for s in roles}
  self.writes=[];self.fail=False;self.drift=False;self.hidden={}
 def read(self,s):return copy.deepcopy(self.data[s])
 def write(self,s,n,v):
  self.writes.append((s,n,v))
  if self.fail:raise TimeoutError('synthetic-private-value')
  self.data[s][n]=v
  if self.drift:self.data[s]['UNRELATED']='changed'
 def prompt(self,label):return self.hidden[label]
 def generate(self):return hashlib.sha256(b'synthetic-generated').hexdigest()

class Tests(unittest.TestCase):
 def execute(self,f,path):return p.execute(f.binding,path,f.read,f.write,f.prompt,f.generate)
 def test_default_no_subprocess(self):
  with patch.object(p.subprocess,'run',side_effect=AssertionError('network')):self.assertEqual(p.main([]),0)
 def test_complete_preserves_maps_stdin_values_not_state(self):
  f=Fake()
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'state';self.execute(f,path);state=json.loads(path.read_text());self.assertEqual(state['status'],'COMPLETE_HOLD');self.assertTrue(f.writes)
   for _,_,value in f.writes:self.assertNotIn(value,path.read_text())
   self.assertIn('retained_custom',json.loads(f.data['finance']['FIN_CALLER_TOKENS']))
   with self.assertRaises(p.Refused):self.execute(f,path)
 def test_wrong_identity_zero_writes(self):
  f=Fake();f.data['legal']['RAILWAY_SERVICE_ID']=str(uuid.uuid4())
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(p.Refused):self.execute(f,Path(d)/'state')
  self.assertEqual(f.writes,[])
 def test_manifest_missing_role_or_duplicate_id_refuses(self):
  for mode in ('missing','duplicate'):
   f=Fake()
   if mode=='missing':del f.binding['services']['seo']
   else:f.binding['services']['seo']['id']=f.binding['services']['legal']['id']
   with tempfile.TemporaryDirectory() as d:
    with self.assertRaises(p.Refused):self.execute(f,Path(d)/'state')
   self.assertFalse(f.writes)
 def test_sealed_omission_is_prompted_never_generated(self):
  f=Fake();name='PORTAL_EVIDENCE_KEY';f.hidden['client-portal.'+name]=f.data['client-portal'].pop(name)
  f.generate=lambda:(_ for _ in ()).throw(AssertionError('rotated'))
  with tempfile.TemporaryDirectory() as d:self.execute(f,Path(d)/'state')
  self.assertFalse(any(s=='client-portal' and n==name for s,n,v in f.writes))
 def test_unexpected_live_name_refuses_no_generation(self):
  f=Fake();f.data['legal']['NEW_SECRET']='private'
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(p.Refused):self.execute(f,Path(d)/'state')
  self.assertFalse(f.writes)
 def test_generation_only_absent_then_full_preflight(self):
  f=Fake();name='PORTAL_EVIDENCE_KEY';del f.data['client-portal'][name];f.binding['services']['client-portal']['names'].remove(name)
  with tempfile.TemporaryDirectory() as d:self.execute(f,Path(d)/'state')
  self.assertIn(('client-portal',name,f.generate()),f.writes)
 def test_timeout_unknown_no_retry(self):
  f=Fake();f.fail=True
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'state'
   with self.assertRaises(p.Refused):self.execute(f,path)
   self.assertEqual(json.loads(path.read_text())['status'],'UNKNOWN');self.assertEqual(len(f.writes),1)
   with self.assertRaises(p.Refused):self.execute(f,path)
   self.assertEqual(len(f.writes),1)
 def test_postwrite_drift_unknown_stops(self):
  f=Fake();f.drift=True
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'state'
   with self.assertRaises(p.Refused):self.execute(f,path)
   self.assertEqual(json.loads(path.read_text())['status'],'UNKNOWN');self.assertEqual(len(f.writes),1)
 def test_transport_stdin_and_skip_deploys(self):
  f=Fake();cli=p.CLI(f.binding)
  class Result:returncode=0;stdout='';stderr=''
  with patch.object(p.subprocess,'run',return_value=Result()) as run:
   cli.write('legal','LEGAL_LINK_TAG_KEY','private-synthetic')
   args,kwargs=run.call_args;self.assertNotIn('private-synthetic',args[0]);self.assertIn('--skip-deploys',args[0]);self.assertEqual(kwargs['input'],'private-synthetic');self.assertIn(f.binding['services']['legal']['id'],args[0])
 def test_apply_missing_ack_zero_subprocess(self):
  with patch.object(p.subprocess,'run',side_effect=AssertionError('network')):
   with self.assertRaises(p.Refused):p.main(['--apply'])
 def test_openssl_output_and_command_are_bounded(self):
  class Result:returncode=0;stdout='a'*64+'\n';stderr=''
  with patch.object(p.subprocess,'run',return_value=Result()) as run:
   self.assertEqual(p.openssl(),'a'*64);self.assertEqual(run.call_args.args[0],['openssl','rand','-hex','32']);self.assertEqual(run.call_args.kwargs['timeout'],15)
 def test_hidden_input_requires_terminal(self):
  with patch.object(p.sys.stdin,'isatty',return_value=False), patch.object(p.getpass,'getpass') as prompt:
   with self.assertRaises(p.Refused):p.hidden('synthetic')
   prompt.assert_not_called()
 def test_interrupt_after_effect_is_unknown(self):
  f=Fake()
  def interrupt(s,n,v):f.writes.append((s,n,v));raise KeyboardInterrupt()
  f.write=interrupt
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'state'
   with self.assertRaises(p.Refused):self.execute(f,path)
   self.assertEqual(json.loads(path.read_text())['status'],'UNKNOWN');self.assertEqual(len(f.writes),1)
 def test_unsupported_map_refuses_without_replacement(self):
  f=Fake();v=json.loads(f.data['legal']['LEGAL_CALLER_TOKENS']);v['unknown_grant']='b'*64;f.data['legal']['LEGAL_CALLER_TOKENS']=json.dumps(v)
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(p.Refused):self.execute(f,Path(d)/'state')
  self.assertFalse(f.writes)
 def test_protected_other_project_refused_before_any_cli(self):
  f=Fake();f.binding.update(project_id='c57ae4eb-6ff9-4d70-b485-4300d74a10cf',project_name='zbestmedia-backend',environment_name='production')
  with patch.object(p.subprocess,'run',side_effect=AssertionError('CLI accessed')):
   with self.assertRaises(p.Refused):p.CLI(f.binding)
 def test_wrong_environment_or_names_refused_before_cli(self):
  for key,value in [('environment_id',str(uuid.uuid4())),('project_name','other'),('environment_name','staging')]:
   f=Fake();f.binding[key]=value
   with patch.object(p.subprocess,'run',side_effect=AssertionError('CLI accessed')):
    with self.assertRaises(p.Refused):p.CLI(f.binding)
 def test_unrelated_sealed_values_do_not_prompt_or_change(self):
  f=Fake();f.binding['services']['legal']['names'].append('UNRELATED_SEALED')
  f.binding['services']['finance']['names'].append('UNRELATED_NULL');f.data['finance']['UNRELATED_NULL']=None
  f.prompt=lambda label:(_ for _ in ()).throw(AssertionError('unnecessary hidden input'))
  with tempfile.TemporaryDirectory() as d:self.execute(f,Path(d)/'state')
  self.assertNotIn('UNRELATED_SEALED',f.data['legal']);self.assertIsNone(f.data['finance']['UNRELATED_NULL'])
  self.assertFalse(any(n.startswith('UNRELATED') for s,n,v in f.writes))
 def test_trigger_extra_manifest_names_refuse_even_sealed(self):
  for visible in (False,True):
   f=Fake();f.binding['services']['backup-trigger']['names'].append('UNEXPECTED_SECRET')
   if visible:f.data['backup-trigger']['UNEXPECTED_SECRET']='synthetic'
   f.prompt=lambda label:(_ for _ in ()).throw(AssertionError('should refuse by name'))
   with tempfile.TemporaryDirectory() as d:
    with self.assertRaises(p.Refused):self.execute(f,Path(d)/'state')
   self.assertFalse(f.writes)
 def test_unrelated_snapshot_drift_still_refuses(self):
  f=Fake();f.binding['services']['legal']['names'].append('UNRELATED_VISIBLE');f.data['legal']['UNRELATED_VISIBLE']='old';original=f.write
  def changed(s,n,v):original(s,n,v);f.data['legal']['UNRELATED_VISIBLE']='new'
  f.write=changed
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(p.Refused):self.execute(f,Path(d)/'state')
  self.assertEqual(len(f.writes),1)
 def test_trigger_disabled_control_preserved_without_prompt(self):
  f=Fake();f.binding['services']['backup-trigger']['names'].append('BACKUP_LAUNCH_ENABLED');f.data['backup-trigger']['BACKUP_LAUNCH_ENABLED']='0'
  f.prompt=lambda label:(_ for _ in ()).throw(AssertionError('control must not prompt'))
  with tempfile.TemporaryDirectory() as d:self.execute(f,Path(d)/'state')
  self.assertEqual(f.data['backup-trigger']['BACKUP_LAUNCH_ENABLED'],'0')
  self.assertFalse(any(s=='backup-trigger' and n=='BACKUP_LAUNCH_ENABLED' for s,n,v in f.writes))
 def test_trigger_control_hidden_or_enabled_refuses(self):
  for value in (None,'1','false','',0):
   f=Fake();f.binding['services']['backup-trigger']['names'].append('BACKUP_LAUNCH_ENABLED')
   if value is not None:f.data['backup-trigger']['BACKUP_LAUNCH_ENABLED']=value
   f.prompt=lambda label:(_ for _ in ()).throw(AssertionError('control must not prompt'))
   with tempfile.TemporaryDirectory() as d:
    with self.assertRaises(p.Refused):self.execute(f,Path(d)/'state')
   self.assertFalse(f.writes)
if __name__=='__main__':unittest.main()
