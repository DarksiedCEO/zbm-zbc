"""Offline maintenance configuration guards; no launcher activation or providers."""
import copy,json,tempfile,unittest
from pathlib import Path
from deploy.docker.maintenance.backup_config import load,child_environment,Refused,MEMBERS,APP_NAMES
TOKEN='a1'*32  # public synthetic fixture, never a usable credential

def data():
    return {'enabled':True,'coordination_token':TOKEN,'members':{m:{r:'/data/'+m+'-'+r for r in ('source','spool','control')} for m in MEMBERS}}
class Tests(unittest.TestCase):
    def test_default_disabled(self): self.assertFalse(load({}).enabled)
    def test_boolean_only(self):
        for v in (1,'1',None):
            with self.assertRaises(Refused): load({'enabled':v})
    def test_dormant_no_inputs(self):
        with self.assertRaises(Refused): load({'coordination_token':TOKEN})
    def test_exact_members(self):
        for name in MEMBERS:
            d=data();del d['members'][name]
            with self.assertRaises(Refused): load(d)
        d=data();d['members']['portal']=d['members']['legal']
        with self.assertRaises(Refused): load(d)
    def test_requires_dedicated_token(self):
        for token in (None,'','short','${{REFERENCE}}'):
            d=data();d['coordination_token']=token
            with self.assertRaises(Refused): load(d)
    def test_valid_and_no_token_repr(self):
        c=load(data());self.assertEqual(len(c.layouts),5);self.assertNotIn(TOKEN,repr(c))
    def test_overlap_all_roles(self):
        for role in ('source','control','spool'):
            d=data();d['members']['legal'][role]='/data/finance-source/nested'
            with self.assertRaises(Refused): load(d)
        d=data();d['members']['legal']['spool']=d['members']['legal']['source']
        with self.assertRaises(Refused): load(d)
    def test_bad_paths(self):
        for value in ('/','relative','/data/../x','/data//x','/data/./x','/data/x/'):
            d=data();d['members']['legal']['source']=value
            with self.assertRaises(Refused): load(d)
    def test_symlink_alias(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve();(root/'real').mkdir();(root/'alias').symlink_to(root/'real')
            d=data();d['members']['legal']['source']=str(root/'alias'/'missing')
            with self.assertRaises(Refused): load(d)
    def test_backup_credentials_never_child(self):
        env={'RESTIC_PASSWORD':'synthetic-restic','B2_ACCOUNT_KEY':'synthetic-b2','BACKUP_COORD_TOKEN':TOKEN,'AWS_SECRET_ACCESS_KEY':'synthetic-aws','RCLONE_CONFIG':'/secret/file','UNRELATED':'x','PATH':'/usr/bin','LEGAL_SERVICE_TOKEN':'synthetic-app-auth','LEDGER_SERVICE_TOKEN':'synthetic-ledger','LEGAL_CALLER_TOKENS':'{}'}
        for cfg in (load({}),load(data())):
            out=child_environment('legal',env,cfg)
            self.assertEqual(set(out),{'PATH','LEGAL_SERVICE_TOKEN','LEDGER_SERVICE_TOKEN','LEGAL_CALLER_TOKENS'})
    def test_alias_refuses_not_silent_auth_drop(self):
        with self.assertRaises(Refused): child_environment('legal',{'LEGAL_SERVICE_TOKEN':TOKEN},load(data()))
    def test_all_inventory_settings_retained(self):
        for member,names in APP_NAMES.items():
            env={k:'synthetic-value' for k in names}
            self.assertEqual(child_environment(member,env,load({})),env)
    def test_unknown_app_setting_refuses(self):
        with self.assertRaises(Refused): child_environment('finance',{'FIN_FUTURE_AUTH':'x'},load({}))
    def test_materialized_auth_kept(self):
        env={'FIN_STRIPE_SECRET_KEY_FILE':'/run/zbm-secrets/stripe','FIN_RECEIPT_RESEND_KEY_FILE':'/run/zbm-secrets/mail','FIN_SERVICE_TOKEN':'synthetic-app'}
        self.assertEqual(child_environment('finance',env,load({})),env)
        with self.assertRaises(Refused): child_environment('finance',{'FIN_STRIPE_SECRET_KEY':'synthetic-raw'},load({}))
    def test_dynamic_tax_settings_retained(self):
        env={'FIN_1099NEC_THRESHOLD_2029':'2000.00'}
        self.assertEqual(child_environment('finance',env,load({})),env)
    def test_actual_finance_thin_client_auth(self):
        env={prefix+'_'+suffix:'synthetic-value' for prefix in ('FIN_LEGAL','FIN_COMPLIANCE','FIN_VI') for suffix in ('URL','TOKEN','CALLER_TOKEN')}
        self.assertEqual(child_environment('finance',env,load({})),env)
    def test_symlink_loop_and_safe_absent_suffix(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve();(root/'loop').symlink_to(root/'loop')
            for suffix in ('loop','loop/child'):
                d=data();d['members']['finance']['source']=str(root/suffix)
                with self.assertRaises(Refused):load(d)
            d=data();d['members']['finance']['source']=str(root/'absent'/'future')
            self.assertTrue(load(d).enabled)
    def test_unknown_fields(self):
        d=data();d['command']='arbitrary'
        with self.assertRaises(Refused):load(d)
        d=data();d['members']['legal']['target']='arbitrary'
        with self.assertRaises(Refused):load(d)
class Packaging(unittest.TestCase):
    def test_exact_five_packages_remain_disabled(self):
        root=Path(__file__).resolve().parents[1]
        for service in ('ledger-rust','finance-py','legal-py','compliance-py','seo-py'):
            text=(root/'services'/service/'Dockerfile').read_text()
            with self.subTest(service=service):
                self.assertIn('ENV BACKUP_LAUNCH_ENABLED=0',text)
                self.assertNotIn('BACKUP_COORD_TOKEN=',text)
                self.assertIn('USER 10001:10001',text)
                for name in ('launcher.py','backup_config.py','app_env_names.json'):
                    self.assertIn('deploy/docker/maintenance/'+name,text)
        for name in ('launcher.py','backup_config.py','app_env_names.json'):
            self.assertTrue((root/'deploy/docker/maintenance'/name).is_file())

if __name__=='__main__': unittest.main(verbosity=2)
