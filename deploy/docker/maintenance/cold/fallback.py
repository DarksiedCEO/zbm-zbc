"""Local separately frozen alias-copy dispatcher; no scheduling, credentials or provider calls.
A supplied sender must be bounded. UNKNOWN is terminal until operator investigation.
"""
import dataclasses,fcntl,hashlib,json,sqlite3
from datetime import datetime
from pathlib import Path
from alert_contract import prepare,RECIPIENT,FrozenResendRequest,validate_request
from resend_receipts import _email

def pack(request):
 value=dataclasses.asdict(request);value['body']=request.body.decode()
 for k in ('first_attempt_at','retry_until'):value[k]=value[k].isoformat()
 return value

def unpack(value):
 value=dict(value);value['body']=value['body'].encode()
 for k in ('first_attempt_at','retry_until'):value[k]=datetime.fromisoformat(value[k])
 return FrozenResendRequest(**value)

class FallbackDispatcher:
 def __init__(self,root,profile,send,*,secondary=None):
  if secondary is not None and (not _email(secondary) or secondary.lower()==RECIPIENT):raise ValueError('invalid secondary recipient')
  self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.profile=profile;self.send=send;self.secondary=secondary
 def step(self,event,now):
  primary=prepare(self.profile,event,now)
  with (self.root/'dispatch.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX)
   db=sqlite3.connect(self.root/'fallback.sqlite')
   try:
    db.execute('PRAGMA synchronous=FULL');db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
    row=db.execute('SELECT value FROM events WHERE id=?',(primary.idempotency_key,)).fetchone()
    if row:state=json.loads(row[0])
    else:
     if db.execute('SELECT count(*) FROM events').fetchone()[0]>=1000:raise ValueError('capacity exhausted; no eviction')
     state={'secondary_address':self.secondary,'primary':self.slot(primary),'secondary':{'status':'UNCONFIGURED'}}
     if self.secondary:
      body=json.loads(primary.body);body['to']=[self.secondary];raw=json.dumps(body,sort_keys=True,separators=(',',':')).encode()
      request=dataclasses.replace(primary,body=raw,body_sha256=hashlib.sha256(raw).hexdigest(),idempotency_key='backup-fallback/'+hashlib.sha256(primary.idempotency_key.encode()+b'\0'+self.secondary.encode()).hexdigest())
      if not validate_request(self.profile,request,now):raise ValueError('invalid fallback envelope')
      state['secondary']=self.slot(request)
    if state['secondary_address']!=self.secondary:raise ValueError('frozen destination mismatch')
    def save():
     db.execute('INSERT OR REPLACE INTO events VALUES (?,?)',(primary.idempotency_key,json.dumps(state,sort_keys=True)));db.commit()
    def result():return dict(primary=state['primary']['status'],secondary=state['secondary']['status'],inbox_delivered=False)
    if state['primary']['status']=='DISPATCHING':state['primary']['status']='UNKNOWN';save()
    lane='secondary' if state['primary']['status'] in ('EXHAUSTED','UNKNOWN','EXPIRED','ACCEPTED') else 'primary';item=state[lane]
    if item['status']=='DISPATCHING':item['status']='UNKNOWN';save()
    if item['status'] not in ('PENDING','FAILED'):return result()
    if now.timestamp()<item['next_at']:return result()
    request=unpack(item['envelope'])
    if not validate_request(self.profile,request,now):item['status']='EXPIRED';save();return result()
    item['attempts']+=1;item['status']='DISPATCHING';save()
    if hasattr(self,'after_reservation'):self.after_reservation()
    try:answer=self.send(request,now)
    except Exception:answer={}
    if type(answer)is dict and answer.get('outcome')=='accepted' and type(answer.get('provider_message_id'))is str and 0<len(answer['provider_message_id'])<=256:
     item['status']='ACCEPTED';item['provider_message_id']=answer['provider_message_id']
    elif type(answer)is dict and answer.get('outcome')=='failed':
     item['status']='EXHAUSTED' if item['attempts']==3 else 'FAILED';item['next_at']=now.timestamp()+[30,120,0][item['attempts']-1]
    else:item['status']='UNKNOWN'
    save();return result()
   finally:db.close()
 @staticmethod
 def slot(request):return dict(status='PENDING',attempts=0,next_at=request.first_attempt_at.timestamp(),envelope=pack(request))
