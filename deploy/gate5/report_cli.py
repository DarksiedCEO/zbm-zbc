"""Explicit, single-attempt operator delivery of an existing approved SEO export."""
import argparse
import getpass
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_delivery import Outbox, Refused, freeze, sha
from report_transport import Transport, decode, sender_valid


def read_report(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 8 * 1024 * 1024:
            raise Refused('REPORT_EXPORT_BOUND')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) != info.st_size:
            raise Refused('REPORT_EXPORT_CHANGED')
        return decode(raw)
    finally:
        os.close(fd)


def prepare(export, *, client, invoice, sender, recipient):
    # This is an operator-supplied existing render, not an authorization bypass
    # or a claim that this offline tool authenticated the source service.
    required = {'report_id', 'tenant_id', 'audit_id', 'format', 'media_type',
                'audit_report_sha256', 'document_sha256', 'bytes', 'content'}
    if type(export) is not dict or not required <= set(export):
        raise Refused('EXISTING_REPORT_EXPORT_REQUIRED')
    if export['format'] != 'html' or export['media_type'] != 'text/html; charset=utf-8':
        raise Refused('HTML_REPORT_REQUIRED')
    if type(export['content']) is not str or not sender_valid(sender):
        raise Refused('REPORT_OR_SENDER_REFUSED')
    report = export['content'].encode('utf-8')
    binding = {k: export[k] for k in ('report_id', 'tenant_id', 'audit_id',
                                      'audit_report_sha256', 'document_sha256')}
    binding.update(client_id=client, invoice_id=invoice, document_bytes=export['bytes'])
    key, body = freeze(binding, report, sender, recipient)
    preview = dict(identity=key, approval_sha256=sha(body), document_sha256=sha(report),
                   document_bytes=len(report), report_id=export['report_id'],
                   tenant_id=export['tenant_id'], audit_id=export['audit_id'],
                   client_id=client, invoice_id=invoice, sender=sender, recipient=recipient,
                   subject='Your SEO audit report', attachment_name='seo-report.html',
                   state='DRY_RUN', network=False)
    return binding, report, preview


def private_outbox(directory):
    root = Path(directory)
    if not root.is_absolute() or root.resolve() != root or root.is_symlink():
        raise Refused('ABSOLUTE_PRIVATE_OUTBOX_REQUIRED')
    root.mkdir(mode=0o700, exist_ok=True)
    parent_fd = os.open(root.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    st = root.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Refused('PRIVATE_OUTBOX_REQUIRED')
    path = root / 'reports.sqlite3'
    return Outbox(path) if path.exists() or path.is_symlink() else Outbox.create(path)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', required=True, help='Existing SEO HTML render JSON export')
    for field in ('client', 'invoice', 'sender', 'recipient'):
        p.add_argument('--' + field, required=True)
    p.add_argument('--outbox', help='Durable absolute private directory; reuse for every invocation')
    p.add_argument('--execute', action='store_true', help='Explicitly send once; default is offline')
    p.add_argument('--approve-sha256', help='Exact dry-run approval digest, after reviewing every field')
    a = p.parse_args(argv)
    binding, report, preview = prepare(read_report(a.report), client=a.client, invoice=a.invoice,
                                       sender=a.sender, recipient=a.recipient)
    if not a.execute:
        print(json.dumps(preview, sort_keys=True))
        return 0
    if a.approve_sha256 != preview['approval_sha256'] or not a.outbox:
        raise Refused('EXPLICIT_EXACT_APPROVAL_REQUIRED')
    if not sys.stdin.isatty():
        raise Refused('HIDDEN_TERMINAL_CREDENTIAL_REQUIRED')
    # Validate before enqueue/effect; key is never an argument, file, or output.
    credential = getpass.getpass('Dedicated report Resend sending key: ')
    transport = Transport(credential, a.sender)
    credential = None
    outbox = private_outbox(a.outbox)
    try:
        key = outbox.enqueue(binding, report, sender=a.sender, recipient=a.recipient)
        outcome = outbox.dispatch(key, transport)
    finally:
        outbox.close()
    print(json.dumps(dict(identity=key, **outcome), sort_keys=True))
    return 0 if outcome['status'] == 'ACCEPTED' else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('REPORT_DELIVERY_REFUSED_OR_UNKNOWN; retain outbox and reconcile before any retry.', file=sys.stderr)
        raise SystemExit(2)
