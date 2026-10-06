"""
A NON-PRODUCTION SMS sender for the live run (AEGIS round 2, V2-L4): it "sends" by appending one JSON line per
message to ``SVC_NONPROD_OUTBOX_FILE``, so ``outbound-tick`` can be exercised end to end (``message_sending`` on the
ledger, the provider call, ``message_sent`` on the ledger) through the production entrypoint. config.py allows it
only with ``SVC_NON_PRODUCTION=1`` and ``SVC_SMS_PROVIDER=nonprod_file``; it never reaches anyone.
"""

from __future__ import annotations

import json
import os

from ports import Outbound


class NonProdFileSender:
    wired = True

    def __init__(self, path: str):
        self.path = path

    def send(self, msg: Outbound) -> str:
        line = json.dumps({"message_id": msg.message_id, "brand": msg.brand, "channel": msg.channel, "to": msg.to,
                           "sender": msg.sender, "text": msg.text}, sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return "sent"
