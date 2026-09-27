"""
Engine-owned path resolver, shipped read-only into every sandbox (round 19 R8; hash pinned in ``adapters/sandbox.py``).

Usage: ``python3 -I /mnt/dlv/resolve.py -- <absolute path>...``. For every operand it prints (NUL-separated, in order)
the real path of the LONGEST EXISTING PREFIX joined with the not-yet-existing remainder, or an empty field when the
operand is not absolute, contains ``..``, the existing prefix cannot be resolved (a dangling or looping symlink, a
permission error) or the remainder is not plain. One process per guardrail decision, whatever the operand count.
"""

import os
import sys


def resolve(path: str) -> str:
    if not path.startswith("/") or "\0" in path or ".." in path.split("/"):
        return ""
    p = os.path.normpath(path)
    rest: list[str] = []
    cur = p
    while cur and not os.path.lexists(cur):
        parent, name = os.path.split(cur)
        if not name or parent == cur:
            return ""
        rest.insert(0, name)
        cur = parent
    try:
        base = os.path.realpath(cur, strict=True)
    except (OSError, ValueError, RecursionError):
        return ""
    if not os.path.isdir(base) and rest:
        return ""                      # the remainder hangs off a file: nothing can be created there
    return os.path.normpath(os.path.join(base, *rest)) if rest else base


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    sys.stdout.write("\0".join(resolve(a) for a in args))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
