"""Four immutable bounded incident slots. Caller owns the control process lock.

No evidence deletion, implicit resolution, or authority supplied by this store.
The alert intent lives outside the slot directory for existing outbox delivery.
"""
import hashlib,os
from pathlib import Path
from owner_wire import Refused,canonical,decode
from metadata_retirement import read,save,CAP
SLOT_BYTES=CAP
SLOTS=4
ALERT_BYTES=16384
# Active owner journal + atomic replacement, all slots + one temp, alert reserve.
CONTROL_HEADROOM=2*524288+(SLOTS+1)*1048576+ALERT_BYTES
class Archive:
 def __init__(self,control):
  self.control=Path(control);self.root=self.control/'incident-archive'
  if self.root.is_symlink():raise Refused('archive alias')
  self.root.mkdir(mode=0o700,exist_ok=True)
 def scan(self):
  allowed={'slot-'+str(i)+'.json' for i in range(SLOTS)}
  if any(p.name not in allowed for p in self.root.iterdir()):raise Refused('interrupted or unknown archive entry')
  values=[];seen=set();total=0
  for p in sorted(self.root.iterdir()):
   raw=read(p,SLOT_BYTES);total+=len(raw);v=decode(raw)
   self.validate(v)
   if canonical(v)!=raw or v['identity'] in seen:raise Refused('archive identity or bytes')
   seen.add(v['identity']);values.append((p.name,v))
  if total>SLOTS*SLOT_BYTES:raise Refused('archive byte envelope')
  return values
 @staticmethod
 def validate(v):
  if type(v)is not dict or set(v)!={'identity','phase','evidence'} or type(v['identity'])is not str or not 1<=len(v['identity'])<=128 or v['phase']!='RESOLVED' or type(v['evidence'])is not dict or len(canonical(v))>SLOT_BYTES:raise Refused('archive requires bounded resolved evidence')
 def capacity(self):
  values=self.scan();repairs=self.control/'repair-archives'
  names=sorted(p.name for p in repairs.iterdir()) if repairs.exists() else []
  if len(values)+len(names)>=SLOTS:
   self.full_alert(names);raise Refused('incident archive full; restore fallback required')
  fs=os.statvfs(self.control)
  if fs.f_bavail*fs.f_frsize<CONTROL_HEADROOM or fs.f_favail<32:raise Refused('control safety headroom')
  return {'slots':SLOTS,'used':len(values),'slot_bytes':SLOT_BYTES,'control_headroom':CONTROL_HEADROOM}
 def alert(self):
  p=self.control/'archive-full-alert.json'
  return decode(read(p,ALERT_BYTES))
 def full_alert(self,repair_names=()):
  values=self.scan()
  alert={'schema':1,'event':'incident_archive_full','identity':hashlib.sha256(canonical({'slots':values,'repairs':list(repair_names)})).hexdigest(),'primary':'andre@zbestmedia.com','copy':'hello@zbestmedia.com','independent_fallback':False,'delivery':'PENDING_NOT_SENT'}
  path=self.control/'archive-full-alert.json'
  if path.exists():
   if self.alert()!=alert:raise Refused('archive full alert changed')
  else:save(path,alert)
 def append(self,value):
  self.validate(value);values=self.scan()
  for name,v in values:
   if v['identity']==value['identity']:
    if v!=value:raise Refused('archive identity conflict')
    return {'slot':name,'sha256':hashlib.sha256(canonical(v)).hexdigest()}
  repairs=self.control/'repair-archives'
  repair_names=sorted(p.name for p in repairs.iterdir()) if repairs.exists() else []
  if len(values)+len(repair_names)>=SLOTS:
   self.full_alert(repair_names)
   raise Refused('incident archive full; restore fallback required')
  self.capacity();used={n for n,_ in values};name=next('slot-'+str(i)+'.json' for i in range(SLOTS) if 'slot-'+str(i)+'.json' not in used)
  save(self.root/name,value)
  return {'slot':name,'sha256':hashlib.sha256(canonical(value)).hexdigest()}

def notify_full(control,member,client):
 """Explicit exact retry to existing observer; never recreates body or incident."""
 a=Archive(control).alert()
 return client.call('/submit',{'action':'archive-full','member':member,'identity':a['identity']})

CONTROL_BYTES=8*1048576
CONTROL_ENTRIES=256
def control_capacity(control):
 """Same-volume control inventory plus reserved replacement/notification space."""
 import stat
 root=Path(control);entries=list(root.rglob('*'));total=0
 if len(entries)>CONTROL_ENTRIES:raise Refused('control inode envelope')
 for p in entries:
  st=p.lstat()
  if stat.S_ISREG(st.st_mode):total+=st.st_size
  elif not stat.S_ISDIR(st.st_mode):raise Refused('control entry type')
 if total>CONTROL_BYTES:raise Refused('control total byte envelope')
 fs=os.statvfs(root)
 if fs.f_bavail*fs.f_frsize<CONTROL_HEADROOM or fs.f_favail<32:raise Refused('control safety headroom')
 return {'bytes':total,'entries':len(entries),'byte_limit':CONTROL_BYTES,'entry_limit':CONTROL_ENTRIES,'replacement_reserve':CONTROL_HEADROOM}

def checked_capacity(control,member,client):
 try:
  result=control_capacity(control);Archive(control).capacity();return result
 except Refused:
  if (Path(control)/'archive-full-alert.json').exists():
   try:notify_full(control,member,client)
   except BaseException:pass # frozen local intent survives unavailable observer
  raise
