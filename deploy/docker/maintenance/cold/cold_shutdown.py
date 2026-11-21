"""Conventional in-process worker creation gate, no request identity or journal."""
import threading,time,contextlib
NAMES=frozenset(('fin-thin-client','legal-compliance38-call','cmp-legal-client'))
class Workers:
 def __init__(self):self.lock=threading.RLock();self.threads=set();self.closed=False;self.started=0;self.finished=0
 @contextlib.contextmanager
 def installed(self):
  original=threading.Thread.start;owner=self
  def start(t,*args,**kwargs):
   if t.name not in NAMES:return original(t,*args,**kwargs)
   with owner.lock:
    if owner.closed:raise RuntimeError('cold shutdown refuses new worker')
    owner.threads.add(t);run=t.run
    def finish():
     try:return run()
     finally:
      with owner.lock:owner.threads.discard(t);owner.finished+=1
    t.run=finish
    try:result=original(t,*args,**kwargs);owner.started+=1;return result
    except BaseException:owner.threads.discard(t);t.run=run;raise
  threading.Thread.start=start
  try:yield self
  finally:threading.Thread.start=original
 def close_and_join(self,seconds):
  end=time.monotonic()+seconds
  with self.lock:self.closed=True
  while True:
   with self.lock:current=tuple(self.threads)
   if not current:return True
   remaining=end-time.monotonic()
   if remaining<=0:return False
   for t in current:t.join(max(0,end-time.monotonic()))

def finish(service,workers,seconds=20):
 end=time.monotonic()+seconds;scheduler=getattr(service,'scheduler',None)
 if scheduler is not None:
  if not scheduler.pause_and_drain(max(0,end-time.monotonic())):raise RuntimeError('scheduler drain incomplete')
  scheduler.stop();scheduler._thread.join(max(0,end-time.monotonic()))
  if scheduler.alive():raise RuntimeError('scheduler thread remains')
 if not workers.close_and_join(max(0,end-time.monotonic())):raise RuntimeError('thin client worker remains')
 # Uvicorn already drained HTTP tasks and its event loop has closed. AnyIO
 # worker threads must then stop; we do not infer writer exit from caller exit.
 for t in threading.enumerate():
  if t.name=='AnyIO worker thread':
   t.join(max(0,end-time.monotonic()))
   if t.is_alive():raise RuntimeError('native request worker remains')
 service.close()
 return {'clean':True,'scheduler_joined':scheduler is not None,'workers_started':workers.started,'workers_finished':workers.finished,'service_closed':service.closed}
