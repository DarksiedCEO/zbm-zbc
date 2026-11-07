"""Offline operator capacity report from a fresh, names-and-byte-counts export.

No provider calls or file-content inspection. Supplied measurements are not
independently authenticated by this command. Nonzero exit blocks admission.
"""
import argparse
import datetime as dt
import json
from pathlib import Path

PROFILE = Path(__file__).with_name('capacity_profile.json')
MEMBERS = {'ledger', 'legal', 'compliance', 'finance', 'seo'}


def report(observation, now):
    if type(observation) is not dict or set(observation) != {'measured_at', 'stores'}:
        raise ValueError('MEASUREMENT_SCHEMA')
    stamp = dt.datetime.fromisoformat(observation['measured_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None or not 0 <= (now - stamp).total_seconds() <= 3600:
        raise ValueError('FRESH_MEASUREMENT_REQUIRED')
    stores = observation['stores']
    if type(stores) is not dict or set(stores) != MEMBERS:
        raise ValueError('ALL_FIVE_STORES_REQUIRED')
    caps = json.loads(PROFILE.read_text())['policy']['sources']
    rows = []
    for name in sorted(MEMBERS):
        item = stores[name]
        if type(item) is not dict or set(item) != {'source_bytes', 'one_client_growth_bytes'}:
            raise ValueError('STORE_MEASUREMENT_SCHEMA')
        size, growth = item['source_bytes'], item['one_client_growth_bytes']
        if type(size) is not int or size < 0 or (growth is not None and (type(growth) is not int or growth < 0)):
            raise ValueError('NONNEGATIVE_BYTE_COUNTS_REQUIRED')
        cap = caps[name]
        if size * 2 >= cap:
            status = 'HOLD_QUALIFY_LIMIT_BEFORE_MORE_GROWTH'
        elif growth is None:
            status = 'HOLD_CLIENT_GROWTH_UNKNOWN'
        elif (size + growth) * 2 >= cap:
            status = 'HOLD_NEXT_CLIENT_REACHES_50_PERCENT'
        else:
            status = 'BELOW_MANUAL_THRESHOLD'
        rows.append(dict(store=name, source_bytes=size, cap_bytes=cap,
                         percent=round(100 * size / cap, 2),
                         one_client_growth_bytes=growth, status=status))
    return dict(schema=1, measured_at=observation['measured_at'],
                measurement_origin='operator-supplied; verify custody at sitting',
                status='HOLD' if any(r['status'].startswith('HOLD') for r in rows) else 'CAPACITY_ONLY_PASS',
                stores=rows, production_changed=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('measurement', help='Private JSON byte-count export, no client content')
    args = p.parse_args()
    raw = Path(args.measurement).read_bytes()
    if len(raw) > 8192:
        raise ValueError('MEASUREMENT_BOUND')
    result = report(json.loads(raw), dt.datetime.now(dt.timezone.utc))
    print(json.dumps(result, indent=2))
    return 2 if result['status'] == 'HOLD' else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        print('{"status":"HOLD","category":"INVALID_OR_STALE_CAPACITY_MEASUREMENT"}')
        raise SystemExit(2)
