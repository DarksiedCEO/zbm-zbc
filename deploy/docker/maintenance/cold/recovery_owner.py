"""Owner installer for fixed closed recovery reads and signed Finance redelivery.

Uses existing authenticated control paths; no caller-selected URL or app token.
New source proof is available only before native startup, against owned source.
"""
import base64,hashlib,json,re,threading,http.client,socket,time
from owner_wire import Refused,canonical
from metadata_retirement import read
CAP=4*1048576

def install(o):
 old=o.dispatch;lock=threading.RLock();binding=None;profile=None;results={}
 def closed(started=True):
  if o.front.opened:raise Refused('public recovery must stay closed')
  if started:
   if o.phase!='VERIFIED_CLOSED' or o.child is None or o.child.poll()is not None:raise Refused('verified current child required')
  elif o.phase!='CLOSED' or o.child is not None:raise Refused('fresh stopped owner required')
 def native(key):
  p=profile;prefix={'ledger':'LEDGER','finance':'FIN','legal':'LEGAL','compliance':'COMPLIANCE','seo':'SEO'}[o.member]
  if key=='finance-log' and o.member=='finance':return read(o.source/'fin_log.jsonl',CAP)
  routes={'ledger':{'ledger':'/ledger/entries'},'finance':{'journal':'/fin/v1/journal/zbm/entries','invoice':'/fin/v1/invoices/'+p['invoice']},'legal':{'acceptance':'/legal/v1/acceptances/'+p['acceptance']},'seo':{'audit':'/seo/v1/tenants/'+p['tenant']+'/audits/'+p['audit'],'reports':'/seo/v1/tenants/'+p['tenant']+'/audits/'+p['audit']+'/client-reports'},'compliance':{}}
  path=routes[o.member].get(key)
  if path is None:raise Refused('fixed recovery read')
  headers={'Authorization':'Bearer '+o.env[prefix+'_SERVICE_TOKEN'],'X-Andre-Approval-Token':o.env.get(prefix+'_ANDRE_APPROVAL_TOKEN','')}
  if o.member in ('finance','seo'):headers['X-'+prefix+'-Caller-Token']=json.loads(o.env[prefix+'_CALLER_TOKENS'])['onboarding' if o.member=='finance' else 'dashboard']
  c=http.client.HTTPConnection('127.0.0.1',27000,timeout=20);timer=None
  try:
   c.connect();sock=c.sock
   def expire():
    try:sock.shutdown(socket.SHUT_RDWR)
    except OSError:pass
    sock.close()
   timer=threading.Timer(20,expire);timer.start();c.request('GET',path,headers=headers);r=c.getresponse();raw=r.read(CAP+1)
   if r.status!=200 or len(raw)>CAP:raise Refused('native read refused')
   return raw
  finally:
   if timer:timer.cancel()
   c.close()
 def dispatch(path,body):
  nonlocal binding,profile
  if path not in ('/submit','/status') or body.get('action') not in ('recovery-source','recovery-bind','recovery-read'):return old(path,body)
  with lock:
   if body.get('member')!=o.member or body.get('boot')!=o.boot:raise Refused('recovery boot identity')
   if body['action']=='recovery-source':
    if path!='/status':raise Refused('source read path')
    closed(False)
    if set(body)!={'member','boot','action','set_id'} or not re.fullmatch('cold-[0-9a-f]{32}',str(body['set_id'])):raise Refused('source context')
    from cold_capture import source_inventory
    manifest={'member':o.member,'set_id':body['set_id'],'files':source_inventory(o.source,o.member)}
    return {'member':o.member,'boot':o.boot,'set_id':body['set_id'],'manifest_sha256':hashlib.sha256(canonical(manifest)).hexdigest()}
   closed()
   if body['action']=='recovery-bind':
    if path!='/submit':raise Refused('binding write path')
    if profile is not None:raise Refused('recovery already bound')
    phase=body.get('phase')
    if phase=='begin':
     if set(body)!={'member','boot','action','phase','bytes','sha256'} or binding is not None or type(body['bytes'])is not int or not 1<=body['bytes']<=262144 or not re.fullmatch('[0-9a-f]{64}',str(body['sha256'])):raise Refused('binding shape')
     binding={'raw':bytearray(),'bytes':body['bytes'],'sha256':body['sha256'],'deadline':time.monotonic()+30};return {'state':'RECEIVING'}
    if binding is None or time.monotonic()>binding['deadline']:raise Refused('binding expired')
    if phase=='chunk':
     if set(body)!={'member','boot','action','phase','offset','chunk'} or type(body['offset'])is not int or body['offset']!=len(binding['raw']) or type(body['chunk'])is not str or len(body['chunk'])>2048:raise Refused('binding chunk')
     raw=base64.b64decode(body['chunk'],validate=True)
     if not raw or len(binding['raw'])+len(raw)>binding['bytes']:raise Refused('binding bound')
     binding['raw'].extend(raw);return {'state':'RECEIVING'}
    if phase!='commit' or set(body)!={'member','boot','action','phase'} or len(binding['raw'])!=binding['bytes'] or hashlib.sha256(binding['raw']).hexdigest()!=binding['sha256']:raise Refused('binding digest')
    p=json.loads(binding['raw']);fields={'set_id','account','invoice','client','acceptance','tenant','audit','inventory'}
    if type(p)is not dict or set(p)!=fields or not re.fullmatch('cold-[0-9a-f]{32}',str(p['set_id'])):raise Refused('fixed profile')
    if any(type(p[k])is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',p[k]) for k in fields-{'inventory'}):raise Refused('fixed profile identities')
    if o.member=='finance' and ((o.env.get('FIN_STRIPE_LIVE','0')=='1') != (p['inventory'].get('mode')=='LIVE')):raise Refused('native configured Stripe mode')
    if o.member=='finance':
     from recovery_event import RecoveryEvents
     gate=RecoveryEvents(o,set_id=p['set_id'],account=p['account'],inventory=p['inventory']);gate.install()
    profile=p;binding=None
    return {'state':'BOUND'}
   if profile is None:raise Refused('unbound recovery')
   key=body.get('key');sample=body.get('sample');identity=(key,sample)
   if type(key)is not str or key not in ('ledger','journal','invoice','finance-log','acceptance','audit','reports') or type(sample)is not int or not 0<=sample<=2:raise Refused('fixed observation')
   if path=='/submit':
    if set(body)!={'member','boot','action','key','sample'}:raise Refused('read schema')
    if identity in results:return {'state':results[identity]['state']}
    if any(v['state']=='PENDING' for v in results.values()) or sum(len(v.get('raw',b'')) for v in results.values())+CAP>8*1048576:raise Refused('read capacity')
    results[identity]={'state':'PENDING'}
    def work():
     try:
      closed();raw=native(key);closed()
      with lock:results[identity]={'state':'DONE','raw':raw,'sha256':hashlib.sha256(raw).hexdigest()}
     except BaseException:
      with lock:results[identity]={'state':'UNKNOWN'}
    threading.Thread(target=work,name='fixed-recovery-read',daemon=False).start();return {'state':'PENDING'}
   if set(body)!={'member','boot','action','key','sample','offset'} or type(body['offset'])is not int or body['offset']<0 or body['offset']%12288:raise Refused('read status')
   v=results.get(identity)
   if v is None:raise Refused('read absent')
   if v['state']!='DONE':return {'state':v['state']}
   off=body['offset'];raw=v['raw']
   if off>len(raw):raise Refused('read offset')
   return {'state':'DONE','bytes':len(raw),'sha256':v['sha256'],'offset':off,'chunk':base64.b64encode(raw[off:off+12288]).decode()}
 o.dispatch=dispatch
