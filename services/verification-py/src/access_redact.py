"""
Access-log redaction (red team Oct 30-31 2026, RTA-4; fix wave A). Shared byte-for-byte by every Python service
launcher (devtools/hygiene_check.py L4 checks the copies).

uvicorn's access log wrote the full request target, query string included, for every request: subject ids
(``/compliance/v1/sanctions/status?subject_id=…``), party refs (``/legal/v1/obligations?party_ref=…``), and anything
a caller ever put in a URL (the red team's token-in-path probe landed in five services' logs). This filter rewrites
the target before it is formatted:

- the query string is dropped (``?…`` -> ``?<redacted>``);
- a path segment that is not a plain route word (lowercase letters, ``_``, ``-``; or a version like ``v1``) is
  replaced by ``:id``, so ids, hashes, percent-encoded text and anything token-shaped never reach the log;
- AEGIS F-15b: once a service registers its app (``register_routes(app)``), only the static words of its route
  templates are kept, so a plain lowercase word in an id position (``/acceptances/somesecretword``) is ``:id`` too.
  A service that registers nothing keeps the plain-word rule.

Headers (Authorization, caller and founder tokens) are never in uvicorn's access line; nothing here adds them.
"""

from __future__ import annotations

import copy
import logging
import re

_WORD = re.compile(r"[a-z][a-z_-]{0,47}|v[0-9]{1,2}")
_ROUTE_WORDS: frozenset = frozenset()


def register_routes(app) -> None:
    """Keeps only the static segments of ``app``'s route templates (``None`` clears; AEGIS F-15b)."""
    global _ROUTE_WORDS
    words = set()
    for route in getattr(app, "routes", None) or []:
        for seg in str(getattr(route, "path", "") or "").split("/"):
            if seg and "{" not in seg:
                words.add(seg)
    _ROUTE_WORDS = frozenset(words)


def _keep(seg: str) -> bool:
    return seg == "" or (bool(_WORD.fullmatch(seg)) and (not _ROUTE_WORDS or seg in _ROUTE_WORDS))


def redact_target(target: str) -> str:
    """``/fin/v1/invoices/inv-123/status?x=1`` -> ``/fin/v1/invoices/:id/status?<redacted>``."""
    path, sep, _ = str(target).partition("?")
    segs = [s if _keep(s) else ":id" for s in path.split("/")]
    return "/".join(segs)[:256] + ("?<redacted>" if sep else "")


class RedactAccessFilter(logging.Filter):
    """For the ``uvicorn.access`` logger: rewrites the request target in the record's args."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            record.args = args[:2] + (redact_target(args[2]),) + args[3:]
        return True


def log_config() -> dict:
    """uvicorn's default LOGGING_CONFIG with the redaction filter on ``uvicorn.access`` (pass as ``log_config``)."""
    from uvicorn.config import LOGGING_CONFIG
    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact_access"] = {"()": RedactAccessFilter}
    cfg["loggers"]["uvicorn.access"]["filters"] = ["redact_access"]
    return cfg


def install() -> None:
    """Also attach the filter directly (a launcher that does not pass ``log_config``)."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactAccessFilter) for f in logger.filters):
        logger.addFilter(RedactAccessFilter())
