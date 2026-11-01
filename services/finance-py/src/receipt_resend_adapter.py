"""Explicit ZBM receipt bridge. No configuration lookup, retries, or I/O during preparation."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json

from clock import iso, parse_iso
from ports import ReceiptDeliveryOutcome
from resend_receipts import (FrozenResendRequest, ResendReceiptTransport, ResendTransportProfile,
                             validate_request, valid_message_id)

SCHEMA = 'receipt-resend-v1'
REQUEST_FIELDS = frozenset(('version', 'provider', 'profile_id', 'profile_revision', 'sender', 'entity',
    'client_receipt_id', 'body', 'body_sha256', 'idempotency_key'))
SAVED_FIELDS = frozenset(('schema', 'request', 'contact_revision', 'first_attempt_at', 'retry_until', 'acceptance'))
profile = ResendTransportProfile


def frozen(saved):
    r = saved['request']
    return FrozenResendRequest(r['version'], r['provider'], r['profile_id'], r['profile_revision'],
        r['body'].encode(), r['body_sha256'], r['idempotency_key'], parse_iso(saved['first_attempt_at']),
        parse_iso(saved['retry_until']))


def validate_saved(saved, crid=None):
    try:
        if type(saved) is not dict or set(saved) != SAVED_FIELDS or saved['schema'] != SCHEMA:
            return False
        r = saved['request']
        if type(r) is not dict or set(r) != REQUEST_FIELDS or any(type(v) is not str for v in r.values()):
            return False
        if r['entity'] != 'zbm' or not 0 < len(r['client_receipt_id']) <= 128 or (crid is not None and r['client_receipt_id'] != crid):
            return False
        if type(saved['contact_revision']) is not str or not 0 < len(saved['contact_revision']) <= 128:
            return False
        request = frozen(saved)
        if not validate_request(profile(r['profile_id'], r['profile_revision'], r['sender']), request, request.first_attempt_at):
            return False
        result = saved['acceptance']
        if result is not None and (type(result) is not dict or set(result) != {'provider_message_id', 'accepted_at'}
                or not valid_message_id(result['provider_message_id']) or parse_iso(result['accepted_at']) < request.first_attempt_at):
            return False
        return True
    except Exception:
        return False


def same_request(before, after):
    return {k:v for k,v in before.items() if k != 'acceptance'} == {k:v for k,v in after.items() if k != 'acceptance'}


@dataclass(frozen=True, repr=False)
class ResendReceiptMail:
    profile: ResendTransportProfile
    credential: str
    deadline_seconds: int = 10
    enabled = True

    def prepare(self, rec, contact, text, key, now):
        if rec['entity'] != 'zbm':
            raise ValueError('entity_disabled')
        body = json.dumps({'from': self.profile.sender, 'to': [contact['email']],
            'subject': 'Your payment receipt', 'text': text}, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        saved = {'schema': SCHEMA, 'contact_revision': contact['revision'], 'first_attempt_at': iso(now),
            'retry_until': iso(now + timedelta(hours=23)), 'acceptance': None,
            'request': {'version':'resend-receipt-v1', 'provider':'resend', 'profile_id':self.profile.profile_id,
                'profile_revision':self.profile.revision, 'sender':self.profile.sender, 'entity':'zbm',
                'client_receipt_id':rec['client_receipt_id'], 'body':body,
                'body_sha256':hashlib.sha256(body.encode()).hexdigest(), 'idempotency_key':key}}
        if not validate_saved(saved, rec['client_receipt_id']):
            raise ValueError('invalid_envelope')
        return saved

    def matches(self, saved):
        return validate_saved(saved) and (saved['request']['profile_id'], saved['request']['profile_revision'],
            saved['request']['sender']) == (self.profile.profile_id, self.profile.revision, self.profile.sender)

    def deliver_receipt(self, saved):
        if not self.matches(saved):
            return ReceiptDeliveryOutcome('uncertain')
        return ResendReceiptTransport(self.profile, self.credential, self.deadline_seconds).send_frozen(
            frozen(saved), datetime.now(timezone.utc))
