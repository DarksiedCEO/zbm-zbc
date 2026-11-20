"""Priya's tenant word list T (priya-spec/2 2.4 T-1..T-4, OW-4, PR-3; lead ruling NB-9 option (a), 11:30 PT).

T holds words, never whole queries. It has two inputs:
  (b) operator-added words ("allow this word"): OwnerWordBook.add, each with actor, time and a ledger audit entry;
      a word whose audit entry is not recorded is not added (fail closed).
  (a) harvested words, behind the port TenantTermSource (site page texts, catalog product titles). Live harvesting is
      OFF by default; when it is switched on but the source is NOT_CONNECTED, raises, or returns anything that is not
      text, the harvest contributes nothing (T = operator words only). Wiring real sources (Selene page text, core
      AL-1 catalog) is card P2-2d-live, after PR #51; until then a client's own product words count only if added by hand.
Every word, from either input, must pass OG-1, OG-2, OG-3 and stage 2 as a one-word query (T-2); at most
TENANT_WORDS_MAX words per tenant, operator words first. T is per tenant (T-3); tenant_vocab_sha256 = sha256 of the
sorted canonical words, each followed by "\\n" (UTF-8). Certification runs use empty_tenant_vocab() (T-4).
"""

from __future__ import annotations

import hashlib
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol

from agents import priya_gate as g
from agents.search_performance import classify_query
from ledger import LedgerRecordError, Recorder, derived_id
from ports import NOT_CONNECTED

TENANT_WORDS_MAX = 20_000  # T-2
MIN_LETTERS = 4  # T-1 (a)
MIN_PAGES = 2  # T-1 (a)
EMPTY_TENANT_VOCAB_SHA256 = hashlib.sha256(b"").hexdigest()
HARVEST_STATUSES = ("OFF", "ON", "NOT_CONNECTED", "FAILED")
OWNER_ADD_EVENT = "priya_tenant_word_added"
T2_RULES = ("OG-1", "OG-2", "OG-3")


class TenantTermSource(Protocol):
    """Harvest port (NB-9). ``pages``: one text per tenant page; ``catalog_titles``: one text per catalog product title."""

    name: str

    def pages(self, tenant_id: str) -> Iterable[str]: ...

    def catalog_titles(self, tenant_id: str) -> Iterable[str]: ...


class NotConnectedTermSource:
    """The default source: says NOT_CONNECTED and never returns invented data."""

    name = NOT_CONNECTED

    def pages(self, tenant_id: str) -> Iterable[str]:
        return ()

    def catalog_titles(self, tenant_id: str) -> Iterable[str]:
        return ()


def _check_tenant(tenant_id) -> str:
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("tenant_id must be a non-empty string")
    return tenant_id


def tenant_vocab_sha256(words: Iterable[str]) -> str:
    return hashlib.sha256(
        "".join(w + "\n" for w in sorted(set(words))).encode("utf-8")
    ).hexdigest()


def one_word(word) -> str | None:
    """The canonical form when ``word`` is exactly one canonical token (T-1: words, never whole queries), else None."""
    if not isinstance(word, str):
        return None
    toks = g.tokens(word)
    return toks[0] if len(toks) == 1 and toks[0] == g.canonical(word) else None


def t2_reason(word: str) -> str | None:
    """T-2: None when ``word`` passes OG-1, OG-2, OG-3 and stage 2 as a one-word query; else the reason.
    A confusables file that fails its pin rejects every word (fail closed, NB-3)."""
    status = g.gate_status(frozenset())
    if status != "OK":
        return status
    fired = g.og_fired(word, frozenset())
    for rule in T2_RULES:
        if rule in fired:
            return rule
    if (
        classify_query(word, []) == "pii_suspect"
        or classify_query(g.canonical(word), []) == "pii_suspect"
    ):
        return "STAGE2"
    return None


def _letters_only(word: str) -> bool:
    return bool(word) and all(unicodedata.category(ch).startswith("L") for ch in word)


@dataclass(frozen=True)
class OwnerWord:
    tenant_id: str
    word: str
    actor: str
    at: str
    event_id: str


class OwnerWordBook:
    """T-1 (b): words the operator allows by hand, per tenant, each audited in the ledger before it counts."""

    def __init__(self, recorder: Recorder):
        self._recorder = recorder
        self._by_tenant: dict[str, dict[str, OwnerWord]] = {}
        self._lock = threading.RLock()

    def add(
        self, tenant_id: str, word, actor: str, now: datetime
    ) -> tuple[OwnerWord | None, str | None]:
        _check_tenant(tenant_id)
        if not isinstance(now, datetime) or now.tzinfo is None:
            return None, "BAD_TIME"
        if not isinstance(word, str):
            return None, "NOT_ONE_WORD"
        reason = t2_reason(word)
        if reason:
            return None, reason
        c = one_word(word)
        if c is None:
            return None, "NOT_ONE_WORD"
        with self._lock:
            book = self._by_tenant.setdefault(tenant_id, {})
            if c in book:
                return book[c], None
            if len(book) >= TENANT_WORDS_MAX:
                return None, "T_CAP"
            at = now.isoformat()
            event_id = derived_id("priya-tword", tenant_id, c, actor, at)
            payload = {
                "tenant_id": tenant_id,
                "word": c,
                "actor": actor,
                "at": at,
                "rule": "priya-spec/2 T-1(b)",
            }
            try:
                self._recorder.record(
                    event_id,
                    OWNER_ADD_EVENT,
                    actor,
                    tenant_id,
                    payload,
                    "operator allowed a tenant word (Priya T-1b)",
                )
            except LedgerRecordError:
                return None, "AUDIT_NOT_RECORDED"
            entry = OwnerWord(tenant_id, c, actor, at, event_id)
            book[c] = entry
            return entry, None

    def words(self, tenant_id: str) -> list[OwnerWord]:
        with self._lock:
            return list(self._by_tenant.get(_check_tenant(tenant_id), {}).values())


@dataclass(frozen=True)
class TenantVocab:
    """T for one tenant; usable as og()'s ``tenant`` argument. ``rejected``: counts per reason (never the words)."""

    tenant_id: str
    words: frozenset[str]
    sha256: str
    harvest: str
    rejected: dict[str, int] = field(default_factory=dict)

    def __contains__(self, word) -> bool:
        return word in self.words

    def __len__(self) -> int:
        return len(self.words)

    def report_fields(self) -> dict:
        """T-3: the fields every run report carries."""
        return {
            "tenant_vocab_sha256": self.sha256,
            "tenant_vocab_n": len(self.words),
            "tenant_harvest": self.harvest,
        }


def empty_tenant_vocab(tenant_id: str = "") -> TenantVocab:
    """T-4: certification runs (and every frozen OQ measurement) use T = empty."""
    return TenantVocab(tenant_id, frozenset(), EMPTY_TENANT_VOCAB_SHA256, "OFF", {})


def _texts(items) -> list[str]:
    out = list(items)
    if not all(isinstance(t, str) for t in out):
        raise TypeError("a term source returned something that is not text")
    return out


def _harvest(tenant_id: str, source, rejected: Counter) -> list[str]:
    """T-1 (a) candidates, sorted; cheap rules only (T-2 and the cap are applied by the caller)."""
    page_counts: Counter = Counter()
    for page in _texts(source.pages(tenant_id)):
        page_counts.update(set(g.tokens(page)))
    catalog: set[str] = set()
    for title in _texts(source.catalog_titles(tenant_id)):
        catalog.update(g.tokens(title))
    out = []
    for w in sorted(set(page_counts) | catalog):
        if w not in catalog and page_counts[w] < MIN_PAGES:
            rejected["FEW_PAGES"] += 1
        elif not _letters_only(w):
            rejected["NOT_LETTERS"] += 1
        elif len(w) < MIN_LETTERS:
            rejected["TOO_SHORT"] += 1
        else:
            out.append(w)
    return out


def build_tenant_vocab(
    tenant_id: str,
    owner_book: OwnerWordBook | None = None,
    source: TenantTermSource | None = None,
    harvest_enabled: bool = False,
) -> TenantVocab:
    """T for ``tenant_id``: operator words first, then harvested words (only when ``harvest_enabled`` is True)."""
    _check_tenant(tenant_id)
    rejected: Counter = Counter()
    words: list[str] = (
        [e.word for e in owner_book.words(tenant_id)] if owner_book is not None else []
    )
    if not harvest_enabled:
        status, harvested = "OFF", []
    elif source is None or getattr(source, "name", NOT_CONNECTED) == NOT_CONNECTED:
        status, harvested = "NOT_CONNECTED", []
    else:
        counts: Counter = Counter()
        try:
            status, harvested = "ON", _harvest(tenant_id, source, counts)
        except Exception:  # fail closed: any source failure -> T = operator words only
            status, harvested = "FAILED", []
        else:
            rejected.update(counts)
    seen = set(words)
    for w in harvested:
        if w in seen:
            continue
        if len(seen) >= TENANT_WORDS_MAX:
            rejected["T_CAP"] += 1
            continue
        reason = t2_reason(w)
        if reason:
            rejected[reason] += 1
            continue
        seen.add(w)
    final = frozenset(seen)
    return TenantVocab(
        tenant_id, final, tenant_vocab_sha256(final), status, dict(rejected)
    )
