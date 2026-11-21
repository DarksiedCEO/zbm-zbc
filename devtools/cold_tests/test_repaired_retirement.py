import _runtime_path  # fixed repository-relative packaged runtime
"""Ordinary success guards must not silently accept repaired incidents."""
import unittest,tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from schedule_advance import prepare
from schedule_state import Schedule
from owner_wire import Refused
class ExistingBoundary(unittest.TestCase):
 def test_ordinary_advance_refuses_even_resolved_repair(self):
  c=NS(observer=object(),doc={'phase':'RETIRED','repair_rpc':{'phase':'RESOLVED'}})
  with self.assertRaisesRegex(Refused,'only unrepaired success'):prepare(c)
 def test_failed_observer_cannot_be_relabelled_ordinary_complete(self):
  with tempfile.TemporaryDirectory() as root:
   s=Schedule(Path(root)/'state.db');self.addCleanup(s.close);run='cold-'+'a'*32
   s.bind(100,run);s.dispatched(100,run);s.failed()
   proof={'due':100,'trigger_id':'scheduled-100','run':run,'catalog_sha256':'a'*64,'catalog_snapshot':'b'*64,'completion_sha256':'c'*64}
   with self.assertRaises(Refused):s.complete(proof)
   self.assertEqual(s.current()['phase'],'FAILED')
class RequestedCommand(unittest.TestCase):
 def test_explicit_repaired_retirement_entrypoint_exists(self):
  import repaired_retirement
  self.assertTrue(callable(repaired_retirement.operator_finish))
class RepairedObserver(unittest.TestCase):
 def test_preserves_failed_history_dates_and_next_due(self):
  from repaired_observer import complete
  with tempfile.TemporaryDirectory() as root:
   s=Schedule(Path(root)/'state.db');self.addCleanup(s.close);run='cold-'+'a'*32;s.arm(100,86400);s.bind(100,run);s.dispatched(100,run);s.failed();old=s.current()
   p={'schema':'repaired-complete-v1','due':100,'trigger_id':'scheduled-100','run':run,'catalog_sha256':'a'*64,'catalog_snapshot':'b'*64,'completion_sha256':'c'*64,'incident_sha256':'d'*64,'archives_sha256':'e'*64}
   value=complete(s,p);self.assertEqual(value['phase'],'REPAIRED_COMPLETE');self.assertEqual(s.monitoring()['expected_due'],86500);self.assertEqual(complete(s,p),value)
   import json
   self.assertEqual(json.loads(s.db.execute('select body from repaired_history').fetchone()[0])['original'],old)
   with self.assertRaises(Refused):complete(s,dict(p,incident_sha256='f'*64))
   s.bind(86500,'cold-'+'b'*32);s.dispatched(86500,'cold-'+'b'*32);s.failed()
   with self.assertRaises(Refused):complete(s,dict(p,due=86500,trigger_id='scheduled-86500',run='cold-'+'b'*32))
class ObserverBoundary(unittest.TestCase):
 def test_authenticated_handler_fetches_fixed_proof_not_caller_boolean(self):
  from observer_control import Control
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as root:
   c=Control(Path(root)/'observer.db','a'*64,first_due=100,cadence=86400,grace=900);self.addCleanup(c.close);run='cold-'+'a'*32
   c.schedule.bind(100,run);c.schedule.dispatched(100,run);c.schedule.failed();calls=[]
   p={'schema':'repaired-complete-v1','due':100,'trigger_id':'scheduled-100','run':run,'catalog_sha256':'a'*64,'catalog_snapshot':'b'*64,'completion_sha256':'c'*64,'incident_sha256':'d'*64,'archives_sha256':'e'*64}
   def fetch(path,body):calls.append((path,body));return p
   c.completion=SimpleNamespace(call=fetch)
   with self.assertRaises(Refused):c.dispatch('/submit',{'action':'repaired-complete','due':100,'run':run,'healthy':True})
   self.assertEqual(calls,[])
   result=c.dispatch('/submit',{'action':'repaired-complete','due':100,'run':run});self.assertEqual(result['phase'],'REPAIRED_COMPLETE');self.assertEqual(calls,[('/status',{'kind':'repaired_complete','due':100,'run':run})])
 def test_cli_routes_explicit_command(self):
  from repair_cli import operator_command
  from unittest.mock import patch
  with patch('repaired_retirement.operator_finish',return_value={'phase':'DONE'}) as run:
   client=object();self.assertEqual(operator_command(client,'finish-repaired-run'),{'phase':'DONE'});run.assert_called_once_with(client)
