import _runtime_path  # fixed repository-relative packaged runtime
import unittest,copy,threading
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock
from test_typed_cleanup import Tests as Fixture
import typed_cleanup_coordinator as tc
import b2_adapter as b
from repair_state import digest
from owner_wire import Refused
class CoordinatorTests(unittest.TestCase):
 def setUp(self):
  Fixture.setUp(self);self.events=[];run=self.o.sealed['set_id'];boots={m:'a'*32 for m in b.MEMBERS}
  self.c=NS(root=self.o.root,doc={'run':run,'phase':'FAILED','alert':{'stage':'cleanup'},'repair_intent':None,'boots':boots,'seals':self.seals,'catalog_proof':self.proof})
  self.states={m:{'boot':boots[m],'phase':'OPEN','run':run,'child_pid':123,'operations':{run+':'+m+':upload:1':{'phase':'DONE','receipt':{'member':m,'boot':boots[m],'run':run,'action':'upload','result':self.uploads[m]}}}} for m in b.MEMBERS}
  self.c.status=lambda m,*args:copy.deepcopy(self.states[m])
  self.c.update=lambda **kw:self.c.doc.update(copy.deepcopy(kw))
  self.c.catalog=NS(read=lambda r:copy.deepcopy(self.proof),fresh=self.fresh,publish=lambda p:self.events.append('publish'))
  self.c.command=lambda m,a:self.cleanup(m)
 def fresh(self,r):self.events.append('fresh-all-five');return copy.deepcopy(self.proof)
 def expected(self,m):
  return {'schema':'b2-cleanup-v1','deleted':True,'run':self.c.doc['run'],'member':m,'catalog_sha256':self.proof['catalog_sha256'],'catalog_snapshot':self.proof['catalog_snapshot'],'locator_sha256':digest(self.proof['locator'])}
 def cleanup(self,m):self.events.append('delete-'+m);return self.expected(m)
 def prepare(self):
  self.old=copy.deepcopy(self.c.doc);request={'command':'finish-cleanup','run':self.c.doc['run'],'incident_sha256':digest(self.old)}
  tc.preserve(self.c,request);self.c.doc['repair_rpc']={'request':request};return request
 def test_all_five_fresh_before_mixed_cleanup_states(self):
  run=self.c.doc['run'];members=list(b.MEMBERS);done,failed=members[:2]
  self.states[done]['operations'][run+':'+done+':cleanup:1']={'phase':'DONE','receipt':{'member':done,'boot':'a'*32,'run':run,'action':'cleanup','result':self.expected(done)}}
  self.states[failed]['operations'][run+':'+failed+':cleanup:1']={'phase':'FAILED'}
  self.prepare();original=(self.c.root/'typed-cleanup-original.json').read_bytes()
  def repair(c,m,op,cmd):
   self.events.append('repair-'+m);return {'member':m,'boot':'a'*32,'run':run,'action':'cleanup','result':self.expected(m)}
  with patch('repair_cli.owner_resolution',side_effect=repair):got=tc.finish_cleanup(self.c)
  self.assertEqual(got['phase'],'COMPLETE');self.assertEqual(self.events[:2],['fresh-all-five','publish']);self.assertEqual(len(self.c.doc['cleaned']),5)
  self.assertNotIn('delete-'+done,self.events);self.assertEqual((self.c.root/'typed-cleanup-original.json').read_bytes(),original)
  with self.assertRaises(Refused):tc.finish_cleanup(self.c)
 def test_changed_or_missing_remote_prevents_every_cleanup(self):
  self.prepare();self.c.catalog.fresh=Mock(side_effect=Refused('remote missing'))
  with self.assertRaises(Refused):tc.finish_cleanup(self.c)
  self.assertEqual(self.events,[]);self.assertEqual(self.c.doc['repair_intent']['phase'],'UNRESOLVED')
  with self.assertRaises(Refused):tc.finish_cleanup(self.c)
  self.c.catalog.fresh.assert_called_once()
 def test_stale_boot_and_missing_upload_done_refuse_before_intent(self):
  m=list(b.MEMBERS)[0];self.states[m]['boot']='b'*32
  with self.assertRaises(Refused):self.prepare()
  self.states[m]['boot']='a'*32;self.states[m]['operations'].clear()
  with self.assertRaises(Refused):self.prepare()
  self.assertFalse((self.c.root/'typed-cleanup-original.json').exists());self.assertEqual(self.events,[])
 def test_fresh_catalog_uses_remote_recover_not_local_read_only(self):
  from catalog import Catalog
  cat=Catalog('synthetic',{});context=self.o.root/'context.json';context.write_bytes(b.canonical({'seals':self.seals,'uploads':self.uploads}))
  cat.paths=lambda r:(self.o.root,context);cat.read=Mock(return_value=self.proof)
  remote=NS(catalog=self.proof['catalog'],recover=Mock(return_value=self.proof['locator']))
  with patch('catalog.RemoteCatalog',return_value=remote):self.assertEqual(cat.fresh(self.c.doc['run']),self.proof)
  remote.recover.assert_called_once();self.assertEqual(cat.read.call_count,2)
 def test_coordinator_terminal_preserves_incident_no_retirement(self):
  import coordinator_repair
  self.c.control_lock=threading.RLock();self.c.normal_active=False;self.c.repair_worker=None;self.c.retire=Mock(side_effect=AssertionError('no retirement'))
  request={'command':'finish-cleanup','run':self.c.doc['run'],'incident_sha256':digest(self.c.doc)}
  coordinator_repair.submit(self.c,request);self.c.repair_worker.join(2)
  self.assertFalse(self.c.repair_worker.is_alive());self.assertEqual(self.c.doc['repair_rpc']['phase'],'RESOLVED');self.c.retire.assert_not_called()
  coordinator_repair.submit(self.c,request);self.assertEqual(self.events.count('fresh-all-five'),1)

 def test_changed_fresh_catalog_refuses_before_cleanup(self):
  self.prepare();changed=copy.deepcopy(self.proof);changed['catalog_snapshot']='d'*64
  self.c.catalog.fresh=Mock(return_value=changed)
  with self.assertRaises(Refused):tc.finish_cleanup(self.c)
  self.assertEqual(self.events,[]);self.assertEqual(self.c.doc['repair_intent']['phase'],'UNRESOLVED')
