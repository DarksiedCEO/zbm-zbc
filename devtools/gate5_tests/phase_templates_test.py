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
        if 'limits' in a:assert a['limits']=={'memoryGB':0.512, 'vCPUs':1}
# D03: existing owners stage only apply_plan fields; no owner start-command override; limits later.
names=[x['name'] for x in p['phases']]
owners={'<service-id:%s>'%r for r in ('ledger','legal','finance','seo')}
for op in ops:
    if op['tool']=='update_service' and op['arguments']['serviceId'] in owners|{'<service-id:compliance>'}:
        assert 'startCommand' not in op['arguments']
assert all(isinstance(o['arguments'].get('startCommand',''),str) for o in ops if o['tool']=='update_service')
lim=next(x for x in p['phases'] if x['name']=='owner-limits')['operations']
assert {o['arguments']['serviceId'] for o in lim}==owners and next(o for o in lim if o['arguments']['serviceId']=='<service-id:ledger>')['arguments']['limits']=={'memoryGB':0.256,'vCPUs':1}
assert names.index('routing')<names.index('owner-limits')<names.index('owner-activation')
# D06: Ledger absent from closed configuration; staged only after commissioning.
assert not any(o['arguments'].get('serviceId')=='<service-id:ledger>' for o in closed['operations'])
assert names.index('compliance-commission')<names.index('ledger-switch')<names.index('routing')
# D04 worst case: the writer's staged variables get their own commit before anything else.
assert names.index('secret-custody')+1==names.index('secret-commit')==names.index('compliance-commission')-1
# D01: empty portal service created with the other new resources, not in the portal phase.
new=next(x for x in p['phases'] if x['name']=='new-empty-resources')['operations']
assert any(o['tool']=='create_service' and o['arguments']['name']=='client-portal' for o in new)
assert not any(o['tool']=='create_service' for o in portal_ops) if (portal_ops:=next(x for x in p['phases'] if x['name']=='portal')['operations']) else True
assert p['approved_decisions']['workspace_monthly_budget_usd']==35
assert p['approved_decisions']['usage_remaining_percent'] is None
assert next(x for x in p['phases'] if x['name']=='schedule-activation')['operations'][-1]['arguments']['cronSchedule']=='0 11 * * *'

for op in ops:
    if op['tool']=='connect_service_source':
        assert op['arguments']['staged'] is True
        assert op['arguments']['environmentId']=='<production-environment-id>'
portal=next(x for x in p['phases'] if x['name']=='portal')
assert portal['operations'][-1]['arguments']['targetPort']==8480
assert any('observer cannot alert on its own failure' in x for x in p['prerequisites'])
