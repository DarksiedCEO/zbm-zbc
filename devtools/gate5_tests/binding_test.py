import copy,importlib.util,json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def module(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
b=module('binding',ROOT/'deploy/gate5/binding_proposal.py')
fixture=module('topology_tests',Path(__file__).with_name('topology_test.py'))
class Binding(unittest.TestCase):
 def setUp(self):
  self.plan=fixture.Topology().complete()
  names=b.EXISTING+('client-portal','stripe-gateway','orchestrator','detection')
  self.context={'project_id':'22222222-2222-4222-8222-222222222222','environment_id':'33333333-3333-4333-8333-333333333333','existing_services':{n:'44444444-4444-4444-8444-'+str(i).zfill(12) for i,n in enumerate(names,1)},'existing_volumes':{n:{'id':self.plan['roles'][n]['volume_id'],'mount':'/data','source':self.plan['roles'][n]['contract']['source']} for n in b.EXISTING},'production_limits_binding':None,'compliance_initialization_binding':None}
 def test_disabled_and_unresolved_even_for_complete_topology(self):
  p=b.propose(self.plan,self.context);self.assertEqual(p['status'],'HOLD');self.assertFalse(p['executable']);self.assertFalse(p['schedule_proposal']['enabled'])
  self.assertIn('RUNTIME_PRODUCTION_LIMITS_UNRESOLVED',p['blockers']);self.assertIn('COMPLIANCE_INITIALIZATION_UNRESOLVED',p['blockers'])
  for r in p['roles'].values():self.assertEqual(r['disabled_settings']['BACKUP_LAUNCH_ENABLED'],'0')
 def test_default_template_refuses(self):
  with self.assertRaisesRegex(b.Refused,'TOPOLOGY_INPUTS_BLOCKED'):b.propose(b.t.template(),self.context)
 def test_volume_identity_and_mount_preserved(self):
  for key,value in [('id','55555555-5555-4555-8555-555555555555'),('mount','/volume'),('source','/data/new')]:
   c=copy.deepcopy(self.context);c['existing_volumes']['ledger'][key]=value
   with self.assertRaisesRegex(b.Refused,'EXISTING_VOLUME_CHANGE_REFUSED'):b.propose(self.plan,c)
 def test_routes_and_digest_are_deterministic(self):
  p=b.propose(self.plan,self.context);self.assertEqual(p,b.propose(self.plan,self.context))
  self.assertTrue(all(r['proposed_url']=='http://coordinator.railway.internal:27001' for r in p['central_route_proposals']))
  self.assertEqual(p['roles']['ledger']['approved_image_digest'],self.plan['roles']['ledger']['image_digest'])
 def test_approval_bindings_never_enable_apply(self):
  c=copy.deepcopy(self.context)
  for key,kind in [('production_limits_binding','runtime-production-limits-implemented'),('compliance_initialization_binding','compliance-existing-store-initialized')]:c[key]={'kind':kind,'source_sha':self.plan['source_sha'],'inputs_sha256':b.t.inputs_digest(self.plan),'artifact_sha256':'d'*64}
  p=b.propose(self.plan,c);self.assertEqual(len(p['blockers']),3);self.assertFalse(p['executable'])
 def test_diff_refuses_identity_or_mount_changes(self):
  p=b.propose(self.plan,self.context);q=copy.deepcopy(p);q['roles']['ledger']['volume']['mount']='/volume'
  with self.assertRaises(b.Refused):b.diff(p,q)
  q=copy.deepcopy(p);q['roles']['ledger']['approved_image_digest']='sha256:'+'e'*64
  self.assertIn('roles',b.diff(p,q)['changed_sections']);self.assertFalse(b.diff(p,q)['apply'])
 def test_secret_or_unknown_context_field_refused(self):
  c=dict(self.context,secret='synthetic-secret')
  with self.assertRaisesRegex(b.Refused,'CONTEXT_SCHEMA'):b.propose(self.plan,c)
 def test_seven_observed_services_leave_portal_unprovisioned(self):
  c=copy.deepcopy(self.context);del c['existing_services']['client-portal']
  p=b.propose(self.plan,c)
  routes=[r for r in p['central_route_proposals'] if r['client']=='client-portal']
  self.assertTrue(routes)
  for r in routes:self.assertIsNone(r['service_id']);self.assertEqual(r['identity_mode'],'unprovisioned-proposal-only')
  self.assertIn('CLIENT_PORTAL_NOT_PROVISIONED',p['blockers']);self.assertFalse(p['executable'])
 def test_unknown_or_duplicate_service_inventory_refused(self):
  for mutation in ('unknown','duplicate','missing'):
   c=copy.deepcopy(self.context)
   if mutation=='unknown':c['existing_services']['unapproved-service']='99999999-9999-4999-8999-999999999999'
   elif mutation=='duplicate':c['existing_services']['detection']=c['existing_services']['ledger']
   else:del c['existing_services']['detection']
   with self.assertRaises(b.Refused):b.propose(self.plan,c)
 def test_policy_distributed_to_data_roles_only(self):
  p=b.propose(self.plan,self.context);raw,sha=b.t.capacity_settings(self.plan['capture_limits'])
  self.assertEqual(p['capacity_policy_sha256'],sha)
  for name,row in p['roles'].items():
   self.assertEqual(row['disabled_settings'].get('BACKUP_CAPACITY_POLICY'),raw if name in b.t.MEMBERS+('coordinator',) else None)
if __name__=='__main__':unittest.main()
