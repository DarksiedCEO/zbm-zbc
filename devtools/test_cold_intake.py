from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy/docker/maintenance/cold'))
import sys,threading,http.client,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from cold_front import Front,Server
from intake_gateway import handler
class Intake(unittest.TestCase):
 def test_closed_and_open_actual_loopback(self):
  hits=[]
  class App(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_GET(self):
    hits.append(self.path);body=b'{"ok":true}';self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  backend=ThreadingHTTPServer(('127.0.0.1',0),App);bt=threading.Thread(target=backend.serve_forever,daemon=True);bt.start()
  front=Front({'finance':backend.server_port});srv=Server(('127.0.0.1',0),handler(front));t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
  def req(path):
   c=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=3)
   try:c.request('GET',path);r=c.getresponse();raw=r.read();return r.status,r.getheader('Retry-After'),raw
   finally:c.close()
  try:
   self.assertEqual(req('/fin/v1/invoices')[0:2],(503,'60'));self.assertEqual(hits,[])
   self.assertEqual(req('/health')[0],200);self.assertEqual(hits,[])
   front.open();self.assertEqual(req('/fin/v1/invoices?page=1')[0],200);self.assertEqual(hits,['/fin/v1/invoices?page=1'])
   self.assertEqual(req('/unknown')[0],404);front.close();self.assertEqual(req('/fin/v1/invoices')[0],503)
  finally:srv.shutdown();srv.server_close();t.join();front.stop();backend.shutdown();backend.server_close();bt.join()
if __name__=='__main__':unittest.main()
