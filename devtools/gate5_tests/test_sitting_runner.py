"""Offline tests for freeze_plan.py and sitting_runner.py. Synthetic data only."""
import contextlib, copy, datetime as dt, io, json, os, sys, tempfile, unittest, uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy/gate5'))
import freeze_plan as fp
import sitting_runner as sr

def synthetic_baseline(captured=None):
    names = fp.EXISTING
    services, volumes = [], []
    for n in names:
        sid = str(uuid.uuid4())
        vol = []
        if n in fp.OWNERS:
            vid = str(uuid.uuid4())
            volumes.append({'id': vid, 'name': n + '-data', 'state': 'live', 'region': 'us-west2', 'sizeMB': 5000,
                            'mountedOn': {'serviceId': sid, 'serviceName': n, 'mountPath': '/data'}, 'stagedChangeCount': 0})
            vol = [{'id': vid, 'mountPath': '/data'}]
        services.append({'service': {'id': sid, 'name': n, 'state': 'live'}, 'environment': {}, 'staged': False,
                         'config': {'source': {'repo': 'example/repo', 'branch': 'main'}, 'build': {}, 'deploy': {},
                                    'volumeMounts': {v['id']: {'mountPath': '/data'} for v in vol}},
                         'stagedChanges': [], 'variableNames': ['PORT'], 'volumes': vol,
                         'domains': {'serviceDomains': [], 'customDomains': []}, 'tcpProxies': [], 'latestDeployment': {}, 'tracing': {}})
    return {'schema': 1, 'captured_at': captured or dt.datetime.now(dt.timezone.utc).isoformat(), 'authority': 'synthetic',
            'environment': {'project': {'id': str(uuid.uuid4()), 'name': 'zbm-zbc', 'workspaceName': 'w'},
                            'environment': {'id': str(uuid.uuid4()), 'name': 'production', 'isEphemeral': False},
                            'staged': False, 'region': 'us-west2', 'services': [], 'volumes': volumes, 'buckets': [], 'sharedVariableNames': []},
            'services': services, 'limitations': []}

class Freeze(unittest.TestCase):
    def test_fills_existing_ids_only(self):
        b = synthetic_baseline(); plan = fp.freeze(json.dumps(b))
        text = json.dumps(plan['phases'])
        for s in b['services']:
            self.assertNotIn(f"<service-id:{s['service']['name']}>", text)
        self.assertEqual(set(plan['remaining_slots']), {f'<service-id:{n}>' for n in fp.NEW} | {f'<volume-id:{n}>' for n in ('compliance', 'coordinator', 'observer')}
                         | {'<approved-first-due-unix-seconds>', '<approved-public-portal-digest>', '<Railway-returned-portal-domain>'})
        self.assertTrue(all(p['schedule_observation'] == 'UNKNOWN_NOT_EMPTY' for p in plan['preserve'].values()))
        self.assertTrue(plan['open_decisions'])

    def test_refusals(self):
        cases = []
        b = synthetic_baseline(); b['services'][0]['stagedChanges'] = [{'x': 1}]; cases.append(b)
        b = synthetic_baseline(); b['environment']['volumes'][0]['region'] = 'us-east4'; cases.append(b)
        b = synthetic_baseline(); b['environment']['project']['name'] = 'zbestmedia-backend'; cases.append(b)
        b = synthetic_baseline(); b['services'].pop(); cases.append(b)
        for b in cases:
            with self.assertRaises(fp.Refused):
                fp.freeze(json.dumps(b))

    def test_output_must_be_private_and_new(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 'b.json'; base.write_text(json.dumps(synthetic_baseline()))
            with self.assertRaises(fp.Refused):
                fp.main([str(base), str(Path(d) / 'out.json')])

class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.patches = [patch.object(sr, 'PRIVATE_ROOT', root), patch.object(sr, 'confirm', lambda phrase: None)]
        for p in self.patches: p.start()
        self.run = root / 'run'; self.run.mkdir(mode=0o700)
        self.baseline = synthetic_baseline()
        self.plan = root / 'plan.json'
        self.plan.write_text(json.dumps(fp.freeze(json.dumps(self.baseline))))
        self.out = io.StringIO()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def call(self, *argv):
        with contextlib.redirect_stdout(self.out):
            return sr.main(list(argv))

    def evidence(self, obj):
        f = Path(self.tmp.name) / ('ev-%s.json' % uuid.uuid4().hex[:8]); f.write_text(json.dumps(obj)); return str(f)

    def test_default_is_dormant(self):
        with patch.object(sr.subprocess, 'run', side_effect=AssertionError('subprocess')):
            self.assertEqual(sr.main([]), 0)

    def test_run_dir_must_be_private_0700(self):
        os.chmod(self.run, 0o755)
        with self.assertRaises(sr.Refused): self.call('status', '--run', str(self.run))

    def test_open_and_recommended_decisions_block_begin(self):
        f = Path(self.tmp.name) / 'decisions.json'
        for status in ('OPEN', 'RECOMMENDED'):
            f.write_text(json.dumps({'decisions': [{'id': 'DX', 'status': status, 'blocks': ['baseline']}]}))
            with patch.object(sr, 'DECISIONS', f), self.assertRaisesRegex(sr.Refused, 'OPEN_DECISIONS:DX'):
                self.call('begin', 'baseline', '--run', str(self.run), '--plan', str(self.plan))
        self.assertIsNone(sr.record(self.run, 'baseline'))

    def test_deferred_blocks_only_its_steps(self):
        f = Path(self.tmp.name) / 'decisions.json'
        f.write_text(json.dumps({'decisions': [{'id': 'DX', 'status': 'DEFERRED', 'blocks': ['admission']}]}))
        with patch.object(sr, 'DECISIONS', f):
            self.assertEqual(sr.open_blockers('decisions'), [])
            self.assertEqual(sr.open_blockers('admission'), ['DX'])

    def test_live_decisions_state(self):
        self.assertEqual(sr.open_blockers('decisions'), [])
        self.assertEqual(sr.open_blockers('owner-activation:ledger'), ['D10-compliance-seed'])
        self.assertEqual(sr.open_blockers('admission'), ['D14-audit-price'])
        self.assertEqual(sr.open_blockers('secret-provision'), [])

    def test_commission_refuses_deployment_status_as_proof(self):
        ev = {'state': 'COMMISSIONED_CLOSED', 'seed_sha256': sr.SEED_SHA256, 'seed_approved': False,
              'listener_started': False, 'scheduler_started': False, 'deployment_status': 'SUCCESS'}
        with self.assertRaisesRegex(sr.Refused, 'NOT_PROOF'):
            sr.verify('commission', ev, self.run, json.loads(self.plan.read_text()))

    def test_once_only_evidence_required_and_stop_on_fail(self):
        with patch.object(sr, 'open_blockers', lambda name: []):
            self.call('begin', 'baseline', '--run', str(self.run), '--plan', str(self.plan))
            self.assertEqual(sr.record(self.run, 'baseline')['status'], 'UNKNOWN')
            with self.assertRaisesRegex(sr.Refused, 'NO_RETRY'):
                self.call('begin', 'baseline', '--run', str(self.run), '--plan', str(self.plan))
            with self.assertRaisesRegex(sr.Refused, 'EVIDENCE_REQUIRED'):
                self.call('close', 'baseline', '--run', str(self.run), '--plan', str(self.plan), '--result', 'PASS')
            leaky = self.evidence({'token': 'a' * 64})
            with self.assertRaisesRegex(sr.Refused, 'SECRET'):
                self.call('close', 'baseline', '--run', str(self.run), '--plan', str(self.plan), '--result', 'PASS', '--evidence', leaky)
            self.assertEqual(self.call('close', 'baseline', '--run', str(self.run), '--plan', str(self.plan), '--result', 'FAIL'), 2)
            self.assertTrue((self.run / 'STOP').exists())
            with self.assertRaisesRegex(sr.Refused, 'RUN_STOPPED'):
                self.call('begin', 'capacity', '--run', str(self.run), '--plan', str(self.plan))

    def test_order_enforced(self):
        with patch.object(sr, 'open_blockers', lambda name: []):
            with self.assertRaisesRegex(sr.Refused, 'PREVIOUS_STEP_NOT_PASS'):
                self.call('begin', 'capacity', '--run', str(self.run), '--plan', str(self.plan))

    def test_capacity_gates_only_admission(self):
        needs = {s['name']: s['needs'] for s in sr.STEPS}
        self.assertEqual(needs['decisions'], ('baseline',))
        self.assertEqual(needs['capacity'], ('portal',))
        self.assertEqual(needs['admission'], ('capacity',))
        self.assertEqual([n for n, v in needs.items() if 'capacity' in v], ['admission'])
        names = [s['name'] for s in sr.STEPS]
        self.assertEqual(names[-2:], ['capacity', 'admission'])

    def test_baseline_pass_prints_no_ids(self):
        with patch.object(sr, 'open_blockers', lambda name: []):
            self.call('begin', 'baseline', '--run', str(self.run), '--plan', str(self.plan))
            self.call('close', 'baseline', '--run', str(self.run), '--plan', str(self.plan), '--result', 'PASS', '--evidence', self.evidence(self.baseline))
        self.assertEqual(sr.record(self.run, 'baseline')['status'], 'PASS')
        ids = [s['service']['id'] for s in self.baseline['services']] + [self.baseline['environment']['project']['id']]
        self.assertFalse(any(i in self.out.getvalue() for i in ids))

    def test_baseline_drift_and_staleness_refuse(self):
        stale = copy.deepcopy(self.baseline); stale['captured_at'] = '2026-01-01T00:00:00+00:00'
        drift = copy.deepcopy(self.baseline); drift['services'][0]['variableNames'].append('NEW_NAME')
        for ev, code in ((stale, 'NOT_FRESH'), (drift, 'BASELINE_DRIFT')):
            with self.subTest(code), self.assertRaisesRegex(sr.Refused, code):
                sr.verify('baseline', ev, self.run, json.loads(self.plan.read_text()))

    def test_writer_step_argv_and_proof(self):
        prior = ['baseline', 'decisions', 'merge', 'new-resources', 'closed-configuration']
        for n in prior:
            sr.write_new(self.run / (n + '.json'), {'step': n, 'status': 'PASS'})
        binding = Path(self.tmp.name) / 'binding.json'; binding.write_text('{}')
        seen = []
        def fake(args, timeout):
            seen.append(args); return type('R', (), {'returncode': 0})()
        with patch.object(sr, 'open_blockers', lambda name: []), patch.object(sr.subprocess, 'run', side_effect=fake):
            with self.assertRaisesRegex(sr.Refused, 'WRITER_NOT_COMPLETE'):
                self.call('begin', 'secret-provision', '--run', str(self.run), '--plan', str(self.plan), '--binding', str(binding))
        self.assertEqual(sr.record(self.run, 'secret-provision')['status'], 'UNKNOWN')  # rc 0 without state is not PASS
        args = seen[0]
        self.assertIn('--apply', args); self.assertEqual(sum(a.startswith('--confirm-') for a in args), 4)
        (self.run / 'writer-state.json').write_text(json.dumps({'status': 'COMPLETE_HOLD'}))
        self.call('close', 'secret-provision', '--run', str(self.run), '--plan', str(self.plan), '--result', 'PASS')
        self.assertEqual(sr.record(self.run, 'secret-provision')['status'], 'PASS')

    def test_evidence_verifiers(self):
        frozen = json.loads(self.plan.read_text()); now = dt.datetime.now(dt.timezone.utc)
        good_commission = {'state': 'COMMISSIONED_CLOSED', 'seed_sha256': sr.SEED_SHA256, 'log_sha256': 'b' * 64,
                           'seed_approved': False, 'listener_started': False, 'scheduler_started': False, 'passes': []}
        self.assertTrue(sr.verify('commission', good_commission, self.run, frozen))
        self.assertFalse(sr.SECRETISH.search(sr.DIGEST.sub('', json.dumps(good_commission))))
        for bad in ({**good_commission, 'seed_approved': True}, {**good_commission, 'state': 'DRY_RUN'}, {**good_commission, 'seed_sha256': 'c' * 64}):
            with self.assertRaises(sr.Refused): sr.verify('commission', bad, self.run, frozen)
        tomorrow = (now + dt.timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
        ok = {'cron': '0 11 * * *', 'trigger_launch_enabled': '1', 'first_due_utc_seconds': int(tomorrow.timestamp())}
        self.assertTrue(sr.verify('schedule', ok, self.run, frozen))
        for bad in ({**ok, 'first_due_utc_seconds': int(tomorrow.timestamp()) - 2 * 86400}, {**ok, 'first_due_utc_seconds': int(tomorrow.timestamp()) + 60}, {**ok, 'cron': '0 2 * * *'}):
            with self.assertRaises(sr.Refused): sr.verify('schedule', bad, self.run, frozen)
        hold = {'measured_at': now.isoformat(), 'stores': {m: {'source_bytes': 10**9, 'one_client_growth_bytes': 1} for m in sr.capacity_report.MEMBERS}}
        with self.assertRaisesRegex(sr.Refused, 'CAPACITY_HOLD'): sr.verify('capacity', hold, self.run, frozen)
        with self.assertRaises(sr.Refused): sr.verify('staged', {'staged_services': ['ledger'], 'unrelated_staged_changes': 1, 'readback_matches_frozen_plan': True}, self.run, frozen)
        with self.assertRaises(sr.Refused): sr.verify('crash', {'deployment_status': 'CRASHED', 'matching_email_received': False}, self.run, frozen)
        with self.assertRaises(sr.Refused): sr.verify('attest', {'result': 'PASS', 'checked': []}, self.run, frozen)

    def test_decisions_step_detects_input_drift(self):
        frozen = json.loads(self.plan.read_text()); frozen['input_sha256']['provision_operator.py'] = '0' * 64
        with patch.object(sr, 'open_blockers', lambda name: []), self.assertRaisesRegex(sr.Refused, 'INPUT_DRIFT'):
            sr.verify('decisions', None, self.run, frozen)

if __name__ == '__main__':
    unittest.main()
