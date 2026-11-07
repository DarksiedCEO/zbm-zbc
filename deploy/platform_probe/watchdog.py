"""Independent process: deadline/parent-loss deletion for one owned checkpoint only."""
import json,os,sys,time
from pathlib import Path
import platform_helpers as old
ROOT=Path(__file__).resolve().parent
def restore_controller(out,doc):
 c=old.Controller();c.out=out
 for target,key in [('run','run'),('p','project'),('e','environment'),('ids','services'),('volumes','volumes'),('started','started'),('verified_created','verified_created')]:setattr(c,target,doc[key])
 if c.p in old.DENY or not c.p or c.verified_created.get('id')!=c.p or c.verified_created.get('name')!=c.run or c.verified_created.get('workspaceId')!=old.WORKSPACE:raise old.Stop('WATCHDOG_SCOPE')
 return c
def main():
 from controller import load_scope
 load_scope()
 out=Path(sys.argv[1]).resolve();parent=int(sys.argv[2])
 if out.parent!=ROOT/'evidence' or not out.name.startswith('zbm-probe-'):raise old.Stop('WATCHDOG_PATH')
 (out/'watchdog.ready').write_text(str(os.getpid()))
 while not (out/'finished').exists():
  try:os.kill(parent,0);alive=True
  except ProcessLookupError:alive=False
  path=out/'checkpoint.json'
  if path.exists():
   doc=json.loads(path.read_text());started=doc.get('started')
   if doc.get('project') and (not alive or started is not None and time.time()-started>=2700):
    old.ALLOW_EXECUTION=True;c=restore_controller(out,doc)
    # Separate checkpoint output avoids racing controller checkpoint replacement.
    c.save=lambda event,**fields:(out/'watchdog-result.json').write_text(json.dumps({'event':event,**fields}))
    if not c.teardown():raise old.Stop('WATCHDOG_DELETE_UNPROVEN')
    return
  if not alive:return
  time.sleep(1)
if __name__=='__main__':main()
