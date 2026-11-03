import _runtime_path
import unittest,json,hashlib
from unittest.mock import Mock
from owner_wire import Refused,canonical
from b2_adapter import Refused as B2Refused
from recovery_operator import wait_result,request_from_text
class Operator(unittest.TestCase):
 def test_nonsecret_exact_request_only(self):
  from b2_catalog import CATBASE
  run='cold-'+'a'*32;loc={'schema':'b2-catalog-locator-v1','set_id':run,'repository':CATBASE+run,**dict.fromkeys(('snapshot','catalog_sha256','members_sha256','seals_sha256'),'b'*64)}
  scope=dict.fromkeys(('account','invoice','client','session','payment_intent','acceptance','tenant','audit'),'fixture');scope['mode']='TEST'
  request={'kind':'recover','selection':{'locator':loc,'cutoff_utc':'2026-11-03T00:00:00Z'},'scope':scope}
  self.assertEqual(request_from_text(json.dumps(request)),request)
  for value in (dict(request,password='not-allowed'),dict(request,scope=dict(scope,mode='other')),dict(request,selection={'locator':loc})):
   with self.assertRaises((Refused,B2Refused)):request_from_text(json.dumps(value))
  with self.assertRaises(Refused):request_from_text('x'*16385)
 def test_lost_submit_only_queries_exact_identity(self):
  request={'kind':'recover'};digest=hashlib.sha256(canonical(request)).hexdigest();client=Mock()
  client.call.side_effect=[Refused('lost'),{'state':'RUNNING','request_sha256':digest},{'state':'OPEN','request_sha256':digest}]
  times=iter([0,1,2]);result=wait_result(client,request,clock=lambda:next(times),sleep=lambda _:None)
  self.assertEqual(result['state'],'OPEN');self.assertEqual([x.args[0] for x in client.call.call_args_list],['/submit','/status','/status'])
 def test_conflict_locked_or_unbound_cannot_succeed(self):
  req={'kind':'recover'};digest=hashlib.sha256(canonical(req)).hexdigest()
  for response in ({'state':'OPEN','request_sha256':'a'*64},{'state':'LOCKED','request_sha256':digest},{'state':'WAIT','request_sha256':None}):
   client=Mock();client.call.side_effect=[{},response]
   with self.assertRaises(Refused):wait_result(client,req,clock=lambda:0,sleep=lambda _:None)
 def test_whole_deadline(self):
  req={'kind':'recover'};client=Mock();times=iter([0,901])
  with self.assertRaises(Refused):wait_result(client,req,clock=lambda:next(times),sleep=lambda _:None)
  self.assertEqual(client.call.call_count,1)
if __name__=='__main__':unittest.main()
