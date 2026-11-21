import _runtime_path
import unittest,tempfile,json,time,hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from recovery_owner import install
from recovery_controller import bind,observation
from owner_wire import canonical
class Tests(unittest.TestCase):
 def make(self,root):
  o=SimpleNamespace(member='finance',boot='b',front=SimpleNamespace(opened=False),phase='VERIFIED_CLOSED',child=SimpleNamespace(poll=lambda:None),source=Path(root),env={'FIN_STRIPE_LIVE':'0'},dispatch=lambda p,b: {'old':True});install(o)
  class Client:
   def call(self,p,b):return o.dispatch(p,b)
  return o,Client()
 def profile(self):return {'set_id':'cold-'+'a'*32,'account':'acct_test','invoice':'inv_test','client':'client_test','acceptance':'acc_test','tenant':'ten_test','audit':'aud_test','inventory':{'account':'acct_test','mode':'TEST','events':{}}}
 def test_closed_log_read_chunks_and_open_refusal(self):
  with tempfile.TemporaryDirectory() as d:
   raw=b'x'*30000;Path(d,'fin_log.jsonl').write_bytes(raw);o,c=self.make(d);bind(c,'finance','b',self.profile())
   co=SimpleNamespace(clients={'finance':c},doc={'boots':{'finance':'b'}})
   self.assertEqual(observation(co,'finance','finance-log',0),raw)
   o.front.opened=True
   with self.assertRaises(Exception):observation(co,'finance','finance-log',1)
 def test_binding_wrong_mode_and_status_mutation_refused(self):
  with tempfile.TemporaryDirectory() as d:
   o,c=self.make(d);p=self.profile();p['inventory']['mode']='LIVE'
   with self.assertRaises(Exception):bind(c,'finance','b',p)
   with self.assertRaises(Exception):c.call('/status',{'action':'recovery-bind','member':'finance','boot':'b','phase':'begin','bytes':10,'sha256':'a'*64})
 def test_source_manifest_before_start_only(self):
  with tempfile.TemporaryDirectory() as d:
   o,c=self.make(d);o.phase='CLOSED';o.child=None
   with patch('cold_capture.source_inventory',return_value={'a':{'sha256':'b'*64,'bytes':1}}):
    proof=c.call('/status',{'action':'recovery-source','member':'finance','boot':'b','set_id':'cold-'+'a'*32})
   self.assertEqual(proof['manifest_sha256'],hashlib.sha256(canonical({'member':'finance','set_id':'cold-'+'a'*32,'files':{'a':{'sha256':'b'*64,'bytes':1}}})).hexdigest())
   o.child=SimpleNamespace(poll=lambda:None)
   with self.assertRaises(Exception):c.call('/status',{'action':'recovery-source','member':'finance','boot':'b','set_id':'cold-'+'a'*32})
