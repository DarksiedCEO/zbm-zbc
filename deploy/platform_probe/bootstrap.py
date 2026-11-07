"""Synthetic empty-volume initialization, then exact installed owner; no deploy switching."""
import json,os,sys,subprocess,signal,time,http.client,threading,hmac
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path

MEMBERS=('ledger','legal','compliance','finance','seo')
MESSAGES={'fixed member','fresh mounted volume required','nonempty volume','CAP_KILL absent','seed exited','seed health timeout','seed process group remains','seed unclean stop','seed dependency timeout','seed barrier timeout'}
def health(host):
 c=http.client.HTTPConnection(host,27000,timeout=1)
 try:c.request('GET','/health');return c.getresponse().status==200
 except OSError:return False
 finally:c.close()
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
 deadline=time.monotonic()+180
 dependencies={'ledger':(), 'legal':('ledger',), 'compliance':('ledger','legal'), 'finance':('ledger','legal','compliance'), 'seo':('ledger','finance','compliance')}
 while not all(health(x+'.railway.internal') for x in dependencies[m]):
  if time.monotonic()>=deadline:raise ValueError('seed dependency timeout')
  time.sleep(.5)
 ready_members=set();acknowledged=set();server=None
 if m=='ledger':
  class Barrier(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_GET(self):
    member=self.path.removeprefix('/')
    valid=member in MEMBERS and hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+env['BACKUP_COORD_TOKEN'])
    if valid and len(ready_members)==5:acknowledged.add(member)
    self.send_response(200 if valid else 403);self.end_headers()
   def do_POST(self):
    member=self.path.removeprefix('/')
    valid=member in MEMBERS and hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+env['BACKUP_COORD_TOKEN'])
    if valid:ready_members.add(member)
    self.send_response(200 if valid and len(ready_members)==5 else 202 if valid else 403);self.end_headers()
  server=ThreadingHTTPServer(('0.0.0.0',27003),Barrier)
  threading.Thread(target=server.serve_forever,daemon=True).start()
 p=subprocess.Popen(spec,env=env,start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 # Drain stderr continuously, retaining at most 16KiB. Never print its contents.
 log=Path('/data/probe-seed.stderr');tail=bytearray()
 def capture():
  while True:
   chunk=p.stderr.read(1024)
   if not chunk:break
   tail.extend(chunk);del tail[:-16384]
   fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
   with os.fdopen(fd,'wb') as f:f.write(tail)
 reader=threading.Thread(target=capture,daemon=True);reader.start()
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
  print(json.dumps({'probe':'SEED','result':'PASS','member':m}),flush=True)
  while True:
   if p.poll() is not None:raise ValueError('seed exited')
   if time.monotonic()>=deadline:raise ValueError('seed barrier timeout')
   c=http.client.HTTPConnection('ledger.railway.internal',27003,timeout=2)
   try:
    c.request('POST','/'+m,headers={'Authorization':'Bearer '+env['BACKUP_COORD_TOKEN']})
    released=c.getresponse().status==200
   except OSError:released=False
   finally:c.close()
   if released:
    c=http.client.HTTPConnection('ledger.railway.internal',27003,timeout=2)
    try:
     c.request('GET','/'+m,headers={'Authorization':'Bearer '+env['BACKUP_COORD_TOKEN']})
     if c.getresponse().status==200:break
    finally:c.close()
   time.sleep(.5)
  # Ledger keeps the barrier reachable while every dependent observes release.
  if m=='ledger':
   while len(acknowledged)<5:
    if time.monotonic()>=deadline:raise ValueError('seed barrier timeout')
    time.sleep(.1)
 finally:
  stop_seed();reader.join(timeout=2)
  if server:server.shutdown()
 if p.returncode not in (0,143,-15):raise ValueError('seed unclean stop')
 env.update(BACKUP_LAUNCH_ENABLED='1',PORT='27001');env[prefix+'_PORT']='27001'
 for key,value in list(env.items()):
  if key.endswith('_URL') and '.railway.internal:27000' in value:env[key]=value.replace('.railway.internal:27000','.railway.internal:27001')
 env.pop('PROBE_CODE',None);env.pop('PROBE_ENTRYPOINT',None);env.pop('PROBE_MEMBER',None)
 os.execve(spec[0],spec,env)
if __name__=='__main__':
 try:main()
 except BaseException as e:
  print(json.dumps({'probe':'BOOTSTRAP','result':'FAIL','category':type(e).__name__,'message':str(e) if isinstance(e,ValueError) and str(e) in MESSAGES else 'unexpected bootstrap failure'}),flush=True);raise SystemExit(2)
