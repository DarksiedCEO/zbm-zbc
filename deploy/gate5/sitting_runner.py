#!/usr/bin/env python3
"""Gate 5 sitting runner — guarded, dormant by default. Do not run before the sitting.

Every sitting step is begun once and closed once, each behind a typed approval
on a terminal. 'begin' durably records UNKNOWN before anything happens; a step
can never be begun again. A FAIL close writes STOP, which refuses every later
action until a newly approved run directory is prepared. There is no retry.

This runner makes no provider calls. Live actions are performed by the operator
under separate approval; the runner checks their names-only evidence offline.
The only Railway-touching path it can start is the reviewed provision_operator.py,
which prompts for secrets itself. Output is step names, service names and status.
"""
import argparse, datetime as dt, json, os, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import freeze_plan, capacity_report

PRIVATE_ROOT = Path.home() / '.codex' / 'private'
DECISIONS = ROOT / 'open_decisions.json'
OWNER_ORDER = ('ledger', 'legal', 'compliance', 'finance', 'seo')
SECRETISH = re.compile(r'[0-9a-fA-F]{32,}|(sk|rk|pk|whsec|re)_[A-Za-z0-9_]{8,}|-----BEGIN|\$\{\{')
# Public digests are allowed only as values of keys ending in sha256, or as sha256:<hex>.
DIGEST = re.compile(r'"[a-z_]*sha256"\s*:\s*"[0-9a-f]{64}"|sha256:[0-9a-f]{64}')
SEED_SHA256 = 'bc6d371aa2703a231e03180b1c09907fc25a20dc09f44cdeb5a82f854df070f5'

class Refused(ValueError):
    pass

def need(ok, code):
    if not ok:
        raise Refused(code)

def step(name, kind, needs, actions, rollback, evidence=None):
    return dict(name=name, kind=kind, needs=tuple(needs), actions=tuple(actions), rollback=rollback, evidence=evidence)

STEPS = [
    step('baseline', 'operator', [], ['Read-only inventory of zbm-zbc/production: services, volumes, variable names, domains, staged changes, latest deployments, backup schedules',
        'Confirm environment created after 2025-10-16 and rollback deployments still redeployable',
        'Read back auto-deploy OFF for all 7 existing services (D07); no start command on the four owners (D03); schedules UNKNOWN stay untouched (D13)'],
        'None: read-only.', 'baseline'),
    step('decisions', 'local', ['baseline'], ['Every open_decisions.json entry RESOLVED; frozen plan inputs unchanged'],
        'None.', 'decisions'),
    step('merge', 'operator', ['decisions'], ['Approve exact reviewed head and CI; squash merge with auto-deploy confirmed off for every repo-connected service'],
        'Revert commit on main; repo-connected services must not auto-deploy.', 'attest'),
    step('new-resources', 'operator', ['merge'], ['Staged create: compliance, coordinator, observer, backup-trigger and EMPTY client-portal services (D01: no source, domain or variables); compliance/coordinator/observer volumes (us-west2, 5000 MB)',
        'Review staged diff contains only these; separate approval; commit'],
        'Delete only the newly created empty services/volumes, after founder inspection. Never touch existing volumes.', 'new_ids'),
    step('closed-configuration', 'operator', ['new-resources'], ['Stage per-role source/settings/disabled variables (BACKUP_LAUNCH_ENABLED=0); Ledger is NOT staged (D06)',
        'Compare staged diff to frozen plan; separate approval; commit'],
        'Discard staged changes; redeploy recorded rollback deployments for touched owners.', 'staged'),
    step('secret-provision', 'writer', ['closed-configuration'], ['Precondition: zero staged changes environment-wide (D04)',
        'provision_operator.py --apply with four confirmations; hidden input on this terminal only'],
        'Reconcile names-only state against fresh remote names; never rerun; prepare separately approved continuation.', 'writer_state'),
    step('secret-commit', 'operator', ['secret-provision'], ['Staged diff contains exactly the writer names from writer-state.json and nothing else (D04); approve; commit',
        'Read back: legacy Ledger and any touched orchestrator/stripe-gateway/detection still run their pre-sitting build (D07); zero staged changes remain'],
        'Do not discard: writes are durable. Reconcile names against writer-state.json; redeploy recorded rollback deployments if a build changed.', 'staged'),
    step('compliance-commission', 'operator', ['secret-commit'], ['Legacy Ledger runs its unchanged build with the committed compliance-py grant; redeploy its CURRENT deployment only if the commit did not restart it (D06)',
        'Compliance: stage start command = the exact compliance_README one-shot command; deploy once (restart NEVER); capture COMMISSIONED_CLOSED output; stage start command back to none; commit without deploying (D11)',
        'Evidence is the COMMISSIONED_CLOSED output line only; Railway deployment status is never proof'],
        'Stop Compliance; leave store untouched for inspection; legacy Ledger unchanged.', 'commission'),
    step('ledger-switch', 'operator', ['compliance-commission'], ['Review the still-staged Ledger owner change against frozen plan; separate approval; commit'],
        'Redeploy recorded legacy Ledger rollback deployment.', 'staged'),
    step('routing', 'operator', ['ledger-switch'], ['Staged routing phase is the only URL writer (D02); existing public domains stay (D08); compare the raw staged diff, not CLI list output (D05); approve; commit',
        'Redeploy CURRENT deployments of orchestrator, stripe-gateway, detection; never rebuild from main (D07)'],
        'Restore recorded URL names to their pre-sitting references; redeploy affected services.', 'staged'),
    step('owner-limits', 'operator', ['routing'], ['Stage approved limits on ledger, legal, finance, seo; read back effective byte limits; approve; commit'],
        'Restore pre-sitting limit settings (recorded at baseline as UNKNOWN unless exposed).', 'staged'),
    *[step('owner-activation:' + o, 'operator', ['owner-limits'] if i == 0 else ['owner-activation:' + OWNER_ORDER[i - 1]],
           [f'Set BACKUP_LAUNCH_ENABLED=1 on {o}; deploy; native health and control authentication pass before next owner'],
           f'Set BACKUP_LAUNCH_ENABLED=0 on {o}; redeploy; stop activation order.', 'staged') for i, o in enumerate(OWNER_ORDER)],
    step('control-activation', 'operator', ['owner-activation:seo'], ['Enable coordinator, then observer and alerts; authenticated control health; alert delivery receipt'],
        'Disable observer alerts and coordinator launch; redeploy.', 'staged'),
    step('crash-notification', 'operator', ['control-activation'], ['Founder confirms effective notification settings; approved synthetic crash on approved target; matching email received'],
        'Redeploy the recorded healthy deployment of the crash target.', 'crash'),
    step('schedule-activation', 'operator', ['crash-notification'], ['Set trigger launch, cron 0 11 * * *, observer first due; read back all three'],
        'Clear trigger cron and set BACKUP_LAUNCH_ENABLED=0 on backup-trigger.', 'schedule'),
    step('backup-proof', 'operator', ['schedule-activation'], ['All-five seal/verify/restart/reopen; B2 snapshots and catalog; restore to approved empty isolated targets; reconcile; delete targets; report cost'],
        'Keep platform backup schedules; delete only approved restore targets; preservation repository untouched.', 'attest'),
    step('schedule-removal', 'operator', ['backup-proof'], ['Separately approved removal of platform backupSchedules on the four owner volumes; read back'],
        'Re-add the recorded schedules if readback differs.', 'attest'),
    step('portal', 'operator', ['schedule-removal'], ['Packages-only session: publish approved portal image; verify digest and anonymous pull; logout',
        'Approved service/domain/cost; settings before first deploy; public health; acceptance flow'],
        'Remove portal domain and service after founder inspection; package deletion only with founder approval.', 'attest'),
    # Capacity gates only admission: it needs measured one-client growth, which exists only after
    # the portal is live. A HOLD leaves the step unclosed and admission closed; nothing else waits on it.
    step('capacity', 'local', ['portal'], ['Run capacity_report on fresh all-five source sizes and measured one-client growth; begin only with that evidence in hand'],
        'None: read-only. Admission stays closed.', 'capacity'),
    step('admission', 'operator', ['capacity'], ['Alert inbox evidence, operator-run report, live Stripe configuration, SOW 4.1 price, capacity report, explicit founder GO'],
        'Admission stays closed.', 'attest'),
]
BY_NAME = {s['name']: s for s in STEPS}

def run_dir(path):
    p = Path(path)
    need(p.is_absolute() and str(p.resolve()).startswith(str(PRIVATE_ROOT.resolve()) + '/'), 'RUN_DIR_MUST_BE_PRIVATE')
    need(p.is_dir() and (p.stat().st_mode & 0o077) == 0, 'RUN_DIR_MODE_0700')
    return p

def record(d, name):
    f = d / (name.replace(':', '_') + '.json')
    return json.loads(f.read_text()) if f.exists() else None

def write_new(f, obj):
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview((json.dumps(obj, sort_keys=True) + '\n').encode())
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(f.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)

def confirm(phrase):
    need(sys.stdin.isatty(), 'TERMINAL_REQUIRED')
    need(input(f'Type exactly "{phrase}" to continue: ') == phrase, 'NOT_APPROVED')

def open_blockers(name):
    decisions = json.loads(DECISIONS.read_text())['decisions']
    # DEFERRED never blocks starting the sitting, only the steps it lists.
    return sorted(d['id'] for d in decisions if d['status'] != 'RESOLVED' and (name.split(':')[0] in d['blocks']
                  or (name == 'decisions' and d['status'] != 'DEFERRED')))

def status(d):
    stop = (d / 'STOP').exists()
    return {'stopped': stop, 'steps': [{'step': s['name'], 'status': (record(d, s['name']) or {}).get('status', 'NOT_STARTED')} for s in STEPS]}

def load_evidence(path):
    raw = Path(path).read_text()
    need(len(raw) <= 2_000_000 and not SECRETISH.search(DIGEST.sub('', raw)), 'EVIDENCE_MAY_CONTAIN_SECRET')
    return json.loads(raw)

def verify(kind, ev, d, frozen):
    if kind == 'decisions':
        blockers = open_blockers('decisions')
        need(not blockers, 'OPEN_DECISIONS:' + ','.join(blockers))
        for n, h in frozen['input_sha256'].items():
            need(freeze_plan.sha((ROOT / n).read_bytes()) == h, 'INPUT_DRIFT:' + n)
        return 'all decisions resolved; inputs match frozen plan'
    if kind == 'writer_state':
        f = d / 'writer-state.json'
        need(f.exists() and json.loads(f.read_text()).get('status') == 'COMPLETE_HOLD', 'WRITER_NOT_COMPLETE')
        return 'writer COMPLETE_HOLD'
    if kind == 'baseline':
        freeze_plan.check_baseline(ev)
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ev['captured_at'].replace('Z', '+00:00'))
        need(dt.timedelta(0) <= age <= dt.timedelta(hours=2), 'BASELINE_NOT_FRESH')
        for s in ev['services']:
            p = frozen['preserve'][s['service']['name']]
            need(sorted(s['variableNames']) == p['variableNames'] and s['domains'] == p['domains'], 'BASELINE_DRIFT:' + s['service']['name'])
        return 'baseline matches frozen plan'
    if kind == 'capacity':
        result = capacity_report.report(ev, dt.datetime.now(dt.timezone.utc))
        need(result['status'] == 'CAPACITY_ONLY_PASS', 'CAPACITY_HOLD')
        return 'capacity below manual threshold for all five stores'
    if kind == 'new_ids':
        uuid = freeze_plan.UUID
        need(set(ev) == {'services', 'volumes'} and set(ev['services']) == {'compliance', 'coordinator', 'observer', 'backup-trigger', 'client-portal'}
             and set(ev['volumes']) == {'compliance', 'coordinator', 'observer'}, 'NEW_ID_SCHEMA')
        ids = list(ev['services'].values()) + list(ev['volumes'].values())
        need(all(type(i) is str and uuid.match(i) for i in ids) and len(set(ids)) == len(ids), 'NEW_IDS_INVALID')
        return 'new identities recorded (names only shown)'
    if kind == 'staged':
        need(set(ev) == {'staged_services', 'unrelated_staged_changes', 'readback_matches_frozen_plan'}, 'STAGED_SCHEMA')
        need(ev['unrelated_staged_changes'] == 0 and ev['readback_matches_frozen_plan'] is True, 'STAGED_MISMATCH')
        return 'staged/committed services: ' + ','.join(sorted(ev['staged_services']))
    if kind == 'commission':
        need('deployment_status' not in ev, 'DEPLOYMENT_STATUS_IS_NOT_PROOF')
        need(ev.get('state') == 'COMMISSIONED_CLOSED' and ev.get('seed_sha256') == SEED_SHA256
             and ev.get('seed_approved') is False and ev.get('listener_started') is False
             and ev.get('scheduler_started') is False, 'COMMISSION_NOT_CLOSED')
        return 'compliance commissioned closed; seed not approved'
    if kind == 'crash':
        need(ev.get('deployment_status') == 'CRASHED' and ev.get('matching_email_received') is True, 'CRASH_EMAIL_NOT_PROVEN')
        return 'crash email received; OOM and child-failure delivery remain unproven'
    if kind == 'schedule':
        need(ev.get('cron') == '0 11 * * *' and ev.get('trigger_launch_enabled') == '1', 'SCHEDULE_READBACK')
        due = ev.get('first_due_utc_seconds')
        need(type(due) is int and due > dt.datetime.now(dt.timezone.utc).timestamp() and due % 86400 == 39600, 'FIRST_DUE_INVALID')
        return 'schedule read back; first due in the future at 11:00 UTC'
    if kind == 'attest':
        need(set(ev) == {'result', 'checked'} and ev['result'] == 'PASS' and type(ev['checked']) is list and ev['checked'], 'ATTESTATION_INCOMPLETE')
        return 'operator attested ' + str(len(ev['checked'])) + ' checks'
    raise Refused('UNKNOWN_EVIDENCE_KIND')

def begin(d, name, frozen_path, binding=None):
    need(not (d / 'STOP').exists(), 'RUN_STOPPED')
    s = BY_NAME.get(name); need(s is not None, 'UNKNOWN_STEP')
    need(record(d, name) is None, 'STEP_ALREADY_BEGUN_NO_RETRY')
    need(all((record(d, n) or {}).get('status') == 'PASS' for n in s['needs']), 'PREVIOUS_STEP_NOT_PASS')
    blockers = open_blockers(name)
    need(not blockers, 'OPEN_DECISIONS:' + ','.join(blockers))
    frozen = json.loads(Path(frozen_path).read_text())
    need(frozen['schema'] == 'gate5-frozen-plan-v1' and frozen['status'] == 'HOLD', 'FROZEN_PLAN')
    print(json.dumps({'step': name, 'actions': s['actions'], 'rollback': s['rollback']}, indent=1))
    confirm('BEGIN ' + name)
    write_new(d / (name.replace(':', '_') + '.json'), {'step': name, 'status': 'UNKNOWN'})
    if name == 'decisions':
        return close(d, name, 'PASS', None, frozen_path, confirmed=True)
    if s['kind'] == 'writer':
        need(binding and str(Path(binding).resolve()).startswith(str(PRIVATE_ROOT.resolve()) + '/'), 'BINDING_MUST_BE_PRIVATE')
        args = [sys.executable, '-I', '-B', str(ROOT / 'provision_operator.py'), '--apply', '--binding', binding, '--state', str(d / 'writer-state.json'),
                *['--confirm-' + f for f in ('exclusive-sitting', 'auto-deploy-off', 'manual-sealing', 'presence-manifest-current')]]
        rc = subprocess.run(args, timeout=3600).returncode  # inherits this terminal for hidden prompts
        return close(d, name, 'PASS' if rc == 0 else 'FAIL', None, frozen_path, confirmed=True)
    print(json.dumps({'step': name, 'status': 'UNKNOWN', 'next': 'perform actions, then close with evidence'}))
    return 'UNKNOWN'

def close(d, name, result, evidence, frozen_path, confirmed=False):
    s = BY_NAME.get(name); need(s is not None, 'UNKNOWN_STEP')
    rec = record(d, name); need(rec is not None and rec['status'] == 'UNKNOWN', 'STEP_NOT_OPEN')
    need(result in ('PASS', 'FAIL'), 'RESULT')
    note = None
    if result == 'PASS' and s['evidence']:
        frozen = json.loads(Path(frozen_path).read_text())
        if s['evidence'] in ('decisions', 'writer_state'):
            note = verify(s['evidence'], None, d, frozen)
        else:
            need(evidence is not None, 'EVIDENCE_REQUIRED')
            note = verify(s['evidence'], load_evidence(evidence), d, frozen)
    if not confirmed:
        confirm(f'CLOSE {name} {result}')
    write_new(d / (name.replace(':', '_') + '.closed.json'), {'step': name, 'status': result, 'note': note})
    f = d / (name.replace(':', '_') + '.json')
    tmp = f.with_suffix('.tmp'); write_new(tmp, {'step': name, 'status': result}); os.replace(tmp, f)
    if result == 'FAIL':
        write_new(d / 'STOP', {'stopped_at': name})
        print(json.dumps({'step': name, 'status': 'FAIL', 'stop': True, 'rollback': s['rollback']}, indent=1))
    else:
        print(json.dumps({'step': name, 'status': 'PASS', 'note': note}))
    return result

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd')
    for c in ('status', 'begin', 'close'):
        p = sub.add_parser(c); p.add_argument('--run', required=True)
        if c != 'status':
            p.add_argument('step'); p.add_argument('--plan', required=True)
        if c == 'begin':
            p.add_argument('--binding')
        if c == 'close':
            p.add_argument('--result', required=True); p.add_argument('--evidence')
    a = ap.parse_args(argv)
    if a.cmd is None:
        print('HOLD: sitting runner is dormant. Steps: ' + ', '.join(s['name'] for s in STEPS)); return 0
    d = run_dir(a.run)
    if a.cmd == 'status':
        print(json.dumps(status(d), indent=1)); return 0
    if a.cmd == 'begin':
        begin(d, a.step, a.plan, a.binding); return 0
    return 0 if close(d, a.step, a.result, a.evidence, a.plan) == 'PASS' else 2

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Refused as e:
        print('SITTING_REFUSED: ' + str(e), file=sys.stderr); raise SystemExit(2)
    except (OSError, ValueError, KeyError, TypeError):
        print('SITTING_REFUSED: unreadable input or record; no retry', file=sys.stderr); raise SystemExit(2)
