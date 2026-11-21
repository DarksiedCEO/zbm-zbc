import sys,unittest,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy/gate5'))
import provisioning_session as s
import credential_plan as c
from test_credential_plan import fixture,secret

def identities():return {'project_id':'synthetic-project','project_name':'zbm-zbc','environment_id':'synthetic-environment','environment_name':'production','services':{name:'synthetic-'+name for name in {k[0] for k in c.REQUIRED}}}
class SessionTests(unittest.TestCase):
 def test_refuses_unsupported_adapter_before_read(self):
  with self.assertRaises(s.Refused):s.run(object(),None)
 def test_complete_names_only_direct_copies_and_preserved_maps(self):
  a=s.MemoryAdapter(identities(),fixture());out=s.run(a,identities())
  self.assertEqual(out['state'],'SIMULATED_COMPLETE_NOT_APPLIED')
  for role,dests in c.DIRECT.items():
   for dest in dests:self.assertEqual(a._inventory[role],a._inventory[dest])
  for role in c.ROLES:self.assertNotIn(fixture()[role]._value,repr(out))
  self.assertFalse(c.plan(a._inventory)._deltas)
 def test_identity_custody_and_reuse_fail_before_writes(self):
  for mutate in (lambda a:a._identity.update(environment_name='staging'),lambda a:a._inventory.update({('legal','LEGAL_LINK_TAG_KEY'):c.Cell('present','sealed')}),lambda a:a._inventory.update({('client-portal','PORTAL_EVIDENCE_KEY'):a._inventory[('legal','LEGAL_LINK_TAG_KEY')]})):
   a=s.MemoryAdapter(identities(),fixture());mutate(a)
   with self.assertRaises((s.Refused,c.Refused)):s.run(a,identities())
   self.assertFalse(a.writes)
 def test_generation_all_validated_before_first_mutation(self):
  i=fixture()
  for role in c.GENERATED:i[role]=c.Cell.absent()
  g=s.SyntheticGenerator({role:secret('new-'+'.'.join(role)) for role in c.GENERATED});a=s.MemoryAdapter(identities(),i)
  s.run(a,identities(),g);self.assertEqual(set(g.calls),c.GENERATED);self.assertEqual(len(g.calls),12)
  self.assertEqual(set(a.writes[:12]),c.GENERATED)
 def test_bad_generated_collision_stops_before_any_write(self):
  i=fixture();role=('client-portal','PORTAL_EVIDENCE_KEY');i[role]=c.Cell.absent();a=s.MemoryAdapter(identities(),i);g=s.SyntheticGenerator({role:i[('legal','LEGAL_LINK_TAG_KEY')]._value})
  with self.assertRaises(c.Refused):s.run(a,identities(),g)
  self.assertFalse(a.writes)
 def test_mid_write_unknown_reentry_adopts_and_never_regenerates(self):
  i=fixture();role=('legal','LEGAL_LINK_TAG_KEY');i[role]=c.Cell.absent();v=secret('new-link');g=s.SyntheticGenerator({role:v});a=s.MemoryAdapter(identities(),i,interrupt_after=1)
  with self.assertRaisesRegex(s.Refused,'UNKNOWN'):s.run(a,identities(),g)
  self.assertEqual(a._inventory[role]._value,v);a.interrupt_after=None
  unused=s.SyntheticGenerator({});s.run(a,identities(),unused);self.assertFalse(unused.calls)
  self.assertEqual(a._inventory[('client-portal','PORTAL_LINK_TAG_KEY')]._value,v)
 def test_drift_at_compare_write_stops_no_more_mutations(self):
  a=s.MemoryAdapter(identities(),fixture(),drift_before=2)
  with self.assertRaises(s.Refused):s.run(a,identities())
  self.assertEqual(len(a.writes),2)
 def test_successful_reentry_is_noop(self):
  a=s.MemoryAdapter(identities(),fixture());s.run(a,identities());n=len(a.writes)
  self.assertEqual(s.run(a,identities())['names'],());self.assertEqual(len(a.writes),n)
 def test_absence_metadata_not_permission_to_generate_existing_group(self):
  i=fixture();role=('legal','LEGAL_LINK_TAG_KEY');v=i[role]._value;i[role]=c.Cell.absent();i[('client-portal','PORTAL_LINK_TAG_KEY')]=c.Cell.supplied(v)
  a=s.MemoryAdapter(identities(),i);g=s.SyntheticGenerator({});s.run(a,identities(),g);self.assertFalse(g.calls);self.assertEqual(a._inventory[role]._value,v)
if __name__=='__main__':unittest.main()
