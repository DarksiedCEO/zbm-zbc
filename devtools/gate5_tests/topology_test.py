import importlib.util,json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('topology_validator',ROOT/'deploy/gate5/topology_validator.py');v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
class Topology(unittest.TestCase):
 def test_default_plan_is_not_ready(self):
  plan=json.loads((ROOT/'deploy/gate5/topology_plan.json').read_text())
  self.assertTrue(v.validate(plan));self.assertFalse(plan['executable'])
 def complete(self):
  p=v.template();p['source_sha']='a'*40;p['external_inventory_complete']=True
  p['schedule'].update(cron_utc='0 11 * * *',first_due_utc_seconds=1794049200)
  # Compute an aligned synthetic day, not a rollout date.
  p['schedule']['first_due_utc_seconds']=20000*86400+11*3600
  for i,(role,row) in enumerate(p['roles'].items(),1):
   row['image_digest']='sha256:'+'b'*64
   if role!='backup-trigger':row['volume_id']='11111111-1111-4111-8111-'+str(i).zfill(12)
   row['resources']={'memory_bytes':268435456,'vcpus':1,'pid_limit':128}
  p['capture_limits']={m:{'source':524288,'stage':1572864,'repository':3145728,'control_reserve':1048576} for m in v.MEMBERS}
  self.bind(p);return p
 def bind(self,p):
  proof={'source_sha':p['source_sha'],'artifact_sha256':'c'*64,'inputs_sha256':v.inputs_digest(p)}
  p['evidence']={k:dict(proof) for k in p['evidence']};p['external_inventory_evidence']=dict(proof)
 def test_complete_synthetic_input_is_only_a_plan(self):
  p=self.complete();self.assertEqual(v.validate(p),[]);self.assertFalse(p['executable'])
 def test_missing_evidence_and_identities_refuse(self):
  for mutate in (lambda p:p.update(source_sha=None),lambda p:p['roles']['ledger'].update(image_digest=None),lambda p:p['roles']['ledger'].update(volume_id=None),lambda p:p['roles']['ledger'].update(resources=None),lambda p:p.update(capture_limits=None),lambda p:p['evidence'].update(compliance_initialized=None),lambda p:p['evidence'].update(dns_connectivity=None),lambda p:p.update(external_inventory_complete=False)):
   p=self.complete();mutate(p);self.assertTrue(v.validate(p))
 def test_no_routing_or_mount_bypass_even_with_rebound_evidence(self):
  for mutate in (lambda p:p['roles']['ledger']['contract'].update(source='/volume/ledger'),lambda p:p['routing']['client-portal'].update(PORTAL_LEGAL_URL='http://legal.railway.internal:27001'),lambda p:p['coordination'].update(secret_references=True),lambda p:p.update(executable=True),lambda p:p.update(stripe_private_name='finance')):
   p=self.complete();mutate(p);self.bind(p);self.assertTrue(v.validate(p))
 def test_modified_resources_invalidate_approval_binding(self):
  p=self.complete();p['roles']['ledger']['resources']['memory_bytes']*=2
  self.assertIn('EVIDENCE_RESOURCES_APPROVAL',v.validate(p))
 def test_no_shared_volume_or_provider_secrets(self):
  p=self.complete();p['roles']['seo']['volume_id']=p['roles']['ledger']['volume_id'];self.bind(p);self.assertIn('DISTINCT_VOLUMES',v.validate(p))
  p=self.complete();p['secret']='forbidden';self.assertEqual(v.validate(p),['PLAN_SCHEMA'])
 def test_schedule_mismatch_and_nonfinite_refuse(self):
  p=self.complete();p['schedule']['first_due_utc_seconds']+=1;self.bind(p);self.assertIn('SCHEDULE_ALIGNMENT',v.validate(p))
  p=self.complete();p['roles']['ledger']['resources']['vcpus']=float('nan');self.assertTrue(v.validate(p))
 def test_capacity_limits_must_match_runtime_even_with_rebound_evidence(self):
  for field,value in [('stage',1),('repository',1),('control_reserve',1),('source',9*1048576),('source',True)]:
   p=self.complete();p['capture_limits']['ledger'][field]=value;self.bind(p)
   self.assertIn('RUNTIME_CAPACITY_POLICY',v.validate(p))
 def test_data_only_larger_capacity_is_not_rollout_qualified(self):
  p=self.complete();p['capture_limits']['finance']={'source':1048576,'stage':2097152,'repository':4194304,'control_reserve':1048576}
  self.bind(p);self.assertIn('RUNTIME_CAPACITY_POLICY',v.validate(p))
 def test_capacity_policy_is_pure_and_canonical(self):
  p=self.complete();raw,digest=v.capacity_settings(p['capture_limits'])
  self.assertEqual(len(digest),64);self.assertEqual(json.loads(raw)['sources']['ledger'],524288)
  self.assertEqual((raw,digest),v.capacity_settings(dict(reversed(list(p['capture_limits'].items())))))
if __name__=='__main__':unittest.main()
