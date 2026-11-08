"""Pure backup-alert envelope using the RC's existing bounded Resend transport.
No credential lookup, automatic send, account or schedule is provided here.
"""
import dataclasses,fcntl,hashlib,json,os,re,sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
SOURCE=Path(__file__).parent
sys.path.insert(0,str(SOURCE))
from resend_receipts import FrozenResendRequest,ResendTransportProfile,validate_request

RECIPIENT='andre@zbestmedia.com'
ERRORS={'LOCKED','UPLOAD_FAILED','OVERDUE','MISSING_TRIGGER','STALLED','DISK_PRESSURE','CAPTURE_FAILED'}

def prepare(profile,event,first_attempt_at):
    """Freeze the complete provider body once, before the first delivery attempt."""
    if set(event)!={'set_id','phase','error','retained_bytes','last_complete'}:raise ValueError('unexpected alert fields')
    for key in ['set_id','phase']:
        if not isinstance(event[key],str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,96}',event[key]):raise ValueError('unsafe alert label')
    if event['error'] not in ERRORS:raise ValueError('unsafe error code')
    if event['retained_bytes'] is not None and (type(event['retained_bytes']) is not int or not 0<=event['retained_bytes']<=2**63-1):raise ValueError('invalid retained size')
    last=event['last_complete']
    if last is not None and (type(last) is not int or not 0<=last<=253402300799):raise ValueError('invalid completion timestamp')
    if not isinstance(first_attempt_at,datetime) or first_attempt_at.utcoffset()!=timedelta(0):raise ValueError('UTC required')
    text='ZBM backup requires attention.\n'+''.join(key+': '+('unknown' if event[key] is None else str(event[key]))+'\n' for key in ['set_id','phase','error','retained_bytes','last_complete'])
    body=json.dumps({'from':profile.sender,'to':[RECIPIENT],'subject':'ZBM backup: '+event['error'],'text':text},sort_keys=True,separators=(',',':')).encode()
    identity=json.dumps({k:event[k] for k in ['set_id','phase','error']},sort_keys=True,separators=(',',':')).encode()
    key='backup/'+hashlib.sha256(identity).hexdigest()
    # Existing transport wire-schema version is reused; this is not a payment receipt.
    request=FrozenResendRequest('resend-receipt-v1','resend',profile.profile_id,profile.revision,body,hashlib.sha256(body).hexdigest(),key,
                                first_attempt_at,first_attempt_at+timedelta(minutes=35))
    if not validate_request(profile,request,first_attempt_at):raise ValueError('invalid provider envelope')
    return request

class FrozenAlertStore:
    """Persist one envelope per logical event; restart cannot extend its window.

    The outbox dispatcher must also retain its attempt counters. This class
    never sends and never resets an expired event or changes its sender/body.
    """
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
    def get_or_create(self,profile,event,now):
        candidate=prepare(profile,event,now)
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state=json.loads(self.path.read_text()) if self.path.exists() else {}
            key=candidate.idempotency_key
            if key in state:
                value=dict(state[key]);value['body']=value['body'].encode()
                for name in ['first_attempt_at','retry_until']:value[name]=datetime.fromisoformat(value[name])
                frozen=FrozenResendRequest(**value)
                if frozen.idempotency_key!=key or not validate_request(profile,frozen,now):raise ValueError('expired or mismatched frozen alert; no automatic regeneration')
                return frozen
            value=dataclasses.asdict(candidate);value['body']=candidate.body.decode()
            for name in ['first_attempt_at','retry_until']:value[name]=value[name].isoformat()
            state[key]=value
            temporary=self.path.with_suffix('.tmp')
            with temporary.open('w') as out:json.dump(state,out,sort_keys=True);out.flush();os.fsync(out.fileno())
            os.replace(temporary,self.path)
            fd=os.open(self.path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
            return candidate
