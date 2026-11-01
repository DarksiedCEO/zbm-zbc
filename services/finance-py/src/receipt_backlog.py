"""Bounded recorded-field inventory only (ADR 0040), never receipt/history validation or I/O."""
from __future__ import annotations

import base64
from datetime import date, datetime, timezone
import hashlib
import json
import re

from errors import Conflict, Invalid, Unavailable

MAX_RECEIPTS = 5000
MAX_ATTEMPTS = 2147483647
MAX_RESPONSE_BYTES = 262144
STATES = frozenset(('pending_send', 'sending', 'sent', 'withdrawn', 'withdrawn_after_send',
                    'delivery_uncertain', 'delivery_failed', 'delivery_reviewed'))
TIMES = ('created_at', 'claimed_at', 'last_attempt_at', 'legacy_sent_at', 'withdrawn_at', 'acceptance_recorded_at')
_ID = re.compile(r'fin-(crc|inv)-[0123456789ABCDEFGHJKMNPQRSTVWXYZ]{26}\Z')
_ISO = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)\Z')
_MISSING = object()


def _id(value, prefix):
    return type(value) is str and len(value) <= 128 and value.startswith('fin-' + prefix + '-') and bool(_ID.fullmatch(value))


def _time(value):
    if type(value) is not str or len(value) > 40 or not _ISO.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        return None


def _date(value):
    if type(value) is not str or len(value) != 10:
        return None
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _row(key, rec, envelope, ordinal):
    valid_key = _id(key, 'crc')
    # Invalid keys are never stringified, compared, hashed or copied. Ordinal is snapshot-local,
    # based on collection iteration order, and explicitly not an action identity.
    ref = 'record:' + hashlib.sha256(key.encode('ascii')).hexdigest() if valid_key else f'placeholder:{ordinal:04d}'
    issues = []
    if type(rec) is not dict:
        rec = {}
        issues.append('malformed_record')
    valid_identity = valid_key and _id(rec.get('client_receipt_id'), 'crc') and rec['client_receipt_id'] == key
    invoice = rec.get('invoice_id')
    if not valid_identity or not _id(invoice, 'inv'):
        issues.append('invalid_identity')
    status = rec.get('status')
    if type(status) is not str or len(status) > 32 or status not in STATES:
        status = 'unknown'
        issues.append('unknown_status')
    entity = rec.get('entity')
    if type(entity) is not str or len(entity) > 3 or entity not in ('zbm', 'zbc'):
        entity = 'unknown'
        issues.append('unknown_entity')
    attempts = rec.get('attempts')
    if type(attempts) is not int or attempts < 0 or attempts > MAX_ATTEMPTS:
        attempts = None
        issues.append('malformed_attempts')
    row = {'row_ref':ref, 'client_receipt_id':key if valid_identity else None,
           'invoice_id':invoice if valid_identity and _id(invoice, 'inv') else None,
           'entity':entity, 'status':status, 'attempts':attempts, 'paid_on':_date(rec.get('paid_on'))}
    if row['paid_on'] is None:
        issues.append('invalid_paid_on')
    for field in TIMES[:-1]:
        value = rec.get('sent_at' if field == 'legacy_sent_at' else field)
        row[field] = _time(value)
        if value is not None and row[field] is None:
            issues.append('invalid_' + field)
    if row['created_at'] is None:
        issues.append('missing_created_at' if rec.get('created_at') is None else 'invalid_created_at')
    review = rec.get('delivery_review', _MISSING)
    row['manual_delivery_review_recorded'] = False if review is _MISSING else True if type(review) is dict else 'unknown'
    if row['manual_delivery_review_recorded'] == 'unknown':
        issues.append('review_unknown')
    acceptance, recorded = 'unknown', None
    if not valid_identity:
        pass  # Skipped/unbound lookup cannot establish absence. Never inspect unsafe keys.
    elif envelope is _MISSING:
        acceptance = 'absent'
    elif type(envelope) is dict:
        schema = envelope.get('schema')
        request = envelope.get('request')
        if (type(schema) is str and len(schema) <= 32 and schema == 'receipt-resend-v1'
                and valid_identity and type(request) is dict
                and _id(request.get('client_receipt_id'), 'crc') and request['client_receipt_id'] == key):
            slot = envelope.get('acceptance', _MISSING)
            if slot is None:
                acceptance = 'absent'
            elif type(slot) is dict:
                recorded = _time(slot.get('accepted_at'))
                if recorded is not None:
                    acceptance = 'present'
    if acceptance == 'unknown':
        issues.append('acceptance_unknown')
    row.update(acceptance_record=acceptance, acceptance_recorded_at=recorded, issues=sorted(set(issues)))
    return row


def snapshot_rows(records, envelopes):
    """Caller holds its writer lock. Only bounded scalars escape; private nested fields stay untouched."""
    if type(records) is not dict or type(envelopes) is not dict:
        raise Unavailable('receipt_backlog_collection_unavailable')
    if len(records) > MAX_RECEIPTS:
        raise Unavailable('receipt_backlog_capacity')
    return [_row(key, rec, envelopes.get(key, _MISSING) if _id(key, 'crc') else _MISSING, ordinal)
            for ordinal, (key, rec) in enumerate(records.items())]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError('nonfinite')


def query(limit=None, cursor=None):
    """Strict decimal HTTP query strings, not FastAPI integer coercion."""
    selected = None
    if limit is not None:
        if type(limit) is not str or len(limit) > 3 or not re.fullmatch(r'[1-9][0-9]{0,2}', limit):
            raise Invalid('invalid_backlog_limit')
        selected = int(limit)
        if selected > 100:
            raise Invalid('invalid_backlog_limit')
    parsed = None
    if cursor is not None:
        try:
            if type(cursor) is not str or not 1 <= len(cursor) <= 256 or not re.fullmatch(r'[A-Za-z0-9_-]+', cursor):
                raise ValueError('shape')
            raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode('ascii')
            parsed = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
            if type(parsed) is not dict or set(parsed) != {'v', 'snapshot', 'offset', 'limit'}:
                raise ValueError('fields')
            if any(type(parsed[k]) is not int for k in ('v', 'offset', 'limit')):
                raise ValueError('types')
            if parsed['v'] != 1 or not 0 <= parsed['offset'] < MAX_RECEIPTS or not 1 <= parsed['limit'] <= 100:
                raise ValueError('bounds')
            digest = parsed['snapshot']
            if type(digest) is not str or len(digest) != 64 or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ValueError('digest')
            if _token(parsed) != cursor or (selected is not None and selected != parsed['limit']):
                raise ValueError('canonical')
            selected = parsed['limit']
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise Invalid('invalid_backlog_cursor') from None
    return 50 if selected is None else selected, parsed


def _token(value):
    return base64.urlsafe_b64encode(_canonical(value).encode('ascii')).decode('ascii').rstrip('=')


def page(rows, now, limit=None, cursor=None):
    selected, parsed = query(limit, cursor)
    ordered = sorted(rows, key=lambda row: (row['created_at'] is not None, datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.min.replace(tzinfo=timezone.utc), row['row_ref']))
    digest = hashlib.sha256(_canonical(ordered).encode('ascii')).hexdigest()
    start = 0
    if parsed is not None:
        if parsed['snapshot'] != digest:
            raise Conflict('receipt_backlog_snapshot_changed')
        start = parsed['offset']
        if start >= len(ordered):
            raise Invalid('invalid_backlog_cursor')
    as_of = None
    if type(now) is datetime:
        try:
            if now.tzinfo is not None and now.utcoffset() is not None:
                as_of = now.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            pass
    result_rows = []
    for original in ordered[start:start + selected]:
        row = {**original, 'issues':list(original['issues']), 'creation_age_seconds':None}
        if as_of is not None:
            for field in TIMES:
                if row[field] is not None and datetime.fromisoformat(row[field]) > as_of:
                    row['issues'].append('future_' + field)
            if row['created_at'] is not None:
                elapsed = (as_of - datetime.fromisoformat(row['created_at'])).total_seconds()
                if elapsed >= 0:
                    row['creation_age_seconds'] = int(elapsed)
        result_rows.append(row)
    end = start + len(result_rows)
    result = {'scope':'recorded_finance_client_receipts',
              'as_of':None if as_of is None else as_of.isoformat().replace('+00:00', 'Z'),
              'snapshot':digest, 'total':len(ordered), 'limit':selected,
              'next_cursor':_token({'v':1,'snapshot':digest,'offset':end,'limit':selected}) if end < len(ordered) else None,
              'rows':result_rows, 'issues':['clock_unknown'] if as_of is None else [],
              'external_request_intake_complete':False, 'contractual_deadline_evaluated':False,
              'delivery_verified':False, 'acceptance_record_validated':False}
    if len(_canonical(result).encode('ascii')) > MAX_RESPONSE_BYTES:
        raise Unavailable('receipt_backlog_response_capacity')
    return result
