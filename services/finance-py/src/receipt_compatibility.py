"""Receipt history capability checks. Local snapshots NEVER authorize deployment.

The legacy image cannot execute this guard. External rollback enforcement remains
required; no flag, marker, or successful preflight changes that fact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Iterable

from store import LOG_NAME, DataDirLock, verify_lines

LEGACY_SOURCE = '8e632340b70f32389a4fa3149023b0ed68dbe7c0'
RECEIPT_SOURCE = '774e46b2ca5fc9b5758eaba909450156b27d88c9'
# Fixed source capabilities, not user assertions and not image attestations.
TARGETS = (LEGACY_SOURCE, RECEIPT_SOURCE)
# Snapshot of the supported collection vocabulary at RECEIPT_SOURCE. Deliberately
# not imported from a future runtime: profile capabilities must not drift silently.
KNOWN_COLLECTIONS = frozenset(('rc_proposals', 'rate_cards', 'profiles', 'client_profiles', 'payees',
    'callbacks', 'tax', 'handoffs', 'payables', 'batches', 'items', 'exceptions', 'recon_runs', 'breaks',
    'invoices', 'receipts', 'disputes', 'refunds', 'treasury_ops', 'controls', 'close', 'locks',
    'tax_readiness', 'job_runs', 'rail_events', 'integrity_checks', 'clawbacks', 'offboarding',
    'shortfalls', 'media_buys', 'client_receipts', 'client_contacts', 'receipt_envelopes',
    'stripe_sessions', 'stripe_events', 'stripe_disputes', 'stripe_payouts'))
KNOWN_OPS = frozenset(('put', 'journal', 'integrity_finding', 'idem', 'proposal', 'decision',
                       'lease', 'reconcile', 'founder_refused', 'injection', 'audit', 'evidence'))
OLD_STATES = frozenset(('pending_send', 'sending', 'sent', 'withdrawn', 'withdrawn_after_send'))
NEW_STATES = frozenset(('delivery_uncertain', 'delivery_failed', 'delivery_reviewed'))
RECEIPT_FIELDS = frozenset(('client_receipt_id', 'receipt_id', 'entity', 'client_id', 'invoice_id',
    'invoice_kind', 'what_you_bought', 'items', 'period', 'amount_paid', 'currency', 'paid_on', 'method',
    'status', 'attempts', 'created_at', 'sent_at', 'withdrawn_at', 'claimed_at', 'claimed_by',
    'last_attempt_at', 'delivery_review'))
CONTACT_FIELDS = frozenset(('email', 'revision'))
ENVELOPE_FIELDS = frozenset(('envelope', 'contact_revision', 'first_attempt_at', 'retry_until'))
MESSAGE_FIELDS = frozenset(('to', 'subject', 'text', 'idempotency_key'))
MAX_LOG_BYTES = 256 * 1024 * 1024


def assess(records: Iterable[dict], target: str) -> tuple[str, ...]:
    """Inspect complete history, including superseded terminal states. No PII output."""
    if target not in TARGETS:
        return ('unknown_target',)
    reasons = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('data'), dict):
            return ('invalid_history',)
        ops = record['data'].get('ops', [])
        if not isinstance(ops, list):
            return ('invalid_history',)
        for op in ops:
            if not isinstance(op, (list, tuple)) or len(op) != 2 or not isinstance(op[0], str) or \
                    not isinstance(op[1], dict):
                return ('invalid_history',)
            kind, value = op
            if kind not in KNOWN_OPS:
                reasons.add('unknown_operation')
            if kind != 'put':
                continue
            coll = value.get('coll')
            rec = value.get('rec')
            if not isinstance(coll, str) or not isinstance(rec, dict):
                return ('invalid_history',)
            if coll not in KNOWN_COLLECTIONS:
                reasons.add('unknown_collection')
            if coll in ('client_contacts', 'receipt_envelopes') and target == LEGACY_SOURCE:
                reasons.add('receipt_collections_unsupported')
            if coll == 'client_contacts' and set(rec) - CONTACT_FIELDS:
                reasons.add('unknown_receipt_semantics')
            if coll == 'receipt_envelopes' and (set(rec) - ENVELOPE_FIELDS or
                    not isinstance(rec.get('envelope'), dict) or set(rec['envelope']) - MESSAGE_FIELDS):
                reasons.add('unknown_receipt_semantics')
            if coll == 'client_receipts':
                if set(rec) - RECEIPT_FIELDS:
                    reasons.add('unknown_receipt_semantics')
                status = rec.get('status')
                if not isinstance(status, str) or status not in OLD_STATES | NEW_STATES:
                    reasons.add('unknown_receipt_semantics')
                elif status in NEW_STATES and target == LEGACY_SOURCE:
                    reasons.add('receipt_states_unsupported')
    return tuple(sorted(reasons))


def assess_current(records):
    """Current runtime semantics, distinct from immutable historical target profiles."""
    from copy import deepcopy
    from receipt_resend_adapter import SCHEMA, validate_saved, same_request
    reasons = set()
    previous = {}
    receipts = {}
    contacts = {}
    for record in records:
        cleaned = deepcopy(record)
        touched = set()
        if not isinstance(record, dict) or not isinstance(record.get('data'), dict):
            return ('invalid_history',)
        ops = record['data'].get('ops', [])
        if not isinstance(ops, list):
            return ('invalid_history',)
        for index, op in enumerate(ops):
            if not isinstance(op, (list, tuple)) or len(op) != 2 or not isinstance(op[1], dict):
                return ('invalid_history',)
            kind, value = op
            if kind == 'put' and value.get('coll') in ('client_receipts', 'client_contacts') and (
                    type(value.get('id')) is not str or type(value.get('rec')) is not dict):
                return ('invalid_history',)
            if kind == 'put' and value.get('coll') == 'client_receipts':
                receipts[value.get('id')] = value.get('rec')
                touched.add(value.get('id'))
            if kind == 'put' and value.get('coll') == 'client_contacts':
                contacts[value.get('id')] = value.get('rec')
            if kind != 'put' or value.get('coll') != 'receipt_envelopes':
                continue
            rec, key = value.get('rec'), value.get('id')
            if not isinstance(rec, dict) or type(key) is not str:
                return ('invalid_history',)
            old = previous.get(key)
            if rec.get('schema') == SCHEMA:
                if not validate_saved(rec, key) or old is not None and (old.get('schema') != SCHEMA or
                        not same_request(old, rec) or old['acceptance'] is not None and old['acceptance'] != rec['acceptance']):
                    reasons.add('unknown_receipt_semantics')
                if old is None:
                    receipt = receipts.get(key)
                    try:
                        import json
                        body = json.loads(rec['request']['body'])
                        view = {k: receipt[k] for k in ('client_receipt_id', 'entity', 'invoice_id', 'what_you_bought',
                            'items', 'period', 'amount_paid', 'currency', 'paid_on', 'method')}
                        contact = contacts[receipt['client_id']]
                        expected = 'Payment receipt\n' + json.dumps(view, ensure_ascii=True, sort_keys=True)
                        if rec['acceptance'] is not None or receipt['entity'] != 'zbm' or body['text'] != expected or \
                                body['to'] != [contact['email']] or rec['contact_revision'] != contact['revision']:
                            reasons.add('unknown_receipt_semantics')
                    except (KeyError, TypeError, ValueError):
                        reasons.add('unknown_receipt_semantics')
                # Only validated new vocabulary is projected for the frozen older evaluator.
                cleaned['data']['ops'][index] = ['evidence', {}]
            elif old is not None and old.get('schema') == SCHEMA:
                reasons.add('unknown_receipt_semantics')
            previous[key] = deepcopy(rec)
            touched.add(key)
        for key in touched:
            envelope = previous.get(key) or {}
            if envelope.get('schema') == SCHEMA:
                receipt = receipts.get(key) or {}
                if receipt.get('entity') != 'zbm' or receipt.get('client_receipt_id') != key:
                    reasons.add('unknown_receipt_semantics')
                if receipt.get('status') in ('sent', 'withdrawn_after_send') and envelope.get('acceptance') is None:
                    reasons.add('unknown_receipt_semantics')
                if envelope.get('acceptance') is not None and receipt.get('status') not in (
                        'sent', 'withdrawn_after_send', 'delivery_uncertain', 'delivery_failed', 'delivery_reviewed'):
                    reasons.add('unknown_receipt_semantics')
        reasons.update(assess([cleaned], RECEIPT_SOURCE))
    return tuple(sorted(reasons))


def require_current(records: Iterable[dict]) -> None:
    """Called under the service's existing ownership claim, before replay/writes."""
    if assess_current(records):
        raise RuntimeError('receipt_compatibility_refused')


def _read_history(data_dir: str) -> tuple[list[dict], str]:
    # Read one opened regular inode, never follow a journal symlink or block on FIFO.
    fd = os.open(os.path.join(data_dir, LOG_NAME), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_LOG_BYTES:
            raise ValueError('history_unavailable')
        chunks = []
        remaining = MAX_LOG_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        if len(raw) > MAX_LOG_BYTES or (raw and not raw.endswith(b'\n')):
            raise ValueError('history_unavailable')
        records = verify_lines(raw.split(b'\n')[:-1] if raw else [])
        return records, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def preflight(data_dir: str, target: str) -> dict:
    """Stopped-volume diagnostic. Lock is held through scan, released on return.

    Output expires immediately on release: it is not a scan-to-launch permit.
    This checks local hash-chain structure, NOT remote ledger truth or an image.
    """
    result = {'target_source_sha': target if target in TARGETS else None,
              'snapshot_only': True, 'artifact_verified': False, 'deployment_authorized': False,
              'ledger_verified': False, 'reasons': []}
    if target not in TARGETS:
        result['reasons'] = ['unknown_target']
        return result
    lock = None
    try:
        if not os.path.isdir(data_dir) or os.path.islink(data_dir):
            raise ValueError('directory_unavailable')
        lock = DataDirLock(data_dir)
        token = lock.claim()
    except Exception:
        if lock is not None:
            lock.release()
        result['reasons'] = ['directory_unavailable']
        return result
    try:
        try:
            records, digest = _read_history(data_dir)
            result['reasons'] = list(assess(records, target))
            result['journal_sha256'] = digest
        except Exception:
            result['reasons'] = ['history_unavailable']
        return result
    finally:
        lock.release_claim(token)
        lock.release()


class _Arguments(argparse.ArgumentParser):
    def error(self, message):
        # Never echo arbitrary command-line values into diagnostics.
        raise ValueError('invalid_arguments')


def main() -> int:
    parser = _Arguments(add_help=False, description='Local snapshot only; never authorizes a deployment or rollback.')
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--target', required=True)
    try:
        args = parser.parse_args()
    except ValueError:
        print(json.dumps({'reasons': ['invalid_arguments'], 'deployment_authorized': False,
                          'artifact_verified': False, 'ledger_verified': False, 'snapshot_only': True}))
        return 2
    result = preflight(args.data_dir, args.target)
    print(json.dumps(result, sort_keys=True))
    return 2 if result['reasons'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
