"""Existing gateway envelope only; original signature still verified by Finance."""
import hashlib,hmac,json,re,socket,threading,time
from owner_wire import Refused,canonical,decode
MAX=262144
class Inbox:
 def __init__(self):
  self.cv=threading.Condition();self.inventory=None;self.eligible=set();self.rows={};self.queue=[];self.closed=False
 def install(self,inventory,eligible):
  with self.cv:
   if self.inventory is not None or self.closed:raise Refused('recovery intake already bound')
   self.inventory=json.loads(canonical(inventory));self.eligible=set(eligible)
 def accept(self,envelope):
  if type(envelope)is not dict or set(envelope)!={'request_id','payload','signature'} or not isinstance(envelope['request_id'],str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}',envelope['request_id']):raise Refused('fixed gateway envelope')
  raw=envelope['payload'];sig=envelope['signature']
  if type(raw)is not str or not 1<=len(raw.encode())<=MAX or type(sig)is not str or not 1<=len(sig)<=1024 or any(ord(c)<32 or ord(c)>126 for c in sig):raise Refused('signed gateway bounds')
  raw=raw.encode();event=decode(raw);eid=event.get('id');obj=event.get('data',{}).get('object',{})
  with self.cv:
   if self.closed or self.inventory is None:raise Refused('recovery not receiving')
   proof=self.inventory['events'].get(eid)
   if eid not in self.eligible or not proof or event.get('type')!=proof['type'] or event.get('created')!=proof['created'] or event.get('livemode')is not (self.inventory['mode']=='LIVE') or event.get('api_version')!='2026-09-30.endive' or obj.get('id')!=proof['object_id'] or hashlib.sha256(canonical(obj)).hexdigest()!=proof['object_sha256']:raise Refused('uninventoried delivery')
   identity=hashlib.sha256(raw).hexdigest();signature=hashlib.sha256(sig.encode()).hexdigest()
   if eid in self.rows:
    row=self.rows[eid]
    if row['body']!=identity:raise Refused('changed delivery')
    # A new signature does not authorize another dispatch. Normal Finance handles
    # subsequent fresh redeliveries only after the global publication barrier.
    return 200 if row['state']=='DONE' and row['signature']==signature else 503
   if len(self.rows)>=1000 or self.queue or any(r['state']=='PENDING' for r in self.rows.values()):raise Refused('one recovery delivery slot')
   self.rows[eid]={'state':'PENDING','body':identity,'signature':signature};self.queue.append((eid,raw,sig));self.cv.notify_all();return 503
 def take(self,deadline):
  with self.cv:
   while not self.queue and not self.closed:
    left=deadline-time.monotonic()
    if left<=0:raise Refused('signed recovery delivery deadline')
    self.cv.wait(left)
   if self.closed:raise Refused('recovery intake closed')
   return self.queue.pop(0)
 def done(self,eid):
  with self.cv:self.rows[eid]['state']='DONE';self.cv.notify_all()
 def close(self):
  with self.cv:self.closed=True;self.cv.notify_all()

def server(service,address=('0.0.0.0',27001)):
 from cold_front import Server
 from intake_gateway import handler
 # Use existing intake proxy after successful publication. Before that there is
 # exactly one restricted application route, never a general closed-front bypass.
 from http.server import BaseHTTPRequestHandler
 class Handler(BaseHTTPRequestHandler):
  protocol_version='HTTP/1.1'
  def log_message(self,*a):pass
  def setup(self):
   super().setup();self.connection.settimeout(20);sock=self.connection
   def expire():
    try:sock.shutdown(socket.SHUT_RDWR)
    except OSError:pass
    sock.close()
   self.timer=threading.Timer(20,expire);self.timer.daemon=True;self.timer.start()
  def finish(self):
   try:super().finish()
   finally:self.timer.cancel()
  def respond(self,status,body,extra=None):
   self.close_connection=True;self.send_response(status);self.send_header('Content-Length',str(len(body)));self.send_header('Content-Type','application/json');self.send_header('Connection','close');self.send_header('Cache-Control','no-store')
   if status==503:self.send_header('Retry-After','60')
   for k,v in (extra or {}).items():self.send_header(k,v)
   self.end_headers()
   try:self.wfile.write(body)
   except OSError:pass
  def do_GET(self):self.route()
  def do_POST(self):self.route()
  def do_PUT(self):self.route()
  def do_DELETE(self):self.route()
  def route(self):
   if self.command=='GET' and self.path=='/health':return self.respond(200,b'{"supervisor":"alive","business_readiness":"separate"}')
   if service.is_open():return handler(service.co.front).route(self)
   if self.command!='POST' or self.path!='/fin/v1/stripe/events':return self.respond(503,b'{"error":"recovery closed"}')
   try:
    for name,expected in (('Authorization','Bearer '+service.gateway_auth['service_token']),('X-FIN-Caller-Token',service.gateway_auth['caller_token'])):
     values=self.headers.get_all(name,[])
     if len(values)!=1 or not hmac.compare_digest(values[0],expected):raise Refused('gateway authentication')
    lengths=self.headers.get_all('Content-Length',[])
    if len(lengths)!=1 or self.headers.get('Transfer-Encoding') or self.headers.get('Expect') or sum(len(k)+len(v) for k,v in self.headers.items())>16384:raise Refused('framing')
    n=int(lengths[0])
    if not 1<=n<=2*MAX+4096:raise Refused('gateway body bound')
    raw=self.rfile.read(n)
    if len(raw)!=n:raise Refused('short gateway body')
    status=service.inbox.accept(decode(raw));return self.respond(status,b'{"state":"accepted"}' if status==200 else b'{"state":"pending"}')
   except Exception:return self.respond(503,b'{"error":"recovery delivery unavailable"}')
 class BoundedServer(Server):
  def handle_error(self,*a):pass
 return BoundedServer(address,Handler)
