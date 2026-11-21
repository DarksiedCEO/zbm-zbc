"""Inherited parent-child scheduler channel; no network endpoint or credentials."""
import json,re,secrets,socket,threading,time
class Refused(RuntimeError):pass
def identity(epoch,instance):
 if not re.fullmatch(r'owner-[1-9][0-9]{0,9}',epoch) or not re.fullmatch('[a-f0-9]{32}',instance):raise Refused('child context')
def send(sock,value):
 raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode()+b'\n'
 if len(raw)>2048:raise Refused('frame bound')
 sock.sendall(raw)
def receive(sock,deadline=None,frame_seconds=25):
 data=bytearray()
 while len(data)<2048:
  if deadline is not None:
   remaining=deadline-time.monotonic()
   if remaining<=0:raise Refused('frame deadline')
   sock.settimeout(remaining)
  else:sock.settimeout(None)
  b=sock.recv(1)
  if deadline is None:deadline=time.monotonic()+frame_seconds
  if not b:raise Refused('channel closed')
  if b==b'\n':
   def pairs(rows):
    d={}
    for k,v in rows:
     if k in d:raise Refused('duplicate field')
     d[k]=v
    return d
   try:return json.loads(data,object_pairs_hook=pairs)
   except (ValueError,UnicodeError):raise Refused('invalid frame') from None
  data.extend(b)
 raise Refused('frame bound')
class Parent:
 def __init__(self,sock,epoch,instance,timeout=25):
  identity(epoch,instance)
  if not 0<timeout<=25:raise Refused('timeout bound')
  self.timeout=timeout;self.sock=sock;self.sock.settimeout(timeout);self.epoch=epoch;self.instance=instance;self.lock=threading.Lock();self.fenced=False
 def command(self,command):
  if command not in ('pause','resume'):raise Refused('command')
  with self.lock:
   if self.fenced:raise Refused('channel outcome unknown')
   request={'epoch':self.epoch,'instance':self.instance,'command':command,'id':secrets.token_hex(16)}
   try:
    deadline=time.monotonic()+self.timeout
    self.sock.settimeout(self.timeout)
    send(self.sock,request);reply=receive(self.sock,deadline)
    if not isinstance(reply,dict) or set(reply)!={'request','ok','state'} or reply['request']!=request or reply['ok']is not True:raise Refused('child refused')
    state=reply['state']
    if not isinstance(state,dict) or set(state)!={'pause_requested','inflight_ticks','thread_alive','ready_for_backup'}:raise Refused('state schema')
    if any(type(state[k])is not bool for k in ('pause_requested','thread_alive','ready_for_backup')) or type(state['inflight_ticks'])is not int or state['inflight_ticks']<0 or not state['thread_alive']:raise Refused('state type')
    if command=='pause' and (not state['ready_for_backup'] or not state['pause_requested'] or state['inflight_ticks']!=0):raise Refused('drain unproved')
    if command=='resume' and (state['pause_requested'] or state['ready_for_backup']):raise Refused('resume unproved')
    return state
   except BaseException:
    self.fenced=True;raise Refused('scheduler outcome unknown') from None
 def close(self):
  self.fenced=True
  try:self.sock.shutdown(socket.SHUT_RDWR)
  except OSError:pass
  self.sock.close()
class Child:
 def __init__(self,sock,scheduler,epoch,instance,drain_seconds=20):
  identity(epoch,instance)
  if not 0<drain_seconds<=20:raise Refused('drain bound')
  self.sock=sock;self.scheduler=scheduler;self.epoch=epoch;self.instance=instance;self.seconds=drain_seconds;self.closing=threading.Event();self.thread=None
 def start(self):
  if self.thread is not None:raise Refused('already started')
  self.thread=threading.Thread(target=self.run,name='cold-scheduler-control',daemon=False);self.thread.start()
 def run(self):
  try:
   while not self.closing.is_set():
    request=receive(self.sock)
    if not isinstance(request,dict) or set(request)!={'epoch','instance','command','id'} or request['epoch']!=self.epoch or request['instance']!=self.instance or request['command']not in ('pause','resume') or not isinstance(request['id'],str) or not re.fullmatch('[a-f0-9]{32}',request['id']):raise Refused('request context')
    if request['command']=='pause':ok=self.scheduler.pause_and_drain(self.seconds)
    else:self.scheduler.resume();ok=True
    self.sock.settimeout(25)
    state=self.scheduler.backup_state();send(self.sock,{'request':request,'ok':ok is True,'state':state})
    if not ok:break
  except BaseException:pass # No exception/credential or service data leaves the channel.
  finally:
   if not self.closing.is_set():
    try:self.scheduler.pause_and_drain(self.seconds)
    except BaseException:pass # Parent receives no clean/drain evidence.
   try:self.sock.shutdown(socket.SHUT_RDWR)
   except OSError:pass
   self.sock.close()
 def close(self):
  self.closing.set()
  try:self.sock.shutdown(socket.SHUT_RDWR)
  except OSError:pass
  if self.thread is not None:
   self.thread.join(self.seconds+1)
   if self.thread.is_alive():raise Refused('scheduler control did not stop')
  self.sock.close()
