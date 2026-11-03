"""Private fixed application dependency relay. No business receipt, effect retry or durable request journal."""
import hashlib,hmac,http.client,json,secrets,socket,threading,time,re
from http.server import BaseHTTPRequestHandler,HTTPServer
from socketserver import ThreadingMixIn
MEMBERS=('ledger','finance','legal','compliance','seo')
MAX_BODY=4*1024*1024
class Refused(RuntimeError):pass
def enc(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(b):return hashlib.sha256(b).hexdigest()
def decode(b):
 def pairs(items):
  out={}
  for k,v in items:
   if k in out:raise Refused('duplicate field')
   out[k]=v
  return out
 try:return json.loads(b,object_pairs_hook=pairs)
 except (ValueError,UnicodeError):raise Refused('JSON') from None
def key(root,boots,source,target):
 return hmac.new(root.encode(),b'cold-dependency-v1\0'+enc({'source':source,'target':target,'boots':boots}),hashlib.sha256).digest()
def mac(k,kind,meta,body):return hmac.new(k,kind.encode()+b'\0'+enc(meta)+b'\0'+body,hashlib.sha256).hexdigest()
# Derived from the pinned actual thin-client callsites. Queries are not needed by
# this invoice/audit/report profile; payout routes remain deliberately absent.
DOC=r'[a-z][a-z0-9_]{0,63}'
ACC=r'lg-[a-z]{3}-[0-9A-Z]{26}'
OID=r'[A-Z0-9][A-Z0-9-]{1,39}'
CLIENT_ID=r'[A-Za-z0-9._-]{1,100}'
INVOICE=r'fin-inv-[0-9A-HJKMNP-TV-Z]{26}'
ROUTES={
 ('finance','legal','GET'):(rf'/legal/v1/documents/{DOC}/versions/[0-9]{{1,4}}\.[0-9]{{1,4}}',rf'/legal/v1/acceptances/{ACC}'),
 ('compliance','legal','GET'):(rf'/legal/v1/documents/{DOC}/current',rf'/legal/v1/acceptances/{ACC}'),
 ('legal','compliance','GET'):(rf'/compliance/v1/register/{OID}',),
 ('legal','compliance','POST'):(r'/compliance/v1/register/proposals',),
 ('finance','compliance','GET'):(rf'/compliance/v1/register/{OID}',rf'/compliance/v1/clients/{CLIENT_ID}/activation',r'/compliance/v1/readiness'),
 ('seo','compliance','GET'):(rf'/compliance/v1/clients/{CLIENT_ID}/activation',),
 ('seo','finance','GET'):(rf'/fin/v1/invoices/{INVOICE}/status',),
}
CALLER_HEADER={'legal':'x-legal-caller-token','compliance':'x-compliance-caller-token','finance':'x-fin-caller-token'}
def profile(source,target,method,path,body):
 if type(path)is not str or len(path)>512 or any(x in path for x in ('%', '?', '#', '//')) or any(x in ('.','..') for x in path.split('/')):raise Refused('path profile')
 if type(body)is not bytes or len(body)>(65536 if method=='POST' else 0):raise Refused('body profile')
 if source in MEMBERS[1:] and target=='ledger':
  if (method,path) not in (('GET','/ledger/entries'),('GET','/ledger/verify'),('POST','/ledger/events')):raise Refused('fixed route')
  return
 if not any(re.fullmatch(pattern,path) for pattern in ROUTES.get((source,target,method),())):raise Refused('unqualified dependency')
 if method=='POST':
  value=decode(body)
  if type(value)is not dict or type(value.get('request_id'))is not str or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',value['request_id']):raise Refused('proposal identity')
  # Application authorization and the complete proposal schema remain the target's job.
def headers(raw,target='ledger'):
 allowed=('authorization','content-type','accept','accept-encoding')
 if target in CALLER_HEADER:allowed+= (CALLER_HEADER[target],)
 result={}
 for k,v in raw.items():
  if k.lower() in allowed:
   if k.lower() in result or type(v)is not str or any(ord(c)<32 or ord(c)==127 for c in v):raise Refused('duplicate or invalid app header')
   result[k.lower()]=v
 if len(enc(result))>16384:raise Refused('headers')
 return result
class Authority:
 def __init__(self,root,member,boot,allowed,forward,clock=time.time):
  self.root,self.member,self.boot,self.allowed,self.forward,self.clock=root,member,boot,allowed,forward,clock
  self.boots=None;self.lock=threading.RLock();self.seen={};self.last=0;self.unknown=False;self.active=0;self.counts={}
 def bind(self,boots):
  if set(boots)!=set(MEMBERS) or boots[self.member]!=self.boot or any(type(v)is not str or len(v)!=32 or any(c not in '0123456789abcdef' for c in v) for v in boots.values()):raise Refused('roster')
  with self.lock:
   if self.boots is not None and self.boots!=boots:raise Refused('roster changed')
   self.boots=dict(boots)
 def invoke(self,meta,body,signature):
  fields={'source','target','source_boot','target_boot','method','path','headers','nonce','issued','expires','body_sha256'}
  with self.lock:
   if self.boots is None or type(meta)is not dict or set(meta)!=fields:raise Refused('context')
   src=meta['source'];target=meta['target']
   if src not in MEMBERS or target!=self.member or meta['source_boot']!=self.boots[src] or meta['target_boot']!=self.boot:raise Refused('boot binding')
   k=key(self.root,self.boots,src,target)
   if not hmac.compare_digest(signature,mac(k,'request',meta,body)):raise Refused('authentication')
   if type(meta['issued'])is not int or type(meta['expires'])is not int or meta['expires']-meta['issued']!=60:raise Refused('time schema')
   now=self.clock()
   if now+2<self.last:self.unknown=True
   self.last=max(now,self.last)
   if self.unknown or not self.allowed() or meta['issued']>now+2 or meta['expires']<now-2:raise Refused('phase/time')
   nonce=meta['nonce']
   if type(nonce)is not str or len(nonce)!=32 or any(c not in '0123456789abcdef' for c in nonce):raise Refused('nonce')
   profile(src,target,meta['method'],meta['path'],body)
   if meta['body_sha256']!=digest(body) or type(meta['headers'])is not dict or headers(meta['headers'],target)!=meta['headers']:raise Refused('payload')
   self.seen={n:x for n,x in self.seen.items() if x[1]!='DONE' or x[0]>=now-2}
   if nonce in self.seen:raise Refused('replay')
   if len(self.seen)>=1024 or self.active>=2:raise Refused('capacity')
   self.seen[nonce]=(meta['expires'],'ACTIVE');self.active+=1
   label=src+' '+meta['method']+' '+meta['path'];self.counts[label]=self.counts.get(label,0)+1
  try:
   status,hs,out=self.forward(meta['method'],meta['path'],meta['headers'],body)
   if type(status)is not int or not 100<=status<=599 or len(out)>MAX_BODY:raise Refused('response envelope')
   reply={'request_sha256':digest(enc(meta)),'status':status,'headers':hs,'bytes':len(out)}
   signature=mac(k,'response',reply,out)
   with self.lock:self.seen[nonce]=(meta['expires'],'DONE')
   return reply,out,signature
  except BaseException:
   with self.lock:self.unknown=True;self.seen[nonce]=(meta['expires'],'UNKNOWN')
   raise
  finally:
   with self.lock:self.active-=1
class Client:
 def __init__(self,root,boots,source,target,exchange):self.root,self.boots,self.source,self.target,self.exchange=root,dict(boots),source,target,exchange
 def call(self,method,path,hs,body):
  profile(self.source,self.target,method,path,body);now=int(time.time());meta={'source':self.source,'target':self.target,'source_boot':self.boots[self.source],'target_boot':self.boots[self.target],'method':method,'path':path,'headers':headers(hs,self.target),'nonce':secrets.token_hex(16),'issued':now,'expires':now+60,'body_sha256':digest(body)}
  k=key(self.root,self.boots,self.source,self.target)
  reply,out,sig=self.exchange(meta,body,mac(k,'request',meta,body)) # exactly one effect attempt
  if type(reply)is not dict or set(reply)!={'request_sha256','status','headers','bytes'} or reply['request_sha256']!=digest(enc(meta)) or type(reply['status'])is not int or reply['bytes']!=len(out) or len(out)>MAX_BODY or not hmac.compare_digest(sig,mac(k,'response',reply,out)):raise Refused('response authentication')
  return reply['status'],reply['headers'],out

def expire(sock):
 try:sock.shutdown(socket.SHUT_RDWR)
 except OSError:pass
 try:sock.close()
 except OSError:pass

def http_exchange(host,port,path,body,hs,limit=MAX_BODY,timeout=30):
 c=http.client.HTTPConnection(host,port,timeout=timeout);timer=None
 try:
  c.connect();owned=c.sock;timer=threading.Timer(timeout,expire,args=(owned,));timer.daemon=True;timer.start()
  c.request('POST' if path=='/dependency' else hs.pop(':method'),path,body,hs)
  r=c.getresponse();lengths=r.headers.get_all('Content-Length',[])
  if len(lengths)!=1 or r.getheader('Transfer-Encoding'):raise Refused('response framing')
  n=int(lengths[0])
  if not 0<=n<=limit:raise Refused('response cap')
  data=r.read(n)
  if len(data)!=n:raise Refused('short response')
  return r.status,r.headers,data
 finally:
  if timer:timer.cancel()
  c.close()
class BoundedServer(ThreadingMixIn,HTTPServer):
 daemon_threads=True;block_on_close=False
 def __init__(self,address,handler):self.slots=threading.BoundedSemaphore(2);super().__init__(address,handler)
 def process_request(self,s,a):
  if not self.slots.acquire(False):self.shutdown_request(s);return
  try:super().process_request(s,a)
  except BaseException:self.slots.release();raise
 def process_request_thread(self,s,a):
  try:super().process_request_thread(s,a)
  finally:self.slots.release()
def handler(callback,remote=False):
 class Handler(BaseHTTPRequestHandler):
  protocol_version='HTTP/1.1'
  def log_message(self,*a):pass
  def setup(self):
   super().setup();self.connection.settimeout(2);self.timer=threading.Timer(32,expire,args=(self.connection,));self.timer.daemon=True;self.timer.start()
  def finish(self):
   self.timer.cancel()
   try:super().finish()
   except OSError:pass
  def do_GET(self):self.route()
  def do_POST(self):self.route()
  def route(self):
   status=503;hs={'Retry-After':'60','Cache-Control':'no-store'};out=b'{"error":"dependency unavailable; outcome unconfirmed"}'
   try:
    # BaseHTTPRequestHandler normalizes a leading // in self.path. Inspect
    # the original parsed request line before any callback/signing instead.
    raw_target=self.requestline.split()[1]
    if '//' in raw_target or raw_target!=self.path:raise Refused('raw request target')
    lengths=self.headers.get_all('Content-Length',[])
    if (len(lengths)!=1 if remote or self.command=='POST' else len(lengths)>1) or self.headers.get('Transfer-Encoding') or sum(len(k)+len(v) for k,v in self.headers.items())>20000:raise Refused('framing')
    n=int(lengths[0]) if lengths else 0;assert 0<=n<=65536;body=self.rfile.read(n);assert len(body)==n
    if remote:
     if self.command!='POST' or self.path!='/dependency' or len(self.headers.get_all('X-Context',[]))!=1 or len(self.headers.get_all('X-MAC',[]))!=1:raise Refused('endpoint')
     meta=decode(bytes.fromhex(self.headers['X-Context']));reply,out,sig=callback(meta,body,self.headers['X-MAC']);status=200;hs={'X-Context':enc(reply).hex(),'X-MAC':sig}
    else:status,hs,out=callback(self.command,self.path,self.headers,body)
   except Exception:pass
   self.close_connection=True;self.send_response(status);self.send_header('Content-Length',str(len(out)));self.send_header('Connection','close')
   for k,v in hs.items():self.send_header(k,v)
   self.end_headers()
   try:self.wfile.write(out)
   except OSError:pass
 return Handler
