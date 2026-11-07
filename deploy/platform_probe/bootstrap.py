"""Synthetic empty-volume initialization, then exact installed owner; no deploy switching."""
import json,os,sys,subprocess,signal,time,http.client
from pathlib import Path

def main():
 m=os.environ['PROBE_MEMBER']; spec=json.loads(os.environ['PROBE_ENTRYPOINT']); env=dict(os.environ)
 if m not in ('ledger','legal','compliance','finance','seo') or not spec or spec[0]!='/usr/local/bin/zbm-entrypoint':raise ValueError('fixed member')
 if os.getuid()!=0 or not os.path.ismount('/data'):raise ValueError('fresh mounted volume required')
 for entry in Path('/data').iterdir():
  if entry.name!='lost+found' or entry.is_symlink() or not entry.is_dir() or entry.stat().st_uid!=0 or any(entry.iterdir()):raise ValueError('nonempty volume')
 caps={}
 for line in Path('/proc/self/status').read_text().splitlines():
  if line.split(':')[0] in ('CapEff','CapBnd','NoNewPrivs'):caps[line.split(':')[0]]=line.split(':',1)[1].strip()
 if not int(caps['CapEff'],16)&(1<<5):raise ValueError('CAP_KILL absent')
 print(json.dumps({'probe':'CAP_KILL','result':'PASS','member':m,'caps':caps}),flush=True)
 prefix={'ledger':'LEDGER','finance':'FIN','legal':'LEGAL','compliance':'COMPLIANCE','seo':'SEO'}[m]
 env.update(BACKUP_LAUNCH_ENABLED='0',PORT='27000');env[prefix+'_PORT']='27000'
 p=subprocess.Popen(spec,env=env,start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 def stop_seed():
  if p.poll() is None:os.killpg(p.pid,signal.SIGTERM)
  p.wait(timeout=25)
  try:os.killpg(p.pid,0)
  except ProcessLookupError:return
  raise ValueError('seed process group remains')
 try:
  ready=False
  for _ in range(90):
   if p.poll() is not None:raise ValueError('seed exited')
   try:
    c=http.client.HTTPConnection('127.0.0.1',27000,timeout=1);c.request('GET','/health');ready=c.getresponse().status==200;c.close()
   except OSError:pass
   if ready:break
   time.sleep(1)
  if not ready:raise ValueError('seed health timeout')
 finally:stop_seed()
 if p.returncode not in (0,143,-15):raise ValueError('seed unclean stop')
 print(json.dumps({'probe':'SEED','result':'PASS','member':m}),flush=True)
 env.update(BACKUP_LAUNCH_ENABLED='1',PORT='27001');env[prefix+'_PORT']='27001'
 env.pop('PROBE_CODE',None);env.pop('PROBE_ENTRYPOINT',None);env.pop('PROBE_MEMBER',None)
 os.execve(spec[0],spec,env)
if __name__=='__main__':
 try:main()
 except BaseException as e:
  print(json.dumps({'probe':'BOOTSTRAP','result':'FAIL','category':type(e).__name__}),flush=True);raise SystemExit(2)
