"""One offline contract: desired disabled binding settings compose with custody.

Known authorized project constants satisfy the guard; every service ID, image,
volume, credential, CLI response and approval proof here is synthetic. This is
not a production binding or provider execution.
"""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import binding_test
from test_provision_operator import Fake
import provision_operator as operator
import credential_plan as credentials


class Composition(unittest.TestCase):
    def test_disabled_binding_and_required_custody_compose(self):
        fixture = binding_test.Binding()
        fixture.setUp()
        fake = Fake()
        context = copy.deepcopy(fixture.context)
        context.update(project_id=fake.binding['project_id'],
                       environment_id=fake.binding['environment_id'])
        context['existing_services'] = {
            role: fake.binding['services'][role]['id']
            for role in context['existing_services']
        }
        proposal = binding_test.b.propose(fixture.plan, context)
        self.assertEqual(proposal['status'], 'HOLD')
        self.assertFalse(proposal['executable'])
        self.assertFalse(proposal['schedule_proposal']['enabled'])
        for role, row in proposal['roles'].items():
            for name, value in row['disabled_settings'].items():
                fake.data[role][name] = value
                if name not in fake.binding['services'][role]['names']:
                    fake.binding['services'][role]['names'].append(name)
        # Required sealed input must be prompted; unrelated sealed input must not.
        key = ('client-portal', 'PORTAL_EVIDENCE_KEY')
        hidden_value = fake.data[key[0]].pop(key[1])
        fake.binding['services']['legal']['names'].append('UNCHANGED_SEALED')
        prompts = []
        def hidden(label):
            prompts.append(label)
            self.assertEqual(label, '.'.join(key))
            return hidden_value
        calls = []
        ids = {row['id']: role for role, row in fake.binding['services'].items()}
        def cli_run(command, **kwargs):
            calls.append(command)
            self.assertEqual(command[:2], ['railway', 'variable'])
            self.assertEqual(command[command.index('--project')+1], fake.binding['project_id'])
            self.assertEqual(command[command.index('--environment')+1], fake.binding['environment_id'])
            role = ids[command[command.index('--service')+1]]
            if command[2] == 'list':
                self.assertIn('--json', command)
                return subprocess.CompletedProcess(command, 0, json.dumps(fake.read(role)), '')
            self.assertEqual(command[2], 'set')
            self.assertIn('--stdin', command)
            self.assertIn('--skip-deploys', command)
            name = command[command.index('--stdin')-1]
            value = kwargs['input']
            self.assertNotIn(value, command)
            fake.write(role, name, value)
            return subprocess.CompletedProcess(command, 0, '', '')
        transport = operator.CLI(fake.binding)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'operator-state'
            with patch.object(operator.subprocess, 'run', side_effect=cli_run):
                result = operator.execute(fake.binding, state, transport.read,
                                          transport.write, hidden,
                                          lambda: self.fail('existing fixture must not generate'))
            self.assertEqual(result['status'], 'COMPLETE_HOLD')
            self.assertEqual(json.loads(state.read_text())['status'], 'COMPLETE_HOLD')
        self.assertEqual(prompts, ['.'.join(key)])
        self.assertTrue(calls)
        self.assertTrue(fake.writes)
        for role, row in proposal['roles'].items():
            for name, value in row['disabled_settings'].items():
                self.assertEqual(fake.data[role][name], value)
                self.assertFalse(any(s == role and n == name for s,n,v in fake.writes))
        for source, destinations in credentials.DIRECT.items():
            for role,name in destinations:
                self.assertEqual(fake.data[role][name], fake.data[source[0]][source[1]])
        # D02: URL cutover belongs to the staged routing phase; the writer writes none.
        for role,name,value in fake.writes:
            self.assertNotIn('${{', value)
            self.assertNotIn((role,name), credentials.URLS)
        self.assertIn('retained_custom', json.loads(fake.data['finance']['FIN_CALLER_TOKENS']))
        self.assertIn('retained_dashboard', json.loads(fake.data['seo']['SEO_CALLER_TOKENS']))
        self.assertNotIn('UNCHANGED_SEALED', fake.data['legal'])
        # Completion does not turn a topology proposal into apply authority.
        self.assertFalse(proposal['executable'])


if __name__ == '__main__':
    unittest.main()
