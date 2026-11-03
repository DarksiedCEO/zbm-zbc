"""Explicitly enabled fixed coordinator, observer or trigger. No provider test mode."""
import os,sys,signal,threading,time,re
from pathlib import Path
from datetime import datetime,timezone
ROLES={'coordinator','observer','trigger','repair','recover'}
REPAIRS={'inspect','retry-upload','resume-originals','resume-sealed','finish-cleanup','recover-catalog','finish-advance','finish-repaired-run','rearm-idle'}
class Refused(ValueError):pass

def configuration(env,argv):
 if not argv or argv[0]not in ROLES or env.get('BACKUP_LAUNCH_ENABLED')!='1':raise Refused('explicit fixed role required')
 if len(argv)!=(2 if argv[0]=='repair' else 1) or (argv[0]=='repair' and argv[1]not in REPAIRS):raise Refused('fixed role/command required')
 role=argv[0];token=env.get('BACKUP_COORD_TOKEN','')
 if not re.fullmatch('[0-9a-f]{64}',token):raise Refused('dedicated coordination credential required')
 result={'role':role,'token':token}
 if role=='repair':result['command']=argv[1]
 if role=='coordinator':
  pw=env.get('BACKUP_RESTIC_PASSWORD','');kid=env.get('BACKUP_B2_KEY_ID','');key=env.get('BACKUP_B2_APPLICATION_KEY','')
  if any(not 1<=len(v)<=4096 or any(ord(c)<33 or ord(c)>126 for c in v) for v in (pw,kid,key)) or len({token,pw,kid,key})!=4:raise Refused('distinct supervisor credentials required')
  result.update(password=pw,credentials={'key_id':kid,'key':key})
 elif role=='observer':
  raw=env.get('BACKUP_FIRST_DUE_UTC_SECONDS','')
  if not raw.isdecimal() or not 0<=int(raw)<=253402214399:raise Refused('fixed first due required')
  result['first_due']=int(raw)
 elif any(env.get(k) for k in ('BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_RESEND_API_KEY')):
  raise Refused('operator/trigger must not possess provider credentials')
 return result

def volume():
 root=Path('/volume')
 if root.is_symlink() or not root.is_dir() or root.resolve()!=root:raise Refused('fixed persistent volume required')
 for name in ('control','backup'):
  p=root/name
  if p.is_symlink():raise Refused('volume alias')
  p.mkdir(mode=0o700,exist_ok=True)
 return root

def main(env=None,argv=None):
 env=dict(os.environ if env is None else env);argv=sys.argv[1:] if argv is None else argv
 c=configuration(env,argv)
 from owner_wire import Client
 if c['role']=='recover':
  if os.getuid()!=0:raise Refused('explicit supervisor root profile required')
  from recovery_controller import main as recovery_main
  return recovery_main(argv=[],expected_token=c['token'])
 if c['role']=='repair':
  from repair_cli import operator_command
  from owner_wire import canonical
  client=Client('coordinator','coordinator.railway.internal',27002,c['token'])
  print(canonical(operator_command(client,c['command'])).decode(),flush=True)
  return 0
 if c['role']=='trigger':
  client=Client('coordinator','coordinator.railway.internal',27002,c['token'])
  status=client.call('/status',{'kind':'scheduled_backup'});due=status['observer']['monitoring']['expected_due']
  if type(due)is not int:raise Refused('armed schedule unavailable')
  if due>int(time.time()):return 0
  client.call('/submit',{'kind':'scheduled_backup','due':due});return 0
 if os.getuid()!=0:raise Refused('explicit supervisor root profile required')
 root=volume();stop=threading.Event()
 for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
 if c['role']=='observer':
  from observer_control import Control
  from monitor_sender import Sender
  from cold_alerts import Alerts
  sender=Sender.from_environment(env)
  control=Control(root/'control/monitor.sqlite',c['token'],first_due=c['first_due'],cadence=86400,grace=900)
  alerts=Alerts(control,root/'control/outbox',sender.profile,sender);srv=control.server();thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start()
  try:
   while not stop.wait(1):alerts.tick(datetime.now(timezone.utc))
  finally:srv.shutdown();srv.server_close();thread.join(3);control.close()
 else:
  from coordinator import Coordinator
  from intake_gateway import server
  retained=(root/'control/run.json').exists()
  co=Coordinator(root/'control',c['token'],c['password'],recover=retained,transport_credentials=c['credentials'])
  srv=server(co.front);thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start()
  try:
   if not retained:co.start()
   # Retained state never automatically reopens or repeats an uncertain effect.
   # The supported operator rearm/restore command must establish its own evidence.
   stop.wait()
  finally:
   co.front.close();srv.shutdown();srv.server_close();thread.join(3)
   co.catalog_server.shutdown();co.catalog_server.server_close()
 return 0
if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception:print('BACKUP_ADMIN_REFUSED',file=sys.stderr);raise SystemExit(2)
