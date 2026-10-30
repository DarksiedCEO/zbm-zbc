"""Build and check the ledger's LEDGER_CALLERS value (docs/deploy/railway.md, "LEDGER_CALLERS"). Standard library only;
Andre runs it on his own machine from a checkout. It never prints a token, and never prints a hash except as the value
it writes with --out (or to stdout without --out).

    python3 devtools/ledger_callers.py new                     --out ~/zbm/ledger-callers.json
    python3 devtools/ledger_callers.py add CALLER    --current ~/zbm/ledger-callers.json --out ~/zbm/ledger-callers.next.json
    python3 devtools/ledger_callers.py rotate CALLER --current ~/zbm/ledger-callers.json --out ~/zbm/ledger-callers.next.json
    python3 devtools/ledger_callers.py check         --current ~/zbm/ledger-callers.json

The value maps the lowercase hex SHA-256 of each caller's token to its grant (services/ledger-rust/README.md). It
holds hashes only, so `add` and `rotate` need the current value and the ONE new token: the other callers' tokens
(Sealed in Railway, not readable back) are never asked for, and their entries are copied, not re-typed.

CALLERS below is the one place that sets each deployed caller's ledger rights (AEGIS detection-branch M-2, M-4): every
caller writes exactly one department, and nothing reads more (no read_departments, no read_all). A current value whose
entries differ from it in any way is refused, as is anything the ledger itself would refuse at start (load_callers in
services/ledger-rust/src/bin/server.rs): a key that is not 64 lowercase hex characters, a repeated key, an unknown
field. On top of the ledger's rules it refuses an empty or short token (under the ledger's 32 bytes), a token with
whitespace or a non-printable character, the same token for two callers, a caller name not in CALLERS (so a typo
cannot cut a department off or invent one), the same caller listed twice, and a token typed differently the second
time it is asked for.

Each command prints a summary to stderr: one line per caller with what it writes and what it reads. No token, no hash.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
from typing import Callable, Optional

# caller -> (the department it writes, where its token lives). Write scope, one department, reads only that one.
CALLERS: dict[str, tuple[str, str]] = {
    "orchestrator-go": ("revenue_recovery", "LEDGER_SERVICE_TOKEN on orchestrator"),
    "finance-py": ("finance", "LEDGER_SERVICE_TOKEN on finance"),
    "legal-py": ("legal", "LEDGER_SERVICE_TOKEN on legal"),
    "seo-py": ("seo", "LEDGER_SERVICE_TOKEN on seo"),
    "detection-py": ("rr_store", "DETECTION_LEDGER_TOKEN on detection"),
}
KNOWN_DEPARTMENTS = frozenset(d for d, _where in CALLERS.values())
MIN_TOKEN_BYTES = 32                       # ledger-rust MIN_TOKEN_BYTES: a shorter configured token is refused (401)
MAX_CALLERS_FILE_BYTES = 1024 * 1024       # ledger-rust MAX_CALLERS_FILE_BYTES
LEDGER_FIELDS = {"caller", "departments", "scope", "read_departments", "read_all"}   # serde(deny_unknown_fields)
HASH = re.compile(r"[0-9a-f]{64}")
SLUG = re.compile(r"[a-z0-9_]{1,64}")


class Refused(Exception):
    """A value or token this script will not produce or accept. The message never holds a token or a hash."""


def entry(caller: str) -> dict:
    """The exact grant CALLERS gives `caller`."""
    department, _where = CALLERS[caller]
    return {"caller": caller, "departments": [department], "scope": "write"}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_token(caller: str, token: str) -> None:
    if not token or not token.strip():
        raise Refused(f"{caller}: the token is empty")
    if any(not (0x21 <= ord(c) <= 0x7E) for c in token):
        raise Refused(f"{caller}: the token has whitespace or a non-printable character (paste it exactly, nothing "
                      "around it)")
    if len(token.encode("utf-8")) < MIN_TOKEN_BYTES:
        raise Refused(f"{caller}: the token is {len(token)} characters; the ledger refuses a token under "
                      f"{MIN_TOKEN_BYTES} bytes (generate one with `openssl rand -hex 32`)")


def known_caller(caller: str) -> str:
    if caller not in CALLERS:
        raise Refused(f"unknown caller {caller!r}; the callers are: {', '.join(CALLERS)} (a new one is added to CALLERS "
                      "in devtools/ledger_callers.py, reviewed, first)")
    return caller


def _no_duplicate_keys(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise Refused("the current value lists the same key twice (the ledger refuses this at start)")
        out[k] = v
    return out


def parse_current(text: str) -> list[tuple[str, dict]]:
    """The current LEDGER_CALLERS value as (hash, entry) pairs in order, refused unless the ledger would load it AND
    every entry is exactly its caller's grant in CALLERS."""
    if len(text.encode("utf-8")) > MAX_CALLERS_FILE_BYTES:
        raise Refused(f"the current value is larger than {MAX_CALLERS_FILE_BYTES} bytes")
    try:
        raw = json.loads(text, object_pairs_hook=lambda pairs: pairs)
    except ValueError as e:
        raise Refused(f"the current value is not JSON ({e.msg}, line {e.lineno} column {e.colno})") from None
    if not isinstance(raw, list) or (raw and not isinstance(raw[0], tuple)):
        raise Refused("the current value is not a JSON object of callers")
    if not raw:
        raise Refused("the current value names no callers (the ledger refuses this at start)")
    out, seen_hashes, seen_callers = [], set(), set()
    for n, (key, value) in enumerate(raw, 1):
        if not isinstance(key, str) or not HASH.fullmatch(key):
            raise Refused(f"entry {n}: its key is not the lowercase hex SHA-256 of a token (64 characters)")
        if key in seen_hashes:
            raise Refused(f"entry {n}: the same key is listed twice (the ledger refuses this at start)")
        if not isinstance(value, list) or not all(isinstance(p, tuple) for p in value):
            raise Refused(f"entry {n}: not an object")
        fields = _no_duplicate_keys(value)
        unknown = sorted(set(fields) - LEDGER_FIELDS)
        if unknown:
            raise Refused(f"entry {n}: unknown field(s) {unknown} (the ledger refuses this at start)")
        caller = fields.get("caller")
        if not isinstance(caller, str):
            raise Refused(f"entry {n}: no caller name")
        if caller not in CALLERS:
            raise Refused(f"entry {n}: unknown caller {caller!r}; the callers are: {', '.join(CALLERS)}")
        if caller in seen_callers:
            raise Refused(f"entry {n}: caller {caller} is listed twice (each caller has one token)")
        for list_field in ("departments", "read_departments"):
            if list_field in fields and not isinstance(fields[list_field], list):
                raise Refused(f"entry {n} ({caller}): {list_field} is not a list")
        for d in list(fields.get("departments") or []) + list(fields.get("read_departments") or []):
            if not isinstance(d, str) or not SLUG.fullmatch(d):
                raise Refused(f"entry {n} ({caller}): a department is not [a-z0-9_]{{1,64}}")
            if d not in KNOWN_DEPARTMENTS:
                raise Refused(f"entry {n} ({caller}): unknown department {d!r}")
        if fields != entry(caller):
            raise Refused(f"entry {n} ({caller}): its grant is not the one devtools/ledger_callers.py gives it "
                          f"({describe(entry(caller))}); found {describe(fields)}")
        seen_hashes.add(key)
        seen_callers.add(caller)
        out.append((key, dict(fields)))
    return out


def describe(e: dict) -> str:
    scope = e.get("scope")
    deps = ",".join(e.get("departments") or []) or "nothing"
    if e.get("read_all"):
        reads = "EVERY department (read_all)"
    else:
        extra = list(e.get("read_departments") or [])
        reads = ",".join(list(e.get("departments") or []) + extra) or "nothing"
        reads += " only" if not extra else f" (incl. read_departments {','.join(extra)})"
    return f"{scope} {deps}; reads {reads}"


def summary(pairs: list[tuple[str, dict]]) -> str:
    lines = [f"  {e['caller']:<16} {describe(e)}" for _h, e in pairs]
    present = {e["caller"] for _h, e in pairs}
    missing = [c for c in CALLERS if c not in present]
    lines.append(f"  {len(pairs)} caller(s)" + (f"; not in this value: {', '.join(missing)}" if missing else
                                                 "; every caller in devtools/ledger_callers.py is present"))
    return "\n".join(lines)


def ask_token(caller: str, prompt: Callable[[str], str]) -> str:
    where = CALLERS[caller][1]
    token = prompt(f"{caller}: paste the {where} (not shown): ")
    check_token(caller, token)
    if prompt(f"{caller}: paste it again to confirm: ") != token:
        raise Refused(f"{caller}: the two entries differ; nothing was written")
    return token


def _refuse_duplicate(new_hash: str, caller: str, pairs: list[tuple[str, dict]]) -> None:
    for h, e in pairs:
        if h == new_hash:
            raise Refused(f"{caller}: this token is already {e['caller']}'s; every caller needs its own token")


def build_new(prompt: Callable[[str], str]) -> list[tuple[str, dict]]:
    pairs: list[tuple[str, dict]] = []
    for caller in CALLERS:
        h = token_hash(ask_token(caller, prompt))
        _refuse_duplicate(h, caller, pairs)
        pairs.append((h, entry(caller)))
    return pairs


def build_add(current: list[tuple[str, dict]], caller: str, prompt: Callable[[str], str]) -> list[tuple[str, dict]]:
    known_caller(caller)
    if any(e["caller"] == caller for _h, e in current):
        raise Refused(f"{caller} is already in the current value; use `rotate {caller}` to give it a new token")
    token = ask_token(caller, prompt)
    if token in {h for h, _e in current}:
        raise Refused(f"{caller}: that is a hash from the current value, not a token")
    h = token_hash(token)
    _refuse_duplicate(h, caller, current)
    return current + [(h, entry(caller))]


def build_rotate(current: list[tuple[str, dict]], caller: str, prompt: Callable[[str], str]) -> list[tuple[str, dict]]:
    known_caller(caller)
    if not any(e["caller"] == caller for _h, e in current):
        raise Refused(f"{caller} is not in the current value; use `add {caller}`")
    token = ask_token(caller, prompt)
    if token in {h for h, _e in current}:
        raise Refused(f"{caller}: that is a hash from the current value, not a token")
    h = token_hash(token)
    for old, e in current:
        if old == h:
            raise Refused(f"{caller}: this is the token it already has" if e["caller"] == caller else
                          f"{caller}: this token is already {e['caller']}'s; every caller needs its own token")
    return [((h, entry(caller)) if e["caller"] == caller else (old, e)) for old, e in current]


def render(pairs: list[tuple[str, dict]]) -> str:
    return json.dumps(dict(pairs))


def write_out(path: str, text: str, current_path: Optional[str]) -> None:
    if current_path and os.path.abspath(os.path.expanduser(path)) == os.path.abspath(os.path.expanduser(current_path)):
        raise Refused("--out is the --current file; write the new value to a new file and keep the current one for "
                      "rollback")
    try:
        fd = os.open(os.path.expanduser(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise Refused(f"--out {path} already exists; nothing was written (choose a new name)") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text + "\n")


def main(argv: Optional[list[str]] = None, prompt: Callable[[str], str] = getpass.getpass,
         stdout=None, stderr=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    ap = argparse.ArgumentParser(prog="ledger_callers.py", description=__doc__.split("\n\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new", help="a value for every caller (asks for every token)")
    p_new.add_argument("--out")
    for name, helptext in (("add", "add one caller to the current value (asks for its token only)"),
                           ("rotate", "give one caller of the current value a new token (asks for it only)")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("caller")
        p.add_argument("--current", required=True, help="a file holding the current LEDGER_CALLERS value")
        p.add_argument("--out")
    p_check = sub.add_parser("check", help="check a value and print its summary")
    p_check.add_argument("--current", required=True)
    args = ap.parse_args(argv)
    try:
        current = None
        if args.cmd != "new":
            try:
                with open(os.path.expanduser(args.current), encoding="utf-8") as f:
                    current = parse_current(f.read())
            except OSError as e:
                raise Refused(f"--current {args.current}: {e.strerror}") from None
        if args.cmd == "check":
            print("LEDGER_CALLERS is valid:\n" + summary(current), file=stderr)
            return 0
        if args.cmd == "new":
            pairs = build_new(prompt)
        elif args.cmd == "add":
            pairs = build_add(current, args.caller, prompt)
        else:
            pairs = build_rotate(current, args.caller, prompt)
        parse_current(render(pairs))          # what is written passes the same check as what is read
        if args.out:
            write_out(args.out, render(pairs), getattr(args, "current", None))
            print(f"wrote the new LEDGER_CALLERS value to {args.out} (mode 0600):\n" + summary(pairs), file=stderr)
        else:
            print(render(pairs), file=stdout)
            print("LEDGER_CALLERS value above:\n" + summary(pairs), file=stderr)
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
