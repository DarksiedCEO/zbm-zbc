import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(REPO/'deploy/gate5'),str(REPO/'services/compliance-py/src'),str(REPO/'services/compliance-py/tests')]
import compliance_commission as command
from fakes import FakeLedgerClient
from store import DataDirLock, LOCK_NAME, LOG_NAME


class Commission(unittest.TestCase):
    def test_native_seed_clean_close_existing_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();ledger=FakeLedgerClient()
            result=command.commission(root,ledger)
            self.assertEqual(result['state'],'COMMISSIONED_CLOSED')
            self.assertFalse(result['seed_approved']);self.assertFalse(result['listener_started']);self.assertFalse(result['scheduler_started'])
            self.assertEqual([p['existing_store'] for p in result['passes']],[False,True])
            self.assertEqual(result['passes'][0]['proposal_id'],result['passes'][1]['proposal_id'])
            self.assertGreater(result['passes'][1]['log_records'],result['passes'][0]['log_records'])
            self.assertEqual(len(ledger.of_type('seed_loaded')),1)
            self.assertEqual(result['log_sha256'],hashlib.sha256((root/LOG_NAME).read_bytes()).hexdigest())
            self.assertEqual({p.name for p in root.iterdir()},{LOCK_NAME,LOG_NAME})
            lock=DataDirLock(str(root));lock.release()
            self.assertNotIn('api',sys.modules)
            before=(root/LOG_NAME).read_bytes();calls=ledger.calls
            with self.assertRaises(command.Refused):command.commission(root,ledger)
            self.assertEqual(ledger.calls,calls);self.assertEqual((root/LOG_NAME).read_bytes(),before)

    def test_empty_file_is_not_seed_and_existing_store_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();(root/LOG_NAME).write_bytes(b'');ledger=FakeLedgerClient()
            with self.assertRaises(command.Refused):command.commission(root,ledger)
            self.assertEqual(ledger.calls,0);self.assertEqual((root/LOG_NAME).read_bytes(),b'')

    def test_wrong_seed_refuses_before_store_or_ledger_effect(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();ledger=FakeLedgerClient()
            with patch.object(command,'EXPECTED_SEED','0'*64),self.assertRaises(command.Refused):command.commission(root,ledger)
            self.assertEqual(list(root.iterdir()),[]);self.assertEqual(ledger.calls,0)

    def test_ledger_unavailable_never_reports_commissioned(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();ledger=FakeLedgerClient(fail_all=True)
            with self.assertRaises(Exception):command.commission(root,ledger)
            self.assertEqual(ledger.events,[])
            lock=DataDirLock(str(root));lock.release()
            with self.assertRaises(command.Refused):command.commission(root,ledger)

    def test_dry_run_and_missing_ack_have_no_application_side_effect(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            with patch.object(command,'commission') as effect,contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(command.main(['--store',str(root)]),0)
                with self.assertRaises(command.Refused):command.main(['--store',str(root),'--execute'])
                effect.assert_not_called()
            self.assertEqual(json.loads(out.getvalue())['state'],'DRY_RUN');self.assertEqual(list(root.iterdir()),[])

    def test_explicit_legacy_route_only_and_no_supervised_bypass(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            args=['--store',str(root),'--execute','--confirm-empty-store','--confirm-ingress-closed','--ledger-phase','legacy-pre-supervisor']
            env={'LEDGER_SERVICE_URL':'http://ledger.railway.internal:8090','LEDGER_SERVICE_TOKEN':'x'*64,'BACKUP_LAUNCH_ENABLED':'0'}
            with patch.dict(command.os.environ,env,clear=True),patch('ledger.HttpLedgerClient') as client,patch.object(command,'commission',return_value={'state':'fixture'}) as effect,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(command.main(args),0)
                client.assert_called_once_with(env['LEDGER_SERVICE_URL'],env['LEDGER_SERVICE_TOKEN'])
                effect.assert_called_once_with(root,client.return_value)
            for url,enabled in [('http://ledger.railway.internal:27001','0'),('http://ledger.railway.internal:27003','0'),('http://ledger.railway.internal:8090','1')]:
                env.update(LEDGER_SERVICE_URL=url,BACKUP_LAUNCH_ENABLED=enabled)
                with patch.dict(command.os.environ,env,clear=True),patch('ledger.HttpLedgerClient') as client,self.assertRaises(command.Refused):command.main(args)
                client.assert_not_called()
            with patch.object(command,'commission') as effect,self.assertRaises(command.Refused):command.main(args[:-2])
            effect.assert_not_called();self.assertEqual(list(root.iterdir()),[])

    def test_image_layout_dry_run_and_docker_copy_binding(self):
        import importlib.util
        import shutil
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();script=root/'app/deploy/gate5/compliance_commission.py';script.parent.mkdir(parents=True)
            shutil.copyfile(command.__file__,script)
            seed=root/'app/services/compliance-py/seed/compliance_obligations_seed.json';seed.parent.mkdir(parents=True);shutil.copyfile(command.SEED,seed)
            store=root/'store';store.mkdir()
            spec=importlib.util.spec_from_file_location('commission_packaged',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            self.assertEqual(module.SOURCE,root/'app/services/compliance-py/src')
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(module.main(['--store',str(store)]),0)
            self.assertEqual(list(store.iterdir()),[])
        docker=(REPO/'services/compliance-py/Dockerfile').read_text()
        self.assertIn('COPY deploy/gate5/compliance_commission.py /app/deploy/gate5/compliance_commission.py',docker)
        self.assertIn('ENV BACKUP_LAUNCH_ENABLED=0',docker)
        self.assertIn('USER 10001:10001',docker)
        self.assertIn('"-m", "api"]',docker)

    def test_noncanonical_or_symlink_target_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td).resolve();root=parent/'store';root.mkdir();link=parent/'link';link.symlink_to(root)
            with self.assertRaises(command.Refused):command.empty_target(link)
            with self.assertRaises(command.Refused):command.empty_target('relative')
            self.assertEqual(list(root.iterdir()),[])

if __name__=='__main__':unittest.main()
