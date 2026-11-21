"""Fixed owner-local dependency ports; no caller-selected destination or raw fallback."""
import threading
from dependency_relay import Authority,Client,BoundedServer,handler,http_exchange,decode,enc,Refused,MEMBERS,profile
PORTS={'ledger':27110,'legal':27111,'compliance':27112,'finance':27113}
TARGETS={'ledger':(), 'finance':('ledger','legal','compliance'), 'legal':('ledger','compliance'), 'compliance':('ledger','legal'), 'seo':('ledger','finance','compliance')}
ENV_KEYS={'finance':{'legal':'FIN_LEGAL_URL','compliance':'FIN_COMPLIANCE_URL'},'legal':{'compliance':'LEGAL_COMPLIANCE_URL'},'compliance':{'legal':'COMPLIANCE_LEGAL_URL'},'seo':{'finance':'SEO_FINANCE_URL','compliance':'SEO_COMPLIANCE_URL'},'ledger':{}}
def configure_environment(member,env):
 if member not in TARGETS:raise Refused('source member')
 if member!='ledger':env['LEDGER_SERVICE_URL']='http://127.0.0.1:27110'
 for target,name in ENV_KEYS[member].items():
  if name in env:env[name]='http://127.0.0.1:'+str(PORTS[target])
 if 'COLD_LEGAL_HOST' in env:
  if 'legal' not in TARGETS[member]:raise Refused('fixture Legal source')
  env['COLD_LEGAL_HOST']='127.0.0.1';env['COLD_LEGAL_PORT']=str(PORTS['legal'])
def local_callback(owner,target):
 if target not in TARGETS[owner.member]:raise Refused('fixed source target')
 def local(method,path,hs,body):
  profile(owner.member,target,method,path,body) # malformed local input never dispatched
  if owner.relay.boots is None or owner.relay.unknown or owner.fenced or owner.journal.recovered or owner.relay_mode not in ('STARTUP','NORMAL','DRAIN'):raise Refused('source closed')
  def exchange(meta,data,sig):
   status,raw,out=http_exchange(target+'.railway.internal',27003,'/dependency',data,{'Content-Length':str(len(data)),'X-Context':enc(meta).hex(),'X-MAC':sig})
   if status!=200 or len(raw.get_all('X-Context',[]))!=1 or len(raw.get_all('X-MAC',[]))!=1:raise Refused('target unavailable')
   return decode(bytes.fromhex(raw['X-Context'])),out,raw['X-MAC']
  try:return Client(owner.token,owner.relay.boots,owner.member,target,exchange).call(method,path,hs,body)
  except BaseException:
   # Forwarding uncertainty never means cancellation or permission to capture.
   owner.relay.unknown=True
   raise
 return local

def install_dependency_relay(owner):
 owner.relay_mode='CLOSED';owner.relay_servers=[];owner.relay_threads=[]
 def allowed():return not owner.fenced and not owner.journal.recovered and owner.relay_mode in ('STARTUP','NORMAL','DRAIN') and owner.child is not None and owner.child.poll() is None
 def forward(method,path,hs,body):
  status,raw,out=http_exchange('127.0.0.1',27000,path,body,{**hs,':method':method,'Content-Length':str(len(body))})
  return status,{k:v for k,v in raw.items() if k.lower() in ('content-type','retry-after','cache-control')},out
 owner.relay=Authority(owner.token,owner.member,owner.boot,allowed,forward)
 listeners=[(('0.0.0.0',27003),owner.relay.invoke,True)]
 listeners += [(('127.0.0.1',PORTS[target]),local_callback(owner,target),False) for target in TARGETS[owner.member]]
 try:
  for address,callback,remote in listeners:
   server=BoundedServer(address,handler(callback,remote));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();owner.relay_servers.append(server);owner.relay_threads.append(thread)
  configure_environment(owner.member,owner.env)
 except BaseException:
  close_dependency_relay(owner)
  raise

def close_dependency_relay(owner):
 owner.relay_mode='CLOSED'
 for s in owner.relay_servers:s.shutdown();s.server_close()
 for t in owner.relay_threads:t.join(2)
