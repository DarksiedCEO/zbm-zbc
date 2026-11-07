"""One local-seal protocol cycle. No upload, provider, restore, publish or reopen calls."""
import sys,os,json,time,secrets
sys.path.insert(0,'/opt/zbm-maintenance/cold')
from owner_wire import Client
MEMBERS=('ledger','legal','compliance','finance','seo')

def emit(check,result,**fields):print(json.dumps({'probe':check,'result':result,**fields}),flush=True)
def main():
 clients={m:Client(m,m+'.railway.internal',27002,os.environ['BACKUP_COORD_TOKEN']) for m in MEMBERS};boots={}
 deadline=time.monotonic()+240
 for m,c in clients.items():
  while True:
   try:s=c.call('/status',{'member':m,'boot':'discover'});break
   except Exception:
    if time.monotonic()>deadline:raise ValueError('owner startup deadline')
    time.sleep(2)
  if s['phase']!='CLOSED' or s['child_pid'] is not None:raise ValueError('fresh owner required')
  boots[m]=s['boot']
 emit('NETWORKING','PASS',members=list(boots))
 for m,c in clients.items():c.call('/relay-bind',{'member':m,'boot':boots[m],'boots':boots})
 run='cold-'+secrets.token_hex(16)
 def action(m,a):
  c=clients[m];operation=run+':'+m+':'+a+':1'
  c.call('/submit',{'member':m,'boot':boots[m],'run':run,'action':a,'operation':operation})
  end=time.monotonic()+120
  while time.monotonic()<end:
   s=c.call('/status',{'member':m,'boot':boots[m],'operation':operation});op=s['operations'].get(operation,{})
   if s.get('failure') or s.get('fenced') or op.get('phase')=='FAILED':raise ValueError('owner refused '+a)
   if op.get('phase')=='DONE':return s
   time.sleep(.5)
  raise ValueError('operation timeout '+a)
 for m in MEMBERS:
  s=action(m,'start')
  if s['phase']!='READY_CLOSED':raise ValueError('start state')
 emit('NATIVE_START','PASS')
 for a in ('close','stop'):
  for m in reversed(MEMBERS):
   s=action(m,a)
   if a=='stop' and (s['phase']!='STOPPED_LOCKED' or s['child_pid'] is not None):raise ValueError('stop unproved')
 emit('ALL_WRITER_STOP','PASS')
 for m in MEMBERS:
  s=action(m,'seal')
  if s['phase']!='SEALED_LOCKED':raise ValueError('seal state')
 emit('ENCRYPTED_LOCAL_SEAL','PASS')
 for m in MEMBERS:
  s=action(m,'restart')
  if s['phase']!='READY_CLOSED' or not s['child_pid']:raise ValueError('restart state')
 emit('RESTART_READINESS','PASS')
 emit('UPLOAD_RESTORE','UNKNOWN',reason='outside approved provider-free scope')
 emit('COMPLETE','PASS',run=run)
if __name__=='__main__':
 try:main()
 except Exception as e:emit('ADMIN','FAIL',category=type(e).__name__);raise SystemExit(2)
