"""Explicit repaired completion; ordinary FAILED and outbox history retained."""
import copy,re
from owner_wire import Refused,canonical,decode
from schedule_state import valid_due,valid_run
KEYS={'schema','due','trigger_id','run','catalog_sha256','catalog_snapshot','completion_sha256','incident_sha256','archives_sha256'}
def validate(p):
 if type(p)is not dict or set(p)!=KEYS or p['schema']!='repaired-complete-v1':raise Refused('repaired proof schema')
 valid_due(p['due']);valid_run(p['run'])
 if p['trigger_id']!='scheduled-'+str(p['due']) or any(type(p[k])is not str or re.fullmatch('[0-9a-f]{64}',p[k])is None for k in KEYS-{'schema','due','trigger_id','run'}):raise Refused('repaired proof binding')
def complete(s,p):
 validate(p);s.transaction()
 try:
  s.db.execute('CREATE TABLE IF NOT EXISTS repaired_history(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL)')
  row=s.db.execute('SELECT body FROM repaired_history WHERE id=1').fetchone();old=s.current()
  if row:
   value=decode(row[0].encode())
   if value['proof']!=p or old is None or old['phase']!='REPAIRED_COMPLETE' or old['completion']!=p:raise Refused('repaired archive occupied')
   s.db.commit();return old
  if old is None or old['phase'] not in ('FAILED','DISPATCHED') or any(old[k]!=p[k] for k in ('due','run','trigger_id')):raise Refused('repaired current identity')
  history={'original':old,'proof':copy.deepcopy(p)};raw=canonical(history)
  if len(raw)>8192:raise Refused('repaired history bound')
  s.db.execute('INSERT INTO repaired_history(id,body) VALUES(1,?)',(raw.decode(),))
  value=dict(old,phase='REPAIRED_COMPLETE',completion=copy.deepcopy(p));s.save(value);s.db.commit();return value
 except BaseException:s.db.rollback();raise
