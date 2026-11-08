import json,os,signal,socket,subprocess,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BINARY=None

def cargo_artifact(output,crate):
 """Select the exact compiler artifact, never the first filesystem match."""
 crate=Path(crate).resolve();found=[]
 for line in output.splitlines():
  record=json.loads(line)
  if record.get('reason')!='compiler-artifact':continue
  target=record.get('target',{})
  if target.get('name')!='server' or target.get('kind')!=['bin'] or record.get('profile',{}).get('test')is not True:continue
  if Path(record.get('manifest_path','')).resolve()!=crate/'Cargo.toml' or Path(target.get('src_path','')).resolve()!=crate/'src/bin/server.rs':continue
  executable=record.get('executable')
  if type(executable)is not str or not Path(executable).is_absolute():raise RuntimeError('Cargo server test executable missing')
  found.append(Path(executable))
 if len(found)!=1:raise RuntimeError('Cargo did not emit exactly one server test artifact')
 binary=found[0]
 if not binary.is_file() or not os.access(binary,os.X_OK):raise RuntimeError('Cargo artifact is not executable')
 return binary

def build_binary():
 crate=ROOT/'services/ledger-rust'
 result=subprocess.run(['cargo','test','--locked','--offline','--manifest-path',str(crate/'Cargo.toml'),'--bin','server','--no-run','--message-format=json'],cwd=crate,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=240,check=False)
 if result.returncode:raise RuntimeError('offline server test build failed: '+result.stderr.decode(errors='replace')[-4096:])
 if len(result.stdout)>2097152:raise RuntimeError('Cargo artifact output envelope')
 return cargo_artifact(result.stdout.decode(),crate)

class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  global BINARY
  BINARY=build_binary()
 def test_held_blocking_write_waits_then_persists(self):
  with tempfile.TemporaryDirectory(prefix='cold-ledger-')as d:
   root=Path(d).resolve();d=str(root);p=subprocess.Popen([str(BINARY),'--ignored','--exact','cold_tests::held_blocking_child','--nocapture'],env={'PATH':'/usr/bin:/bin','COLD_FIXTURE':d},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
   try:
    end=time.monotonic()+5
    while not (root/'entered').exists() and p.poll()is None and time.monotonic()<end:time.sleep(.01)
    
    if not (root/'entered').exists():
     out,err=p.communicate(timeout=1);self.fail('child fixture setup failed: '+err.decode())
    p.send_signal(signal.SIGTERM);time.sleep(.15)
    self.assertIsNone(p.poll(),'SIGTERM killed accepted detached blocking write')
    (root/'release').write_text('release')
    out,err=p.communicate(timeout=5);self.assertEqual(p.returncode,0,err.decode());self.assertIn(b'CLEAN_SHUTDOWN',err)
    lines=err.decode().splitlines(); instance=next(x.split('INSTANCE ',1)[1].split() for x in lines if 'INSTANCE ' in x); receipt=json.loads(next(x.split('CLEAN_SHUTDOWN ',1)[1] for x in lines if 'CLEAN_SHUTDOWN ' in x))
    self.assertEqual(receipt['pid'],p.pid);self.assertEqual(instance,[str(p.pid),receipt['instance']]);self.assertEqual(len(receipt['instance']),32);self.assertEqual(receipt['head']['entries'],1)
    entries=[json.loads(x) for x in (root/'ledger.jsonl').read_text().splitlines()]
    self.assertEqual(len(entries),1);self.assertEqual(entries[0]['event_id'],'cold-event')
   finally:
    if p.poll()is None:p.kill();p.wait(timeout=2)
    p.stdout.close();p.stderr.close()
 def test_timeout_unknown_and_queued_job(self):
  for queued in (False,True):
   with self.subTest(queued=queued),tempfile.TemporaryDirectory(prefix='cold-ledger-')as d:
    root=Path(d).resolve();env={'PATH':'/usr/bin:/bin','COLD_FIXTURE':str(root)}
    if queued:env['COLD_QUEUED']='1'
    p=subprocess.Popen([str(BINARY),'--ignored','--exact','cold_tests::held_blocking_child','--nocapture'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
     end=time.monotonic()+5
     while not(root/'entered').exists() and p.poll()is None and time.monotonic()<end:time.sleep(.01)
     self.assertTrue((root/'entered').exists());time.sleep(.08)
     p.send_signal(signal.SIGTERM)
     if queued:
      time.sleep(.15);self.assertIsNone(p.poll());(root/'release').write_text('release')
     out,err=p.communicate(timeout=3)
     if queued:
      self.assertEqual(p.returncode,0,err.decode());self.assertTrue((root/'queued_finished').exists());self.assertIn(b'CLEAN_SHUTDOWN',err)
     else:
      self.assertEqual(p.returncode,2,err.decode());self.assertNotIn(b'CLEAN_SHUTDOWN',err);self.assertIn(b'UNKNOWN',err)
    finally:
     if p.poll()is None:p.kill();p.wait(timeout=2)
     p.stdout.close();p.stderr.close()
 def test_accepted_http_body_drains_before_clean_exit(self):
  with tempfile.TemporaryDirectory(prefix='cold-ledger-')as d:
   root=Path(d).resolve();p=subprocess.Popen([str(BINARY),'--ignored','--exact','cold_tests::held_blocking_child','--nocapture'],env={'PATH':'/usr/bin:/bin','COLD_FIXTURE':str(root)},stdout=subprocess.PIPE,stderr=subprocess.PIPE);sock=None
   try:
    end=time.monotonic()+5
    while not(root/'entered').exists() and p.poll()is None and time.monotonic()<end:time.sleep(.01)
    self.assertTrue((root/'entered').exists());port=int((root/'port').read_text())
    sock=socket.create_connection(('127.0.0.1',port),timeout=2)
    body=json.dumps({'event_id':'http-event','department':'finance','event_type':'cold_fixture','actor':'fixture','subject_id':'fixture','payload_sha256':'a'*64,'summary':'Synthetic HTTP write'}).encode()
    sock.sendall(('POST /ledger/events HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer synthetic-token-for-cold-fixture-only\r\nContent-Length: '+str(len(body))+'\r\nConnection: close\r\n\r\n').encode());time.sleep(.08)
    p.send_signal(signal.SIGTERM);(root/'release').write_text('release');time.sleep(.1);self.assertIsNone(p.poll())
    sock.sendall(body);reply=b''
    while True:
     chunk=sock.recv(8192)
     if not chunk:break
     reply+=chunk
    self.assertIn(b'201',reply.split(b'\r\n')[0]);sock.close();sock=None
    out,err=p.communicate(timeout=3);self.assertEqual(p.returncode,0,err.decode());self.assertIn(b'CLEAN_SHUTDOWN',err)
    self.assertEqual(len((root/'ledger.jsonl').read_text().splitlines()),2)
   finally:
    if sock:sock.close()
    if p.poll()is None:p.kill();p.wait(timeout=2)
    p.stdout.close();p.stderr.close()
if __name__=='__main__':unittest.main()
