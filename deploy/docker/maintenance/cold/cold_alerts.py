"""Bridge the observer's durable events to the existing bounded email outbox.

Trusted construction only. No credential lookup, HTTP route, installed schedule
or provider factory. A supplied sender must be the bounded transport; tests use
an explicit synthetic sender. The secondary is a same-inbox copy.
"""
import threading
from datetime import datetime,timedelta
from fallback import FallbackDispatcher
from owner_wire import Refused

class Alerts:
 def __init__(self,control,root,profile,send):
  self.control=control;self.lock=threading.Lock()
  self.dispatcher=FallbackDispatcher(root,profile,send,secondary='hello@zbestmedia.com')
 def tick(self,now):
  if not isinstance(now,datetime) or now.utcoffset()!=timedelta(0):raise Refused('UTC required')
  if not self.lock.acquire(False):raise Refused('alert dispatch already active')
  try:
   pending=self.control.tick();results=[]
   for row in pending[:1]:
    event=row['body']['event'];error={'MISSING_TRIGGER':'MISSING_TRIGGER','MISSED_COMPLETION':'OVERDUE','BACKUP_FAILED':'LOCKED','ARCHIVE_FULL':'LOCKED','RECOVERY_LOCKED':'LOCKED'}.get(event['error'])
    if error is None:raise Refused('alert event unsupported')
    # Unknown retained bytes are not asserted as zero: this bridge does not
    # originate disk-pressure events; source is the durable schedule only.
    mapped={'set_id':event['run'] or event['trigger_id'],'phase':'scheduled-backup','error':error,'retained_bytes':None,'last_complete':None}
    outcome=self.dispatcher.step(mapped,now)
    terminal={'ACCEPTED','UNKNOWN','EXHAUSTED','EXPIRED'}
    if outcome['primary'] in terminal and outcome['secondary'] in terminal:
     status='ACCEPTED' if outcome['primary']==outcome['secondary']=='ACCEPTED' else ('UNKNOWN' if 'UNKNOWN' in (outcome['primary'],outcome['secondary']) else 'FAILED')
     with self.control.store() as store:
      if any(x['id']==row['id'] for x in store.pending()):store.attempt(row['id'],status)
    results.append(outcome)
   return results
  finally:self.lock.release()
