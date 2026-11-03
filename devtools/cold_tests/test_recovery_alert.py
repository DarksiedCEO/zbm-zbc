import _runtime_path
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from datetime import datetime,timezone
from observer_control import Control
from schedule_state import MAX_EVENTS
from cold_alerts import Alerts
from owner_wire import Refused
class RecoveryAlert(unittest.TestCase):
 def test_durable_idempotent_without_schedule_mutation(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'monitor.sqlite';c=Control(p,'a'*64,first_due=2000000000,cadence=86400,grace=900)
   body={'action':'recovery-locked','run':'cold-'+'b'*32,'identity':'c'*64}
   before=c.dispatch('/status',{'kind':'monitor_status'})
   one=c.dispatch('/submit',body);self.assertEqual(one,c.dispatch('/submit',body));c.close()
   c=Control(p,'a'*64,first_due=2000000000,cadence=86400,grace=900)
   self.assertEqual(before,c.dispatch('/status',{'kind':'monitor_status'}))
   with c.store() as s:
    rows=s.pending();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['body']['event']['error'],'RECOVERY_LOCKED')
   c.close()
 def test_fixed_shape_and_capacity_never_evict(self):
  with tempfile.TemporaryDirectory() as d:
   c=Control(Path(d)/'monitor.sqlite','a'*64,first_due=2000000000,cadence=86400,grace=900)
   base={'action':'recovery-locked','run':'cold-'+'b'*32,'identity':'c'*64}
   for bad in (dict(base,reason='secret'),dict(base,run='bad'),dict(base,identity='bad')):
    with self.assertRaises(Refused):c.dispatch('/submit',bad)
   for i in range(MAX_EVENTS):c.dispatch('/submit',dict(base,identity=f'{i:064x}'))
   with self.assertRaises(Refused):c.dispatch('/submit',base)
   with c.store() as s:self.assertEqual(len(s.pending()),MAX_EVENTS)
   c.close()
 def test_existing_alert_bridge_maps_locked(self):
  with tempfile.TemporaryDirectory() as d:
   c=Control(Path(d)/'monitor.sqlite','a'*64,first_due=2000000000,cadence=86400,grace=900)
   c.dispatch('/submit',{'action':'recovery-locked','run':'cold-'+'b'*32,'identity':'c'*64})
   dispatcher=Mock();dispatcher.step.return_value={'primary':'ACCEPTED','secondary':'ACCEPTED'}
   with patch('cold_alerts.FallbackDispatcher',return_value=dispatcher):
    a=Alerts(c,Path(d)/'outbox',{},None);a.tick(datetime.now(timezone.utc))
   self.assertEqual(dispatcher.step.call_args.args[0]['error'],'LOCKED')
   with c.store() as s:self.assertEqual(s.pending(),[])
   c.close()
if __name__=='__main__':unittest.main()
