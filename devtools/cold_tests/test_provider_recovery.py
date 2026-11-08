import _runtime_path
import unittest,time,json
from provider_recovery import ProviderInterval,Refused
class Port:
 def __init__(self):self.calls=[];self.live=False
 def _call(self,method,path,query=None):
  self.calls.append((method,path,query))
  if path=='/v1/account':return 'ok',200,{'id':'acct_test','object':'account'}
  if path=='/v1/events':return 'ok',200,{'object':'list','url':'/v1/events','has_more':False,'data':[{'id':'evt_one','object':'event','type':'charge.refunded','created':100,'livemode':self.live,'api_version':'2026-09-30.endive','data':{'object':{'id':'ch_test','object':'charge','payment_intent':'pi_test'}}}]}
  raise AssertionError('unexpected transport')
class Tests(unittest.TestCase):
 def make(self,p):return ProviderInterval(p,account='acct_test',invoice='inv_test',client='client_test',session='cs_test',payment_intent='pi_test',cutoff=90,through=110,now=111)
 def test_inventory_uses_all_deliveries_and_bound_account(self):
  p=Port();v=self.make(p).inventory();self.assertEqual(v['events']['evt_one']['object_id'],'ch_test')
  self.assertIn('types[]',str(p.calls));self.assertNotIn('refund.created',str(p.calls));self.assertNotIn('delivery_success',str(p.calls));self.assertTrue(all(x[0]=='GET' for x in p.calls))
 def test_live_and_old_interval_refused(self):
  p=Port();p.live=True
  with self.assertRaises(Refused):self.make(p).inventory()
  with self.assertRaises(Refused):ProviderInterval(p,account='acct_test',invoice='inv_test',client='client_test',session='cs_test',payment_intent='pi_test',cutoff=0,through=30*86400,now=31*86400)
 def test_reconcile_provider_totals_against_complete_native_shape(self):
  from test_money_recovery import fixture
  class MoneyPort(Port):
   refund=50000
   amount=150000
   def _call(self,method,path,query=None):
    if path.startswith('/v1/checkout/sessions/'):
     return 'ok',200,{'id':'cs_test','object':'checkout.session','livemode':self.live,'payment_intent':'pi_test','client_reference_id':'inv_test','currency':'usd','amount_total':self.amount,'payment_status':'paid'}
    if path.startswith('/v1/payment_intents/'):
     return 'ok',200,{'id':'pi_test','object':'payment_intent','livemode':self.live,'status':'succeeded','currency':'usd','amount_received':self.amount,'metadata':{'invoice_id':'inv_test'},'latest_charge':{'livemode':self.live,'payment_intent':'pi_test','currency':'usd','amount':self.amount,'amount_refunded':self.refund,'balance_transaction':{'currency':'usd','amount':self.amount,'fee':4380,'net':self.amount-4380}}}
    return super()._call(method,path,query)
  p=MoneyPort();v=self.make(p);inventory=v.inventory();snapshot=fixture()
  result=v.reconcile(inventory,snapshot);self.assertEqual(result['state'],'RECONCILED_CLOSED');self.assertEqual(result['business_admission'],'NOT_OPENED')
  p.refund=150000;snapshot=fixture(150000);snapshot['invoice']['status']='issued'
  self.assertEqual(v.reconcile(inventory,snapshot)['money']['receivable_cents'],150000)
  p.refund=150001
  with self.assertRaises(Refused):v.provider_money(snapshot['invoice'])
  p.refund=0;p.amount=90000;snapshot['invoice']['total']='900.00'
  self.assertEqual(v.provider_money(snapshot['invoice'])['amount_cents'],90000)
  p.live=True
  live=ProviderInterval(p,account='acct_test',invoice='inv_test',client='client_test',session='cs_test',payment_intent='pi_test',cutoff=90,through=110,now=111,livemode=True)
  self.assertEqual(live.inventory()['mode'],'LIVE')
  self.assertEqual(live.provider_money(snapshot['invoice'])['amount_cents'],90000)
 def test_missing_delivery_never_reconciled(self):
  p=Port();v=self.make(p)
  with self.assertRaises(Refused):v.require_deliveries(v.inventory(),b'{"data":{"ops":[]}}\n')
if __name__=='__main__':unittest.main()
