"""Fixed Python app bootstrap with ordinary post-server worker shutdown.

Owner supplies member, epoch, COLD_INSTANCE and an already-filtered app env.
This module owns no child process, backup operation, admission or provider port.
"""
import importlib
import json
import os
import re
import sys
from cold_shutdown import Workers,finish

APP_PATHS={
 'finance':'/app/services/finance-py/src',
 'legal':'/app/services/legal-py/src',
 'compliance':'/app/services/compliance-py/src',
 'seo':'/app/services/seo-py/src',
}
class Refused(RuntimeError):pass

def context(args):
 if len(args)!=2 or args[0] not in APP_PATHS:raise Refused('fixed Python member required')
 member,epoch=args
 if re.fullmatch(r'owner-[1-9][0-9]{0,9}',epoch) is None:raise Refused('owner epoch required')
 instance=os.environ.get('COLD_INSTANCE','')
 if re.fullmatch('[0-9a-f]{32}',instance) is None:raise Refused('instance required')
 if os.environ.get('ZBM_STARTUP_PROFILE')!='existing-store':raise Refused('existing-store profile required')
 # Defense in depth; actual owner must filter before spawning the application.
 if any(k.startswith(('BACKUP_','RESTIC_','B2_')) or k in ('COORD_TOKEN','COLD_COORD_TOKEN','COLD_BACKUP_PASSWORD') for k in os.environ):raise Refused('supervisor environment leaked')
 if 'api' in sys.modules or 'serve' in sys.modules:raise Refused('application module already loaded')
 return member,epoch,instance

def service_of(app):
 for _ in range(4):
  if hasattr(app,'state'):return app.state.service
  app=app.app
 raise Refused('application service unavailable')

def main(args=None):
 args=sys.argv[1:] if args is None else args
 member=epoch=instance=None;called=False
 try:
  member,epoch,instance=context(args)
  workers=Workers();old_path=list(sys.path)
  try:
   sys.path.insert(0,APP_PATHS[member])
   with workers.installed():
    serve=importlib.import_module('serve');native=serve.run
    def run(app,*a,**kw):
     nonlocal called
     if called:raise Refused('repeated server invocation')
     called=True;service=service_of(app);exit_code=0;failure=None;control=None
     if member=='compliance' and os.environ.get('COMPLIANCE_SCHEDULER_ENABLED','').strip()=='1':
      import socket
      from scheduler_channel import Child
      raw=os.environ.get('COLD_SCHEDULER_FD','')
      if not raw.isdecimal() or not 3<=int(raw)<=1024:raise Refused('scheduler descriptor missing')
      channel=socket.socket(fileno=int(raw))
      if channel.family!=socket.AF_UNIX or channel.type!=socket.SOCK_STREAM:raise Refused('scheduler channel type')
      scheduler=getattr(service,'scheduler',None)
      if scheduler is None or scheduler.backup_state().get('ready_for_backup')is not True:raise Refused('scheduler not initially paused')
      control=Child(channel,scheduler,epoch,instance);control.start()
     if member=='compliance':
      try:
       from native_readiness import from_observation,encode
       receipt=from_observation(service.observe_integrity(),os.getpid(),instance,member,epoch)
       print(encode(receipt),flush=True)
      except BaseException:
       if control is not None:control.close()
       finish(service,workers,20)
       raise Refused('native startup observation refused') from None
     try:native(app,*a,**kw)
     except SystemExit as exc:
      if type(exc.code)is int and exc.code==143:exit_code=143
      else:failure=exc
     except BaseException as exc:failure=exc
     if control is not None:control.close()
     result=finish(service,workers,20)
     if failure is not None:raise Refused('native server did not stop cleanly') from None
     if result.get('clean')is not True or result.get('service_closed')is not True:raise Refused('service close unproved')
     result.update(pid=os.getpid(),instance=instance,member=member,epoch=epoch)
     print('PYTHON_CLEAN_SHUTDOWN '+json.dumps(result,sort_keys=True),flush=True)
     if exit_code:raise SystemExit(exit_code)
    serve.run=run
    try:
     api=importlib.import_module('api');api.main()
     if not called:raise Refused('server invocation missing')
    finally:serve.run=native
  finally:sys.path[:]=old_path
  return 0
 except SystemExit as exc:
  if type(exc.code)is int and exc.code==143:return 143
  code='SystemExit'
 except BaseException as exc:code=type(exc).__name__
 # Never print exception text, environment values, paths or application data.
 print('PYTHON_SHUTDOWN_UNKNOWN '+json.dumps({'pid':os.getpid(),'member':member,'epoch':epoch,'instance':instance,'error_type':code},sort_keys=True),file=sys.stderr,flush=True)
 return 2

if __name__=='__main__':raise SystemExit(main())
