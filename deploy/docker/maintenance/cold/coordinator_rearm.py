"""One idle-retired rearm transaction. Interrupted stages require restore fallback."""
import copy,time,threading
from owner_wire import Refused,MEMBERS,decode
from metadata_retirement import read,save
from idle_rearm import intent
from incident_archive import Archive

def load(c):return decode(read(c.root/'idle-rearm.json'))
def permit(c,b):
 if set(b)!={'kind','member','boot','identity'}:raise Refused('rearm permit schema')
 v=load(c)
 if v['identity']!=b['identity'] or v['boots'].get(b['member'])!=b['boot'] or v['phase'] not in ('PREPARED','LOCKED','VERIFIED'):raise Refused('rearm permit unavailable')
 return v

def call(c,m,action,v):
 body={'member':m,'boot':v['boots'][m],'identity':v['identity'],'action':action}
 try:c.clients[m].call('/rearm',body)
 except Refused:pass
 end=time.monotonic()+55
 while time.monotonic()<end:
  state=c.clients[m].call('/rearm-status',body)
  if state.get('identity')!=v['identity'] or state.get('boot')!=v['boots'][m]:raise Refused('owner rearm status changed')
  record=state.get('actions',{}).get(action)
  if record:
   if record['phase']=='DONE':return record['result']
   if record['phase']!='PENDING':raise Refused('owner rearm unknown; restore required')
  time.sleep(.05)
 raise Refused('owner rearm deadline; restore required')

def observer_binding(c):
 observed=c.observer.call('/status',{'kind':'monitor_status'})
 current=observed.get('current');monitor=observed.get('monitoring',{})
 good=c.doc.get('last_success',{})
 if type(current)is not dict or current.get('phase') not in ('COMPLETE','REPAIRED_COMPLETE') or current.get('run')!=good.get('run') or monitor.get('state')!='ARMED' or type(monitor.get('expected_due'))is not int or monitor['expected_due']<=current.get('due',monitor['expected_due']):raise Refused('observer next due not established')
 proof=current.get('completion',{})
 if any(proof.get(k)!=good.get(k) for k in ('run','catalog_sha256','catalog_snapshot')):raise Refused('observer last good mismatch')
 return observed

def run(c):
 from coordinator import START,STOP
 path=c.root/'idle-rearm.json'
 if path.exists() or path.with_name(path.name+'.tmp').exists():raise Refused('existing rearm; restore fallback required')
 boots={}
 for m in MEMBERS:
  state=c.clients[m].call('/status',{'member':m,'boot':'discover'})
  if state.get('member')!=m or state.get('run')is not None or state.get('last_success')!=c.doc.get('last_success'):raise Refused('owner not idle retired')
  boots[m]=state['boot']
 v=intent(c.doc,boots);v['observer']=observer_binding(c)
 from incident_archive import checked_capacity
 checked_capacity(c.root,'coordinator',c.observer);v['phase']='PREPARED';save(path,v)
 c.front.close()
 try:
  for m in MEMBERS:call(c,m,'close',v)
  locks={m:call(c,m,'lock',v) for m in STOP}
  v=dict(v,phase='LOCKED',locks=locks);save(path,v)
  for m in MEMBERS:call(c,m,'bind',v)
  for m in START:call(c,m,'start',v)
  verified={m:call(c,m,'verify',v) for m in MEMBERS}
  v=dict(v,phase='VERIFIED',verified=verified);save(path,v)
  for m in MEMBERS:call(c,m,'publish',v)
  for m in MEMBERS:
   s=c.clients[m].call('/status',{'member':m,'boot':boots[m]})
   if s.get('boot')!=boots[m] or s.get('phase')!='OPEN' or s.get('fenced')is not False or type(s.get('child_pid'))is not int:raise Refused('current owner publication refusal')
  # Preserve the existing next-run identity and observer due. No fake backup
  # completion or observer acknowledgment is produced by this transaction.
  if observer_binding(c)!=v['observer']:raise Refused('observer changed during rearm')
  old=copy.deepcopy(c.doc)
  Archive(c.root).append({'identity':v['identity'],'phase':'RESOLVED','evidence':{'kind':'idle-rearm-original','original':old,'locked':locks,'verified':verified}})
  c.update(boots=boots,rearm_identity=v['identity'])
  v=dict(v,phase='DONE');save(path,v);c.front.open();return v
 except BaseException:
  c.front.close();v=dict(v,phase='UNKNOWN',fallback='remote-restore');save(path,v)
  # Fail closed across direct per-owner fronts, too. No guessed stop/adoption.
  for m in MEMBERS:
   try:c.clients[m].call('/rearm-close',{'member':m,'boot':boots[m],'identity':v['identity']})
   except BaseException:pass
  raise

def control(c,path,b):
 if b!={'command':'rearm-idle'}:raise Refused('fixed rearm command')
 if path=='/repair-status':return load(c)
 if path!='/repair':raise Refused('rearm route')
 with c.control_lock:
  if c.normal_active or c.repair_worker and c.repair_worker.is_alive():raise Refused('coordinator busy')
  if (c.root/'idle-rearm.json').exists():return load(c)
  c.normal_active=True
  def work():
   try:run(c)
   finally:
    with c.control_lock:c.normal_active=False
  c.repair_worker=threading.Thread(target=work,daemon=True);c.repair_worker.start()
 return {'phase':'STARTING'}
