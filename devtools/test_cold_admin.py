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
 def recovery_env(self):
  return {'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64,'BACKUP_RESTIC_PASSWORD':'pw','BACKUP_B2_KEY_ID':'id','BACKUP_B2_APPLICATION_KEY':'key','BACKUP_RECOVERY_STRIPE_KEY':'_'.join(('sk','test','disposable')),'FIN_SERVICE_TOKEN':'b'*64,'FIN_RAIL_GATEWAY_CALLER_TOKEN':'c'*64}
 def test_recover_requires_protected_existing_inputs(self):
  env=self.recovery_env();c=configuration(env,['recover'])
  self.assertEqual(c['gateway_auth'],{'service_token':'b'*64,'caller_token':'c'*64});self.assertEqual(c['stripe_key'],env['BACKUP_RECOVERY_STRIPE_KEY'])
  for key in ('BACKUP_RECOVERY_STRIPE_KEY','FIN_SERVICE_TOKEN','FIN_RAIL_GATEWAY_CALLER_TOKEN','BACKUP_B2_APPLICATION_KEY'):
   missing=dict(env);missing.pop(key)
   with self.assertRaises(Refused):configuration(missing,['recover'])
  with self.assertRaises(Refused):configuration(dict(env,FIN_SERVICE_TOKEN='a'*64),['recover'])
 def test_recover_dispatch_requires_root_and_persistent_service(self):
  from unittest.mock import patch
  from admin_entrypoint import main
  env=self.recovery_env()
  with patch('admin_entrypoint.os.getuid',return_value=10001),patch('recovery_service.run') as run:
   with self.assertRaises(Refused):main(env,['recover'])
   run.assert_not_called()
  with patch('admin_entrypoint.os.getuid',return_value=0),patch('admin_entrypoint.volume') as volume,patch('recovery_service.run',return_value=2) as run:
   self.assertEqual(main(env,['recover']),2)
   volume.assert_called_once_with()
   self.assertEqual(run.call_args.args[0]['token'],'a'*64)
 def test_recovery_trigger_fixed_target_and_nonsecret_result(self):
  from unittest.mock import patch,Mock
  from contextlib import redirect_stdout
  import io,json
  from admin_entrypoint import main
  from b2_catalog import CATBASE
  run='cold-'+'a'*32
  locator={'schema':'b2-catalog-locator-v1','set_id':run,'repository':CATBASE+run,**dict.fromkeys(('snapshot','catalog_sha256','members_sha256','seals_sha256'),'b'*64)}
  scope=dict.fromkeys(('account','invoice','client','session','payment_intent','acceptance','tenant','audit'),'fixture');scope['mode']='TEST'
  request={'kind':'recover','selection':{'locator':locator,'cutoff_utc':'2026-11-03T00:00:00Z'},'scope':scope}
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64,'BACKUP_RECOVERY_REQUEST':json.dumps(request)}
  client=Mock();out=io.StringIO();result={'state':'OPEN','request_sha256':'b'*64}
  with patch('owner_wire.Client',return_value=client) as factory,patch('recovery_operator.wait_result',return_value=result) as operation,redirect_stdout(out):
   self.assertEqual(main(env,['recover-trigger']),0)
  factory.assert_called_once_with('coordinator','coordinator.railway.internal',27002,'a'*64)
  operation.assert_called_once_with(client,request)
  self.assertEqual(json.loads(out.getvalue()),result)
  self.assertNotIn(env['BACKUP_COORD_TOKEN'],out.getvalue())
 def test_recovery_trigger_rejects_provider_credentials(self):
  env={'BACKUP_LAUNCH_ENABLED':'1','BACKUP_COORD_TOKEN':'a'*64}
  for key in ('BACKUP_RECOVERY_STRIPE_KEY','FIN_SERVICE_TOKEN','FIN_RAIL_GATEWAY_CALLER_TOKEN','BACKUP_B2_APPLICATION_KEY'):
   with self.assertRaises(Refused):configuration(dict(env,**{key:'not-for-trigger'}),['recover-trigger'])
if __name__=='__main__':unittest.main()
