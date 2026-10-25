#!/usr/bin/env python3
"""
Perplexity first-real-call smoke (ADR 0017 W4-6 step 5a): EXACTLY ONE bounded request to the real API, nothing else.

usage (Andre, on a machine with the key; never in CI):
    SEO_PERPLEXITY_API_KEY_FILE=/path/to/key python3 devtools/smoke_one_call.py --one-real-call
    (or SEO_PERPLEXITY_API_KEY=... on Railway's shell; set one of the two, not both)

What it does: builds the production adapter (src/perplexity.py: fixed host and path, the guarded deadline backend,
public-address check) with ONE attempt (no retry), a meter that allows ONE request, and the usual hard deadline
(SEO_PERPLEXITY_TIMEOUT_SECONDS, default 30), and asks one fixed generic question that carries no personal data.
The request is the service's own: the Agent API (``POST /v1/agent``, model ``perplexity/sonar``, effort ``fast``) by
default, or the legacy Sonar request when SEO_PERPLEXITY_PROTOCOL=sonar (the rollback; ADR 0017 W4-8).

What it prints (JSON, one object): the protocol and endpoint, the outcome (``ANSWER`` or the closed engine code and
detail), the model the API reported, the effort version (``model_version``, Agent API only), the finish reason (the
Agent API's ``status``), the answer's length and SHA-256 (never its text), how many citation URLs were parsed and
their first hosts, ``zero_sources`` and the NAMES of top-level response keys the parser does not model
(``unmodelled_keys``: a moved citations field shows up here), the usage and reported cost, and ``requests_sent``
(0 or 1). It never prints the key, and it touches
no ledger, no data directory and no budget: it is a protocol check before budgets are raised, not an audit.

Exit 0 when the API answered and the answer parsed; 1 for any engine code (ENGINE_PROTOCOL_ERROR means the response
shape changed: do NOT raise budgets, see ADR 0017 W4-1 and W4-8; the detail says what: ``MODEL_NOT_ALLOWED``,
``RESPONSE_<STATUS>``, ``NO_ANSWER_TEXT``, ``TOO_LARGE`` or none for a shape the parser refused); 2 for a usage or
configuration error.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FLAG = "--one-real-call"
PROMPT = "What is search engine optimization? Answer in two sentences."


def _key(env: dict):
    import perplexity
    from config import _secret_file_bytes
    path = (env.get("SEO_PERPLEXITY_API_KEY_FILE") or "").strip()
    plain = (env.get("SEO_PERPLEXITY_API_KEY") or "").strip()
    if bool(path) == bool(plain):
        raise RuntimeError("set exactly one of SEO_PERPLEXITY_API_KEY_FILE or SEO_PERPLEXITY_API_KEY")
    if path:
        try:
            value, source = _secret_file_bytes(env, "SEO_PERPLEXITY_API_KEY_FILE").decode("ascii", "strict"), "file"
        except UnicodeDecodeError:                        # its message would quote a byte of the file
            raise RuntimeError("SEO_PERPLEXITY_API_KEY_FILE must hold the key as printable ASCII") from None
    else:
        value, source = plain, "env"
    try:
        return perplexity.ApiKey(value, source)
    except ValueError:
        raise RuntimeError("the key must be 20..512 printable ASCII characters without spaces") from None


def _engine(env: dict):
    """The production adapter from the service's own settings rules: ``config._int`` and the closed model list, whose
    errors name the variable and never echo its value (a mistyped value could be the key: AEGIS round 2)."""
    import perplexity
    from config import _int
    timeout = _int(env, "SEO_PERPLEXITY_TIMEOUT_SECONDS", 30, 5, 120)
    protocol = (env.get("SEO_PERPLEXITY_PROTOCOL") or perplexity.DEFAULT_PROTOCOL).strip()
    if protocol not in perplexity.PROTOCOLS:
        raise RuntimeError(f"SEO_PERPLEXITY_PROTOCOL must be one of {', '.join(perplexity.PROTOCOLS)}")
    try:
        model = perplexity.resolve_model(protocol, env.get("SEO_PERPLEXITY_MODEL"))
    except ValueError as exc:                             # names the allowed values, never the setting
        raise RuntimeError(f"SEO_PERPLEXITY_MODEL {exc}") from None
    return perplexity.PerplexityEngine(_key(env), model=model, timeout_s=timeout, attempts=1, protocol=protocol)


def main(argv=None, env=None, engine_factory=None, out=None) -> int:
    import perplexity
    from agents.prompt_guard import personal_data
    from svc_engines import _Meter
    argv = sys.argv[1:] if argv is None else argv
    env = dict(os.environ) if env is None else env
    out = out or sys.stdout
    if argv != [FLAG]:
        print(f"refused: this makes one REAL, billed request; pass exactly {FLAG}", file=out)
        return 2
    assert personal_data(PROMPT) is None
    try:
        eng = (engine_factory or _engine)(env)
    except RuntimeError as exc:                           # our own messages: a variable name, never its value
        print(f"configuration error: {exc}"[:300], file=out)
        return 2
    except (ValueError, TypeError, UnicodeError) as exc:  # anything else: its type only, never its text
        print(f"configuration error: {type(exc).__name__}", file=out)
        return 2
    eng.attempts = 1                                      # one attempt, whatever the factory set
    meter = _Meter(1, Decimal("5"))                       # and the meter refuses a second request regardless
    protocol = getattr(eng, "protocol", perplexity.DEFAULT_PROTOCOL)
    report: dict = {"engine": perplexity.ENGINE, "protocol": protocol,
                    "endpoint": perplexity.BASE_URL + perplexity.PATHS.get(protocol, perplexity.PATH)}
    try:
        a = eng.ask(PROMPT, meter=meter)
    except perplexity.EngineError as e:
        report.update(outcome=e.code, detail=e.detail, requests_sent=meter.calls)
        print(json.dumps(report, sort_keys=True), file=out)
        return 1
    hosts = []
    for u in a.citations[:10]:
        try:
            hosts.append((urlsplit(u).hostname or "?")[:100])
        except ValueError:
            hosts.append("?")
    report.update(outcome=a.status, model=a.model, model_version=a.model_version, finish_reason=a.finish_reason, answer_chars=len(a.text),
                  answer_sha256=a.answer_sha256, citations=len(a.citations), first_hosts=hosts, usage=a.usage,
                  zero_sources=not a.citations, unmodelled_keys=list(getattr(a, "unmodelled_keys", ()) or ()),
                  requests_sent=meter.calls)
    print(json.dumps(report, sort_keys=True), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
