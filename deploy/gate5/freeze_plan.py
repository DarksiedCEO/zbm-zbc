#!/usr/bin/env python3
"""Freeze the filled NON-secret rollout plan. Offline; no provider calls.

Reads the private names/settings-only baseline, fills existing-service IDs into
the phase templates and writes the result to a NEW private file (mode 0600).
New-role IDs, first-due time, portal digest and portal domain stay as slots.
Prints names and status only; never IDs. The output is a review artifact, not
an API payload, and stays outside this public repository.
"""
import argparse, hashlib, json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import phase_templates

EXISTING = ('detection', 'finance', 'ledger', 'legal', 'orchestrator', 'seo', 'stripe-gateway')
OWNERS = ('finance', 'ledger', 'legal', 'seo')
NEW = ('backup-trigger', 'client-portal', 'compliance', 'coordinator', 'observer')
INPUTS = ('release_images.json', 'approved_decisions.json', 'topology_plan.json', 'capacity_profile.json',
          'open_decisions.json', 'phase_templates.py', 'credential_plan.py', 'provision_operator.py', 'apply_plan.mjs')
UUID = re.compile('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

class Refused(ValueError):
    pass

def need(ok, code):
    if not ok:
        raise Refused(code)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def check_baseline(b):
    env = b['environment']
    need(env['project']['name'] == 'zbm-zbc' and env['environment']['name'] == 'production', 'TARGET_NAMES')
    need(UUID.match(env['project']['id']) and UUID.match(env['environment']['id']), 'TARGET_IDS')
    names = sorted(s['service']['name'] for s in b['services'])
    need(names == sorted(EXISTING), 'EXISTING_SERVICE_SET')
    need(all(s['stagedChanges'] == [] for s in b['services']), 'STAGED_CHANGES_PRESENT')
    need(all(v['stagedChangeCount'] == 0 for v in env['volumes']), 'STAGED_VOLUME_CHANGES')
    vols = {v['mountedOn']['serviceName']: v for v in env['volumes']}
    need(sorted(vols) == sorted(OWNERS), 'VOLUME_OWNERS')
    for name, v in vols.items():
        need(v['region'] == 'us-west2' and v['sizeMB'] == 5000 and v['mountedOn']['mountPath'] == '/data', 'VOLUME_FACTS:' + name)
    return vols

def freeze(baseline_raw):
    b = json.loads(baseline_raw)
    vols = check_baseline(b)
    env = b['environment']
    ids = {s['service']['name']: s['service']['id'] for s in b['services']}
    need(all(UUID.match(i) for i in ids.values()) and len(set(ids.values())) == len(ids), 'SERVICE_IDS')
    fill = {'<production-project-id>': env['project']['id'], '<production-environment-id>': env['environment']['id'],
            **{f'<service-id:{n}>': i for n, i in ids.items()}}
    text = json.dumps(phase_templates.templates(), sort_keys=True)
    for slot, value in fill.items():
        text = text.replace(json.dumps(slot), json.dumps(value))
    plan = json.loads(text)
    remaining = sorted(set(re.findall(r'<[^<>"]+>', text)))
    allowed = {f'<service-id:{n}>' for n in NEW} | {f'<volume-id:{n}>' for n in ('compliance', 'coordinator', 'observer')} | {
        '<approved-first-due-unix-seconds>', '<approved-public-portal-digest>', '<Railway-returned-portal-domain>'}
    need(set(remaining) <= allowed, 'UNEXPECTED_SLOT')
    decisions = json.loads((ROOT / 'open_decisions.json').read_text())
    preserve = {}
    for s in b['services']:
        name = s['service']['name']
        mounts = s['config'].get('volumeMounts') or {}
        preserve[name] = {
            'variableNames': sorted(s['variableNames']), 'domains': s['domains'], 'tcpProxies': s['tcpProxies'],
            'volume': vols.get(name), 'source': s['config'].get('source'),
            'schedule_observation': 'EXPOSED' if any('backupSchedules' in m for m in mounts.values()) else 'UNKNOWN_NOT_EMPTY',
            'restart_policy_observation': s['config'].get('deploy', {}).get('restartPolicyType') or 'UNKNOWN',
            'auto_deploy_observation': 'UNKNOWN'}
    return dict(
        schema='gate5-frozen-plan-v1', status='HOLD', executable=False, provider_calls=0,
        baseline_sha256=sha(baseline_raw.encode()), baseline_captured_at=b['captured_at'],
        input_sha256={n: sha((ROOT / n).read_bytes()) for n in INPUTS},
        open_decisions=[d['id'] for d in decisions['decisions'] if d['status'] != 'RESOLVED'],
        remaining_slots=remaining, preserve=preserve, untouched_services=sorted(set(EXISTING) - set(OWNERS)),
        phases=plan['phases'], prerequisites=plan['prerequisites'], commit_rule=plan['commit_rule'],
        preservation=plan['preservation'], approved_decisions=plan['approved_decisions'])

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('baseline'); ap.add_argument('out')
    a = ap.parse_args(argv)
    out = Path(a.out)
    need(out.is_absolute() and str(out.resolve()).startswith(str(Path.home() / '.codex' / 'private') + '/'), 'OUT_MUST_BE_PRIVATE')
    frozen = freeze(Path(a.baseline).read_text())
    data = (json.dumps(frozen, indent=1, sort_keys=True) + '\n').encode()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    print(json.dumps({'status': frozen['status'], 'plan_sha256': sha(data), 'phases': [p['name'] for p in frozen['phases']],
                      'remaining_slots': len(frozen['remaining_slots']), 'open_decisions': frozen['open_decisions']}))
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Refused as e:
        print('FREEZE_REFUSED: ' + str(e), file=sys.stderr); raise SystemExit(2)
