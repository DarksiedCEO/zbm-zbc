import _runtime_path
import unittest,hashlib,time,json,threading,http.client
from types import SimpleNamespace
from owner_wire import canonical
from recovery_gateway import Inbox,server

def fixture():
 obj={'id':'ch_test','amount':100};event={'id':'evt_test','type':'charge.refunded','created':123,'livemode':False,'api_version':'2026-09-30.endive','data':{'object':obj}}
 proof={'type':event['type'],'created':123,'object_id':'ch_test','object_sha256':hashlib.sha256(canonical(obj)).hexdigest()}
 inv={'account':'acct_test','mode':'TEST','events':{'evt_test':proof}}
 env={'request_id':'stripe-test','payload':canonical(event).decode(),'signature':'t=123,v1=synthetic'}
 return inv,env
class GatewayTests(unittest.TestCase):
 def test_duplicate_outcome_only_tamper_and_deadline(self):
  i=Inbox();inv,e=fixture();i.install(inv,{'evt_test'})
  self.assertEqual(i.accept(e),503);self.assertEqual(i.accept(e),503);self.assertEqual(len(i.queue),1)
  eid,raw,sig=i.take(time.monotonic()+1);self.assertEqual(eid,'evt_test');self.assertEqual(sig,e['signature'])
  i.done(eid);self.assertEqual(i.accept(e),200);self.assertEqual(i.queue,[])
  changed=dict(e,signature='new-signature');self.assertEqual(i.accept(changed),503);self.assertEqual(i.queue,[])
  v=json.loads(e['payload']);v['data']['object']['amount']=200
  with self.assertRaises(Exception):i.accept(dict(e,payload=canonical(v).decode()))
  with self.assertRaises(Exception):i.take(time.monotonic()-.01)
  i.close()
  with self.assertRaises(Exception):i.accept(e)
 def test_actual_loopback_gateway_auth_and_closed_routes(self):
  inv,e=fixture();inbox=Inbox();inbox.install(inv,{'evt_test'})
  service=SimpleNamespace(inbox=inbox,gateway_auth={'service_token':'service','caller_token':'caller'},is_open=lambda:False)
  s=server(service,('127.0.0.1',0));t=threading.Thread(target=s.serve_forever);t.start()
  def call(path,headers):
   c=http.client.HTTPConnection('127.0.0.1',s.server_port,timeout=2)
   try:c.request('POST',path,canonical(e),headers);r=c.getresponse();r.read();return r.status
   finally:c.close()
  try:
   headers={'Authorization':'Bearer service','X-FIN-Caller-Token':'caller'}
   self.assertEqual(call('/fin/v1/stripe/events',{}),503);self.assertEqual(len(inbox.queue),0)
   self.assertEqual(call('/fin/v1/stripe/events?other=1',headers),503);self.assertEqual(len(inbox.queue),0)
   self.assertEqual(call('/fin/v1/stripe/events',headers),503);self.assertEqual(len(inbox.queue),1)
   inbox.take(time.monotonic()+1);inbox.done('evt_test');self.assertEqual(call('/fin/v1/stripe/events',headers),200)
  finally:s.shutdown();s.server_close();t.join(2)
