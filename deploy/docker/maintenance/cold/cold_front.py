"""Fixed loopback intake gate; no app ownership, backend effects, or retry."""
import http.client,json,socket,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class Server(ThreadingHTTPServer):
 daemon_threads=True
 block_on_close=False
 def __init__(self,*args):self.connections=threading.BoundedSemaphore(8);super().__init__(*args)
 def process_request(self,request,address):
  if not self.connections.acquire(False):
   request.settimeout(.2)
   try:request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 60\r\nConnection: close\r\n\r\n')
   except OSError:pass
   self.shutdown_request(request);return
  try:super().process_request(request,address)
  except BaseException:self.connections.release();raise
 def process_request_thread(self,request,address):
  try:super().process_request_thread(request,address)
  finally:self.connections.release()
class Front:
 def __init__(self,targets,*,bind="127.0.0.1",port=0):
  self.targets=dict(targets);self.ports={};self.servers=[];self.threads=[];self.cv=threading.Condition();self.opened=False;self.active=0;self.dispatched=0;self.slots=threading.BoundedSemaphore(8)
  for member,target in self.targets.items():
   handler=self.handler(target);s=Server((bind,port),handler);s.daemon_threads=True;s.block_on_close=False
   t=threading.Thread(target=s.serve_forever,daemon=True);t.start();self.servers.append(s);self.threads.append(t);self.ports[member]=s.server_port
 def close(self,timeout=20):
  end=time.monotonic()+timeout
  with self.cv:
   self.opened=False
   while self.active:
    left=end-time.monotonic()
    if left<=0:raise TimeoutError('intake drain incomplete')
    self.cv.wait(left)
 def open(self):
  with self.cv:
   if self.active:raise RuntimeError('cannot open with retained front request')
   self.opened=True
 def stop(self):
  self.close()
  for s in self.servers:s.shutdown();s.server_close()
  for t in self.threads:t.join(2)
 def handler(self,target):
  owner=self
  class Handler(BaseHTTPRequestHandler):
   protocol_version='HTTP/1.1'
   def log_message(self,*a):pass
   def setup(self):super().setup();self.connection.settimeout(20)
   def respond(self,status,body,extra=None):
    self.close_connection=True;self.send_response(status);self.send_header('Content-Length',str(len(body)));self.send_header('Content-Type','application/json');self.send_header('Connection','close')
    for k,v in (extra or {}).items():self.send_header(k,v)
    self.end_headers()
    try:self.wfile.write(body)
    except OSError:pass
   def do_GET(self):self.route()
   def do_POST(self):self.route()
   def do_PUT(self):self.route()
   def do_DELETE(self):self.route()
   def route(self):
    if not owner.slots.acquire(False):return self.respond(503,b'{"error":"temporarily unavailable"}',{'Retry-After':'60'})
    admitted=False;conn=None
    try:
     with owner.cv:
      if not owner.opened:return self.respond(503,b'{"error":"We cannot confirm the result right now; retry the same submission later."}',{'Retry-After':'60'})
      owner.active+=1;admitted=True
     deadline=time.monotonic()+20
     lengths=self.headers.get_all('Content-Length',[])
     if self.headers.get('Transfer-Encoding') or len(lengths)>1 or sum(len(k)+len(v) for k,v in self.headers.items())>16384:raise ValueError('framing')
     n=int(lengths[0]) if lengths else 0
     if n<0 or n>1048576 or not self.path.startswith('/') or self.path.startswith('//'):raise ValueError('request bounds')
     data=self.rfile.read(n)
     if len(data)!=n:raise ValueError('short request')
     headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','connection','content-length','transfer-encoding','expect','upgrade','proxy-authorization')};headers['Content-Length']=str(n);headers['Connection']='close'
     host,port=target if isinstance(target,tuple) else ('127.0.0.1',target)
     conn=http.client.HTTPConnection(host,port,timeout=max(.001,deadline-time.monotonic()));conn.connect();sock=conn.sock
     with owner.cv:owner.dispatched+=1
     conn.request(self.command,self.path,data,headers);sock.settimeout(max(.001,deadline-time.monotonic()));r=conn.getresponse();cls=[v for k,v in r.getheaders() if k.lower()=='content-length']
     if len(cls)!=1 or r.getheader('Transfer-Encoding'):raise ValueError('response framing')
     wanted=int(cls[0]);out=bytearray()
     if not 0<=wanted<=2097152:raise ValueError('response bounds')
     while len(out)<wanted:
      left=deadline-time.monotonic()
      if left<=0:raise TimeoutError('response deadline')
      sock.settimeout(left);chunk=r.read1(min(65536,wanted-len(out)))
      if not chunk:raise ValueError('incomplete response')
      out.extend(chunk)
     extra={}
     if r.getheader('Retry-After'):extra['Retry-After']=r.getheader('Retry-After')
     self.respond(r.status,bytes(out),extra)
    except (OSError,ValueError,http.client.HTTPException):self.respond(503,b'{"error":"We cannot confirm the result right now; retry the same submission later."}',{'Retry-After':'60'})
    finally:
     if conn:conn.close()
     if admitted:
      with owner.cv:owner.active-=1;owner.cv.notify_all()
     owner.slots.release()
  return Handler
