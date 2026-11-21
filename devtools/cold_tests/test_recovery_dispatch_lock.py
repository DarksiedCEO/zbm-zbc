import _runtime_path
import threading,unittest
from types import SimpleNamespace
from recovery_service import Service
from coordinator import Coordinator

class WatchedLock:
 def __init__(self):self.lock=threading.RLock();self.waiting=threading.Event()
 def __enter__(self):self.waiting.set();self.lock.acquire();return self
 def __exit__(self,*args):self.lock.release()
class DispatchLock(unittest.TestCase):
 def test_scheduled_poll_does_not_block_owner_advance_callback(self):
  lock=WatchedLock();callback=threading.Event();errors=[]
  c=SimpleNamespace(control_lock=lock,doc={'run':'cold-'+'a'*32,'phase':'RETIRED'},observer=SimpleNamespace(call=lambda *a:{'monitoring':{'state':'ARMED','expected_due':1793735672},'current':None}))
  def dispatch(path,body):
   if body.get('kind')=='advance-permit':callback.set();return {'permit':'exact-fixture'}
   return Coordinator.catalog_status(c,path,body)
  c.catalog_status=dispatch
  s=object.__new__(Service);s.lock=threading.RLock();s.state='OPEN';s.co=c
  def invoke(body):
   try:s.dispatch('/status',body)
   except BaseException as e:errors.append(e)
  lock.lock.acquire()
  poll=threading.Thread(target=invoke,args=({'kind':'scheduled_backup'},))
  owner=threading.Thread(target=invoke,args=({'kind':'advance-permit'},))
  try:
   poll.start();self.assertTrue(lock.waiting.wait(1));owner.start()
   available=callback.wait(.2)
  finally:lock.lock.release();poll.join(1);owner.join(1)
  self.assertFalse(poll.is_alive());self.assertFalse(owner.is_alive());self.assertEqual(errors,[])
  self.assertTrue(available,'owner advance-permit blocked behind scheduled poll waiting for coordinator lock')
if __name__=='__main__':unittest.main()
