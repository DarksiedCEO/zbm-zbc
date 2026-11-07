"""Explicit enabled cold owner; disabled entrypoint remains unchanged."""
import os,sys,signal
sys.path.insert(0,'/opt/zbm-maintenance')
from cold_config import config,prerequisites

def main():
 if os.getuid()!=0:raise ValueError('root opt-in required')
 cfg=config(dict(os.environ),sys.argv[1:])
 sys.path.insert(0,'/opt/zbm-maintenance/cold')
 from capacity_policy import initialize
 initialize(dict(os.environ),required=True)
 if cfg['recovery']:
  from recovery_owner_boot import run
  return run(cfg)
 cfg.pop('recovery')
 prerequisites(cfg)
 from owner import Owner
 from owner_wire import Server
 owner=Owner(cfg);server=Server(('::',27002),owner.token,owner.dispatch)
 def stop(*_):raise KeyboardInterrupt
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 try:server.serve_forever(poll_interval=.1)
 finally:server.server_close();owner.cleanup()
 return 0
if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception:
  print('maintenance launcher: cold owner refused',file=sys.stderr);raise SystemExit(1)
