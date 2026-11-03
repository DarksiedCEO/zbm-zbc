"""One bounded nonsecret recovery trigger; the persistent service owns ingress."""
import hashlib,re,time
from owner_wire import Refused,canonical,decode
from packaged_restore import selection

def request_from_text(raw):
 if type(raw)is not str or not 1<=len(raw.encode())<=16384:raise Refused('bounded recovery selection required')
 request=decode(raw.encode())
 if type(request)is not dict or set(request)!={'kind','selection','scope'} or request['kind']!='recover':raise Refused('fixed recovery selection')
 selection(request['selection'])
 scope=request['scope'];fields={'account','invoice','client','session','payment_intent','acceptance','tenant','audit','mode'}
 if type(scope)is not dict or set(scope)!=fields or any(type(v)is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',v) for v in scope.values()) or scope['mode']not in ('TEST','LIVE'):raise Refused('fixed recovery scope')
 return request

def wait_result(client,request,*,clock=time.monotonic,sleep=time.sleep):
 digest=hashlib.sha256(canonical(request)).hexdigest();end=clock()+900
 try:client.call('/submit',request)
 except Refused:pass # only query this exact intent; never repeat an uncertain start
 while clock()<end:
  value=client.call('/status',{'kind':'recovery_status'})
  if type(value)is not dict or set(value)!={'state','request_sha256'} or value['request_sha256']!=digest:raise Refused('recovery response identity')
  if value['state']=='OPEN':return value
  if value['state']!='RUNNING':raise Refused('recovery closed or unresolved')
  sleep(2)
 raise Refused('recovery operator deadline; persistent service remains authoritative')
