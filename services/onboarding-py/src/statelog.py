"""
State-bearing local log (Wave F, M-1): the service's consequential state is rebuilt from the local record log at start.

Every operation line carries the state it applied, as a DELTA of the entities it changed (``StateTracker.delta``):
``{"v": 1, "ops": [["set", container, key, value], ["del", container, key], ["whole", container, value],
["append", container, [items]], ["extend", container, key, [items]], ["lset", container, key, [items]],
["ldel", container, key]]}`` (the last three: a container of per-key append-only lists). Start-up
applies every line's delta in log order (``StateTracker.apply``), so replay is deterministic, order-preserving and
idempotent (applying the same lines to a fresh process always gives the same state; a second restart gives the same
state again). A line the replay cannot interpret -- an unknown state version, container, operation or type tag, a field
the class does not have -- refuses start-up with the line number and the reason; nothing is ever skipped.

Values are encoded exactly (``Codec``): every non-JSON value is a single-key tagged object (``$dc`` dataclass, ``$m``
pydantic model, ``$e`` enum member, ``$dec`` Decimal, ``$dt``/``$date``/``$time``/``$td``, ``$l``/``$t``/``$s``/``$fs``
/``$d``/``$od`` containers, ``$f`` float, ``$b`` bytes). Classes are resolved through an allowlist built from the
service's own modules, never imported by name from the log. Decoding bypasses constructors and validators (the value
is restored exactly as it was held, never re-decided), and every decoded entity must re-encode to the identical bytes
(else start-up is refused: a replay that is not faithful is never served).

Change detection is by hash: ``TrackedDict`` records which keys an operation read or wrote (any iteration marks the
whole container), and only those entities are re-encoded and compared with the hash of what the log already holds. An
entity mutated in place after a ``get`` is therefore always caught; an operation's cost is its own entities, not the
whole state. (Services share no code: this module is copied verbatim into onboarding-py and creative-py.)
"""

from __future__ import annotations

import base64
import dataclasses
import enum
import hashlib
import inspect
import json
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional

STATE_V = 1

try:  # pydantic is a dependency of both services; the codec works without it for plain dataclasses
    from pydantic import BaseModel
except ImportError:  # pragma: no cover
    BaseModel = None  # type: ignore[assignment,misc]


class StateCodecError(ValueError):
    """A value this codec cannot encode, or an encoded value it cannot interpret."""


class StateReplayError(RuntimeError):
    """Start-up refused: a log line's state could not be interpreted or replayed faithfully."""


def canonical(enc: Any) -> bytes:
    return json.dumps(enc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(enc: Any) -> str:
    return hashlib.sha256(canonical(enc)).hexdigest()


def _classes_of(mod) -> Iterable[type]:
    seen: set[int] = set()
    stack = [v for v in vars(mod).values() if inspect.isclass(v) and v.__module__ == mod.__name__]
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        yield c
        stack.extend(v for v in vars(c).values() if inspect.isclass(v) and v.__module__ == mod.__name__)


CACHE_MAX = 200_000   # identity-cached encodings of deeply immutable objects (cleared whole when full)


class Codec:
    def __init__(self, modules: Iterable[Any]):
        self.by_name: dict[str, type] = {}
        self._cache: dict[int, tuple[Any, Any]] = {}
        for mod in modules:
            for c in _classes_of(mod):
                if dataclasses.is_dataclass(c) or issubclass(c, enum.Enum) or (
                        BaseModel is not None and issubclass(c, BaseModel)):
                    self.by_name[self.name(c)] = c

    @staticmethod
    def name(cls: type) -> str:
        return f"{cls.__module__}:{cls.__qualname__}"

    def _cls(self, name: Any) -> type:
        c = self.by_name.get(name) if isinstance(name, str) else None
        if c is None:
            raise StateCodecError(f"unknown type {name!r} (not one of this service's state classes)")
        return c

    def _known(self, cls: type) -> str:
        n = self.name(cls)
        if self.by_name.get(n) is not cls:
            raise StateCodecError(f"{n} is not a registered state class")
        return n

    # ------------------------------------------------------------------------------------------------- encode

    def enc(self, v: Any) -> Any:
        return self._enc(v)[0]

    def _enc(self, v: Any) -> tuple[Any, bool]:
        """(the encoding, whether ``v`` is deeply immutable). A deeply immutable frozen object's encoding is cached by
        identity (the cache holds the object, so its id cannot be reused), so an operation re-encodes only what it
        could have changed: a client's 2 000 frozen facts cost a lookup each, not a re-encoding."""
        if isinstance(v, enum.Enum):              # first: a str/int enum member is never held as a bare str/int
            return {"$e": [self._known(type(v)), v.name]}, True
        if v is None or type(v) in (bool, str, int):
            return v, True
        if isinstance(v, (str, int)):
            raise StateCodecError(f"a {type(v).__qualname__} (a str/int subclass) cannot be held in the state log")
        if type(v) is float:
            return {"$f": v.hex()}, True
        if isinstance(v, Decimal):
            return {"$dec": str(v)}, True
        if isinstance(v, datetime):
            return {"$dt": v.isoformat()}, True
        if isinstance(v, date):
            return {"$date": v.isoformat()}, True
        if isinstance(v, time):
            return {"$time": v.isoformat()}, True
        if isinstance(v, timedelta):
            return {"$td": [v.days, v.seconds, v.microseconds]}, True
        if isinstance(v, bytes):
            return {"$b": base64.b64encode(v).decode("ascii")}, True
        is_model = BaseModel is not None and isinstance(v, BaseModel)
        is_dc = not is_model and dataclasses.is_dataclass(v) and not isinstance(v, type)
        frozen = (is_model and bool(type(v).model_config.get("frozen"))) or (
            is_dc and type(v).__dataclass_params__.frozen)  # type: ignore[attr-defined]
        if frozen:
            hit = self._cache.get(id(v))
            if hit is not None and hit[0] is v:
                return hit[1], True
        if is_model:
            if getattr(v, "__pydantic_private__", None):
                raise StateCodecError(f"{type(v).__qualname__} holds private attributes; not encodable")
            # the declared fields only (a cached property also lives in __dict__: it is derived, never held)
            fields, imm = self._pairs((k, x) for k, x in v.__dict__.items() if k in type(v).model_fields)
            extra = getattr(v, "__pydantic_extra__", None)
            if extra:
                e, _ = self._enc(dict(extra))
                fields.append(["$extra", e])
                imm = False
            out, imm = {"$m": [self._known(type(v)), fields, sorted(v.model_fields_set)]}, imm and frozen
        elif is_dc:
            fields, imm = self._pairs((f.name, getattr(v, f.name)) for f in dataclasses.fields(v))
            out, imm = {"$dc": [self._known(type(v)), fields]}, imm and frozen
        elif isinstance(v, (tuple, list)):
            items = [self._enc(x) for x in v]
            out = {"$t" if isinstance(v, tuple) else "$l": [e for e, _ in items]}
            imm = isinstance(v, tuple) and all(i for _, i in items)
        elif isinstance(v, (set, frozenset)):
            items = [self._enc(x) for x in v]
            out = {"$fs" if isinstance(v, frozenset) else "$s": sorted((e for e, _ in items), key=canonical)}
            imm = isinstance(v, frozenset) and all(i for _, i in items)
        elif isinstance(v, dict):
            out = {"$od" if isinstance(v, OrderedDict) else "$d": [[self._enc(k)[0], self._enc(x)[0]]
                                                                     for k, x in v.items()]}
            imm = False
        else:
            raise StateCodecError(f"a {type(v).__module__}.{type(v).__qualname__} cannot be held in the state log")
        if frozen and imm:
            if len(self._cache) >= CACHE_MAX:
                self._cache.clear()
            self._cache[id(v)] = (v, out)
        return out, imm

    def _pairs(self, items) -> tuple[list, bool]:
        out, imm = [], True
        for k, x in items:
            e, i = self._enc(x)
            out.append([k, e])
            imm = imm and i
        return out, imm

    # ------------------------------------------------------------------------------------------------- decode

    def dec(self, e: Any) -> Any:
        if e is None or isinstance(e, (bool, str)) or (isinstance(e, int) and not isinstance(e, bool)):
            return e
        if not isinstance(e, dict) or len(e) != 1:
            raise StateCodecError(f"not an encoded value: {type(e).__name__}")
        (tag, a), = e.items()
        try:
            if tag == "$l":
                return [self.dec(x) for x in self._list(a)]
            if tag == "$t":
                return tuple(self.dec(x) for x in self._list(a))
            if tag == "$s":
                return {self.dec(x) for x in self._list(a)}
            if tag == "$fs":
                return frozenset(self.dec(x) for x in self._list(a))
            if tag in ("$d", "$od"):
                out = OrderedDict() if tag == "$od" else {}
                for pair in self._list(a):
                    k, x = self._list(pair, 2)
                    out[self.dec(k)] = self.dec(x)
                return out
            if tag == "$dec":
                return Decimal(self._str(a))
            if tag == "$f":
                return float.fromhex(self._str(a))
            if tag == "$dt":
                return datetime.fromisoformat(self._str(a))
            if tag == "$date":
                return date.fromisoformat(self._str(a))
            if tag == "$time":
                return time.fromisoformat(self._str(a))
            if tag == "$td":
                d, s, us = self._list(a, 3)
                return timedelta(days=d, seconds=s, microseconds=us)
            if tag == "$b":
                return base64.b64decode(self._str(a), validate=True)
            if tag == "$e":
                name, member = self._list(a, 2)
                cls = self._cls(name)
                if not issubclass(cls, enum.Enum) or member not in cls.__members__:
                    raise StateCodecError(f"{name} has no member {member!r}")
                return cls[member]
            if tag == "$dc":
                name, fields = self._list(a, 2)
                return self._dataclass(self._cls(name), fields)
            if tag == "$m":
                name, fields, fset = self._list(a, 3)
                return self._model(self._cls(name), fields, fset)
        except StateCodecError:
            raise
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            raise StateCodecError(f"malformed {tag} value ({type(exc).__name__}: {exc})") from None
        raise StateCodecError(f"unknown type tag {tag!r}")

    @staticmethod
    def _list(a: Any, n: Optional[int] = None) -> list:
        if not isinstance(a, list) or (n is not None and len(a) != n):
            raise StateCodecError("malformed encoded list")
        return a

    @staticmethod
    def _str(a: Any) -> str:
        if not isinstance(a, str):
            raise StateCodecError("malformed encoded scalar")
        return a

    def _dataclass(self, cls: type, fields: Any) -> Any:
        if not dataclasses.is_dataclass(cls):
            raise StateCodecError(f"{self.name(cls)} is not a dataclass")
        known = {f.name: f for f in dataclasses.fields(cls)}
        given = {}
        for pair in self._list(fields):
            k, x = self._list(pair, 2)
            if k not in known:
                raise StateCodecError(f"{self.name(cls)} has no field {k!r}")
            given[k] = self.dec(x)
        obj = cls.__new__(cls)
        for k, f in known.items():
            if k in given:
                v = given[k]
            elif f.default is not dataclasses.MISSING:
                v = f.default
            elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                v = f.default_factory()  # type: ignore[misc]
            else:
                raise StateCodecError(f"{self.name(cls)} field {k!r} is missing and has no default")
            object.__setattr__(obj, k, v)
        return obj

    def _model(self, cls: type, fields: Any, fset: Any) -> Any:
        if BaseModel is None or not issubclass(cls, BaseModel):
            raise StateCodecError(f"{self.name(cls)} is not a model")
        values, extra = {}, None
        for pair in self._list(fields):
            k, x = self._list(pair, 2)
            if k == "$extra":
                extra = self.dec(x)
                continue
            if k not in cls.model_fields:
                raise StateCodecError(f"{self.name(cls)} has no field {k!r}")
            values[k] = self.dec(x)
        fs = set(self._list(fset))
        if not fs <= set(cls.model_fields):
            raise StateCodecError(f"{self.name(cls)} fields-set names an unknown field")
        obj = cls.model_construct(_fields_set=fs, **values)
        if extra:
            object.__setattr__(obj, "__pydantic_extra__", dict(extra))
        return obj


class TrackedDict(dict):
    """A dict that remembers which keys were read or written since ``reset`` (any iteration: all of them)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reset()

    def reset(self) -> None:
        self.touched: dict = {}      # key -> None, first-touch order
        self.new_order: dict = {}    # keys set while absent, in insertion order
        self.deleted: set = set()
        self.all = False

    def _t(self, k) -> None:
        self.touched.setdefault(k, None)

    def __getitem__(self, k):
        self._t(k)
        return super().__getitem__(k)

    def get(self, k, default=None):
        self._t(k)
        return super().get(k, default)

    def __setitem__(self, k, v):
        self._t(k)
        if not super().__contains__(k):
            self.new_order[k] = None
        super().__setitem__(k, v)

    def __delitem__(self, k):
        self._t(k)
        self.deleted.add(k)
        self.new_order.pop(k, None)
        super().__delitem__(k)

    def pop(self, k, *default):
        self._t(k)
        if super().__contains__(k):
            self.deleted.add(k)
            self.new_order.pop(k, None)
        return super().pop(k, *default)

    def setdefault(self, k, default=None):
        if not super().__contains__(k):
            self[k] = default
        self._t(k)
        return super().__getitem__(k)

    def popitem(self):
        self.all = True
        return super().popitem()

    def update(self, *a, **k):
        for key, v in dict(*a, **k).items():
            self[key] = v

    def clear(self):
        self.all = True
        super().clear()

    def __iter__(self):
        self.all = True
        return super().__iter__()

    def keys(self):
        self.all = True
        return super().keys()

    def values(self):
        self.all = True
        return super().values()

    def items(self):
        self.all = True
        return super().items()

    def raw_set(self, k, v) -> None:
        super().__setitem__(k, v)

    def raw_del(self, k) -> None:
        super().pop(k, None)

    def raw_keys(self) -> list:
        return list(super().keys())

    def raw_get(self, k):
        return super().get(k)

    def raw_contains(self, k) -> bool:
        return super().__contains__(k)


class StateTracker:
    """The consequential state of one service: keyed containers (``TrackedDict``) and whole values."""

    def __init__(self, codec: Codec):
        self.codec = codec
        self.keyed: dict[str, TrackedDict] = {}
        self.whole: dict[str, tuple[Callable[[], Any], Callable[[Any], None]]] = {}
        self._held: dict[str, dict[bytes, str]] = {}   # container -> canonical(enc key) -> digest of what the log holds
        self._whole_held: dict[str, str] = {}
        self.appended: dict[str, Callable[[], list]] = {}
        self._appended_held: dict[str, int] = {}
        self.lists: dict[str, TrackedDict] = {}
        self._lists_held: dict[str, dict[bytes, int]] = {}

    def add_keyed(self, name: str, owner: Any, attr: str) -> TrackedDict:
        td = TrackedDict(dict.items(getattr(owner, attr)))
        setattr(owner, attr, td)
        self.keyed[name] = td
        return td

    def add_whole(self, name: str, get: Callable[[], Any], set_: Callable[[Any], None]) -> None:
        self.whole[name] = (get, set_)

    def add_keyed_lists(self, name: str, owner: Any, attr: str) -> TrackedDict:
        """A dict of lists that only ever grow (a memory store): a line carries only each touched key's new items, so
        its size does not grow with the store (AEGIS F-1)."""
        td = TrackedDict(dict.items(getattr(owner, attr)))
        setattr(owner, attr, td)
        self.lists[name] = td
        return td

    def add_append_only(self, name: str, get: Callable[[], list]) -> None:
        """A list that only ever grows (items never change once appended): a line carries only the new items."""
        self.appended[name] = get

    def baseline(self) -> None:
        """What the log holds = the state now (at start, after replay): every entity's digest."""
        for name, td in self.keyed.items():
            self._held[name] = {canonical(self.codec.enc(k)): digest(self.codec.enc(td.raw_get(k)))
                                for k in td.raw_keys()}
            td.reset()
        for name, (get, _) in self.whole.items():
            self._whole_held[name] = digest(self.codec.enc(get()))
        for name, get in self.appended.items():
            self._appended_held[name] = len(get())
        for name, td in self.lists.items():
            self._lists_held[name] = {canonical(self.codec.enc(k)): len(td.raw_get(k)) for k in td.raw_keys()}
            td.reset()

    def delta(self) -> tuple[Optional[dict], Callable[[], None]]:
        """The state change since the log was last told (None if nothing changed), and the callable that marks it
        held once its line is written (or owed)."""
        ops: list = []
        held_upd: list = []
        for name, td in self.keyed.items():
            held = self._held.setdefault(name, {})
            # copies taken in one C-level step each: a read on another thread (an unlocked GET) may mark keys while
            # this runs; reads never change state, so a mark that lands after the copy is simply compared next time
            touched, new_order, deleted = list(td.touched), list(td.new_order), set(td.deleted)
            if td.all:
                present = td.raw_keys()
                cands = present + [k for k in touched if not td.raw_contains(k)]
                now = {canonical(self.codec.enc(k)) for k in present}
                gone = [kc for kc in held if kc not in now]
            else:
                new_set = set(new_order)
                old = [k for k in touched if not (k in new_set and canonical(self.codec.enc(k)) not in held)]
                cands = old + [k for k in new_order if td.raw_contains(k)]
                gone = []
            seen: set = set()
            for k in cands:
                ek = self.codec.enc(k)
                kc = canonical(ek)
                if kc in seen:
                    continue
                seen.add(kc)
                if td.raw_contains(k):
                    ev = self.codec.enc(td.raw_get(k))
                    dg = digest(ev)
                    if k in deleted and kc in held:
                        ops.append(["del", name, ek])        # deleted and set again: it moved to the end
                    elif held.get(kc) == dg:
                        continue
                    ops.append(["set", name, ek, ev])
                    held_upd.append((name, kc, dg))
                elif kc in held:
                    ops.append(["del", name, ek])
                    held_upd.append((name, kc, None))
            for kc in gone:
                if kc not in seen:
                    ops.append(["del", name, json.loads(kc)])
                    held_upd.append((name, kc, None))
        whole_upd = []
        for name, (get, _) in self.whole.items():
            ev = self.codec.enc(get())
            dg = digest(ev)
            if self._whole_held.get(name) != dg:
                ops.append(["whole", name, ev])
                whole_upd.append((name, dg))
        list_upd = []
        for name, td in self.lists.items():
            held_n = self._lists_held.setdefault(name, {})
            keys = td.raw_keys() if td.all else list(td.touched)
            seen_k: set = set()
            for k in keys:
                ek = self.codec.enc(k)
                kc = canonical(ek)
                if kc in seen_k:
                    continue
                seen_k.add(kc)
                if not td.raw_contains(k):
                    if kc in held_n:
                        ops.append(["ldel", name, ek])
                        list_upd.append((name, kc, None))
                    continue
                items, n = td.raw_get(k), held_n.get(kc, 0)
                if len(items) > n:
                    ops.append(["extend", name, ek, [self.codec.enc(x) for x in items[n:]]])
                elif len(items) < n:      # never in an append-only store; carried whole rather than lost
                    ops.append(["lset", name, ek, [self.codec.enc(x) for x in items]])
                else:
                    continue
                list_upd.append((name, kc, len(items)))
            if td.all:
                present = {canonical(self.codec.enc(k)) for k in td.raw_keys()}
                for kc in [kc for kc in held_n if kc not in present]:
                    ops.append(["ldel", name, json.loads(kc)])
                    list_upd.append((name, kc, None))
        app_upd = []
        for name, get in self.appended.items():
            items, n = get(), self._appended_held.get(name, 0)
            if len(items) < n:
                raise StateCodecError(f"append-only state {name!r} shrank ({n} -> {len(items)})")
            if len(items) > n:
                ops.append(["append", name, [self.codec.enc(x) for x in items[n:]]])
                app_upd.append((name, len(items)))

        def mark() -> None:
            for name, kc, dg in held_upd:
                if dg is None:
                    self._held[name].pop(kc, None)
                else:
                    self._held[name][kc] = dg
            for name, dg in whole_upd:
                self._whole_held[name] = dg
            for name, n in app_upd:
                self._appended_held[name] = n
            for name, kc, n in list_upd:
                if n is None:
                    self._lists_held[name].pop(kc, None)
                else:
                    self._lists_held[name][kc] = n
            self.reset()

        if not ops:
            return None, mark
        return {"v": STATE_V, "ops": ops}, mark

    def reset(self) -> None:
        for td in self.keyed.values():
            td.reset()
        for td in self.lists.values():
            td.reset()

    def apply(self, state: Any, where: str) -> None:
        """Apply one line's state delta (start-up replay). Raises StateReplayError naming ``where`` on anything it
        cannot interpret; every value must re-encode to exactly what the line holds."""
        def bad(why: str) -> StateReplayError:
            return StateReplayError(f"refusing to start: {where}: {why}; the state log cannot be replayed faithfully "
                                    "(inspect the log; nothing was skipped)")
        if not isinstance(state, dict) or set(state) != {"v", "ops"}:
            raise bad("malformed state delta")
        if state["v"] != STATE_V:
            raise bad(f"state version {state['v']!r} is not one this build replays ({STATE_V})")
        if not isinstance(state["ops"], list):
            raise bad("malformed state operations")
        for op in state["ops"]:
            try:
                if not isinstance(op, list) or not op or op[0] not in ("set", "del", "whole", "append", "extend", "lset", "ldel"):
                    raise bad(f"unknown state operation {op[:1] if isinstance(op, list) else op!r}")
                if op[0] in ("extend", "lset", "ldel"):
                    if len(op) != (3 if op[0] == "ldel" else 4) or op[1] not in self.lists:
                        raise bad(f"unknown keyed-list container {op[1:2]!r}")
                    td = self.lists[op[1]]
                    k = self.codec.dec(op[2])
                    if canonical(self.codec.enc(k)) != canonical(op[2]):
                        raise bad(f"a {op[1]} key does not re-encode identically")
                    if op[0] == "ldel":
                        td.raw_del(k)
                        continue
                    if not isinstance(op[3], list):
                        raise bad(f"malformed {op[1]} items")
                    items = [self.codec.dec(x) for x in op[3]]
                    if [canonical(self.codec.enc(x)) for x in items] != [canonical(x) for x in op[3]]:
                        raise bad(f"{op[1]} items do not re-encode identically")
                    if op[0] == "lset" or not td.raw_contains(k):
                        td.raw_set(k, [])
                    td.raw_get(k).extend(items)
                    continue
                if op[0] == "append":
                    if len(op) != 3 or op[1] not in self.appended or not isinstance(op[2], list):
                        raise bad(f"unknown append-only container {op[1:2]!r}")
                    items = [self.codec.dec(x) for x in op[2]]
                    if [canonical(self.codec.enc(x)) for x in items] != [canonical(x) for x in op[2]]:
                        raise bad(f"{op[1]} items do not re-encode identically")
                    self.appended[op[1]]().extend(items)
                    continue
                if op[0] == "whole":
                    if len(op) != 3 or op[1] not in self.whole:
                        raise bad(f"unknown whole-state container {op[1:2]!r}")
                    v = self.codec.dec(op[2])
                    if canonical(self.codec.enc(v)) != canonical(op[2]):
                        raise bad(f"{op[1]} does not re-encode identically")
                    self.whole[op[1]][1](v)
                    continue
                if len(op) != (4 if op[0] == "set" else 3) or op[1] not in self.keyed:
                    raise bad(f"unknown state container {op[1:2]!r}")
                td = self.keyed[op[1]]
                k = self.codec.dec(op[2])
                if canonical(self.codec.enc(k)) != canonical(op[2]):
                    raise bad(f"a {op[1]} key does not re-encode identically")
                if op[0] == "del":
                    td.raw_del(k)
                    continue
                v = self.codec.dec(op[3])
                if canonical(self.codec.enc(v)) != canonical(op[3]):
                    raise bad(f"{op[1]} entry {k!r} does not re-encode identically")
                td.raw_set(k, v)
            except StateCodecError as exc:
                raise bad(str(exc)) from None
            except TypeError as exc:   # e.g. an unhashable key
                raise bad(f"{type(exc).__name__}: {exc}") from None

    def snapshot(self) -> bytes:
        """The whole state, canonically encoded (tests compare a live service with its restarted copy)."""
        out = {name: [[self.codec.enc(k), self.codec.enc(td.raw_get(k))] for k in td.raw_keys()]
               for name, td in sorted(self.keyed.items())}
        out.update({f"whole:{n}": self.codec.enc(g()) for n, (g, _) in sorted(self.whole.items())})
        out.update({f"append:{n}": self.codec.enc(list(g())) for n, g in sorted(self.appended.items())})
        out.update({f"lists:{name}": [[self.codec.enc(k), self.codec.enc(td.raw_get(k))] for k in td.raw_keys()]
                    for name, td in sorted(self.lists.items())})
        return canonical(out)
