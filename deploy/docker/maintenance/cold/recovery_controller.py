"""Single fixed explicit-mode recovery command; stdin credentials, native closed evidence.

No arbitrary routes or provider ingestion bypass. Original signed redeliveries
arrive on bounded stdin frames; Finance verifies them. Provider account/mode must match native Finance configuration.
"""
import base64,hashlib,json,re,sys,time,select
from owner_wire import MEMBERS,canonical,Refused
from coordinator import START
from provider_recovery import ProviderInterval

def provider_scope(port,scope,now):
 # Provider-created Checkout lifetime is conservative; operator cutoff is ignored.
 kind,status,s=port._call('GET','/v1/checkout/sessions/'+scope['session'])
 if kind!='ok' or status!=200 or type(s)is not dict or s.get('object')!='checkout.session' or s.get('id')!=scope['session'] or s.get('client_reference_id')!=scope['invoice'] or s.get('payment_intent')!=scope['payment_intent'] or s.get('livemode')is not (scope.get('mode','TEST')=='LIVE'):raise Refused('provider lifetime binding')
 return ProviderInterval(port,**{k:scope[k] for k in ('account','invoice','client','session','payment_intent')},cutoff=s.get('created'),through=now,now=now,livemode=scope.get('mode','TEST')=='LIVE')

def bind(client,member,boot,profile):
 raw=canonical(profile);base={'member':member,'boot':boot,'action':'recovery-bind'}
 if not 1<=len(raw)<=262144:raise Refused('profile bound')
 client.call('/submit',dict(base,phase='begin',bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
 for off in range(0,len(raw),1536):client.call('/submit',dict(base,phase='chunk',offset=off,chunk=base64.b64encode(raw[off:off+1536]).decode()))
 if client.call('/submit',dict(base,phase='commit'))!={'state':'BOUND'}:raise Refused('profile not bound')

def observation(co,member,key,sample):
 base={'member':member,'boot':co.doc['boots'][member],'action':'recovery-read','key':key,'sample':sample}
 # A lost submit reply is resolved only by status, never another effect request.
 try:co.clients[member].call('/submit',base)
 except Refused:pass
 end=time.monotonic()+25;raw=bytearray();identity=None
 while time.monotonic()<end:
  v=co.clients[member].call('/status',dict(base,offset=len(raw)))
  if v.get('state')=='PENDING':time.sleep(.1);continue
  if v.get('state')!='DONE' or type(v.get('bytes'))is not int or not 0<=v['bytes']<=4*1048576 or v.get('offset')!=len(raw):raise Refused('closed read incomplete')
  current=(v['bytes'],v.get('sha256'))
  if identity is None:identity=current
  if identity!=current:raise Refused('read changed')
  chunk=base64.b64decode(v['chunk'],validate=True)
  if len(chunk)>12288 or len(raw)+len(chunk)>v['bytes']:raise Refused('read framing')
  raw.extend(chunk)
  if len(raw)==v['bytes']:
   if hashlib.sha256(raw).hexdigest()!=v['sha256']:raise Refused('read digest')
   return bytes(raw) if key=='finance-log' else json.loads(raw)
  if len(chunk)!=12288:raise Refused('short read')
 raise Refused('closed read deadline')

def business_checks(invoice,acceptance,audit,reports,scope):
 legal=invoice.get('legal_ref',{})
 if acceptance.get('acceptance_id')!=scope['acceptance'] or legal.get('acceptance_id')!=scope['acceptance'] or any(legal.get(k)!=acceptance.get(k) for k in ('doc_id','version','doc_sha256')) or acceptance.get('evidence_sufficient')is not True or acceptance.get('basis_in_force')is not True:raise Refused('actual acceptance evidence')
 if audit.get('audit_id')!=scope['audit'] or audit.get('tenant_id')!=scope['tenant'] or audit.get('invoice_id')!=scope['invoice']:raise Refused('actual audit identity')
 rows=reports.get('reports') if isinstance(reports,dict) else reports
 if not isinstance(rows,list) or len(rows)>1000:raise Refused('actual report inventory required')
 disposition='COMPLETED_HASH_BOUND_REPORT'
 if audit.get('status')=='interrupted':
  if rows or audit.get('report') is not None or audit.get('report_sha256') is not None or type(audit.get('completed_at'))is not str or not audit['completed_at'] or type(audit.get('interrupted_reason'))is not str or not audit['interrupted_reason']:raise Refused('terminal interrupted evidence')
  disposition='TERMINAL_INTERRUPTED_NO_REEXECUTION'
 else:
  if audit.get('status')!='completed' or not rows:raise Refused('audit incomplete or ambiguous')
  sha=audit.get('report_sha256')
  if type(sha)is not str or not re.fullmatch('[0-9a-f]{64}',sha):raise Refused('audit digest')
  for row in rows:
   if row.get('audit_id')!=scope['audit'] or row.get('tenant_id')!=scope['tenant'] or row.get('audit_report_sha256')!=sha or not re.fullmatch('[0-9a-f]{64}',str(row.get('document_sha256'))):raise Refused('report binding')
 return {'audit_disposition':disposition,'acceptance_sha256':hashlib.sha256(canonical(acceptance)).hexdigest(),'audit_sha256':hashlib.sha256(canonical(audit)).hexdigest(),'reports_sha256':hashlib.sha256(canonical(reports)).hexdigest()}

def execute(value,*,restore,coordinator_factory,provider_factory,deliveries,clock=time.time):
 fields={'schema','restore','coordination_token','stripe_key','scope'}
 if type(value)is not dict or set(value)!=fields or value['schema']!='fixed-application-recovery-v1':raise Refused('fixed recovery command')
 scope=value['scope'];sf={'account','invoice','client','session','payment_intent','acceptance','tenant','audit','mode'}
 if type(scope)is not dict or set(scope)!=sf or any(type(v)is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',v) for v in scope.values()):raise Refused('fixed recovery scope')
 if not re.fullmatch('[0-9a-f]{64}',str(value['coordination_token'])):raise Refused('coordination credential')
 if scope['mode'] not in ('TEST','LIVE') or type(value['stripe_key'])is not str or not value['stripe_key'].startswith('sk_test_' if scope['mode']=='TEST' else 'sk_live_'):raise Refused('key/profile mode mismatch')
 data=restore(value['restore']);loc=data['selected_catalog'];run=loc['set_id']
 if data.get('state')!='DATA_RESTORED_CLOSED' or data.get('business_admission')!='NOT_OPENED':raise Refused('closed data result required')
 co=coordinator_factory(value['coordination_token'],value['restore'],run)
 try:
  co.front.close();co.discover()
  for m in MEMBERS:
   p=co.clients[m].call('/status',{'action':'recovery-source','member':m,'boot':co.doc['boots'][m],'set_id':run})
   if p!={'member':m,'boot':co.doc['boots'][m],'set_id':run,'manifest_sha256':data['data']['members'][m]['manifest_sha256']}:raise Refused('restored source mount mismatch')
  for m in MEMBERS:co.clients[m].call('/relay-bind',{'member':m,'boot':co.doc['boots'][m],'boots':co.doc['boots']})
  for m in START:co.command(m,'start')
  for m in MEMBERS:co.command(m,'verify')
  provider=provider_scope(provider_factory(value['stripe_key'],scope['mode']),scope,int(clock()));inventory=provider.inventory()
  profile={k:scope[k] for k in ('account','invoice','client','acceptance','tenant','audit')};profile.update(set_id=run,inventory=inventory)
  for m in MEMBERS:bind(co.clients[m],m,co.doc['boots'][m],profile)
  from recovery_event import submit_signed
  seen=set()
  for raw,signature in deliveries(inventory):
   eid=json.loads(raw).get('id')
   if eid not in inventory['events'] or eid in seen:raise Refused('unexpected signed delivery')
   submit_signed(co.clients['finance'],{'member':'finance','boot':co.doc['boots']['finance'],'set_id':run,'account':scope['account'],'event_id':eid},raw,signature);seen.add(eid)
  # Existing committed events need not be resent; actual full log proves them.
  snapshot={'journal':observation(co,'finance','journal',0),'finance_log':observation(co,'finance','finance-log',0),'invoice':observation(co,'finance','invoice',0),'ledger_events':observation(co,'ledger','ledger',0)}
  if type(snapshot['ledger_events'])is not list or any(type(x)is not dict for x in snapshot['ledger_events']):raise Refused('native Ledger shape')
  snapshot['ledger_events']=[x for x in snapshot['ledger_events'] if x.get('kind')=='event']
  proof=provider.reconcile(inventory,snapshot)
  app=business_checks(snapshot['invoice'],observation(co,'legal','acceptance',0),observation(co,'seo','audit',0),observation(co,'seo','reports',0),scope)
  for m in MEMBERS:co.command(m,'verify')
  co.update(phase='RECOVERY_RECONCILED_CLOSED',recovery={'selected_catalog':loc,'provider':proof,'applications':app,'mode':scope['mode'],'operator_cutoff_used':False})
  co.publish()
  return {'state':'APPLICATION_RECOVERED','selected_catalog':loc,'reconciliation':proof,'applications':app,'business_admission':'OPEN_'+scope['mode']}
 except BaseException:
  co.front.close()
  raise

def frame(stream,limit,deadline):
 # Byte-bounded and whole-deadline stdin; no unbounded readline/trickle wait.
 import os
 raw=bytearray()
 while time.monotonic()<deadline:
  ready,_,_=select.select([stream.fileno()],[],[],max(0,deadline-time.monotonic()))
  if not ready:break
  b=os.read(stream.fileno(),1)
  if not b:raise Refused('stdin ended')
  if b==b'\n':
   def pairs(items):
    out={}
    for key,value in items:
     if key in out:raise Refused('duplicate stdin key')
     out[key]=value
    return out
   return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(Refused('nonfinite stdin')))
  raw.extend(b)
  if len(raw)>limit:raise Refused('stdin frame bound')
 raise Refused('stdin deadline')

def restore_group(value,token,*,client_factory=None):
 """Each existing service restores only its own fresh volume; no shared mount."""
 from packaged_restore import selection
 from owner_wire import Client
 if type(value)is not dict or set(value)!={'schema','password','credentials','selection'} or value['schema']!='fixed-b2-data-restore-v1':raise Refused('fixed selected restore')
 locator=selection(value['selection']);factory=client_factory or (lambda m:Client(m,m+'.railway.internal',27002,token))
 clients={m:factory(m) for m in MEMBERS};boots={};receipts={}
 for m,c in clients.items():
  v=c.call('/status',{'action':'recovery-restore','member':m,'boot':'discover'})
  if set(v)!={'member','boot','phase'} or v['member']!=m or v['phase']!='RESTORE_WAIT' or not re.fullmatch('[0-9a-f]{32}',str(v['boot'])):raise Refused('fresh recovery owner required')
  boots[m]=v['boot']
 for m,c in clients.items():
  op='restore-'+hashlib.sha256(canonical({'member':m,'locator':locator})).hexdigest();base={'action':'recovery-restore','member':m,'boot':boots[m],'operation':op}
  try:c.call('/submit',dict(base,locator=locator))
  except Refused:pass # no write retry; query exactly the same attempt
  end=time.monotonic()+130
  while time.monotonic()<end:
   v=c.call('/status',base)
   if v.get('operation')!=op:raise Refused('restore status identity')
   if v.get('state')=='PENDING':time.sleep(.1);continue
   r=v.get('receipt',{})
   if v.get('state')!='DONE' or r.get('schema')!='member-restore-v1' or r.get('member')!=m or r.get('set_id')!=locator['set_id'] or r.get('catalog_snapshot')!=locator['snapshot'] or r.get('catalog_sha256')!=locator['catalog_sha256'] or r.get('application_start')!='NOT_PERFORMED' or not re.fullmatch('[0-9a-f]{64}',str(r.get('manifest_sha256'))):raise Refused('member restore unknown/mismatch')
   receipts[m]=r;break
  else:raise Refused('member restore deadline')
 return {'state':'DATA_RESTORED_CLOSED','business_admission':'NOT_OPENED','selected_catalog':locator,'data':{'members':receipts}}

def retain_locked(value,*,root=None,client=None):
 """Immutable bounded refusal, then independent observer outbox; never reopen."""
 import os,stat
 from pathlib import Path
 from packaged_restore import selection
 from owner_wire import Client
 loc=selection(value['restore']['selection']);token=value['coordination_token']
 if type(token)is not str or not re.fullmatch('[0-9a-f]{64}',token):raise Refused('alert identity')
 body={'action':'recovery-locked','run':loc['set_id'],'identity':hashlib.sha256(canonical(loc)).hexdigest()}
 root=Path('/volume/control') if root is None else Path(root)
 root.mkdir(mode=0o700,parents=True,exist_ok=True)
 if root.is_symlink() or root.resolve()!=root:raise Refused('alert custody')
 path=root/'recovery-locked.json';tmp=root/'recovery-locked.tmp';raw=canonical(body)
 if tmp.exists():raise Refused('alert publication interrupted')
 if path.exists() or path.is_symlink():
  fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
  try:
   st=os.fstat(fd)
   if not stat.S_ISREG(st.st_mode) or st.st_size>1024 or os.read(fd,1025)!=raw:raise Refused('retained recovery incident differs')
  finally:os.close(fd)
 else:
  fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path);fd=os.open(root,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
 c=client or Client('observer','observer.railway.internal',27003,token)
 result=c.call('/submit',body)
 if type(result)is not dict or set(result)!={'event_id','retained'} or result['retained']is not True or not re.fullmatch('[0-9a-f]{64}',str(result['event_id'])):raise Refused('observer alert unconfirmed')
 return result

def main(argv=None,expected_token=None):
 handles=[];value=None;token_checked=False
 try:
  if (sys.argv[1:] if argv is None else argv):raise Refused('no argv credentials')
  value=frame(sys.stdin.buffer,16384,time.monotonic()+30)
  if expected_token is not None:
   import hmac
   if type(value)is not dict or type(value.get('coordination_token'))is not str or not hmac.compare_digest(value['coordination_token'],expected_token):raise Refused('configured coordination token required')
  token_checked=True
  restore=lambda data:restore_group(data,value['coordination_token'])
  from coordinator import Coordinator
  def factory(token,r,run):
   import intake_gateway,threading
   co=Coordinator('/volume/control',token,r['password'],transport_credentials={'key_id':r['credentials']['key_id'],'key':r['credentials']['application_key']},run_id=run)
   gateway=intake_gateway.server(co.front);thread=threading.Thread(target=gateway.serve_forever,daemon=True);thread.start();handles.append((co,gateway,thread));return co
  def provider(key,mode):
   from provider_recovery import transport_from_key
   return transport_from_key(key,mode)
  def deliveries(inv):
   end=time.monotonic()+180
   for _ in range(len(inv['events'])+1):
    v=frame(sys.stdin.buffer,351000,end)
    if v=={'phase':'reconcile'}:return
    if type(v)is not dict or set(v)!={'body_base64','signature'}:raise Refused('signed frame shape')
    raw=base64.b64decode(v['body_base64'],validate=True)
    if not 1<=len(raw)<=262144:raise Refused('signed frame bound')
    yield raw,v['signature']
   raise Refused('signed frame count')
  result=execute(value,restore=restore,coordinator_factory=factory,provider_factory=provider,deliveries=deliveries)
  print(json.dumps(result,sort_keys=True,separators=(',',':')),flush=True)
  # Keep the exclusive coordinator/front alive; native owner lifetime is separate.
  while True:time.sleep(1)
 except Exception:
  for co,_,_ in handles:co.front.close()
  if token_checked:
   try:retain_locked(value)
   except Exception:print('RECOVERY_ALERT_UNCONFIRMED',flush=True)
  print('RECOVERY_REFUSED_CLOSED',flush=True);return 2
 finally:
  for co,gateway,thread in handles:
   co.front.close();gateway.shutdown();gateway.server_close();thread.join(2);co.front.stop();co.catalog_server.shutdown();co.catalog_server.server_close()
if __name__=='__main__':raise SystemExit(main())
