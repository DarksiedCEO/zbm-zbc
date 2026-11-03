"""Fixed-target authenticated run-level control. No business request accounting."""
import hashlib,hmac,http.client,json,secrets,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
MEMBERS=('ledger','finance','legal','compliance','seo')
MAX_REQUEST=4096;MAX_RESPONSE=32768
class Refused(RuntimeError):pass
def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def decode(raw):
 def pairs(rows):
  out={}
  for k,v in rows:
   if k in out:raise Refused('duplicate key')
   out[k]=v
  return out
 try:return json.loads(raw,object_pairs_hook=pairs)
 except (ValueError,UnicodeError):raise Refused('invalid JSON') from None
def mac(key,kind,nonce,body):return hmac.new(key.encode(),kind.encode()+b'\0'+nonce.encode()+b'\0'+body,hashlib.sha256).hexdigest()
def keycheck(key):
 if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise Refused('coordination token shape')
def envelope(member,boot,run,action,operation):
 if member not in MEMBERS or not all(isinstance(x,str) and 1<=len(x)<=96 for x in (boot,run,action,operation)):raise Refused('context')
 return dict(member=member,boot=boot,run=run,action=action,operation=operation)
class Client:
 def __init__(self,member,host,port,key):
  keycheck(key)
  if member not in (*MEMBERS,'coordinator','observer') or host not in ('127.0.0.1',member+'.railway.internal') or type(port)is not int or not 1024<=port<=32767:raise Refused('fixed target')
  self.member,self.host,self.port,self.key=member,host,port,key
 def call(self,path,body):
  if path not in ('/status','/submit','/repair','/repair-status','/relay-bind','/rearm','/rearm-status','/rearm-close'):raise Refused('fixed control path')
  raw=canonical(body)
  if len(raw)>MAX_REQUEST:raise Refused('request size')
  nonce=secrets.token_hex(16);c=http.client.HTTPConnection(self.host,self.port,timeout=3)
  try:
   deadline=time.monotonic()+3;c.connect();sock=c.sock
   c.request('POST',path,raw,{'Content-Type':'application/json','X-Nonce':nonce,'X-MAC':mac(self.key,'request:'+path,nonce,raw),'Connection':'close'})
   r=c.getresponse();lengths=[v for k,v in r.getheaders() if k.lower()=='content-length']
   if len(lengths)!=1 or r.getheader('Transfer-Encoding'):raise Refused('response framing')
   n=int(lengths[0])
   if not 0<=n<=MAX_RESPONSE:raise Refused('response size')
   out=bytearray()
   while len(out)<n:
    left=deadline-time.monotonic()
    if left<=0:raise Refused('response deadline')
    sock.settimeout(left);chunk=r.read1(min(65536,n-len(out)))
    if not chunk:raise Refused('short response')
    out.extend(chunk)
   out=bytes(out)
   if len(out)!=n or not hmac.compare_digest(r.getheader('X-MAC',''),mac(self.key,'response:'+path,nonce,out)):raise Refused('response authentication')
   value=decode(out)
   if r.status!=200:raise Refused(str(value.get('error','control refusal'))[:100])
   return value
  except (OSError,http.client.HTTPException,ValueError):raise Refused('control outcome unavailable') from None
  finally:c.close()
class Server(ThreadingHTTPServer):
 daemon_threads=True;block_on_close=False
 def __init__(self,address,key,dispatch):
  keycheck(key);self.key=key;self.dispatch=dispatch;self.slots=threading.BoundedSemaphore(8)
  super().__init__(address,Handler)
 def process_request(self,request,address):
  if not self.slots.acquire(False):self.shutdown_request(request);return
  try:super().process_request(request,address)
  except BaseException:self.slots.release();raise
 def process_request_thread(self,request,address):
  try:super().process_request_thread(request,address)
  finally:self.slots.release()
class Handler(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def setup(self):super().setup();self.connection.settimeout(2)
 def do_POST(self):
  nonce=self.headers.get('X-Nonce','');status=200
  try:
   if self.path not in ('/submit','/status','/repair','/repair-status','/relay-bind','/rearm','/rearm-status','/rearm-close') or len(nonce)!=32 or any(c not in '0123456789abcdef' for c in nonce):raise Refused('envelope')
   ns=self.headers.get_all('Content-Length',[])
   if len(ns)!=1 or self.headers.get('Transfer-Encoding') or len(self.headers.get_all('X-MAC',[]))!=1:raise Refused('framing')
   n=int(ns[0])
   if not 0<n<=MAX_REQUEST:raise Refused('request size')
   end=time.monotonic()+2;raw=bytearray()
   while len(raw)<n:
    left=end-time.monotonic()
    if left<=0:raise Refused('request deadline')
    self.connection.settimeout(left);part=self.rfile.read1(min(4096,n-len(raw)))
    if not part:raise Refused('short request')
    raw.extend(part)
   raw=bytes(raw)
   if len(raw)!=n or not hmac.compare_digest(self.headers.get('X-MAC',''),mac(self.server.key,'request:'+self.path,nonce,raw)):raise Refused('authentication')
   result=self.server.dispatch(self.path,decode(raw))
  except Exception as exc:status=409;result={'error':str(exc)[:100] if isinstance(exc,Refused) else type(exc).__name__}
  out=canonical(result)
  if len(out)>MAX_RESPONSE:status=409;out=b'{"error":"response envelope"}'
  self.close_connection=True;self.send_response(status);self.send_header('Content-Length',str(len(out)));self.send_header('Connection','close');self.send_header('X-MAC',mac(self.server.key,'response:'+self.path,nonce,out));self.end_headers()
  try:self.wfile.write(out)
  except OSError:pass
