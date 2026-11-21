"""Fixed closed Finance recovery ingress; only unchanged signed webhook bodies.

Installer is trusted supervisor configuration, never an application-facing API.
No signature generation, auth bypass, public opening or durable retry authority.
"""
import base64,hashlib,json,re,threading,http.client,socket,time
from owner_wire import Refused,canonical
MAX=256*1024
class RecoveryEvents:
 def __init__(self,owner,*,set_id,account,inventory):
  if owner.member!='finance' or not re.fullmatch('cold-[0-9a-f]{32}',set_id) or inventory.get('account')!=account or inventory.get('mode') not in ('TEST','LIVE') or not re.fullmatch('acct_[A-Za-z0-9_]{1,120}',account):raise Refused('recovery scope')
  if type(inventory.get('events'))is not dict or len(inventory['events'])>1000:raise Refused('inventory bound')
  self.o=owner;self.set_id=set_id;self.account=account;self.inventory=json.loads(canonical(inventory));self.lock=threading.RLock();self.pending=None;self.results={}
 def closed(self):
  o=self.o
  if o.phase!='VERIFIED_CLOSED' or o.front.opened or o.child is None or o.child.poll()is not None:raise Refused('closed live Finance required')
 def native(self,raw,signature,eid):
  o=self.o;token=json.loads(o.env['FIN_CALLER_TOKENS'])['rail_gateway']
  body=canonical({'request_id':'recovery-'+hashlib.sha256((self.set_id+eid).encode()).hexdigest()[:32],'payload':raw.decode('utf-8'),'signature':signature})
  c=http.client.HTTPConnection('127.0.0.1',27000,timeout=20);timer=None
  try:
   c.connect();sock=c.sock
   def expire():
    try:sock.shutdown(socket.SHUT_RDWR)
    except OSError:pass
    sock.close()
   timer=threading.Timer(20,expire);timer.start()
   c.request('POST','/fin/v1/stripe/events',body,{'Authorization':'Bearer '+o.env['FIN_SERVICE_TOKEN'],'X-FIN-Caller-Token':token,'Content-Type':'application/json'})
   r=c.getresponse();data=r.read(32769)
   if len(data)>32768:raise Refused('native response bound')
   value=json.loads(data)
   if r.status!=200 or value.get('event_id')!=eid:raise Refused('native signed redelivery refused')
   return {'event_id':eid,'status':value.get('status'),'body_sha256':hashlib.sha256(raw).hexdigest()}
  finally:
   if timer:timer.cancel()
   c.close()
 def apply(self,body):
  with self.lock:
   self.closed()
   common={'member','boot','action','set_id','account','event_id','phase'}
   if not common<=set(body) or body['member']!='finance' or body['boot']!=self.o.boot or body['action']!='recovery-event' or body['set_id']!=self.set_id or body['account']!=self.account:raise Refused('recovery context')
   eid=body['event_id'];event=self.inventory['events'].get(eid)
   if event is None:raise Refused('uninventoried event')
   phase=body['phase']
   if phase=='status':
    if set(body)!=common:raise Refused('status schema')
    return self.results.get(eid,{'state':'ABSENT'})
   if phase=='begin':
    if set(body)!=common|{'bytes','sha256','signature'} or type(body['bytes'])is not int or not 1<=body['bytes']<=MAX or not re.fullmatch('[0-9a-f]{64}',str(body['sha256'])) or type(body['signature'])is not str or not 1<=len(body['signature'])<=1024 or any(ord(c)<32 or ord(c)>126 for c in body['signature']):raise Refused('signed framing')
    if eid in self.results:raise Refused('existing attempt; status only')
    if self.pending is not None:raise Refused('one pending delivery')
    self.pending={'event_id':eid,'size':body['bytes'],'sha256':body['sha256'],'signature':body['signature'],'raw':bytearray(),'deadline':time.monotonic()+30};self.results[eid]={'state':'RECEIVING'};return {'state':'RECEIVING'}
   p=self.pending
   if p is None or p['event_id']!=eid or time.monotonic()>p['deadline']:
    if p is not None:self.results[p['event_id']]={'state':'UNKNOWN'};self.pending=None
    raise Refused('delivery absent/expired')
   if phase=='chunk':
    if set(body)!=common|{'offset','chunk'} or type(body['offset'])is not int or body['offset']!=len(p['raw']) or type(body['chunk'])is not str or len(body['chunk'])>2048:raise Refused('chunk context')
    raw=base64.b64decode(body['chunk'],validate=True)
    if not raw or len(raw)>1536 or len(p['raw'])+len(raw)>p['size']:raise Refused('chunk bound')
    p['raw'].extend(raw);return {'state':'RECEIVING','bytes':len(p['raw'])}
   if phase!='commit' or set(body)!=common:raise Refused('fixed phase')
   raw=bytes(p['raw'])
   if len(raw)!=p['size'] or hashlib.sha256(raw).hexdigest()!=p['sha256']:raise Refused('body digest')
   v=json.loads(raw);obj=v.get('data',{}).get('object',{})
   if v.get('id')!=eid or v.get('type')!=event['type'] or v.get('livemode')is not (self.inventory['mode']=='LIVE') or v.get('created')!=event['created'] or obj.get('id')!=event['object_id'] or hashlib.sha256(canonical(obj)).hexdigest()!=event['object_sha256'] or v.get('api_version')!='2026-09-30.endive':raise Refused('inventoried identity')
   self.results[eid]={'state':'PENDING'};self.pending=None
   def work():
    try:
     self.closed();result=self.native(raw,p['signature'],eid);self.closed()
     with self.lock:self.results[eid]={'state':'DONE','result':result}
    except BaseException:
     with self.lock:self.results[eid]={'state':'UNKNOWN'}
   threading.Thread(target=work,name='signed-recovery-event',daemon=False).start();return {'state':'PENDING'}
 def install(self):
  old=self.o.dispatch
  def dispatch(path,body):
   if path in ('/submit','/status') and body.get('action')=='recovery-event':
    if (path=='/status') != (body.get('phase')=='status'):raise Refused('event control path')
    return self.apply(body)
   return old(path,body)
  self.o.dispatch=dispatch

def submit_signed(client,context,raw,signature):
 """Trusted command receives ORIGINAL provider body+signature, never synthesizes it."""
 if type(raw)is not bytes or not 1<=len(raw)<=MAX:raise Refused('signed input bound')
 base=dict(context,action='recovery-event')
 client.call('/submit',dict(base,phase='begin',bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),signature=signature))
 for offset in range(0,len(raw),1536):
  client.call('/submit',dict(base,phase='chunk',offset=offset,chunk=base64.b64encode(raw[offset:offset+1536]).decode()))
 try:client.call('/submit',dict(base,phase='commit'))
 except Refused:pass # only exact same event status; never repeat the effect
 end=time.monotonic()+25
 while time.monotonic()<end:
  result=client.call('/status',dict(base,phase='status'))
  if result['state']=='DONE':return result
  if result['state']!='PENDING':raise Refused('signed delivery unresolved')
  time.sleep(.1)
 raise Refused('signed delivery deadline')
