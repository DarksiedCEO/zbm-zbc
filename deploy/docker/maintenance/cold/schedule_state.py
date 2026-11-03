"""Bounded durable run trigger and independent synthetic alert outbox.

No provider send or completion authority: complete() is internal and receives
only the fixed authenticated completion adapter's result. One unresolved run
blocks another backup, never application admission. SQLite DELETE journaling
bounds database+rollback journal to approximately 2 MiB plus FS metadata.
"""
import copy,hashlib,json,re,sqlite3
from pathlib import Path
from owner_wire import canonical,decode,Refused
MAX_EVENTS=64;MAX_BODY=4096

def check(ok):
 if not ok:raise Refused('schedule/outbox evidence refused')
def valid_due(due):check(type(due)is int and 0<=due<=253402300799)
def valid_run(run):check(type(run)is str and re.fullmatch('cold-[0-9a-f]{32}',run)is not None)
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()
class Schedule:
 def __init__(self,path,*,check_same_thread=True):
  self.path=Path(path);self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  check(not self.path.is_symlink())
  self.db=sqlite3.connect(self.path,timeout=2,isolation_level=None,check_same_thread=check_same_thread)
  self.db.execute('PRAGMA journal_mode=DELETE');self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA page_size=4096');self.db.execute('PRAGMA max_page_count=256')
  check(self.db.execute('PRAGMA quick_check').fetchone()==('ok',))
  self.db.executescript('CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS current (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, body TEXT NOT NULL, sha TEXT NOT NULL, delivery TEXT NOT NULL, attempts INTEGER NOT NULL);')
  # A previous delivery invocation may have reached the provider. Preserve that
  # uncertainty; do not send again or change the frozen body/idempotency key.
  self.db.execute("UPDATE events SET delivery='UNKNOWN' WHERE delivery='DISPATCHING'")
  self.pending(include_terminal=True)
 def configuration(self):
  row=self.db.execute('SELECT body FROM schedule WHERE id=1').fetchone()
  if row is None:return None
  check(len(row[0])<=512);v=decode(row[0].encode())
  check(set(v)=={'version','first_due','cadence','grace'} and type(v['version'])is int and v['version']==1)
  valid_due(v['first_due']);check(type(v['cadence'])is int and 60<=v['cadence']<=2678400)
  check(type(v['grace'])is int and 1<=v['grace']<=86400);valid_due(v['first_due']+v['grace'])
  return v
 def arm(self,first_due,cadence,*,grace=900):
  valid_due(first_due);check(type(cadence)is int and 60<=cadence<=2678400)
  check(type(grace)is int and 1<=grace<=86400);valid_due(first_due+grace)
  v={'version':1,'first_due':first_due,'cadence':cadence,'grace':grace};self.transaction()
  try:
   old=self.configuration()
   if old is not None:check(old==v)
   else:
    # No ambiguous retroactive migration of an already-running unarmed monitor.
    check(self.current() is None)
    self.db.execute('INSERT INTO schedule(id,body) VALUES(1,?)',(canonical(v).decode(),))
   self.db.commit();return v
  except BaseException:self.db.rollback();raise
 def monitoring(self):
  config=self.configuration()
  if config is None:return {'state':'UNARMED','expected_due':None}
  current=self.current();due=config['first_due']
  if current is not None:
   check(current['due']>=due and (current['due']-due)%config['cadence']==0)
   due=current['due']+(config['cadence'] if current['phase'] in ('COMPLETE','REPAIRED_COMPLETE') else 0)
  valid_due(due);valid_due(due+config['grace'])
  return {'state':'ARMED','expected_due':due}
 def close(self):self.db.close()
 def transaction(self):self.db.execute('BEGIN IMMEDIATE')
 def current(self):
  row=self.db.execute('SELECT body FROM current WHERE id=1').fetchone()
  if row is None:return None
  check(len(row[0])<=8192);v=decode(row[0].encode())
  check(set(v)=={'due','trigger_id','run','phase','completion'});valid_due(v['due']);valid_run(v['run'])
  check(v['trigger_id']=='scheduled-'+str(v['due']) and v['phase'] in ('BOUND','DISPATCHED','FAILED','COMPLETE','REPAIRED_COMPLETE'))
  return v
 def save(self,v):
  raw=canonical(v);check(len(raw)<=8192)
  self.db.execute('INSERT OR REPLACE INTO current(id,body) VALUES(1,?)',(raw.decode(),))
 def bind(self,due,run):
  valid_due(due);valid_run(run);self.transaction()
  try:
   config=self.configuration()
   if config is not None:check(due==self.monitoring()['expected_due'])
   old=self.current()
   if old and old['phase'] not in ('COMPLETE','REPAIRED_COMPLETE'):
    check(old['due']==due and old['run']==run);self.db.commit();return old
   if old:check(due>old['due'] and run!=old['run'])
   check(self.db.execute('SELECT count(*) FROM events').fetchone()[0]<MAX_EVENTS)
   value={'due':due,'trigger_id':'scheduled-'+str(due),'run':run,'phase':'BOUND','completion':None};self.save(value);self.db.commit();return value
  except BaseException:self.db.rollback();raise
 def dispatched(self,due,run):
  self.transaction()
  try:
   v=self.current();check(v is not None and v['due']==due and v['run']==run and v['phase']=='BOUND');v['phase']='DISPATCHED';self.save(v);self.db.commit()
  except BaseException:self.db.rollback();raise
 def failed(self):
  self.transaction()
  try:
   v=self.current();check(v is not None and v['phase'] in ('BOUND','DISPATCHED'));v['phase']='FAILED';self.save(v);self.db.commit()
  except BaseException:self.db.rollback();raise
 def complete(self,proof):
  check(type(proof)is dict and set(proof)=={'due','trigger_id','run','catalog_sha256','catalog_snapshot','completion_sha256'})
  for k in ('catalog_sha256','catalog_snapshot','completion_sha256'):check(type(proof[k])is str and re.fullmatch('[0-9a-f]{64}',proof[k])is not None)
  self.transaction()
  try:
   v=self.current();check(v is not None and v['phase']=='DISPATCHED' and all(proof[k]==v[k] for k in ('due','trigger_id','run')))
   v.update(phase='COMPLETE',completion=copy.deepcopy(proof));self.save(v);self.db.commit()
  except BaseException:self.db.rollback();raise
 def observe(self,now,*,deadline=900):
  valid_due(now);check(type(deadline)is int and 1<=deadline<=86400)
  self.transaction()
  try:
   v=self.current();config=self.configuration();event=None
   # Armed deadline is durable and cannot be moved by observer arguments/restart.
   grace=config['grace'] if config is not None else deadline
   if v and v['phase'] not in ('COMPLETE','REPAIRED_COMPLETE') and (v['phase']=='FAILED' or now>=v['due']+grace):
    code='BACKUP_FAILED' if v['phase']=='FAILED' else 'MISSED_COMPLETION'
    event={'trigger_id':v['trigger_id'],'run':v['run'],'due':v['due'],'error':code}
   elif config is not None and (v is None or v['phase'] in ('COMPLETE','REPAIRED_COMPLETE')):
    due=self.monitoring()['expected_due']
    if now>=due+grace:event={'trigger_id':'scheduled-'+str(due),'run':None,'due':due,'error':'MISSING_TRIGGER'}
   if event is not None:
    eid=digest(event);body={'event':event,'to':['andre@zbestmedia.com'],'cc':['hello@zbestmedia.com'],'copy_is_independent_fallback':False,'idempotency_key':'cold-alert/'+eid}
    raw=canonical(body);check(len(raw)<=MAX_BODY)
    row=self.db.execute('SELECT body,sha FROM events WHERE id=?',(eid,)).fetchone()
    if row:check(row==(raw.decode(),hashlib.sha256(raw).hexdigest()))
    else:
     check(self.db.execute('SELECT count(*) FROM events').fetchone()[0]<MAX_EVENTS)
     self.db.execute('INSERT INTO events VALUES(?,?,?,?,0)',(eid,raw.decode(),hashlib.sha256(raw).hexdigest(),'PENDING'))
   self.db.commit()
  except BaseException:self.db.rollback();raise
  return self.pending()
 def pending(self,include_terminal=False):
  rows=self.db.execute('SELECT id,body,sha,delivery,attempts FROM events ORDER BY id').fetchall();check(len(rows)<=MAX_EVENTS);out=[]
  for eid,raw,sha,status,attempts in rows:
   check(len(raw)<=MAX_BODY and hashlib.sha256(raw.encode()).hexdigest()==sha and status in ('PENDING','DISPATCHING','ACCEPTED','FAILED','UNKNOWN') and type(attempts)is int and attempts in (0,1))
   body=decode(raw.encode());check(set(body)=={'event','to','cc','copy_is_independent_fallback','idempotency_key'} and digest(body['event'])==eid and body['idempotency_key']=='cold-alert/'+eid and body['to']==['andre@zbestmedia.com'] and body['cc']==['hello@zbestmedia.com'] and body['copy_is_independent_fallback']is False)
   if include_terminal or status=='PENDING':out.append({'id':eid,'body':body,'delivery':status,'attempts':attempts})
  return out
 def archive_full(self,member,identity):
  check(member in ('coordinator','ledger','finance','legal','compliance','seo'))
  check(type(identity)is str and re.fullmatch('[0-9a-f]{64}',identity)is not None)
  event={'trigger_id':'archive-'+identity,'run':None,'due':None,'error':'ARCHIVE_FULL','member':member}
  eid=digest(event);body={'event':event,'to':['andre@zbestmedia.com'],'cc':['hello@zbestmedia.com'],'copy_is_independent_fallback':False,'idempotency_key':'cold-alert/'+eid}
  raw=canonical(body);check(len(raw)<=MAX_BODY);self.transaction()
  try:
   old=self.db.execute('SELECT body,sha FROM events WHERE id=?',(eid,)).fetchone()
   if old is not None:check(old==(raw.decode(),hashlib.sha256(raw).hexdigest()))
   else:
    check(self.db.execute('SELECT count(*) FROM events').fetchone()[0]<MAX_EVENTS)
    self.db.execute('INSERT INTO events VALUES(?,?,?,?,0)',(eid,raw.decode(),hashlib.sha256(raw).hexdigest(),'PENDING'))
   self.db.commit()
  except BaseException:self.db.rollback();raise
  return {'event_id':eid,'retained':True}
 def recovery_locked(self,run,identity):
  valid_run(run)
  check(type(identity)is str and re.fullmatch('[0-9a-f]{64}',identity)is not None)
  event={'trigger_id':'recovery-'+identity,'run':run,'due':None,'error':'RECOVERY_LOCKED'}
  eid=digest(event);body={'event':event,'to':['andre@zbestmedia.com'],'cc':['hello@zbestmedia.com'],'copy_is_independent_fallback':False,'idempotency_key':'cold-alert/'+eid}
  raw=canonical(body);check(len(raw)<=MAX_BODY);self.transaction()
  try:
   old=self.db.execute('SELECT body,sha FROM events WHERE id=?',(eid,)).fetchone()
   if old is not None:check(old==(raw.decode(),hashlib.sha256(raw).hexdigest()))
   else:
    check(self.db.execute('SELECT count(*) FROM events').fetchone()[0]<MAX_EVENTS)
    self.db.execute('INSERT INTO events VALUES(?,?,?,?,0)',(eid,raw.decode(),hashlib.sha256(raw).hexdigest(),'PENDING'))
   self.db.commit()
  except BaseException:self.db.rollback();raise
  return {'event_id':eid,'retained':True}
 def attempt(self,eid,outcome):
  """Synthetic-only delivery outcome. DISPATCHING is durable before invocation."""
  check(outcome in ('ACCEPTED','FAILED','UNKNOWN'));self.pending(include_terminal=True);self.transaction()
  try:
   row=self.db.execute('SELECT delivery,attempts FROM events WHERE id=?',(eid,)).fetchone();check(row==('PENDING',0))
   self.db.execute("UPDATE events SET delivery='DISPATCHING',attempts=1 WHERE id=?",(eid,));self.db.commit()
  except BaseException:self.db.rollback();raise
  self.db.execute('UPDATE events SET delivery=? WHERE id=? AND delivery=\'DISPATCHING\'',(outcome,eid))
