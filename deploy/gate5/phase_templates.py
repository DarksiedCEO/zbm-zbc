"""Offline review templates only. No credentials, provider calls or apply capability."""
import json
import shlex
from pathlib import Path
import credential_plan as custody

ROOT = Path(__file__).parent

def templates():
    topology = json.loads((ROOT / 'topology_plan.json').read_text())
    images = json.loads((ROOT / 'release_images.json').read_text())
    decisions = json.loads((ROOT / 'approved_decisions.json').read_text())
    capacity = json.loads((ROOT / 'capacity_profile.json').read_text())['policy']
    def identity(role):
        return dict(projectId='<production-project-id>', environmentId='<production-environment-id>',
                    serviceId=f'<service-id:{role}>', staged=True)
    def variables(role, values):
        return dict(tool='set_variables', arguments={**identity(role), 'skipDeploys': True, 'variables': values})
    existing_owners = ('ledger', 'legal', 'finance', 'seo')
    resources = []
    settings = []
    ledger_switch = []
    owner_limits = []
    for role, row in topology['roles'].items():
        # D06: Ledger stays legacy and unstaged until Compliance is commissioned.
        target = ledger_switch if role == 'ledger' else settings
        contract = row['contract']
        if role not in existing_owners:
            resources.append(dict(tool='create_service', arguments=dict(
                projectId='<production-project-id>', environmentId='<production-environment-id>',
                name=role, staged=True), result_slot=f'<service-id:{role}>'))
            if contract['mount']:
                resources.append(dict(tool='create_volume', arguments={**identity(role),
                    'name': role + '-data', 'mountPath': contract['mount'],
                    **decisions['new_volumes']},
                    result_slot=f'<volume-id:{role}>'))
        image = images['images'][role if role in images['images'] else 'admin']
        limits = decisions['resources']['ledger' if role == 'ledger' else 'python_services']
        target.append(dict(tool='connect_service_source', arguments={**identity(role), 'image': image['image']}))
        if role in existing_owners:
            # D03: same fields as apply_plan.mjs; image entrypoint applies. Limits are a later
            # phase because they can affect a running deployment immediately.
            target.append(dict(tool='update_service', arguments={**identity(role),
                'drainingSeconds': 45, 'overlapSeconds': 0, 'restartPolicyType': 'NEVER'}))
            owner_limits.append(dict(tool='update_service', arguments={**identity(role), 'limits': limits}))
        else:
            target.append(dict(tool='update_service', arguments={**identity(role),
                # Owners keep their image entrypoint (D03); admin roles need their role argument.
                **({} if role in custody.OWNERS else {'startCommand': shlex.join(contract['entrypoint'])}), 'drainingSeconds': 45, 'overlapSeconds': 0,
                'restartPolicyType': 'NEVER', 'limits': limits, **({'cronSchedule': None} if role == 'backup-trigger' else {})}))
        values = {'BACKUP_LAUNCH_ENABLED': '0'}
        if role in custody.OWNERS or role == 'coordinator':
            values['BACKUP_CAPACITY_POLICY'] = json.dumps(capacity, sort_keys=True, separators=(',', ':'))
        if role in custody.OWNERS:
            values.update(BACKUP_MEMBER=role, BACKUP_VOLUME='/data', BACKUP_RECOVERY_MODE='0', PORT='27001', RAILWAY_RUN_UID='0')
            values['LEDGER_LOG_PATH' if role == 'ledger' else role.upper()+'_DATA_DIR'] = contract['source']
        if role == 'observer':
            values.update(BACKUP_ALERTS_ENABLED='0', BACKUP_FIRST_DUE_UTC_SECONDS='<approved-first-due-unix-seconds>',
                          BACKUP_ALERT_FROM='andre@zbestmedia.com')
        target.append(variables(role, values))
    # D01: empty portal service early; no source, domain or variables until the portal phase.
    resources.append(dict(tool='create_service', arguments=dict(projectId='<production-project-id>',
        environmentId='<production-environment-id>', name='client-portal', staged=True), result_slot='<service-id:client-portal>'))
    routes = [variables(role, {name: value}) for (role, name), value in sorted(custody.URLS.items())]
    dotted = lambda pair: '.'.join(pair)
    secrets = dict(
        generate_once_if_absent=sorted(map(dotted, custody.GENERATED)),
        direct_copies={dotted(k): list(map(dotted, v)) for k, v in custody.DIRECT.items()},
        caller_maps={dotted(k): {caller:dotted(source) for caller,source in v.items()} for k,v in custody.CALLERS.items()},
        ledger_callers={dotted(k): {'caller':v[0], 'departments':[v[1]], 'scope':'write'} for k,v in custody.LEDGER.items()},
        ledger_map_destination=dotted(custody.LEDGER_MAP), ledger_map_key='sha256 of in-memory credential',
        single_inputs=sorted(map(dotted, custody.SINGLE)), forbidden=sorted(map(dotted, custody.FORBIDDEN)))
    return dict(schema='gate5-phase-templates-v1', status='HOLD', executable=False, provider_calls=0,
        approved_decisions=decisions,
        image_commit='f5fa44b6ab9ae9841188b44ec87a1f89fc63c1c3',
        prerequisites=['Fresh baseline/date/rollback and no unrelated staged changes',
          'Verify effective resource byte limits and whole-workspace forecast within approved $35/month before rollout',
          'Bind future first-due timestamp to night after sitting; approved cron 0 11 * * *, cadence 86400, grace 900',
          'Commission Compliance against still-live legacy Ledger before switching Ledger to owner',
          'Secure production variable writer qualified before sitting; never serialize secrets into this artifact',
          'Restart policy NEVER requires explicit outage-policy acceptance and independently proven crash notification; observer cannot alert on its own failure'],
        phases=[
          dict(name='new-empty-resources', operations=resources, gate='Explicit resource/cost approval; preserve all existing volumes'),
          dict(name='closed-configuration', operations=settings, gate='Per-role scoped stage/readback/approval; launch stays disabled'),
          dict(name='secret-custody', manifest=secrets, gate='Hidden input and memory only; preserve existing credentials; no secret references; the writer writes no URLs (D02)'),
          dict(name='compliance-commission', operations=[], gate='Redeploy the legacy Ledger current deployment (no rebuild) to load the compliance-py grant; verify build unchanged; one-shot commission; restore normal command (D06, D11)'),
          dict(name='ledger-switch', operations=ledger_switch, gate='After COMMISSIONED_CLOSED: stage Ledger owner change, compare readback, separate approval, commit'),
          dict(name='routing', operations=[r for r in routes if r['arguments']['serviceId']!='<service-id:client-portal>'],
               gate='Sole owner of URL cutover (D02). Existing public domains stay (D08). Approve old-to-new URL diff; preserve unrelated variables. orchestrator, stripe-gateway and detection take effect only by redeploying their CURRENT deployment, never a rebuild from main; auto-deploy must read back off (D07)'),
          dict(name='owner-limits', operations=owner_limits,
               gate='Approved limits for the four existing owners; may apply to running deployments immediately; read back effective byte limits'),
          dict(name='owner-activation', operations=[variables(r, {'BACKUP_LAUNCH_ENABLED':'1'}) for r in topology['start_order']],
               gate='Compliance commissioned, coordination ready, ingress closed; each owner healthy before next'),
          dict(name='control-activation', operations=[variables('coordinator', {'BACKUP_LAUNCH_ENABLED':'1'}),
               variables('observer', {'BACKUP_LAUNCH_ENABLED':'1','BACKUP_ALERTS_ENABLED':'1'})],
               gate='Authenticated control health and alert delivery; business stays behind readiness barrier'),
          dict(name='schedule-activation', operations=[variables('backup-trigger', {'BACKUP_LAUNCH_ENABLED':'1'}),
               dict(tool='update_service', arguments={**identity('backup-trigger'), 'cronSchedule':decisions['schedule']['cron_utc']})],
               gate='Explicit activation approval; read back cron, future first due, observer cadence; prove next run'),
          dict(name='backup-proof', operations=[], gate='All-five seal/verify, restart and traffic reopen; B2 remote-only restore/reconciliation. Never delete preservation. Platform schedules untouched until separate post-PASS approval'),
          dict(name='portal', operations=[dict(tool='connect_service_source', arguments={**identity('client-portal'), 'image':'<approved-public-portal-digest>'}),
               variables('client-portal', {'PORT':'8480', 'PORTAL_PORT':'8480', 'PORTAL_BIND_ADDR':'::',
                   'PORTAL_PUBLIC_ORIGIN':'https://<Railway-returned-portal-domain>', 'PORTAL_DISPLAY_TZ':'America/Los_Angeles',
                   'PORTAL_CLIENT_IP_HEADER':'x-real-ip', 'PORTAL_TRUSTED_PROXY_HOPS':'1'}),
               dict(tool='update_service', arguments={**identity('client-portal'), 'healthcheckPath':'/health', 'healthcheckTimeout':120,
                   'limits':decisions['resources']['python_services']}),
               *[r for r in routes if r['arguments']['serviceId']=='<service-id:client-portal>'],
               dict(tool='generate_domain', arguments=dict(projectId='<production-project-id>',environmentId='<production-environment-id>',
               serviceId='<service-id:client-portal>', targetPort=8480))],
               gate='After B2 proof; separately approved publish/service/domain/cost; portal-specific settings preflight, health and client acceptance required')],
        preservation='Never update/delete existing volumes or backupSchedules. Omitted/unreadable schedules are UNKNOWN, not empty.',
        commit_rule='No accept-deploy generated: review complete staged environment diff, ensure only approved phase is staged, then obtain separate approval. Concurrent changes invalidate baseline.',
        pid=dict(decision='ACCEPTED_RAILWAY_MANAGED_FIRST_CLIENT', record='Per-service current process count at sitting',
                 deferred='Explicit per-container cap after launch; no Mac-cap equivalence'))

if __name__ == '__main__':
    print(json.dumps(templates(), indent=2, sort_keys=True))
