import sys,unittest,hashlib,json,pickle
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy/gate5'))
import credential_plan as c

def secret(label):return hashlib.sha256(('synthetic-only-'+label).encode()).hexdigest()
def fixture():
 inv={k:c.Cell.absent() for k in c.REQUIRED}
 for role in c.ROLES:inv[role]=c.Cell.supplied(secret('.'.join(role)))
 maps={k:{} for k in c.CALLERS}
 for key,bindings in c.CALLERS.items():
  for name,role in bindings.items():
   if role not in c.GENERATED:maps[key][name]=inv[role]._value
 maps[('finance','FIN_CALLER_TOKENS')]['retained_custom']=secret('extra-finance')
 maps[('seo','SEO_CALLER_TOKENS')]['retained_dashboard']=secret('extra-seo')
 for k,m in maps.items():inv[k]=c.Cell.supplied(json.dumps(m))
 ledger={}
 for role,args in c.LEDGER.items():
  if role[0]!='compliance':ledger[hashlib.sha256(inv[role]._value.encode()).hexdigest()]=c.grant(*args)
 inv[c.LEDGER_MAP]=c.Cell.supplied(json.dumps(ledger));return inv
class Planner(unittest.TestCase):
 def test_unknown_is_not_absent(self):
  with self.assertRaises(c.Refused):c.plan({})
  for cell in (c.Cell('unknown','unknown'),c.Cell('present','sealed'),c.Cell('absent','provided','oops')):
   i=fixture();i[('legal','LEGAL_LINK_TAG_KEY')]=cell
   with self.assertRaises(c.Refused):c.plan(i)
 def test_all_copy_groups_and_no_extra_trigger_credentials(self):
  p=c.plan(fixture());d=p._deltas
  for name,count in [('BACKUP_COORD_TOKEN',7),('BACKUP_B2_KEY_ID',5),('BACKUP_B2_APPLICATION_KEY',5),('BACKUP_RESTIC_PASSWORD',5)]:
   self.assertEqual(sum(k[1]==name for k in d),count) # source already exists: total8/6
  self.assertFalse(any(k[0]=='backup-trigger' and k[1]!='BACKUP_COORD_TOKEN' for k in d))
  i=fixture();i[('backup-trigger','UNEXPECTED_SECRET')]=c.Cell.supplied(secret('bad'))
  with self.assertRaises(c.Refused):c.plan(i)
 def test_missing_backup_source_adopts_existing_owner_copy(self):
  i=fixture();role=('coordinator','BACKUP_B2_KEY_ID');v=i[role]._value;i[role]=c.Cell.absent();i[('ledger',role[1])]=c.Cell.supplied(v)
  self.assertEqual(c.plan(i)._deltas[role]._value,v)
 def test_matching_map_adopted_not_generated(self):
  i=fixture();role=('legal','LEGAL_COMPLIANCE_CALLER_TOKEN');v=i[role]._value;i[role]=c.Cell.absent()
  i[('compliance','COMPLIANCE_CALLER_TOKENS')]=c.Cell.supplied(json.dumps({'legal_37':v}))
  p=c.plan(i);self.assertFalse(p._generation);self.assertEqual(p._deltas[role]._value,v)
 def test_conflicting_direct_or_map_copy_refuses(self):
  i=fixture();i[('finance','FIN_LEGAL_TOKEN')]=c.Cell.supplied(secret('different'))
  with self.assertRaises(c.Refused):c.plan(i)
  i=fixture();i[('compliance','COMPLIANCE_CALLER_TOKENS')]=c.Cell.supplied(json.dumps({'finance_31':secret('wrong')}))
  with self.assertRaises(c.Refused):c.plan(i)
 def test_logical_reuse_and_extra_grant_reuse_refuse(self):
  i=fixture();i[('client-portal','PORTAL_EVIDENCE_KEY')]=i[('legal','LEGAL_LINK_TAG_KEY')]
  with self.assertRaises(c.Refused):c.plan(i)
  i=fixture();i[('seo','SEO_CALLER_TOKENS')]=c.Cell.supplied(json.dumps({'extra':i[('legal','LEGAL_LINK_TAG_KEY')]._value}))
  with self.assertRaises(c.Refused):c.plan(i)
 def test_complete_caller_maps_and_ledger_preserved(self):
  i=fixture();old=json.loads(i[c.LEDGER_MAP]._value);p=c.plan(i)
  new=p._deltas[c.LEDGER_MAP];self.assertEqual({k:new[k] for k in old},old)
  self.assertEqual(len(new),len(old)+1)
  m=p._deltas[('finance','FIN_CALLER_TOKENS')] if ('finance','FIN_CALLER_TOKENS') in p._deltas else {k:c.Secret(v) for k,v in json.loads(i[('finance','FIN_CALLER_TOKENS')]._value).items()}
  self.assertEqual(m['retained_custom']._value,secret('extra-finance'))
  self.assertNotIn(('seo','SEO_CALLER_TOKENS'),p._deltas)
 def test_existing_compliance_digest_must_match(self):
  i=fixture();old=json.loads(i[c.LEDGER_MAP]._value);old['f'*64]=c.grant('compliance-py','compliance');i[c.LEDGER_MAP]=c.Cell.supplied(json.dumps(old))
  with self.assertRaises(c.Refused):c.plan(i)
 def test_duplicate_map_keys_and_bad_ledger_scope_refuse(self):
  i=fixture();i[('seo','SEO_CALLER_TOKENS')]=c.Cell.supplied('{"a":"'+secret('a')+'","a":"'+secret('b')+'"}')
  with self.assertRaises(c.Refused):c.plan(i)
  i=fixture();d=json.loads(i[c.LEDGER_MAP]._value);next(iter(d.values()))['scope']='read';i[c.LEDGER_MAP]=c.Cell.supplied(json.dumps(d))
  with self.assertRaises(c.Refused):c.plan(i)
 def test_known_absence_generation_only_after_full_preflight(self):
  i=fixture();role=('compliance','LEDGER_SERVICE_TOKEN');i[role]=c.Cell.absent();p=c.plan(i)
  self.assertEqual(p._generation,(role,));self.assertEqual(p._deltas,{})
  i[('observer','BACKUP_RESEND_API_KEY')]=c.Cell('present','sealed')
  with self.assertRaises(c.Refused):c.plan(i)
 def test_all_twelve_absent_roles_remain_placeholders(self):
  i=fixture()
  for role in c.GENERATED:i[role]=c.Cell.absent()
  p=c.plan(i);self.assertEqual(set(p._generation),c.GENERATED);self.assertEqual(len(p._generation),12);self.assertEqual(p._deltas,{})
 def test_malformed_grant_caller_refuses_safely(self):
  i=fixture();d=json.loads(i[c.LEDGER_MAP]._value);next(iter(d.values()))['caller']=[];i[c.LEDGER_MAP]=c.Cell.supplied(json.dumps(d))
  with self.assertRaises(c.Refused):c.plan(i)
 def test_existing_grant_without_token_never_generates(self):
  i=fixture();role=('compliance','LEDGER_SERVICE_TOKEN');d=json.loads(i[c.LEDGER_MAP]._value);d[hashlib.sha256(i[role]._value.encode()).hexdigest()]=c.grant(*c.LEDGER[role]);i[c.LEDGER_MAP]=c.Cell.supplied(json.dumps(d));i[role]=c.Cell.absent()
  with self.assertRaises(c.Refused):c.plan(i)
 def test_repr_summary_and_serialization_do_not_expose_values(self):
  i=fixture();p=c.plan(i);display=repr(p)+repr(p.summary())+repr(i[('legal','LEGAL_SERVICE_TOKEN')])+repr(p._deltas)
  for k,v in i.items():
   if k in c.ROLES:self.assertNotIn(v._value,display)
  with self.assertRaises(TypeError):json.dumps(p)
  with self.assertRaises(TypeError):pickle.dumps(p)
  self.assertFalse(any('RR_STORE' in name for name in p.summary()['proposed_names']))
 def test_secret_references_refuse_but_urls_separate(self):
  i=fixture();i[('finance','FIN_LEGAL_TOKEN')]=c.Cell.supplied('${{legal.LEGAL_SERVICE_TOKEN}}')
  with self.assertRaises(c.Refused):c.plan(i)
  internal={('legal','LEGAL_COMPLIANCE_URL'):'compliance',('compliance','COMPLIANCE_LEGAL_URL'):'legal',('finance','FIN_COMPLIANCE_URL'):'compliance',('seo','SEO_COMPLIANCE_URL'):'compliance'}
  for key,value in c.plan(fixture())._urls.items():
   if key[1].endswith('_URL'):
    self.assertEqual(value,'http://${{'+internal.get(key,'coordinator')+'.RAILWAY_PRIVATE_DOMAIN}}:27001')
 def test_gateway_private_name_matches_native_single_label_contract(self):
  value=c.plan(fixture())._urls[('stripe-gateway','STRIPE_GATEWAY_FINANCE_PRIVATE_NAME')]
  self.assertRegex(value,r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
  self.assertEqual(value,'coordinator')
if __name__=='__main__':unittest.main()
