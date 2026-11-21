import _runtime_path
import unittest,json,hashlib,base64,time
from types import SimpleNamespace as NS
from recovery_event import RecoveryEvents,Refused,submit_signed
from owner_wire import canonical
class Tests(unittest.TestCase):
 def setup_owner(self):
  self.raw=canonical({'id':'evt_one','type':'charge.refunded','livemode':False,'created':100,'api_version':'2026-09-30.endive','data':{'object':{'id':'ch_test','payment_intent':'pi_test'}}})
  obj=json.loads(self.raw)['data']['object']
  o=NS(member='finance',boot='a'*32,phase='VERIFIED_CLOSED',front=NS(opened=False),child=NS(poll=lambda:None),dispatch=lambda *a:None)
  self.binding={'member':'finance','boot':o.boot,'set_id':'cold-'+'b'*32,'account':'acct_test','event_id':'evt_one'}
  v=RecoveryEvents(o,set_id=self.binding['set_id'],account='acct_test',inventory={'account':'acct_test','mode':'TEST','events':{'evt_one':{'type':'charge.refunded','object_id':'ch_test','created':100,'object_sha256':hashlib.sha256(canonical(obj)).hexdigest()}}})
  return o,v
 def test_original_bytes_signature_once_lost_ack_status_only(self):
  o,v=self.setup_owner();seen=[]
  def native(raw,sig,eid):seen.append((raw,sig,eid));return {'event_id':eid,'status':'refunded'}
  v.native=native
  class Client:
   def call(_,path,body):
    result=v.apply(body)
    if body['phase']=='commit':raise Refused('lost acknowledgement')
    return result
  result=submit_signed(Client(),self.binding,self.raw,'t=100,v1='+'c'*64)
  self.assertEqual(result['state'],'DONE');self.assertEqual(seen,[(self.raw,'t=100,v1='+'c'*64,'evt_one')]);self.assertFalse(o.front.opened)
  with self.assertRaises(Refused):submit_signed(Client(),self.binding,self.raw,'t=100,v1='+'c'*64)
  self.assertEqual(len(seen),1)
 def test_wrong_scope_unknown_event_and_open_refused(self):
  o,v=self.setup_owner()
  for field,value in [('set_id','cold-'+'c'*32),('boot','c'*32),('account','acct_other'),('event_id','evt_other')]:
   with self.assertRaises(Refused):v.apply(dict(self.binding,action='recovery-event',phase='status',**{field:value}))
  o.front.opened=True
  with self.assertRaises(Refused):v.apply(dict(self.binding,action='recovery-event',phase='status'))
 def test_changed_event_object_refused_before_native(self):
  o,v=self.setup_owner();v.native=lambda *a:self.fail('must not forward')
  raw=self.raw.replace(b'pi_test',b'pi_wrong')
  class Client:
   def call(_,path,body):return v.apply(body)
  with self.assertRaises(Refused):submit_signed(Client(),self.binding,raw,'t=100,v1='+'c'*64)
if __name__=='__main__':unittest.main()
