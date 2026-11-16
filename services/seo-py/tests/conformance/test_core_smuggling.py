"""core-spec/2 T-5 type smuggling (card C2-6b, from core-v2/c26/t5_matrix.py): for every (sink, field) of
``sink_fields.SINK_FIELDS`` the frozen generator VG plants 200 valueish values, 200 prose sentences, 200 values with
whitespace / "@" / ``{c:..}`` braces, plus the F3c-3 code-shaped canaries: accepted = 0, except values that match the
field's grammar by construction (hex in ``hash``, a brace-less B-CD-01 code in a T-2 field), which are excluded.
CI runs a seeded sample of fields; ``T5_FULL=1`` runs all fields (lead job). Counts only: no value is printed."""
import collections
import os
import random

import sink_fields as sf
import sink_guard as sg
from conformance import gen_values_v2 as vg

N = 200
CI_FIELDS = 160


def _classes(n: int) -> dict:
    r = random.Random("c2-6:t5")
    valueish = vg.values(vg.SEEDS[0], n)
    words = vg.prose(vg.SEEDS[0], 6 * n)
    sentences = [" ".join(words[i * 6:i * 6 + r.randint(3, 6)]) for i in range(n)]
    ws = []
    for i, v in enumerate(vg.values(vg.SEEDS[1], n)):
        k = i % 3
        ws.append(v[:4] + " " + v[4:] if k == 0 else v[:4] + "@" + v[4:] if k == 1
                  else "{c:text:" + "".join(r.choice("0123456789abcdef") for _ in range(16)) + "}")
    hexs = "0123456789abcdef"
    canary = [f"{r.choice(words[:50]).lower()}-page-{''.join(r.choice(hexs) for _ in range(6))}" for _ in range(n)]
    canary = [c if any(ch.isalpha() for ch in c) else "z" + c for c in canary]
    return {"valueish": valueish, "prose_sentence": sentences, "ws_at_brace": ws, "code_canary": canary}


def _excluded(f, v) -> bool:
    return (f.ref and sf.is_ref(v)) or ("hash" in f.types and sg._is_hash(v, True))


def _fields() -> list:
    keys = sorted(sf.SINK_FIELDS)
    if os.environ.get("T5_FULL") == "1":
        return keys
    code = [k for k in keys if "code" in sf.SINK_FIELDS[k].types and sf.SINK_FIELDS[k].values is None]
    rest = [k for k in keys if k not in set(code)]
    r = random.Random("c2-6b:t5:fields")
    return sorted(r.sample(code, min(len(code), CI_FIELDS)) + r.sample(rest, min(len(rest), CI_FIELDS // 4)))


def _matrix(keys, classes) -> tuple:
    acc, plants = collections.Counter(), 0
    for key in keys:
        f, leaf = sf.SINK_FIELDS[key], sf.leaf_key(key[2])
        for cname, vals in classes.items():
            for v in vals:
                if _excluded(f, v):
                    continue
                plants += 1
                if sf.matches(f, v, leaf):
                    acc[(key[0], cname)] += 1
    return plants, acc


def test_t5_no_planted_value_is_accepted_by_any_sampled_field():
    classes = _classes(N)
    assert all(len(v) == N for v in classes.values())
    plants, acc = _matrix(_fields(), classes)
    print(f"T-5 plants={plants} accepted={sum(acc.values())} by_sink_class={dict(acc)}")
    assert plants > 100_000
    assert sum(acc.values()) == 0, dict(acc)


def test_t5_end_to_end_top_level_open_codes_refuse_valueish_and_canaries():
    """Through the real enforcement entry (``sink_guard.violations``) of seo_log / ledger: a planted value in a
    top-level open `code` field is reported as a violation of that path."""
    classes = _classes(N)
    plants = classes["valueish"][:50] + classes["code_canary"][:50]
    checked, missed = 0, 0
    for (sink, kind, path), f in sorted(sf.SINK_FIELDS.items()):
        if sink not in ("seo_log", "ledger") or path.count("/") != 1 or path.endswith(("[]", "{key}")):
            continue
        if f.type != "code" or f.values or f.ref:
            continue
        k = kind if sink == "seo_log" else sf.LEDGER + kind
        for v in plants:
            checked += 1
            if not any(p == path for p, _ in sg.violations(k, {path[1:]: v})):
                missed += 1
    assert checked > 1000
    assert missed == 0, missed
