"""Operator-approved report outbox, distinct from Finance receipts.

Adapted from the private Gate4 frozen-envelope outbox; no automatic retries.
Provider acceptance never claims inbox delivery.
"""
import base64,hashlib,json,os,re,sqlite3
from pathlib import Path
MAX_REPORT=2*1024*1024
CAPACITY=32
class Refused(RuntimeError):pass

def sha(data):return hashlib.sha256(data).hexdigest()
def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def freeze(binding,report,sender,recipient):
 fields={'client_id','invoice_id','audit_id','audit_report_sha256','document_sha256','document_bytes','tenant_id','report_id'}
 if type(binding)is not dict or set(binding)!=fields:raise Refused('INVALID_BINDING')
 for k in ('client_id','invoice_id','audit_id','tenant_id','report_id'):
  if type(binding[k])is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',binding[k]):raise Refused('INVALID_BINDING')
 for k in ('audit_report_sha256','document_sha256'):
  if type(binding[k])is not str or not re.fullmatch('[0-9a-f]{64}',binding[k]):raise Refused('INVALID_HASH')
 if type(report)is not bytes or not 0<len(report)<=MAX_REPORT or type(binding['document_bytes'])is not int or binding['document_bytes']!=len(report) or sha(report)!=binding['document_sha256']:raise Refused('REPORT_MISMATCH')
 for address in (sender,recipient):
  if type(address)is not str or not re.fullmatch('[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,189}',address):raise Refused('INVALID_MAILBOX')
 identity='report/'+sha(encode({'client':binding['client_id'],'invoice':binding['invoice_id'],'audit':binding['audit_id'],'recipient':recipient,'tenant':binding['tenant_id']}))
 body=encode(dict(version='gate5-approved-report-v1',**binding,sender=sender,recipient=recipient,subject='Your SEO audit report',attachment_name='seo-report.html',attachment_base64=base64.b64encode(report).decode()))
 return identity,body

class Outbox:
 def __init__(self,path):
  path=Path(path)
  if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:raise Refused('PRIVATE_OUTBOX_REQUIRED')
  self.db=sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=1)
  self.db.execute('PRAGMA synchronous=FULL')
  try:self.db.execute('SELECT identity,body,digest,status,attempts,message_id FROM deliveries LIMIT 1').fetchall()
  except BaseException:self.db.close();raise
 @classmethod
 def create(cls,path):
  fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
  db=sqlite3.connect(path)
  try:
   db.execute('CREATE TABLE deliveries(identity TEXT PRIMARY KEY,body BLOB NOT NULL,digest TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL,message_id TEXT)');db.commit()
  finally:db.close()
  fd=os.open(Path(path).parent,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
  return cls(path)
 def close(self):self.db.close()
 def enqueue(self,binding,report,*,sender,recipient,capacity=CAPACITY):
  if type(capacity)is not int or not 1<=capacity<=CAPACITY:raise Refused('INVALID_CAPACITY')
  key,body=freeze(binding,report,sender,recipient);digest=sha(body)
  self.db.execute('BEGIN IMMEDIATE')
  try:
   old=self.db.execute('SELECT body,digest FROM deliveries WHERE identity=?',(key,)).fetchone()
   if old:
    if old!=(body,digest):raise Refused('FROZEN_ENVELOPE_CONFLICT')
   else:
    if self.db.execute('SELECT count(*) FROM deliveries').fetchone()[0]>=capacity:raise Refused('OUTBOX_CAPACITY')
    self.db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,NULL)',(key,body,digest,'PENDING',0))
   self.db.commit();return key
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
 def dispatch(self,key,send):
  self.db.execute('BEGIN IMMEDIATE')
  try:
   row=self.db.execute('SELECT body,digest,status,attempts,message_id FROM deliveries WHERE identity=?',(key,)).fetchone()
   if row is None:raise Refused('UNKNOWN_DELIVERY')
   body,digest,status,attempts,message=row
   if type(body)is not bytes or len(body)>3*MAX_REPORT or sha(body)!=digest or type(attempts)is not int or not 0<=attempts<=3:raise Refused('CORRUPT_DELIVERY')
   if status=='ACCEPTED':
    self.db.commit();return dict(status=status,attempts=attempts,provider_message_id=message,inbox_delivered=False)
   if status!='PENDING' or attempts!=0:raise Refused('RECONCILIATION_REQUIRED')
   self.db.execute("UPDATE deliveries SET status='DISPATCHING',attempts=? WHERE identity=?",(attempts+1,key));self.db.commit()
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
  # Fixed report transport has a whole-child deadline. A timeout is ambiguous.
  try:result=send(key,body)
  except Exception:result=None
  status='UNKNOWN';message=None
  if type(result)is dict:
   if result.get('outcome')=='accepted' and type(result.get('message_id'))is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',result['message_id']):status='ACCEPTED';message=result['message_id']
   elif result.get('outcome')=='rejected_before_acceptance':status='EXHAUSTED' if attempts+1>=3 else 'REJECTED'
  self.db.execute('UPDATE deliveries SET status=?,message_id=? WHERE identity=?',(status,message,key))
  return dict(status=status,attempts=attempts+1,provider_message_id=message,inbox_delivered=False)
