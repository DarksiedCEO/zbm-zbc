"""Print the Stripe webhook events Finance subscribes to (the one list: src/stripe_webhooks.py).

Read-only helper for registering the endpoint (Stripe Dashboard, Developers -> Webhooks -> Add endpoint, or the
Stripe CLI). It makes no network call and reads no key.

    python3 services/finance-py/devtools/stripe_webhook_events.py            # one event per line
    python3 services/finance-py/devtools/stripe_webhook_events.py --csv      # comma-separated
    python3 services/finance-py/devtools/stripe_webhook_events.py --form     # enabled_events[]=... (API form fields)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from stripe_webhooks import WEBHOOK_EVENTS  # noqa: E402


def render(mode: str) -> str:
    if mode == "--csv":
        return ",".join(WEBHOOK_EVENTS)
    if mode == "--form":
        return "\n".join(f"enabled_events[]={e}" for e in WEBHOOK_EVENTS)
    return "\n".join(WEBHOOK_EVENTS)


if __name__ == "__main__":
    print(render(sys.argv[1] if len(sys.argv) > 1 else ""))
