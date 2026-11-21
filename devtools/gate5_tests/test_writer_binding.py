"""Offline tests for writer_binding.py. Synthetic IDs and names only."""
import copy, json, sys, tempfile, unittest, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy/gate5'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import writer_binding as wb
import provision_operator as op
from test_sitting_runner import synthetic_baseline

NEW = ('backup-trigger', 'client-portal', 'compliance', 'coordinator', 'observer')

def fixtures():
    original = synthetic_baseline()
    original['environment']['project']['id'] = op.AUTHORIZED_TARGET['project_id']
    original['environment']['environment']['id'] = op.AUTHORIZED_TARGET['environment_id']
    new_ids = {'services': {r: str(uuid.uuid4()) for r in NEW}, 'volumes': {r: str(uuid.uuid4()) for r in ('compliance', 'coordinator', 'observer')}}
    inventory = copy.deepcopy(original)
    for r, i in new_ids['services'].items():
        inventory['services'].append({'service': {'id': i, 'name': r, 'state': 'live'}, 'stagedChanges': [], 'variableNames': ['BACKUP_LAUNCH_ENABLED']})
    return original, new_ids, inventory

class Binding(unittest.TestCase):
    def test_builds_valid_binding_for_all_twelve(self):
        b = wb.build(*fixtures())
        op.validate(b)
        self.assertEqual(len(b['services']), 12)
        self.assertTrue(all(r['complete_names'] is True for r in b['services'].values()))

    def test_refusals(self):
        cases = {}
        o, n, i = fixtures(); i['services'].pop(); cases['INVENTORY_INCOMPLETE'] = (o, n, i)
        o, n, i = fixtures(); i['services'][0]['service']['id'] = str(uuid.uuid4()); cases['ID_MISMATCH'] = (o, n, i)
        o, n, i = fixtures(); i['services'][-1]['service']['id'] = str(uuid.uuid4()); cases['ID_MISMATCH:backup'] = (o, n, i)
        o, n, i = fixtures(); i['services'][1]['stagedChanges'] = [{'x': 1}]; cases['STAGED_CHANGES_PRESENT'] = (o, n, i)
        o, n, i = fixtures(); i['environment']['project']['id'] = str(uuid.uuid4()); cases['TARGET'] = (o, n, i)
        o, n, i = fixtures(); i['services'].append(copy.deepcopy(i['services'][0])); cases['INVENTORY_SERVICE'] = (o, n, i)
        o, n, i = fixtures(); i['services'][0]['variableNames'] = ['lower_case']; cases['BINDING_INVALID'] = (o, n, i)
        for code, args in cases.items():
            with self.subTest(code), self.assertRaisesRegex(wb.Refused, code.split(':')[0]):
                wb.build(*args)

    def test_output_must_be_private(self):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for k, v in zip(('o', 'n', 'i'), fixtures()):
                p = Path(d) / k; p.write_text(json.dumps(v)); paths.append(str(p))
            with self.assertRaisesRegex(wb.Refused, 'PRIVATE'):
                wb.main([*paths, str(Path(d) / 'binding.json')])

if __name__ == '__main__':
    unittest.main()
