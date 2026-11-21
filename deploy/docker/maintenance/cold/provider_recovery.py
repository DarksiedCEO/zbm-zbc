"""First-client TEST recovery: provider inventory + existing signed redelivery.

Trusted constructor only. Never signs, resends, ingests, or opens admission.
All network operations are fixed Stripe GETs through the actual Finance adapter.
The caller holds external admission closed and owns a complete native evidence
snapshot; a supplied boolean, receipt list, or operator assertion is not proof.
"""
import json,re,time,hashlib
class Refused(ValueError):pass
VERSION='2026-09-30.endive'
TYPES={'checkout.session.completed','payment_intent.succeeded','charge.succeeded','charge.refunded'}
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def integer(v):return type(v)is int and v>=0
class ProviderInterval:
 def __init__(self,port,*,account,invoice,client,session,payment_intent,cutoff,through,now,livemode=False):
  for value,prefix in ((account,'acct_'),(session,'cs_'),(payment_intent,'pi_')):
   if type(value)is not str or not re.fullmatch(prefix+r'[A-Za-z0-9_]{1,120}',value):raise Refused('fixed provider identity')
  for value in (invoice,client):
   if type(value)is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',value):raise Refused('fixed business identity')
  if not all(integer(x) for x in (cutoff,through,now)) or not 0<=cutoff<=through<=now or now-cutoff>29*86400:raise Refused('bounded retained interval required')
  if type(livemode)is not bool:raise Refused('configured mode required')
  self.live=livemode
  self.port=port;self.account=account;self.invoice=invoice;self.client=client;self.session=session;self.pi=payment_intent;self.cutoff=cutoff;self.through=through
 def get(self,path,query=None):
  kind,status,value=self.port._call('GET',path,query=query)
  if kind!='ok' or status!=200 or type(value)is not dict or len(json.dumps(value).encode())>1048576:raise Refused('provider read refused')
  return value
 def account_check(self):
  a=self.get('/v1/account')
  if a.get('object')!='account' or a.get('id')!=self.account:raise Refused('provider account mismatch')
 def inventory(self):
  self.account_check();events={};cursor=None;last=None
  for page in range(10):
   q=[('limit','100'),('created[gte]',str(self.cutoff)),('created[lte]',str(self.through))]+[('types[]',typ) for typ in sorted(TYPES)]
   if cursor:q.append(('starting_after',cursor))
   v=self.get('/v1/events',q)
   if v.get('object')!='list' or v.get('url')!='/v1/events' or type(v.get('has_more'))is not bool or type(v.get('data'))is not list or len(v['data'])>100:raise Refused('event page shape')
   for ev in v['data']:
    if type(ev)is not dict or ev.get('object')!='event' or ev.get('livemode')is not self.live or ev.get('api_version')!=VERSION or ev.get('type') not in TYPES:raise Refused('unsupported event scope')
    eid=ev.get('id');created=ev.get('created');obj=ev.get('data',{}).get('object',{})
    if type(eid)is not str or not re.fullmatch('evt_[A-Za-z0-9_]{1,120}',eid) or eid in events or not integer(created) or not self.cutoff<=created<=self.through or (last is not None and created>last):raise Refused('event identity/order')
    if type(obj)is not dict or type(obj.get('id'))is not str:raise Refused('event object')
    typ=ev['type'];oid=obj['id']
    matches=(oid==self.session if typ.startswith('checkout.') else oid==self.pi if typ.startswith('payment_intent.') else obj.get('payment_intent')==self.pi)
    if not matches:raise Refused('outside fixed first-client scope')
    events[eid]={'type':typ,'object_id':oid,'created':created,'object_sha256':digest(obj)};last=created
   if not v['has_more']:
    self.account_check()
    return {'account':self.account,'mode':'LIVE' if self.live else 'TEST','invoice':self.invoice,'payment_intent':self.pi,'cutoff':self.cutoff,'through':self.through,'events':events,'inventory_sha256':digest(events)}
   if not v['data']:raise Refused('empty continuation')
   cursor=v['data'][-1]['id']
  raise Refused('event pagination bound')
 def require_deliveries(self,inventory,finance_log):
  if type(finance_log)is not bytes or len(finance_log)>4*1048576 or not finance_log.endswith(b'\n'):raise Refused('finance evidence bound')
  observed={}
  for line in finance_log.splitlines():
   for kind,value in json.loads(line)['data'].get('ops',[]):
    if kind=='put' and value.get('coll')=='stripe_events':observed[value['id']]=value['rec']
  for eid,event in inventory['events'].items():
   rec=observed.get(eid,{})
   if rec.get('event_id')!=eid or rec.get('type')!=event['type'] or rec.get('object_id')!=event['object_id'] or not isinstance(rec.get('status'),str) or rec['status'].startswith('ignored'):raise Refused('fresh signed delivery not committed')
  return observed
 def provider_money(self,invoice):
  from recovery_money import cents
  if type(invoice)is not dict or invoice.get('invoice_id')!=self.invoice or invoice.get('client_id')!=self.client or invoice.get('currency')!='USD':raise Refused('restored invoice currency/identity')
  amount=cents(invoice.get('total'))
  if amount<=0:raise Refused('restored invoice amount')
  s=self.get('/v1/checkout/sessions/'+self.session)
  if s.get('id')!=self.session or s.get('object')!='checkout.session' or s.get('livemode')is not self.live or s.get('payment_intent')!=self.pi or s.get('client_reference_id')!=self.invoice or s.get('currency')!='usd' or s.get('amount_total')!=amount or s.get('payment_status')!='paid':raise Refused('checkout binding')
  p=self.get('/v1/payment_intents/'+self.pi,[('expand[]','latest_charge.balance_transaction')])
  ch=p.get('latest_charge');bt=ch.get('balance_transaction') if isinstance(ch,dict) else None
  if p.get('id')!=self.pi or p.get('object')!='payment_intent' or p.get('livemode')is not self.live or p.get('status')!='succeeded' or p.get('currency')!='usd' or p.get('amount_received')!=amount or p.get('metadata',{}).get('invoice_id')!=self.invoice:raise Refused('payment binding')
  if not isinstance(ch,dict) or ch.get('livemode')is not self.live or ch.get('payment_intent')!=self.pi or ch.get('currency')!='usd' or ch.get('amount')!=amount or not integer(ch.get('amount_refunded')) or not 0<=ch['amount_refunded']<=amount or not isinstance(bt,dict):raise Refused('charge binding')
  if bt.get('currency')!='usd' or bt.get('amount')!=amount or not integer(bt.get('fee')) or not 0<=bt['fee']<amount or bt.get('net')!=amount-bt['fee']:raise Refused('fee binding')
  return {'amount_cents':amount,'fee_cents':bt['fee'],'refund_cents':ch['amount_refunded']}
 def reconcile(self,inventory,snapshot):
  if inventory!=self.inventory():raise Refused('provider inventory changed')
  if type(snapshot)is not dict or set(snapshot)!={'journal','ledger_events','finance_log','invoice'} or snapshot['journal'].get('next_cursor') is not None:raise Refused('complete closed evidence required')
  self.require_deliveries(inventory,snapshot['finance_log'])
  from recovery_money import check
  money=self.provider_money(snapshot['invoice']);entries=snapshot['journal']['entries'];payments=[x for x in entries if x.get('memo_code')=='F13']
  if len(payments)!=1:raise Refused('single payment scope')
  proof=check(entries,snapshot['ledger_events'],finance_log=snapshot['finance_log'],invoice_id=self.invoice,client_id=self.client,receipt_id=payments[0]['source']['id'],payment_intent=self.pi,**money)
  inv=snapshot['invoice']
  if inv.get('invoice_id')!=self.invoice or inv.get('client_id')!=self.client or inv.get('held_compliance') is not None or inv.get('status')!=('issued' if money['refund_cents']==money['amount_cents'] else 'paid') or inv.get('refunded')!=f"{money['refund_cents']//100}.{money['refund_cents']%100:02d}":raise Refused('invoice readback')
  return {'state':'RECONCILED_CLOSED','mode':'LIVE' if self.live else 'TEST','provider_inventory':inventory,'money':proof,'business_admission':'NOT_OPENED'}
 def wait(self,read_closed_snapshot,*,seconds=60):
  if type(seconds)is not int or not 1<=seconds<=180:raise Refused('wait bound')
  inventory=self.inventory();end=time.monotonic()+seconds
  while True:
   snapshot=read_closed_snapshot()
   try:self.require_deliveries(inventory,snapshot['finance_log'])
   except Refused:
    if time.monotonic()>=end:raise Refused('signed provider redelivery deadline') from None
    time.sleep(min(1,max(0,end-time.monotonic())));continue
   return self.reconcile(inventory,snapshot)


def from_test_key(secret, **scope):
 """Actual Finance adapter factory; no webhook signing credential is needed."""
 if type(secret)is not str or not secret.startswith('sk_test_') or len(secret)>1024:raise Refused('TEST key required')
 import sys
 path='/app/services/finance-py/src'
 if path not in sys.path:sys.path.insert(0,path)
 from stripe_incoming import StripeIncoming
 port=StripeIncoming(secret,'unused-no-signature-generation',False,'https://invalid.example','https://invalid.example',budget_s=2)
 if scope.get('livemode',False)is not False:raise Refused('runnable command TEST-only')
 return ProviderInterval(port,**scope)


def transport_from_key(secret, mode):
 """Explicit operator recovery profile; no call occurs during construction.

 LIVE capability is structural only; local evidence uses injected transports.
 """
 if mode not in ('TEST','LIVE') or type(secret)is not str or not secret.startswith('sk_test_' if mode=='TEST' else 'sk_live_') or len(secret)>1024:raise Refused('key/profile mode mismatch')
 import sys
 path='/app/services/finance-py/src'
 if path not in sys.path:sys.path.insert(0,path)
 from stripe_incoming import StripeIncoming
 return StripeIncoming(secret,'unused-no-signature-generation',mode=='LIVE','https://invalid.example','https://invalid.example',budget_s=2)
