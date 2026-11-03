"""Authenticated trigger-only entry; existing coordinator owns every backup effect."""
import threading,time
from owner_wire import Refused
from schedule_state import valid_due
from observer_link import Link

def alive(c):
 worker=getattr(c,'scheduled_worker',None)
 return bool(worker and worker.is_alive())
def busy_other(c):
 worker=getattr(c,'scheduled_worker',None)
 return bool(worker and worker.is_alive() and threading.current_thread() is not worker)
def observed(c):
 v=c.observer.call('/status',{'kind':'monitor_status'})
 if not isinstance(v,dict) or set(v)!={'monitoring','current'}:raise Refused('observer status schema')
 m=v['monitoring']
 if not isinstance(m,dict) or set(m)!={'state','expected_due'} or m['state']!='ARMED':raise Refused('observer unarmed')
 valid_due(m['expected_due']);return v

def control(c,path,body):
 if path not in ('/submit','/status') or type(body)is not dict or set(body)!=({'kind','due'} if path=='/submit' else {'kind'}) or body['kind']!='scheduled_backup':raise Refused('fixed scheduled trigger')
 with c.control_lock:
  view=observed(c)
  if path=='/status':return {'active':alive(c),'run':c.doc['run'],'phase':c.doc['phase'],'schedule':c.doc.get('schedule'),'observer':view,'failure':c.doc.get('trigger_failure')}
  due=body['due'];valid_due(due);old=view['current']
  if old is not None and old.get('due')==due and old.get('phase') in ('COMPLETE','REPAIRED_COMPLETE'):
   return {'accepted':False,'completed':True,'due':due,'run':old['run']}
  if c.doc.get('schedule') is not None:
   if c.doc['schedule']['due']!=due:raise Refused('scheduled incident pending')
   return {'accepted':False,'completed':False,'due':due,'run':c.doc['run'],'active':alive(c),'recovery_required':not alive(c)}
  if due!=view['monitoring']['expected_due'] or due>int(time.time()):raise Refused('not expected due')
  if c.doc['phase']!='OPEN' or c.normal_active or alive(c) or c.repair_worker and c.repair_worker.is_alive():raise Refused('coordinator not ready')
  from schedule_advance import require_no_pending
  require_no_pending(c)
  run=c.doc['run'];c.bind_schedule(due) # durable reservation before thread/effect
  def execute():
   link=None;stage='notify_started'
   try:
    link=Link(c.root/'notifications',c.observer,due,run)
    link.send('started');stage='capture'
    c.cycle();stage='upload';result=c.transfer()
    if result.get('phase')!='COMPLETE':raise Refused('transfer incomplete')
    stage='retire';c.retire();proof=c.completion.publish(due,run)
    stage='notify_complete';link.send('complete',proof)
    stage='advance';c.next_run()
   except BaseException as exc:
    # Existing owners decide admission. An upload/notification failure never closes it.
    failure={'due':due,'run':run,'stage':stage,'type':type(exc).__name__,'alert_confirmed':False}
    try:
     if link is not None and stage not in ('notify_complete','advance'):link.send('failed');failure['alert_confirmed']=True
    except BaseException:pass # Independent overdue observer remains authoritative.
    try:c.update(trigger_failure=failure)
    except BaseException:pass # Original durable schedule/operation remains unresolved.
  worker=threading.Thread(target=execute,name='scheduled-cold-backup',daemon=False);c.scheduled_worker=worker
  try:worker.start()
  except BaseException:raise Refused('trigger worker unavailable; reservation retained') from None
  return {'accepted':True,'completed':False,'due':due,'run':run}
