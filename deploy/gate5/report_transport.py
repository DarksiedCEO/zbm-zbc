"""Report-only Resend adapter; fixed TLS destination and memory-only credential.
No scheduling, retry or configurable destination.
"""
import base64,hashlib,http.client,json,os,re,selectors,ssl,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from report_delivery import Refused,freeze,encode,MAX_REPORT
MAX_REQUEST=3*1024*1024
MAX_RESPONSE=64*1024
MAX_INPUT=5*1024*1024
MAX_OUTPUT=512
HOST='api.resend.com'

def pairs(items):
 result={}
 for k,v in items:
  if k in result:raise ValueError('duplicate JSON key')
  result[k]=v
 return result

def decode(raw):return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid JSON')))

def sender_valid(sender):
 return type(sender)is str and re.fullmatch('[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@zbestmedia[.]com',sender) is not None and '..' not in sender and '.@' not in sender

def wire_body(key,body,sender):
 try:
  if not sender_valid(sender) or type(key)is not str or not re.fullmatch('report/[0-9a-f]{64}',key) or type(body)is not bytes or len(body)>MAX_REQUEST:raise ValueError()
  data=decode(body)
  fields={'version','client_id','invoice_id','audit_id','audit_report_sha256','document_sha256','document_bytes','tenant_id','report_id','sender','recipient','subject','attachment_name','attachment_base64'}
  if type(data)is not dict or set(data)!=fields or data['version']!='gate5-approved-report-v1' or data['sender']!=sender:raise ValueError()
  report=base64.b64decode(data['attachment_base64'],validate=True)
  binding={k:data[k] for k in ('client_id','invoice_id','audit_id','audit_report_sha256','document_sha256','document_bytes','tenant_id','report_id')}
  expected_key,expected_body=freeze(binding,report,sender,data['recipient'])
  if key!=expected_key or body!=expected_body:raise ValueError()
  wire=encode({'from':sender,'to':[data['recipient']],'subject':data['subject'],'text':'Your approved SEO report is attached. Document SHA256: '+data['document_sha256']+'\nAudit report SHA256: '+data['audit_report_sha256'],'attachments':[{'filename':'seo-report.html','content':data['attachment_base64']}]})
  if len(wire)>MAX_REQUEST:raise ValueError()
  return wire
 except Exception:raise Refused('INVALID_FROZEN_REPORT') from None

def _worker_command():return [sys.executable,'-I','-B',str(Path(__file__).resolve()),'--worker']

def _exchange(payload: bytes, deadline) -> bytes:
    """Bound stdin/stdout while timing the entire owned child, including DNS and trickle reads."""
    child = None
    try:
        child = subprocess.Popen(_worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env={"PATH": os.defpath}, close_fds=True)
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdin.fileno(), False)
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdin, selectors.EVENT_WRITE)
            selector.register(child.stdout, selectors.EVENT_READ)
            sent, output = 0, bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                for key, _ in selector.select(remaining):
                    if key.fileobj is child.stdin:
                        sent += os.write(child.stdin.fileno(), payload[sent:sent + 16384])
                        if sent == len(payload):
                            selector.unregister(child.stdin)
                            child.stdin.close()
                    else:
                        chunk = os.read(child.stdout.fileno(), MAX_OUTPUT + 1 - len(output))
                        if not chunk:
                            child.wait(timeout=max(0, deadline - time.monotonic()))
                            if child.returncode != 0 or time.monotonic() >= deadline:
                                raise ValueError("worker")
                            return bytes(output)
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            raise ValueError("worker output")
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait()
            for pipe in (child.stdin, child.stdout):
                if pipe is not None:
                    pipe.close()

class Transport:
 def __init__(self,credential,sender,*,deadline_seconds=10):
  if type(credential)is not str or not re.fullmatch('re_[A-Za-z0-9_-]{29,253}',credential) or not sender_valid(sender) or type(deadline_seconds)is not int or not 1<=deadline_seconds<=30:raise Refused('INVALID_TRANSPORT_CONFIG')
  self._credential=credential;self.sender=sender;self.deadline_seconds=deadline_seconds
 def __repr__(self):return '<ReportTransport credential=REDACTED fixed_destination=api.resend.com>'
 def _destination(self):return {'mode':'tls'}
 def __call__(self,key,body):
  wire=wire_body(key,body,self.sender)
  payload=encode(dict(self._destination(),credential=self._credential,key=key,body=base64.b64encode(wire).decode(),timeout=self.deadline_seconds))
  if len(payload)>MAX_INPUT:raise Refused('REQUEST_TOO_LARGE')
  try:
   raw=_exchange(payload,time.monotonic()+self.deadline_seconds);out=decode(raw)
   if type(out)is dict and set(out)=={'outcome','message_id'} and out['outcome']=='accepted' and type(out['message_id'])is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',out['message_id']):return out
  except Exception:pass
  return {'outcome':'unknown'}

def attempt(data):
 conn=None
 try:
  timeout=data['timeout'];key=data['key'];credential=data['credential']
  if type(timeout)is not int or not 1<=timeout<=30 or not re.fullmatch('report/[0-9a-f]{64}',key) or not re.fullmatch('re_[A-Za-z0-9_-]{29,253}',credential):return {'outcome':'unknown'}
  body=base64.b64decode(data['body'],validate=True)
  if len(body)>MAX_REQUEST:return {'outcome':'unknown'}
  # Bound response header lines/count as well as body. No redirects/proxies.
  http.client._MAXLINE=4096;http.client._MAXHEADERS=32
  if data['mode']=='tls':conn=http.client.HTTPSConnection(HOST,443,timeout=timeout,context=ssl.create_default_context())
  else:return {'outcome':'unknown'}
  conn.request('POST','/emails',body=body,headers={'Authorization':'Bearer '+credential,'Idempotency-Key':key,'Content-Type':'application/json','Accept':'application/json','Accept-Encoding':'identity','User-Agent':'zbm-gate5-operator-report/1'})
  response=conn.getresponse()
  if response.status!=200 or response.getheader('Content-Type','').split(';')[0].strip().lower()!='application/json' or response.getheader('Content-Encoding','identity')!='identity':return {'outcome':'unknown'}
  lengths=response.headers.get_all('Content-Length',[])
  if len(lengths)>1 or (lengths and (not lengths[0].isdigit() or int(lengths[0])>MAX_RESPONSE)):return {'outcome':'unknown'}
  raw=response.read(MAX_RESPONSE+1)
  if len(raw)>MAX_RESPONSE:return {'outcome':'unknown'}
  # A bounded read may return valid JSON after premature EOF without raising.
  # Declared framing must be complete before interpreting an acceptance.
  if lengths and (len(raw)!=int(lengths[0]) or response.length!=0):return {'outcome':'unknown'}
  result=decode(raw)
  if type(result)is dict and set(result)=={'id'} and type(result['id'])is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',result['id']):return {'outcome':'accepted','message_id':result['id']}
 except Exception:pass
 finally:
  if conn:conn.close()
 return {'outcome':'unknown'}

if __name__=='__main__':
 out={'outcome':'unknown'}
 try:
  if sys.argv[1:]==['--worker']:
   raw=sys.stdin.buffer.read(MAX_INPUT+1)
   if len(raw)<=MAX_INPUT:out=attempt(decode(raw))
 except Exception:pass
 sys.stdout.buffer.write(encode(out));sys.stdout.buffer.flush()
