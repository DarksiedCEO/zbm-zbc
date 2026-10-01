"""
The complete source diff of a run (fix wave 24, E2 — AEGIS N23-D-1/N23-D-4).

The lead's principle: any list of suspicious constructs is a spelling list, so the gate that carries weight is a
reviewer who attests — bound by hash — to having read the entire source diff. ``src_diff`` is that diff: every path the
run changed between its base and its last commit that ``path_class`` (the one classification the engine and the
standalone runner share) does not call test or test infrastructure — a path outside the service directory included —
with rename detection off (a moved or copied file is a full addition, E3). Its sha256 is the run's
``src_diff_sha256``; the report embeds the text in full, and a review that accepts any finding must carry the hash.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from zbm_delivery.runner import detect_framework, path_class


def framework_at(git, test_seed: dict, service: str, sha: str, run_id: str = "-") -> Optional[dict]:
    """The seeded framework of ``services/<service>`` at commit ``sha`` (marker files read from git), or None."""
    name = detect_framework(test_seed, lambda rel: git.blob_exists(sha, f"services/{service}/{rel}", run_id=run_id))
    return test_seed["frameworks"][name] if name else None


def src_paths(names: list[str], fw: dict, service: str) -> list[str]:
    prefix = f"services/{service}/"
    tg, ig = fw.get("test_file_globs", []), fw.get("test_infra_globs", [])
    return sorted(p for p in set(names)
                  if not p.startswith(prefix) or path_class(p[len(prefix):], tg, ig) == "src")


def src_diff(git, fw: dict, service: str, cwd: str, base: str, head: str, run_id: str = "-") -> tuple[str, list[str]]:
    """(diff text, the source paths in it) for ``base..head``."""
    names = git.diff_name_only(cwd, run_id=run_id, commit=head, base=base) if head != base else []
    paths = src_paths(names, fw, service)
    return git.range_diff(cwd, base, head, paths, run_id=run_id), paths


def record(svc, run_id: str, text: str, paths: list[str], base: str, head: str, event_type: str) -> tuple[str, str, Optional[str]]:
    """Store the diff as evidence and return (evidence id, src_diff_sha256, problem). The hash is over the evidence
    EXACTLY as stored and embedded in the report (evidence is redacted of secret shapes like all evidence): what the
    reviewer reads is what the hash binds. A diff too large to be stored whole is a problem (never a truncated diff
    presented as complete)."""
    data = text.encode("utf-8", "surrogatepass")
    if len(data) > MAX_BYTES:
        return "", "", f"the source diff is {len(data)} bytes, over the {MAX_BYTES} that can be embedded whole"
    ev = svc.evidence_put(run_id, "diff", data)
    digest = hashlib.sha256(svc.evidence_read(run_id, ev)).hexdigest()
    return ev, digest, None


# the report is one evidence file (service.EVIDENCE_MAX_BYTES, 8 MiB: longer evidence is cut); it must hold the
# whole diff plus the rest of the report, so a longer diff, or a report that would be cut, fails the run instead
REPORT_MAX_BYTES = 8 * 1024 * 1024
MAX_BYTES = 6 * 1024 * 1024
