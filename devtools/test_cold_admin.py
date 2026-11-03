from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy/docker/maintenance/cold'))
import unittest
from admin_entrypoint import configuration,Refused
class Config(unittest.TestCase):
 def test_disabled(self):
  with self.assertRaises(Refused):configuration({},['coordinator'])
 def test_trigger_no_provider_keys(self):
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64}
  self.assertEqual(configuration(env,['trigger'])['role'],'trigger')
  for k in ('BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_RESEND_API_KEY'):
   with self.assertRaises(Refused):configuration(dict(env,**{k:'secret'}),['trigger'])
 def test_fixed_schedule(self):
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64,'BACKUP_FIRST_DUE_UTC_SECONDS':'1793746800'}
  self.assertEqual(configuration(env,['observer'])['first_due'],1793746800)
  for raw in ('-1','tomorrow','999999999999999'):
   with self.assertRaises(Refused):configuration(dict(env,BACKUP_FIRST_DUE_UTC_SECONDS=raw),['observer'])
 def test_coordinator_credentials(self):
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64,'BACKUP_RESTIC_PASSWORD':'pw','BACKUP_B2_KEY_ID':'id','BACKUP_B2_APPLICATION_KEY':'key'}
  self.assertEqual(configuration(env,['coordinator'])['credentials']['key_id'],'id')
  with self.assertRaises(Refused):configuration(dict(env,BACKUP_RESTIC_PASSWORD='key'),['coordinator'])
 def test_repair_fixed_command_without_provider_credentials(self):
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64}
  self.assertEqual(configuration(env,['repair','rearm-idle'])['command'],'rearm-idle')
  for command in ('force-unlock','/bin/sh','latest',''):
   with self.assertRaises(Refused):configuration(env,['repair',command])
  for key in ('BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_RESEND_API_KEY'):
   with self.assertRaises(Refused):configuration(dict(env,**{key:'not-needed'}),['repair','inspect'])
 def test_repair_calls_only_fixed_private_coordinator(self):
  from unittest.mock import patch,Mock
  import io
  from contextlib import redirect_stdout
  from admin_entrypoint import main
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64}
  client=Mock();out=io.StringIO()
  with patch('owner_wire.Client',return_value=client) as factory,patch('repair_cli.operator_command',return_value={'phase':'RESOLVED'}) as operation,redirect_stdout(out):
   self.assertEqual(main(env,['repair','rearm-idle']),0)
  factory.assert_called_once_with('coordinator','coordinator.railway.internal',27002,'a'*64)
  operation.assert_called_once_with(client,'rearm-idle')
  self.assertNotIn('a'*64,out.getvalue())
  self.assertIn('RESOLVED',out.getvalue())
 def test_recover_explicit_no_environment_provider_credentials(self):
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64}
  self.assertEqual(configuration(env,['recover'])['role'],'recover')
  for key in ('BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_RESEND_API_KEY'):
   with self.assertRaises(Refused):configuration(dict(env,**{key:'not-needed'}),['recover'])
  with self.assertRaises(Refused):configuration(env,['recover','extra'])
 def test_recover_dispatch_requires_root_and_preserves_token_binding(self):
  from unittest.mock import patch
  from admin_entrypoint import main
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64}
  with patch('admin_entrypoint.os.getuid',return_value=10001),patch('recovery_controller.main') as run:
   with self.assertRaises(Refused):main(env,['recover'])
   run.assert_not_called()
  with patch('admin_entrypoint.os.getuid',return_value=0),patch('recovery_controller.main',return_value=2) as run:
   self.assertEqual(main(env,['recover']),2)
   run.assert_called_once_with(argv=[],expected_token='a'*64)
if __name__=='__main__':unittest.main()
