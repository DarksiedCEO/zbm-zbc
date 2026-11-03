import _runtime_path
import unittest,time,hashlib
from types import SimpleNamespace
from owner_wire import canonical
from recovery_owner_boot import RestoreBoot
from b2_catalog import CATBASE
class Tests(unittest.TestCase):
 def fixture(self,fail=False):
  m='finance';cfg={'member':m,'app_env':{},'coord_token':'a'*64,'backup_password':'b'*64,'runtime_inputs':{},'transport_credentials':{'key_id':'id','key':'secret'}};run='cold-'+'c'*32
  loc={'schema':'b2-catalog-locator-v1','set_id':run,'repository':CATBASE+run,'snapshot':'d'*64,'catalog_sha256':'e'*64,'members_sha256':'f'*64,'seals_sha256':'1'*64}
  seen=[]
  def restore(*args):
   seen.append('restore')
   if fail:raise OSError('synthetic')
   return {'schema':'member-restore-v1','member':m,'set_id':run,'catalog_snapshot':loc['snapshot'],'catalog_sha256':loc['catalog_sha256'],'manifest_sha256':'2'*64,'application_start':'NOT_PERFORMED'}
  def check(c):seen.append('prerequisites')
  def construct(c):seen.append('owner');return SimpleNamespace(boot='3'*32,dispatch=lambda p,b:{'normal':True})
  boot=RestoreBoot(cfg,restore=restore,construct=construct,prerequisites=check,front=SimpleNamespace(stop=lambda:seen.append('front-stop')))
  op='restore-'+hashlib.sha256(canonical({'member':m,'locator':loc})).hexdigest();base={'member':m,'boot':boot.boot,'action':'recovery-restore','operation':op}
  return boot,base,loc,seen
 def test_restore_before_owner_and_no_second_effect(self):
  b,base,loc,seen=self.fixture();b.dispatch('/submit',dict(base,locator=loc));b.worker.join(1)
  self.assertEqual(seen,['restore','prerequisites','front-stop','owner']);self.assertEqual(b.dispatch('/status',base)['state'],'DONE')
  with self.assertRaises(Exception):b.dispatch('/submit',dict(base,locator=loc))
  self.assertEqual(seen.count('restore'),1)
 def test_unknown_never_constructs_owner_or_retries(self):
  b,base,loc,seen=self.fixture(True);b.dispatch('/submit',dict(base,locator=loc));b.worker.join(1)
  self.assertEqual(b.dispatch('/status',base)['state'],'UNKNOWN');self.assertEqual(seen,['restore'])
  with self.assertRaises(Exception):b.dispatch('/submit',dict(base,locator=loc))
 def test_boot_and_locator_refusal_before_effect(self):
  b,base,loc,seen=self.fixture()
  with self.assertRaises(Exception):b.dispatch('/submit',dict(base,boot='wrong',locator=loc))
  with self.assertRaises(Exception):b.dispatch('/submit',dict(base,locator=dict(loc,snapshot='5'*64)))
  self.assertEqual(seen,[])

class GroupTests(unittest.TestCase):
 def test_all5_same_selection_and_lost_ack_status_only(self):
  from recovery_controller import restore_group
  from owner_wire import MEMBERS,Refused
  _,_,loc,_=Tests().fixture();seen=[]
  class Client:
   def __init__(self,m):self.m=m;self.op=None
   def call(self,path,b):
    if b['boot']=='discover':return {'member':self.m,'boot':'a'*32,'phase':'RESTORE_WAIT'}
    if path=='/submit':self.op=b['operation'];seen.append(self.m);raise Refused('lost reply')
    return {'state':'DONE','operation':self.op,'receipt':{'schema':'member-restore-v1','member':self.m,'set_id':loc['set_id'],'catalog_snapshot':loc['snapshot'],'catalog_sha256':loc['catalog_sha256'],'manifest_sha256':'b'*64,'application_start':'NOT_PERFORMED'}}
  v={'schema':'fixed-b2-data-restore-v1','password':'ignored by transport','credentials':{},'selection':{'locator':loc,'cutoff_utc':'2026-11-03T00:00:00Z'}}
  r=restore_group(v,'a'*64,client_factory=Client);self.assertEqual(seen,list(MEMBERS));self.assertEqual(set(r['data']['members']),set(MEMBERS));self.assertEqual(r['business_admission'],'NOT_OPENED')
