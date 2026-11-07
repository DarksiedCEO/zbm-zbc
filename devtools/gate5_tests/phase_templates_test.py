"""Focused offline contract checks; no provider execution."""
import importlib.util
import json
import sys
from pathlib import Path
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'deploy/gate5'))
from phase_templates import templates
p=templates()
assert p['executable'] is False and p['provider_calls']==0
assert p['pid']['decision']=='ACCEPTED_RAILWAY_MANAGED_FIRST_CLIENT'
ops=[op for phase in p['phases'] for op in phase.get('operations',[])]
assert not any(o['tool'] in ('accept_deploy','update_volume','delete_volume') for o in ops)
volumes=[o['arguments'] for o in ops if o['tool']=='create_volume']
assert {v['serviceId'] for v in volumes}=={'<service-id:compliance>','<service-id:coordinator>','<service-id:observer>'}
assert all(o['arguments']['staged'] for o in ops if o['tool']!='generate_domain')
assert all(o['arguments']['skipDeploys'] for o in ops if o['tool']=='set_variables')
closed=next(x for x in p['phases'] if x['name']=='closed-configuration')
assert all(o['arguments']['variables']['BACKUP_LAUNCH_ENABLED']=='0' for o in closed['operations'] if o['tool']=='set_variables')
assert all('@sha256:' in o['arguments']['image'] for o in closed['operations'] if o['tool']=='connect_service_source')
assert 'backupSchedules' not in json.dumps(ops)
assert next(x for x in p['phases'] if x['name']=='portal')['operations'][-1]['tool']=='generate_domain'
assert p['phases'].index(next(x for x in p['phases'] if x['name']=='portal')) > p['phases'].index(next(x for x in p['phases'] if x['name']=='backup-proof'))
print('PASS: offline phase template contracts')

assert all(v['region']=='us-west2' and v['sizeMB']==5000 for v in volumes)
for op in closed['operations']:
    if op['tool']=='update_service':
        a=op['arguments']
        assert a['limits']=={'memoryGB':0.256 if a['serviceId']=='<service-id:ledger>' else 0.512, 'vCPUs':1}
assert p['approved_decisions']['workspace_monthly_budget_usd']==35
assert p['approved_decisions']['usage_remaining_percent'] is None
assert next(x for x in p['phases'] if x['name']=='schedule-activation')['operations'][-1]['arguments']['cronSchedule']=='0 11 * * *'
