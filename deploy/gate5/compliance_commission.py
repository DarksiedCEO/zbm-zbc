"""One-time native Compliance seed commissioning; no API or scheduler startup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / 'services/compliance-py/src'
SEED = SOURCE.parent / 'seed/compliance_obligations_seed.json'
EXPECTED_SEED = 'bc6d371aa2703a231e03180b1c09907fc25a20dc09f44cdeb5a82f854df070f5'


class Refused(RuntimeError):
    pass


def empty_target(path):
    root = Path(path)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise Refused('CANONICAL_STORE_REQUIRED')
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise Refused('OWNED_STORE_REQUIRED')
    if any(root.iterdir()):
        raise Refused('EMPTY_STORE_REQUIRED')
    return root


def seed_bytes():
    if SEED.is_symlink():
        raise Refused('PINNED_SEED_REQUIRED')
    raw = SEED.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_SEED:
        raise Refused('PINNED_SEED_REQUIRED')
    return raw


def commission(path, ledger):
    """Trusted local caller; tests supply FakeLedger, CLI constructs native HTTP client.

    Caller must have closed external ingress and stopped existing application
    processes. This command creates no listener and cannot verify deployment routing.
    """
    root = empty_target(path)
    raw = seed_bytes()
    sys.path.insert(0, str(SOURCE))
    # Deliberately never import api: its module body eagerly constructs a service.
    from service import ComplianceService, Config, SPEC_SEED_SHA256
    from store import DataDirLock, RecordLog, LOCK_NAME, LOG_NAME
    from ledger import Recorder
    if SPEC_SEED_SHA256 != EXPECTED_SEED:
        raise Refused('NATIVE_SEED_PIN_CHANGED')
    lock = DataDirLock(str(root))
    services = []
    try:
        if {p.name for p in root.iterdir()} != {LOCK_NAME}:
            raise Refused('STORE_CHANGED_BEFORE_CONSTRUCTION')
        proofs = []
        for existing in (False, True):
            token = lock.claim()
            try:
                svc = ComplianceService(Config(watcher_enabled=False), Recorder(ledger),
                                        RecordLog(str(root)), raw, expected_seed_sha256=EXPECTED_SEED,
                                        dir_lock=lock, lock_token=token,
                                        require_existing_seed=existing)
            except BaseException:
                lock.release_claim(token)
                raise
            services.append(svc)
            try:
                proposals = [p for p in svc.proposals.values() if p['kind'] == 'seed']
                assessment = svc.assess_log()
                if not svc.seed_pinned or len(proposals) != 1 or not len(svc.log) or assessment.problems or svc.reconcile_required or svc.versions:
                    raise Refused('NATIVE_SEED_NOT_CONFIRMED')
                proofs.append({'existing_store': existing, 'proposal_id': proposals[0]['proposal_id'],
                               'log_records': len(svc.log)})
            finally:
                svc.close()
            if not svc.closed or lock.claimed:
                raise Refused('NATIVE_CLOSE_NOT_CONFIRMED')
        if proofs[0]['proposal_id'] != proofs[1]['proposal_id']:
            raise Refused('RESTART_SEED_CHANGED')
        return {'state': 'COMMISSIONED_CLOSED', 'seed_sha256': EXPECTED_SEED,
                'passes': proofs, 'log_sha256': hashlib.sha256((root / LOG_NAME).read_bytes()).hexdigest(),
                'seed_approved': False, 'listener_started': False, 'scheduler_started': False}
    finally:
        for svc in services:
            svc.close()
        lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True, help='Existing, owned, canonical empty data directory')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--ledger-phase', choices=['legacy-pre-supervisor'],
                        help='Only supported before the Ledger supervisor cutover')
    parser.add_argument('--confirm-empty-store', action='store_true')
    parser.add_argument('--confirm-ingress-closed', action='store_true')
    args = parser.parse_args(argv)
    root = empty_target(args.store)
    seed_bytes()
    if not args.execute:
        print(json.dumps({'state': 'DRY_RUN', 'store': str(root), 'seed_sha256': EXPECTED_SEED,
                          'network': False, 'will_approve_seed': False}))
        return 0
    if args.ledger_phase != 'legacy-pre-supervisor' or not args.confirm_empty_store or not args.confirm_ingress_closed:
        raise Refused('EXPLICIT_COMMISSIONING_ACKNOWLEDGMENTS_REQUIRED')
    if os.environ.get('BACKUP_LAUNCH_ENABLED', '0') != '0':
        raise Refused('LEGACY_COMMISSIONING_ONLY')
    url = os.environ.get('LEDGER_SERVICE_URL')
    token = os.environ.get('LEDGER_SERVICE_TOKEN', '')
    if url != 'http://ledger.railway.internal:8090' or len(token) < 32 or not token.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in token) or '${{' in token:
        raise Refused('FIXED_INTERNAL_LEDGER_REQUIRED')
    sys.path.insert(0, str(SOURCE))
    from ledger import HttpLedgerClient
    result = commission(root, HttpLedgerClient(url, token))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('COMPLIANCE_COMMISSION_REFUSED; retain any created state and reconcile; do not wipe or retry.', file=sys.stderr)
        raise SystemExit(2)
