"""Shared building blocks for the MVP fixture pools: a version-stable random stream and fixture-safe identities.

Why a hand-rolled random stream: ``random.Random`` is seeded the same way on every Python, but helpers such as
``sample``/``choices`` have changed between releases. A frozen test set must regenerate byte-for-byte on CI's 3.12,
3.13 and 3.14 and on Andre's Mac, so every draw here comes from SHA-256 in counter mode and nothing else.

Fixture-safety rules (test-set spec v0, rule 6): every record says it is fixture data; phone numbers are only in the
NANP range reserved for fiction (555-0100..555-0199); web addresses use only the reserved ``.test`` TLD or
``example.com`` (RFC 2606/6761); no real people or businesses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

GENERATOR_VERSION = "mvp-pools-v0.1"
FIXTURE_LABEL = "FIXTURE DATA - synthetic, not a real business or person"

# Area codes for the core region (AZ, NV, OR, WA, CA), each with the time zone the pool uses for it.
# Arizona has no daylight saving time (America/Phoenix); the after-hours sets rely on that boundary.
REGION_AREA_CODES: tuple[tuple[str, str, str], ...] = (
    ("602", "AZ", "America/Phoenix"),
    ("480", "AZ", "America/Phoenix"),
    ("702", "NV", "America/Los_Angeles"),
    ("503", "OR", "America/Los_Angeles"),
    ("206", "WA", "America/Los_Angeles"),
    ("213", "CA", "America/Los_Angeles"),
    ("562", "CA", "America/Los_Angeles"),
    ("619", "CA", "America/Los_Angeles"),
)


class Stream:
    """Deterministic draws from SHA-256(label | counter). Same label -> same sequence on every platform."""

    def __init__(self, label: str) -> None:
        self._label = label.encode("utf-8")
        self._counter = 0

    def _next64(self) -> int:
        block = hashlib.sha256(self._label + b"|" + str(self._counter).encode("ascii")).digest()
        self._counter += 1
        return int.from_bytes(block[:8], "big")

    def below(self, n: int) -> int:
        """Uniform integer in [0, n) by rejection sampling (no modulo bias)."""
        if n <= 0:
            raise ValueError("n must be positive")
        limit = (1 << 64) - ((1 << 64) % n)
        while True:
            x = self._next64()
            if x < limit:
                return x % n

    def between(self, lo: int, hi: int) -> int:
        """Uniform integer in [lo, hi] inclusive."""
        if hi < lo:
            raise ValueError("hi < lo")
        return lo + self.below(hi - lo + 1)

    def chance(self, numerator: int, denominator: int) -> bool:
        return self.below(denominator) < numerator

    def pick(self, items: Sequence[T]) -> T:
        if not items:
            raise ValueError("empty sequence")
        return items[self.below(len(items))]

    def shuffled(self, items: Sequence[T]) -> list[T]:
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def child(self, name: str) -> Stream:
        """An independent stream, so adding draws in one place never shifts another record's values."""
        return Stream(self._label.decode("utf-8") + "/" + name)


def fixture_phone(area_code: str, n: int) -> str:
    """E.164 number in the fictional 555-0100..0199 range. ``n`` is folded into 0..99."""
    return f"+1{area_code}5550{100 + (n % 100):03d}"


def is_fixture_phone(value: str) -> bool:
    if len(value) != 12 or not value.startswith("+1") or not value[2:].isdigit():
        return False
    return value[5:9] == "5550" and 100 <= int(value[9:]) <= 199


def fixture_domain(slug: str) -> str:
    return f"{slug}.test"


def is_fixture_domain(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "example.com" or host.endswith((".test", ".example.com"))


def opaque_id(prefix: str, seed: str, *parts: object) -> str:
    """A record id that reveals nothing: not the seed (held-out seeds stay secret even though agents see ids), not the
    record's position, not its planted class. SHA-256 of the seed and the record's coordinates."""
    digest = hashlib.sha256("|".join([seed, *map(str, parts)]).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:16]}"


def canonical_json(obj: object) -> str:
    """One stable serialisation for hashing and for the JSONL files."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(text: str) -> str:
    keep = []
    for ch in text.lower():
        if ch.isalnum() and ch.isascii():
            keep.append(ch)
        elif keep and keep[-1] != "-":
            keep.append("-")
    return "".join(keep).strip("-")
