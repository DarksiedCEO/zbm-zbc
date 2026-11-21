"""Bounded Linux status evidence for the exact live owned child handle."""
import os,re
CAPS=('CapEff','CapPrm','CapBnd','CapAmb','CapInh')
def parse_status(raw,pid):
 if type(raw)is not bytes or len(raw)>16384 or type(pid)is not int or pid<=0:raise ValueError('child status bound')
 values={}
 for line in raw.decode('ascii').splitlines():
  key,sep,value=line.partition(':')
  if key in ('Pid','Uid','NoNewPrivs',*CAPS):
   if not sep or key in values:raise ValueError('child status duplicate')
   values[key]=value.strip()
 if set(values)!=set(('Pid','Uid','NoNewPrivs',*CAPS)):raise ValueError('child status missing')
 if values['Pid']!=str(pid) or values['Uid'].split()!=['10001']*4 or values['NoNewPrivs']!='1':raise ValueError('child privilege identity')
 if any(re.fullmatch('[0-9a-fA-F]{16}',values[k])is None or int(values[k],16)!=0 for k in CAPS):raise ValueError('child capabilities retained')
 return {'pid':pid,'uid':[10001]*4,'NoNewPrivs':1,**{k:0 for k in CAPS}}
def owned_status(child):
 if child is None or type(child.pid)is not int or child.pid<=0 or child.poll()is not None:raise ValueError('owned child not live')
 pid=child.pid;fd=os.open('/proc/'+str(pid)+'/status',os.O_RDONLY|os.O_NOFOLLOW)
 try:raw=os.read(fd,16385)
 finally:os.close(fd)
 proof=parse_status(raw,pid)
 if child.pid!=pid or child.poll()is not None:raise ValueError('owned child changed')
 return proof
