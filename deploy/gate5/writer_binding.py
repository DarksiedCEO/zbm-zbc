#!/usr/bin/env python3
"""Build provision_operator.py's binding file. Offline; no provider calls; no values.

Inputs (all private, names/IDs only):
  original   the step-1 baseline (existing service IDs are pinned to it)
  new_ids    the new-resources evidence: {"services": {role: id}, "volumes": {...}}
  inventory  a FRESH names-only inventory in the same schema as the baseline,
             captured right before the writer and covering all 12 services.
             It must come from a source that lists sealed names (the Railway
             connector's describe output); CLI variable lists omit them.

Writes a new 0600 file under ~/.codex/private/ and prints names and status only.
"""
import argparse, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import provision_operator as op
import freeze_plan as fp

class Refused(ValueError):
    pass

def need(ok, code):
    if not ok:
        raise Refused(code)

def build(original, new_ids, inventory):
    fp.check_baseline(original)
    env = inventory['environment']
    need(env['project']['id'] == op.AUTHORIZED_TARGET['project_id'] and env['environment']['id'] == op.AUTHORIZED_TARGET['environment_id']
         and env['project']['name'] == 'zbm-zbc' and env['environment']['name'] == 'production', 'TARGET')
    need(env['project']['id'] == original['environment']['project']['id']
         and env['environment']['id'] == original['environment']['environment']['id'], 'TARGET_CHANGED')
    pinned = {s['service']['name']: s['service']['id'] for s in original['services']}
    pinned.update(new_ids['services'])
    roles = {s for s, n in op.c.REQUIRED | set(op.c.URLS)}
    need(set(pinned) == roles, 'ROLE_SET')
    rows = {}
    for s in inventory['services']:
        name = s['service']['name']
        need(name in roles and name not in rows, 'INVENTORY_SERVICE:' + name)
        need(s['service']['id'] == pinned[name], 'ID_MISMATCH:' + name)
        need(s.get('stagedChanges') == [], 'STAGED_CHANGES_PRESENT:' + name)  # D04: writer needs zero staged
        names = s['variableNames']
        need(type(names) is list and all(type(n) is str for n in names), 'NAMES:' + name)
        rows[name] = {'id': pinned[name], 'complete_names': True, 'names': sorted(names)}
    need(set(rows) == roles, 'INVENTORY_INCOMPLETE')
    binding = {**op.AUTHORIZED_TARGET, 'services': rows}
    try:
        op.validate(binding)
    except op.Refused:
        raise Refused('BINDING_INVALID') from None
    return binding

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('original'); ap.add_argument('new_ids'); ap.add_argument('inventory'); ap.add_argument('out')
    a = ap.parse_args(argv)
    out = Path(a.out)
    need(out.is_absolute() and str(out.resolve()).startswith(str(Path.home() / '.codex' / 'private') + '/'), 'OUT_MUST_BE_PRIVATE')
    load = lambda p: json.loads(Path(p).read_text())
    binding = build(load(a.original), load(a.new_ids), load(a.inventory))
    data = (json.dumps(binding, indent=1, sort_keys=True) + '\n').encode()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    print(json.dumps({'status': 'BINDING_WRITTEN', 'services': sorted(binding['services']),
                      'names_per_service': {k: len(v['names']) for k, v in sorted(binding['services'].items())}}))
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Refused as e:
        print('BINDING_REFUSED: ' + str(e), file=sys.stderr); raise SystemExit(2)
    except (OSError, ValueError, KeyError, TypeError):
        print('BINDING_REFUSED: unreadable input', file=sys.stderr); raise SystemExit(2)
