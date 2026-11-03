import _runtime_path  # fixed repository-relative packaged runtime
import copy,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from owner_wire import MEMBERS,Refused,decode
import coordinator_rearm as r
class Front:
 def __init__(self):self.opened=True
 def close(self):self.opened=False
 def open(self):self.opened=True
class Client:
 def __init__(self,m,c):self.m,self.c=m,c
 def call(self,path,body):
  if path=='/rearm-close':return {}
  return {'member':self.m,'boot':self.c.new[self.m],'run':None,'last_success':self.c.doc['last_success'],'phase':'OPEN','fenced':False,'child_pid':10}
class Cases(unittest.TestCase):
 def make(self,p):
  old=dict.fromkeys(MEMBERS,'a'*32);new=dict(old,ledger='b'*32)
  c=SimpleNamespace(root=Path(p),front=Front(),new=new)
  c.doc={'phase':'OPEN','run':'cold-'+'c'*32,'boots':old,'last_success':{'run':'cold-'+'d'*32,'catalog_sha256':'e'*64,'catalog_snapshot':'f'*64},'receipts':{},'seals':{},'barrier':None}
  c.observer=SimpleNamespace(call=lambda *a:{'monitoring':{'state':'ARMED','expected_due':200},'current':{'phase':'COMPLETE','run':c.doc['last_success']['run'],'due':100,'completion':c.doc['last_success']}})
  c.clients={m:Client(m,c) for m in MEMBERS};c.update=lambda **kw:c.doc.update(kw)
  return c
 def test_lock_before_bind_and_all_verify_before_publish(self):
  with tempfile.TemporaryDirectory() as d:
   c=self.make(d);events=[]
   def action(c,m,a,v):
    self.assertFalse(c.front.opened)
    disk=r.load(c)
    if a=='bind':self.assertEqual(set(disk['locks']),set(MEMBERS))
    if a=='publish':self.assertEqual(set(disk['verified']),set(MEMBERS))
    events.append((m,a));return {'member':m,'stage':a}
   with patch.object(r,'call',side_effect=action):result=r.run(c)
   self.assertEqual(result['phase'],'DONE');self.assertTrue(c.front.opened)
   self.assertEqual([m for m,a in events if a=='lock'],['seo','finance','compliance','legal','ledger'])
   with self.assertRaises(Refused):r.run(c)
 def test_failure_never_opens_and_preserves_intent(self):
  with tempfile.TemporaryDirectory() as d:
   c=self.make(d)
   def action(c,m,a,v):
    if a=='start':raise Refused('test start failed')
    return {'member':m,'stage':a}
   with patch.object(r,'call',side_effect=action):
    with self.assertRaises(Refused):r.run(c)
   self.assertFalse(c.front.opened);self.assertEqual(r.load(c)['phase'],'UNKNOWN')
   with self.assertRaises(Refused):r.run(c)
if __name__=='__main__':unittest.main()
