import _runtime_path
import unittest,json,copy
from provider_recovery import ProviderInterval,Refused
from recovery_money import digest,check

def fixture(total_refund=50000):
 entries=[];events=[];logs=[];head=None;prev='0'*64;epoch=None;refund=0
 for seq,(flow,amount,accounts) in enumerate([('F11',150000,('1100','4110')),('F13',150000,('1060','1100')),('F13f',4380,('5020','1060')),('F7r',25000,('1100','1060')),('F7r',total_refund-25000,('1100','1060'))],1):
  refund+=amount if flow=='F7r' else 0
  money=lambda n:f'{n//100}.{n%100:02d}'
  source={'kind':'invoice' if flow=='F11' else 'stripe_refund' if flow=='F7r' else 'receipt','id':'inv_test' if flow=='F11' else 'rct_test'}
  lines=[{'account':a,'subledger':'client:client_test' if a=='1100' else None,'debit':money(amount if i==0 else 0),'credit':money(amount if i==1 else 0)} for i,a in enumerate(accounts)]
  eid='entry'+str(seq);le='event'+str(seq);idem=flow+'|'+('inv_test' if flow=='F11' else 'pi_test')+('|'+money(refund) if flow=='F7r' else '')
  e={'entry_id':eid,'entity':'zbm','memo_code':flow,'lines':lines,'source':source,'idempotency_key':idem,'reverses_entry_id':None,'approval_ref':None,'prev_entry_sha256':head,'ledger_event_id':le};head=digest(e);e['entry_sha256']=head;entries.append(e)
  fields=('entity','lines','memo_code','source','idempotency_key','reverses_entry_id','approval_ref')
  payload={'entry_id':eid,'entity':'zbm','flow':flow,'total':money(amount),'content_sha256':digest({k:e[k] for k in fields}),'lines':2,'source_kind':source['kind'],'source_id':source['id'],'rk':'rk'+str(seq),'seq':seq}
  evidence={'event_id':le,'event_type':'journal_entry_posted','rk':'rk'+str(seq),'payload_sha256':digest(payload)}
  events.append({'event_id':le,'department':'finance','event_type':'journal_entry_posted','subject_id':eid,'payload_sha256':digest(payload)})
  ops=[] if seq!=5 else [['put',{'coll':'stripe_events','id':'evt_one','rec':{'event_id':'evt_one','type':'charge.refunded','object_id':'ch_test','status':'refunded'}}]]
  row={'seq':seq,'kind':'put','prev_line_sha256':prev,'data':{'ops':ops,'evidence':[evidence]}};row['record_sha256']=digest(row)
  raw=json.dumps(row,sort_keys=True,separators=(',',':')).encode();prev=__import__('hashlib').sha256(raw).hexdigest();epoch=epoch or prev[:16];logs.append(raw)
  events.append({'event_id':f'fin-log-{epoch}-{seq}-{prev[:40]}','department':'finance','event_type':'local_log_appended','subject_id':'fin-log','payload_sha256':digest({'epoch':epoch,'seq':seq,'line_sha256':prev,'kind':'put'})})
 return {'journal':{'entries':entries,'next_cursor':None},'ledger_events':events,'finance_log':b'\n'.join(logs)+b'\n','invoice':{'invoice_id':'inv_test','client_id':'client_test','status':'paid','total':'1500.00','currency':'USD','refunded':f'{total_refund//100}.{total_refund%100:02d}'}}
class Tests(unittest.TestCase):
 def test_complete_five_entries_actual_evidence_shape(self):
  v=fixture();p=check(v['journal']['entries'],v['ledger_events'],finance_log=v['finance_log'],invoice_id='inv_test',client_id='client_test',receipt_id='rct_test',payment_intent='pi_test',amount_cents=150000,fee_cents=4380,refund_cents=50000)
  self.assertEqual(p['entry_count'],5);self.assertEqual(p['stripe_clearing_cents'],95620)
 def test_full_refund_and_excess(self):
  v=fixture(150000)
  p=check(v['journal']['entries'],v['ledger_events'],finance_log=v['finance_log'],invoice_id='inv_test',client_id='client_test',receipt_id='rct_test',payment_intent='pi_test',amount_cents=150000,fee_cents=4380,refund_cents=150000)
  self.assertEqual(p['stripe_clearing_cents'],-4380)
  with self.assertRaises(Exception):check(v['journal']['entries'],v['ledger_events'],finance_log=v['finance_log'],invoice_id='inv_test',client_id='client_test',receipt_id='rct_test',payment_intent='pi_test',amount_cents=150000,fee_cents=4380,refund_cents=150001)
 def test_changed_ledger_or_amount_refuses(self):
  for mode in ('ledger','amount'):
   v=fixture()
   if mode=='ledger':v['ledger_events'][0]['payload_sha256']='a'*64
   else:v['journal']['entries'][-1]['lines'][0]['debit']='251.00'
   with self.assertRaises(Exception):check(v['journal']['entries'],v['ledger_events'],finance_log=v['finance_log'],invoice_id='inv_test',client_id='client_test',receipt_id='rct_test',payment_intent='pi_test',amount_cents=150000,fee_cents=4380,refund_cents=50000)
if __name__=='__main__':unittest.main()
