"""
A-31: the catalogued mutants of the Wave-1 agents and primitives (spec wave1-spec/1 A-31, SUITE_PLAN 5). Each mutant
is (id, file under src/, anchor text occurring exactly once, replacement, status, tests). The lead gate applies each
behavioural mutant to a COPY of the service (tar, no .git), runs the named conformance tests of the fast tier, and
fails on a survivor; an ``equivalent`` mutant carries its written proof and is not run.

    python devtools/conformance/mutants.py --work <empty dir> [--only <id>]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

SVC = Path(__file__).resolve().parents[2]
T = "tests/conformance/"


def m(mid, file, anchor, repl, tests, status="behavioural", proof=None):
    return {"id": mid, "file": file, "anchor": anchor, "replacement": repl, "tests": [T + t for t in tests],
            "status": status, "proof": proof}


R, F, C, U = "primitives/robots.py", "primitives/fetch.py", "primitives/codes.py", "primitives/safe_url.py"
S, L, SL, D = "agents/selene.py", "agents/logs.py", "svc_logs.py", "agents/delia.py"
RO, E, OS, DR = "agents/roman.py", "agents/entity_check.py", "agents/osei.py", "agents/drift.py"
TR, TF, TC = ["test_b_robots.py", "test_a_robots_cost.py"], ["test_b_fetch.py", "test_a_bodies.py"], \
    ["test_b_codes.py"]

MUTANTS = [
    # ------------------------------------------------------------------ robots (r1c robots.py:102 family incl.)
    m("rb-01", R, "if self.units + units > self.limit:", "if self.units + units > self.limit * 2:", TR),
    m("rb-02", R, "if c == \"%\":", "if c == \"%\" and False:", TR),
    m("rb-03", R, "        if 0xDC80 <= o <= 0xDCFF:", "        if False:", TR),
    m("rb-04", R, "elif o >= 0x80:", "elif o >= 0x100:", TR),
    m("rb-05", R, "    if len(pieces) == 1:\n        return (not rule.anchored) or len(path) == len(first)",
      "    if len(pieces) == 1:\n        return True", TR),
    m("rb-06", R, "        if j < 0:\n            return False", "        if j < -1:\n            return False", TR),
    m("rb-07", R, "return len(path) - len(tail) >= pos and path.endswith(tail)", "return path.endswith(tail)", TR),
    m("rb-08", R, "        if not named:\n            named = [g for g in self.groups if \"*\" in g.agents]",
      "        if True:\n            named = [g for g in self.groups if \"*\" in g.agents]", TR),
    m("rb-09", R, "if p == \"/robots.txt\":", "if p == \"/robots.txt-x\":", TR),
    m("rb-10", R, "if (best is None or r.length > best.length or (r.length == best.length and r.allow",
      "if (best is None or r.length < best.length or (r.length == best.length and r.allow", TR),
    m("rb-11", R, "                                                               and not best.allow)) and _match(r, p):",
      "                                                               and best.allow)) and _match(r, p):", TR),
    m("rb-12", R, "if text.startswith(BOM):", "if text.startswith(BOM) and False:", TR),
    m("rb-13", R, "    if len(raw) > MAX_BYTES:", "    if len(raw) > MAX_BYTES * 4:", TR),
    m("rb-14", R, "    if truncated and lines and not text.endswith((\"\\n\", \"\\r\")):\n        lines = lines[:-1]",
      "    if False:\n        lines = lines[:-1]", TR),
    m("rb-15", R, "            last_was_agent = False\n            if current is None:",
      "            last_was_agent = True\n            if current is None:", TR),
    m("rb-16", R, "            if not v or (allow, v) in seen_rules:", "            if (allow, v) in seen_rules:", TR),
    m("rb-17", R, "key = key.strip().lower().replace(\"_\", \"-\")", "key = key.strip().replace(\"_\", \"-\")", TR),
    m("rb-22", R, "        p = _units(p)\n", "        p = canon(p)\n", TR),
    m("rb-23", R, "    return len(u) + 2 * sum(1 for ch in u if ord(ch) >= _UNIT_BASE)", "    return len(u)", TR),
    # rv-wave1v2 r01: equivalent (the reviewer's proof, checked here)
    m("rb-24", R, "out.append(ch if ch in _UNRESERVED else \"%\" + h.upper())",
      "out.append(ch if ch in _UNRESERVED else \"%\" + h)", TR, status="equivalent",
      proof="hex case inside canon: canon's output is read only by _units (robots.py), which turns every %XX escape "
            "into ONE unit by int(hex, 16), case-insensitive; a decision, a specificity (_octets counts units) and "
            "nothing stored ever sees the escape's hex case, so a lower-case escape decides exactly as an upper-case "
            "one"),
    m("rb-18", R, 'return text.replace("\\r\\n", "\\n").replace("\\r", "\\n").split("\\n")',
      'return text.split("\\n")', TR),
    m("rb-19", R, "(len(p) * r.find_len) // FIND_COST_DIVISOR", "0", ["test_a_robots_cost.py"]),
    m("rb-20", R, "if work is not None and not work.charge(1 + len(p) // 64):", "if False:", TR),
    m("rb-21", R, "            out.lines_ignored += 1\n            continue\n        key = key", "            continue\n        key = key",
      TR),
    # ------------------------------------------------------------------ fetch
    m("ft-01", F, "if scheme not in (\"http\", \"https\"):", "if scheme not in (\"http\", \"https\", \"ftp\"):", TF),
    m("ft-02", F, "        if port is None:                          # B-FT-01", "        if not port:  # B-FT-01", TF),
    m("ft-03", F, "if len(loc.encode(\"utf-8\", \"surrogateescape\")) > LOCATION_MAX:\n                            raise",
      "if len(loc) > LOCATION_MAX * 9:\n                            raise", TF),
    m("ft-04", F, "LOCATION_MAX = 8192", "LOCATION_MAX = 2000", TF),
    m("ft-05", F, "if len(codings) > 1 or (codings and codings[0] not in ENCODINGS):",
      "if codings and codings[0] not in ENCODINGS:", TF),
    m("ft-06", F, "        if self.d is not None and not self.d.eof:", "        if False:", TF),
    m("ft-07", F, "        if self.n > self.limit:\n            if self.keep_prefix:",
      "        if self.n > self.limit * 64:\n            if self.keep_prefix:", TF),
    m("ft-08", F, "                    if hop == self.max_redirects:", "                    if hop == self.max_redirects + 1:", TF,
      status="equivalent", proof="the hop loop is range(max_redirects + 1): after the last hop it ends and raises the "
                                 "same TOO_MANY_REDIRECTS, so the in-loop check only ends the loop one step earlier"),
    m("ft-09", F, "elif 400 <= r.status < 500 and r.status != 429:", "elif 400 <= r.status < 500:", TF),
    m("ft-10", F, "            if rb[\"fetch\"][\"state\"] == \"KILLED\":          # B-FT-08",
      "            if False:          # B-FT-08", TF),
    m("ft-11", F, "            if rb[\"status_class\"] == \"incomplete\":", "            if False:",
      ["test_b_selene.py"]),
    m("ft-12", F, "        if r.state in (\"TOO_LARGE\", \"PROTOCOL_ERROR\", \"RESOURCE_LIMIT\", \"UNSUPPORTED_ENCODING\") and r.status_seen:",
      "        if r.state == \"TOO_LARGE\" and False:", ["test_b_selene.py", "test_a_robots_input.py"]),
    m("ft-13", F, "ROBOTS_WIRE_MAX = 64 * 1024 * 1024", "ROBOTS_WIRE_MAX = 1", ["test_b_selene.py"]),
    m("ft-14", F, "        if d[\"allowed\"] is False:\n            raise _Refused(\"BLOCKED_BY_ROBOTS\"",
      "        if d[\"allowed\"] is None:\n            raise _Refused(\"BLOCKED_BY_ROBOTS\"", TF),
    m("ft-15", F, "        if d[\"allowed\"] is None:\n            raise _Refused(\"ROBOTS_CHECK_INCOMPLETE\"",
      "        if d[\"allowed\"] is 0:\n            raise _Refused(\"ROBOTS_CHECK_INCOMPLETE\"", ["test_b_selene.py"]),
    m("ft-16", F, "if parts.username is not None or parts.password is not None or \"@\" in parts.netloc:",
      "if parts.password is not None:", TF),
    m("ft-17", F, "        if _numeric_host_not_canonical(host):", "        if False:", TF),
    m("ft-18", F, "kept[\"x-robots-tag\"] = safe_url_mod.HEADER_SEP.join(", "kept[\"x-robots-tag\"] = \", \".join(",
      ["test_b_selene.py"]),
    m("ft-19", F, "            if time.monotonic() > deadline:", "            if False:", TF),
    m("ft-20", F, "        out[\"text\"] = r.body[:ROBOTS_MAX_BYTES]", "        out[\"text\"] = r.body[:1000]",
      ["test_b_selene.py", "test_a_robots_input.py"], status="behavioural"),
    # rv-wave1v2 f03: the wire cap, as distinct from the decoded cap (a body of empty gzip members)
    m("ft-21", F, "if raw > wire_max:", "if raw > wire_max * 64:", TF),
    # rv-wave1v2 round 2: the changed fetch code (streamed partial, bounded decode step, declared length)
    m("ft-22", F, "out = self.d.decompress(buf, min(DECODE_STEP, self.limit - self.n + 1))",
      "out = self.d.decompress(buf, self.limit - self.n + 1)", ["test_a_xml.py"]),
    m("ft-23", F, "                self.sunk.update(b)\n", "                pass\n", TF),
    m("ft-24", F, "                                    dec.feed(chunk[:len(chunk) - (raw - wire_max)])",
      "                                    pass", TF),
    m("ft-25", F, "if cut is None and _BUDGET.sink is None and declared and declared.isdigit() and",
      "if cut is None and declared and declared.isdigit() and", TF),
    # ------------------------------------------------------------------ codes / stored forms
    m("cd-01", C, "return h == d or h == \"www.\" + d for d in own)" if False else "    return any(h == d or h == \"www.\" + d for d in own)",
      "    return any(h.endswith(d) for d in own)", TC + ["test_a_ssrf.py"]),
    m("cd-02", C, "    if kind in NEVER_CODED or kind not in KINDS:", "    if kind not in KINDS + NEVER_CODED:", TC),
    m("cd-03", C, "        return \" \".join(raw.split())\n    return raw", "        return raw\n    return raw", TC),
    m("cd-04", C, "        if len(raw) > EVIDENCE_VALUE_MAX:", "        if len(raw) > EVIDENCE_VALUE_MAX * 100:",
      TC + ["test_a_isolation.py"]),
    m("cd-05", C, "_STRUCTURAL = frozenset({\"/\", \"/*\", \"*\", \"/$\", \"/*$\"})",
      "_STRUCTURAL = frozenset({\"/\", \"/*\", \"*\", \"/$\", \"/*$\", \"/private\"})", ["test_d_outputs.py"]),
    m("cd-06", C, "    return pattern.startswith(\"/\") and \"/\" not in pattern[1:] and well_known(pattern[1:])",
      "    return pattern.startswith(\"/\")", ["test_d_outputs.py", "test_d_plants.py"]),
    m("cd-07", C, "    return low in WELL_KNOWN or bool(_SITEMAP_NAME.fullmatch(low))", "    return True", TC),
    m("cd-08", C, "    return low in WELL_KNOWN or bool(_SITEMAP_NAME.fullmatch(low))",
      "    return low in WELL_KNOWN or bool(_SITEMAP_NAME.match(low))", TC, status="behavioural"),
    m("cd-09", C, "        return CODE_RX.sub(lambda m: (values or {}).get(m.group(0), NOT_AVAILABLE), obj)",
      "        return obj", ["test_a_isolation.py", "test_d_outputs.py"]),
    m("cd-10", C, "    out = \"{c:\" + kind + \":\" + c.coder.code(kind, canonical(kind, raw)) + \"}\"",
      "    out = \"{c:\" + kind + \":\" + c.coder.code(\"seg\", canonical(kind, raw)) + \"}\"", TC),
    m("cd-11", U, "    if codes.well_known(seg):\n        return seg", "    if False:\n        return seg", TC),
    m("cd-12", U, "    if _digits(seg) >= 7:                      # phone numbers", "    if _digits(seg) >= 9:  # phone numbers",
      TC),
    m("cd-13", U, "    if seg.isdigit():\n        return \"{id}\"", "    if False:\n        return \"{id}\"", TC),
    m("cd-14", U, "    if \",\" in seg or \"=\" in seg:               # M-S5", "    if \",\" in seg:  # M-S5", TC),
    m("cd-15", U, "    if m and m.group(2).lower() in FILE_EXT:", "    if m:", TC),
    m("cd-16", U, "    return codes.code(\"seg\", seg)", "    return seg", TC + ["test_d_plants.py"]),
    m("cd-17", U, "    return h if codes.own_host(h) else codes.code(\"host\", h)", "    return h", TC),
    m("cd-18", U, "        return (scheme if scheme in OTHER_SCHEMES else \"other\") + \":\"",
      "        return scheme + \":\" + u[len(scheme) + 1:]", TC),
    m("cd-19", U, "    if len(t) > TEMPLATE_MAX:", "    if len(t) > TEMPLATE_MAX * 10:", TC),
    m("cd-20", U, "    return ctype if ctype in MEDIA_TYPES else codes.code(\"ctype\", ctype)", "    return ctype",
      ["test_b_selene.py"]),
    m("cd-21", U, "        if j - i >= 2 and sum(_digits(segs[k]) for k in range(i, j)) >= 7:",
      "        if j - i >= 2 and sum(_digits(segs[k]) for k in range(i, j)) >= 70:", TC),
    # rv-wave1v2 c05: a media type is readable only on the closed list (D-05 content_type plant)
    m("cd-22", U, "    return ctype if ctype in MEDIA_TYPES else codes.code(\"ctype\", ctype)",
      "    return ctype if ctype.startswith((\"text/\", \"application/\")) else codes.code(\"ctype\", ctype)",
      TC + ["test_d_plants.py"]),
    # ------------------------------------------------------------------ Selene
    m("se-01", S, "\"critical\" if per_path[\"/\"][\"allowed\"] is False", "\"high\" if per_path[\"/\"][\"allowed\"] is False",
      ["test_b_selene.py"]),
    m("se-02", S, "        blocked = [safe_path(p) for p, d in per_path.items() if d[\"allowed\"] is False]",
      "        blocked = [safe_path(p) for p, d in per_path.items() if d[\"allowed\"] is not True]", ["test_b_selene.py"]),
    m("se-03", S, "        elif fam[\"token\"] in bots.SEARCH_TOKENS:", "        elif False:", ["test_b_selene.py"]),
    m("se-04", S, "        if approximate:", "        if False:", ["test_b_selene.py"]),
    m("se-05", S, "    if rb[\"status_class\"] == \"unreachable\":\n        out.append",
      "    if rb[\"status_class\"] in (\"unreachable\", \"incomplete\"):\n        out.append", ["test_b_selene.py"]),
    m("se-06", S, "    if f.status >= 400:", "    if f.status > 400:", ["test_b_selene.py"]),
    m("se-07", S, "\"high\" if f.status >= 500 else \"medium\"", "\"high\" if f.status > 500 else \"medium\"",
      ["test_b_selene.py"]),
    m("se-08", S, "        sev = \"medium\" if len(chain) > 1 else \"low\"", "        sev = \"low\"", ["test_b_selene.py"]),
    m("se-09", S, "                                   \"temporary\": any(s in (302, 303, 307) for s in chain)}))",
      "                                   \"temporary\": any(s in (302, 307) for s in chain)}))", ["test_b_selene.py"]),
    m("se-10", S, "        if d in (\"noindex\", \"none\") and (bot is None or bot == t):",
      "        if d in (\"noindex\", \"none\"):", ["test_b_selene.py", "test_a_injection.py"]),
    m("se-11", S, "            if name in (\"robots\", t) and (\"noindex\" in v or \"none\" in v):",
      "            if (\"noindex\" in v or \"none\" in v):", ["test_b_selene.py"]),
    m("se-12", S, "        out.append(finding(\"NOINDEX\", \"high\" if path == \"/\" else \"medium\"",
      "        out.append(finding(\"NOINDEX\", \"medium\" if path == \"/\" else \"medium\"", ["test_b_selene.py"]),
    m("se-13", S, "    if total > 1:", "    if total > 2:", ["test_b_selene.py"]),
    m("se-14", S, "        if not ctx.same_site(host):\n            out.append(finding(\"CANONICAL_OTHER_HOST\"",
      "        if ctx.same_site(host):\n            out.append(finding(\"CANONICAL_OTHER_HOST\"", ["test_b_selene.py"]),
    m("se-15", S, "                and canon_checked < CANONICAL_CHECK_MAX:", "                and canon_checked <= CANONICAL_CHECK_MAX:",
      ["test_b_selene.py"]),
    m("se-16", S, "            elif cf.status != 200 or cf.redirects:", "            elif cf.status != 200:", ["test_b_selene.py"]),
    m("se-17", S, "        outcome = \"OK\" if fetched == len(ctx.paths) and not facts.get(\"robots_matrix_undecided\") else \"PARTIAL\"",
      "        outcome = \"OK\"", ["test_b_selene.py"]),
    m("se-18", S, "    elif not invalid:\n        out.append(finding(\"CANONICAL_MISSING\"", "    elif False:\n        out.append(finding(\"CANONICAL_MISSING\"",
      ["test_b_selene.py"]),
    m("se-19", S, "    return f\"{kind}: {pat if codes.rule_readable(pat) else codes.code('rule', pat)}\"",
      "    return f\"{kind}: {pat}\"", ["test_b_selene.py"]),
    m("se-20", S, "    if parsed is not None and parsed.lines_ignored:", "    if parsed is not None and parsed.lines_ignored > 1:",
      ["test_b_selene.py"]),
    # rv-wave1v2 #1: every same-name meta robots tag counts (primitives/parse.py, Selene's extract)
    m("se-21", "primitives/parse.py", "            have = robots_meta.setdefault(name, [])",
      "            have = robots_meta[name] = []", ["test_b_selene.py"]),
    # ------------------------------------------------------------------ Delia
    m("de-01", D, "MAX_CHILDREN = 10", "MAX_CHILDREN = 11", ["test_b_delia.py"]),
    m("de-02", D, "                if not ctx.same_site(host_of(u)):\n                    children_cross_host += 1",
      "                if False:\n                    children_cross_host += 1", ["test_b_delia.py", "test_a_ssrf.py"]),
    m("de-03", D, "    same = [u for u in declared if ctx.same_site(host_of(u))]", "    same = list(declared)",
      ["test_b_delia.py"]),
    m("de-04", D, "        if sm in seen:\n            continue", "        if False:\n            continue", ["test_b_delia.py"]),
    m("de-05", D, "            missing = f.ok and f.status in (404, 410)", "            missing = f.ok and f.status == 404",
      ["test_b_delia.py"]),
    m("de-06", D, "        if file_urls > MAX_URLS_PER_FILE:", "        if file_urls >= MAX_URLS_PER_FILE:", ["test_b_delia.py"]),
    m("de-07", D, "        if ns != NS:", "        if ns != NS and ns:", ["test_b_delia.py"]),
    m("de-08", D, "            parsed.allowed(\"Googlebot\", path, work)" if False else "            verdict = parsed.allowed(\"Googlebot\", path, work)",
      "            verdict = parsed.allowed(\"Bingbot\", path, work)", ["test_b_delia.py"]),
    m("de-09", D, "        if dt > now + timedelta(days=1):", "        if dt > now + timedelta(days=2):", ["test_b_delia.py"]),
    m("de-10", D, "    if len(values) >= 10 and len({v for v, _ in values}) == 1:",
      "    if len(values) >= 9 and len({v for v, _ in values}) == 1:", ["test_b_delia.py"]),
    m("de-11", D, "    if f.status >= 500:", "    if f.status > 500:", ["test_b_delia.py"]),
    m("de-12", D, "        if \"<!doctype\" in low or \"<!entity\" in low:\n            self.state = \"DTD\"",
      "        if \"<!entity\" in low:\n            self.state = \"DTD\"", ["test_a_xml.py"], status="equivalent",
      proof="expat refuses every DOCTYPE itself (StartDoctypeDeclHandler raises): the text check is a second layer; "
            "the document is still quarantined XML_DOCTYPE_REFUSED by the expat pass (r1b D1-D3)"),
    m("de-13", D, "        if self.n > PROTOCOL_MAX_BYTES:", "        if self.n > PROTOCOL_MAX_BYTES * 8:", ["test_b_delia.py"]),
    m("de-14", D, "        if self.z is not None and not self.z.eof:", "        if False:", ["test_b_osei.py", "test_b_delia.py"]),
    m("de-15", D, "            ctx.osei.quarantine(\"sitemap\", \"DECOMPRESS_FAILED\", sm, None, size=reader.wire_n,",
      "            ctx.osei.quarantine(\"sitemap\", \"DECOMPRESS_FAILED\", sm, None, size=0,", ["test_b_osei.py"]),
    m("de-16", D, "        if p is not None and p.scheme in (\"http\", \"https\") and ctx.same_site(p.hostname):",
      "        if p is not None and p.scheme in (\"http\", \"https\"):", ["test_b_logs.py"]),
    m("de-17", D, "        if parsed is not None and sampled < BLOCK_SAMPLE:", "        if parsed is not None:", ["test_b_delia.py"]),
    m("de-18", D, "            if verdict is False:\n                blocked.append(u)", "            if verdict is not True:\n                blocked.append(u)",
      ["test_b_delia.py"]),
    m("de-19", D, "    if f.content_type not in (\"text/plain\", \"text/markdown\"):", "    if False:", ["test_b_delia.py"]),
    m("de-20", D, "            failures += 1                                    # B-DE-02 / B-DE-16",
      "            failures += 0  # B-DE-02 / B-DE-16", ["test_b_delia.py"]),
    m("de-22", D, "            if chunk[:2] == b\"\\x1f\\x8b\":", "            if False:", ["test_b_delia.py"]),
    m("de-21", D, "    complete = not facts.get(\"sitemap_fetch_failures\") and not facts.get(\"robots_unchecked\")",
      "    complete = True", ["test_b_delia.py"]),
    # rv-wave1v2 round 2: the changed Delia code
    m("de-23", D, "            sampled += 1\n", "            pass\n", ["test_b_delia.py"]),
    m("de-24", D, "WIRE_MAX = PROTOCOL_MAX_BYTES + 1024 * 1024", "WIRE_MAX = PROTOCOL_MAX_BYTES", ["test_b_delia.py"]),
    m("de-25", D, "out = self.z.decompress(buf, min(DECODE_STEP, PROTOCOL_MAX_BYTES - self.n + 1))",
      "out = self.z.decompress(buf, PROTOCOL_MAX_BYTES - self.n + 1)", ["test_a_xml.py"]),
    m("de-26", D, "            ctx.osei.quarantine(\"sitemap\", \"TOO_LARGE\", sm, None, partial={",
      "            (lambda *a, **k: None)(\"sitemap\", \"TOO_LARGE\", sm, None, partial={", ["test_b_delia.py"]),
    m("de-27", D, "        self.decoded.update(b)                      # B-OS-01",
      "        self.decoded.update(b[:max(0, len(b) - (self.n - PROTOCOL_MAX_BYTES))])  # B-OS-01",
      ["test_b_delia.py"]),
    # ------------------------------------------------------------------ Roman
    m("ro-01", RO, "TITLE_MAX, TITLE_MIN = 60, 10", "TITLE_MAX, TITLE_MIN = 61, 10", ["test_b_roman.py"]),
    m("ro-02", RO, "TITLE_MAX, TITLE_MIN = 60, 10", "TITLE_MAX, TITLE_MIN = 60, 9", ["test_b_roman.py"]),
    m("ro-03", RO, "RAW_TEXT_MIN = 500", "RAW_TEXT_MIN = 499", ["test_b_roman.py"]),
    m("ro-04", RO, "        if len(h1) != 1:", "        if len(h1) > 1:", ["test_b_roman.py"]),
    m("ro-05", RO, "        if prev and h[\"level\"] > prev + 1:", "        if prev and h[\"level\"] > prev + 2:", ["test_b_roman.py"]),
    m("ro-06", RO, "\"STRUCTURED_DATA_ABSENT\", \"medium\" if path == \"/\" else \"low\"",
      "\"STRUCTURED_DATA_ABSENT\", \"low\" if path == \"/\" else \"low\"", ["test_b_roman.py"]),
    m("ro-07", RO, "        if org_nodes and not any(n[\"props\"][\"same_as\"] for n in org_nodes):", "        if False:",
      ["test_b_roman.py"]),
    m("ro-08", RO, "    if pages and not entity_seen and \"/\" in pages", "    if False and pages and not entity_seen and \"/\" in pages",
      ["test_b_roman.py"]),
    m("ro-09", RO, "        elif lang == \"invalid\":", "        elif False:", ["test_b_roman.py"]),
    m("ro-10", RO, "            \"lang_declared\": safe_lang(x[\"lang\"]) not in (None, \"invalid\"),",
      "            \"lang_declared\": bool(x[\"lang\"]),", ["test_b_roman.py"]),
    m("ro-11", RO, "    if lang not in cldr(\"language\"):", "    if False:", ["test_b_roman.py"]),
    m("ro-12", RO, "    if m.group(\"region\") and m.group(\"region\")[1:].lower() in cldr(\"region\"):",
      "    if m.group(\"region\"):", ["test_b_roman.py"]),
    m("ro-13", RO, "    return code.lower() in cldr(\"script\")", "    return True", ["test_b_roman.py"]),
    m("ro-14", RO, "                if t in TYPE_ALLOWLIST:", "                if True:", ["test_b_roman.py"]),
    m("ro-15", RO, "            \"crawler_allowed\": allowed, \"indexable\": not noindex_for(f, x, token),",
      "            \"crawler_allowed\": allowed, \"indexable\": True,", ["test_b_roman.py", "test_b_selene.py"],
      status="equivalent", proof="the readiness 'indexable' check is not asserted per engine by a B-RO pass criterion "
                                 "beyond B-SE-12 (Selene's NOINDEX is the measured finding); r1 R-mutant 'indexable "
                                 "forced true' was killed by test_wave1_cert, kept there"),
    m("ro-16", RO, "        if x[\"text_length\"] < RAW_TEXT_MIN:", "        if x[\"text_length\"] <= RAW_TEXT_MIN:", ["test_b_roman.py"]),
    m("ro-17", RO, "    outcome = \"OK\" if read and read == len(pages) else (\"PARTIAL\" if read else \"INSUFFICIENT_EVIDENCE\")",
      "    outcome = \"OK\" if read else \"INSUFFICIENT_EVIDENCE\"", ["test_b_roman.py"]),
    m("ro-18", RO, "        title = \" \".join((x[\"title\"] or \"\").split())", "        title = x[\"title\"] or \"\"",
      ["test_b_roman.py"], status="equivalent",
      proof="parse._s already collapses white space in the extracted title (r1c roman.py:157 equivalent, R-15): the "
            "two expressions give the same string for every extract"),
    m("ro-19", RO, "        if not title:\n            findings.append(finding(\"TITLE_MISSING\"", "        if False:\n            findings.append(finding(\"TITLE_MISSING\"",
      ["test_b_roman.py"]),
    m("ro-20", RO, "        if not x[\"description\"]:", "        if False:", ["test_b_roman.py"]),
    # rv-wave1v2 o01: a white-space-only lang in the extract is missing whatever the parser did to it
    m("ro-21", RO, "        if not (x[\"lang\"] or \"\").strip():", "        if not x[\"lang\"]:", ["test_b_roman.py"]),
    # rv-wave1v2 round 2: the changed lang code
    m("ro-22", RO, "            if sub in variants:\n                return True", "            if False:\n                return True",
      ["test_b_roman.py"]),
    m("ro-23", RO, "            if sub in singletons:\n                return True",
      "            if False:\n                return True", ["test_b_roman.py"]),
    m("ro-24", RO, "    if low in GRANDFATHERED or _PRIVATE_USE.fullmatch(t):", "    if _PRIVATE_USE.fullmatch(t):",
      ["test_b_roman.py"]),
    m("ro-25", RO, "    if low in GRANDFATHERED or _PRIVATE_USE.fullmatch(t):", "    if low in GRANDFATHERED:",
      ["test_b_roman.py"]),
    # ------------------------------------------------------------------ entity check
    m("en-01", E, "                    elif norm(field, site) == norm(field, cval):", "                    elif site == cval:",
      ["test_b_entity.py"]),
    m("en-02", E, "REGIONS = {\"california\": \"ca\"}", "REGIONS = {}", ["test_b_entity.py"]),
    m("en-03", E, "    if read and nodes_seen == 0:", "    if False:", ["test_b_entity.py"]),
    m("en-04", E, "            \"on_site_code\": codes.code(\"entity\", ns or \"\")}", "            \"on_site_code\": ns}",
      ["test_b_entity.py"]),
    m("en-05", E, "                                                detail={\"field\": field}))",
      "                                                detail={\"field\": field, \"canonical\": cval}))", ["test_b_entity.py"]),
    m("en-06", E, "STREET_WORDS = {\"street\": \"st\",", "STREET_WORDS = {\"streetx\": \"st\",", ["test_b_entity.py"]),
    m("en-07", E, "                        if field in (\"street_address\", \"locality\", \"region\") and \"LocalBusiness\" not in \\",
      "                        if field in (\"street_address\", \"locality\", \"region\", \"telephone\") and \"LocalBusiness\" not in \\",
      ["test_b_entity.py"]),
    m("en-08", E, "    if entity is None:\n        return envelope(AGENT, TASK, \"INSUFFICIENT_EVIDENCE\"",
      "    if entity is None:\n        return envelope(AGENT, TASK, \"OK\"", ["test_b_entity.py"]),
    m("en-09", E, "    s = re.sub(r\"[^\\w\\s]\", \" \", v.casefold())", "    s = v.casefold()", ["test_b_entity.py"]),
    m("en-10", E, "    return _finding(*a, url=safe_url(url), **k)", "    return _finding(*a, url=url, **k)",
      ["test_b_entity.py"]),
    m("en-11", E, "\"case_folded_equal\": s.casefold() == c.casefold(),", "\"case_folded_equal\": True,",
      ["test_b_entity.py"]),
    m("en-12", E, "\"whitespace_folded_equal\": \" \".join(s.casefold().split())", "\"whitespace_folded_equal\": True or \"\".join(s.casefold().split())",
      ["test_b_entity.py"]),
    m("en-13", E, "\"on_site_length\": len(s),", "\"on_site_length\": 0,", ["test_b_entity.py"]),
    m("en-14", E, "        return phone_digits(v) or None", "        return v or None", ["test_b_entity.py"]),
    m("en-15", E, "    outcome = \"OK\" if read == len(pages) and read else", "    outcome = \"OK\" if read else",
      ["test_b_entity.py"]),
    m("en-16", E, "    if home[\"state\"] == \"unknown\" and outcome == \"OK\":", "    if False:", ["test_b_entity.py"]),
    m("en-17", E, "\"HOMEPAGE_PHONE_NOT_IN_TEXT_SAMPLE\", \"low\", \"measured\", \"TEST\"",
      "\"HOMEPAGE_PHONE_NOT_IN_TEXT_SAMPLE\", \"low\", \"measured\", \"ACT\"", ["test_b_entity.py"]),
    m("en-18", E, "                if not set(n[\"types\"]) & set(ORG_TYPES):", "                if False:",
      ["test_b_entity.py"]),
    m("en-19", E, "finding(\"ENTITY_FIELD_MISMATCH\", \"high\", \"measured\", \"ACT\"",
      "finding(\"ENTITY_FIELD_MISMATCH\", \"medium\", \"measured\", \"ACT\"", ["test_b_entity.py"]),
    m("en-20", E, "\"canonical_source\": entity[\"fields\"][field][\"source\"],", "\"canonical_source\": None,",
      ["test_b_entity.py"]),
    # rv-wave1v2 e03: a plain Organization node is not asked for a region
    m("en-21", E, "if field in (\"street_address\", \"locality\", \"region\") and \"LocalBusiness\" not in",
      "if field in (\"street_address\", \"locality\") and \"LocalBusiness\" not in", ["test_b_entity.py"]),
    # ------------------------------------------------------------------ log task
    m("lg-01", L, "            if verdict is False:\n                fam[\"robots_blocked\"] += 1",
      "            if verdict is not True:\n                fam[\"robots_blocked\"] += 1", ["test_b_logs.py"]),
    m("lg-02", L, "        elif robots_expected:\n            out[\"robots_unchecked\"] += 1", "        elif False:\n            pass",
      ["test_a_logs.py"], status="equivalent",
      proof="the robots.txt text of an ingest is in its vault inputs blob (B-LG-09) and is read back after a restart, "
            "so 'robots expected but not in memory' cannot occur on this base (A-30 interpretation in the report)"),
    m("lg-03", L, "        if len(raw) > LINE_MAX:", "        if len(raw) >= LINE_MAX:", ["test_a_logs.py"]),
    m("lg-04", L, "        if not 100 <= rec[\"status\"] <= 599:", "        if not 100 <= rec[\"status\"] <= 999:", ["test_b_logs.py"]),
    m("lg-05", L, "                i = sample.get(codes.code(\"path\", exact))", "                i = sample.get(exact)",
      ["test_b_logs.py"]),
    m("lg-06", L, "            if tpl in fam[\"templates\"] or len(fam[\"templates\"]) < TEMPLATES_PER_FAMILY:",
      "            if True:", ["test_b_logs.py"]),
    m("lg-07", SL, "SERVER_ERROR_SHARE = 0.05", "SERVER_ERROR_SHARE = 0.051", ["test_b_logs.py"]),
    m("lg-08", SL, "SERVER_ERROR_MIN_REQUESTS = 20", "SERVER_ERROR_MIN_REQUESTS = 19", ["test_b_logs.py"]),
    m("lg-09", SL, "                if err / f[\"requests\"] > SERVER_ERROR_SHARE:", "                if err / f[\"requests\"] >= SERVER_ERROR_SHARE:",
      ["test_b_logs.py"]),
    m("lg-10", SL, "        if totals[\"lines\"] and not any(totals[\"families\"].get(t) for t in SEARCH_TOKENS):",
      "        if False:", ["test_b_logs.py"]),
    m("lg-11", SL, "            uncrawled = [p for i, p in enumerate(sample) if i not in hit]", "            uncrawled = []",
      ["test_b_logs.py"]),
    m("lg-12", SL, "    if isinstance(sample, dict) and sample.get(\"codes_key_id\") == key_id:",
      "    if isinstance(sample, dict):", ["test_b_logs.py"], status="equivalent",
      proof="within one test run the codes key never rotates between the audit and the ingest, so the guard is not "
            "exercised by the fast tier; the rotation case is pinned by test_b_drift B-DR-05 at the report level"),
    m("lg-13", L, "        if rec[\"ua\"] is None:\n            out[\"no_user_agent\"] += 1", "        if False:\n            pass",
      ["test_b_logs.py"]),
    m("lg-14", L, "    return f\"{s // 100}xx\" if 100 <= s <= 599 else \"other\"", "    return \"2xx\"", ["test_b_logs.py"]),
    m("lg-15", L, "        tpl = _su.template_path(rec[\"path\"])", "        tpl = rec[\"path\"]", ["test_b_logs.py"]),
    m("lg-16", SL, "            if f[\"spoofed\"]:", "            if False:", ["test_b_logs.py"], status="equivalent",
      proof="the DNS verification port is NOT_CONNECTED in the conformance service, so no request is ever 'spoofed'; "
            "B-LG-02 'spoofed' is pinned by tests/test_logs.py with a fake verifier"),
    m("lg-17", SL, "        if robots_ok and unchecked:", "        if False:", ["test_b_logs.py"], status="equivalent",
      proof="robots_unchecked needs the chunk's counted work to run out (ROBOTS_CHUNK_WORK) or a lost robots text; "
            "pinned by tests/test_wave1_cert.py::test_log_chunk_robots_budget"),
    m("lg-18", L, "        if tok is None:\n            out[\"non_bot\"] += 1", "        if tok is None:\n            pass",
      ["test_b_logs.py"]),
    m("lg-19", L, "        if len(raw) > LINE_MAX:", "        if len(raw) > LINE_MAX * 2:", ["test_b_logs.py"]),
    m("lg-20", SL, "            if blocked:\n                findings.append", "            if False:\n                findings.append",
      ["test_b_logs.py"], status="equivalent",
      proof="ROBOTS_DISALLOWED_PATHS_REQUESTED is not asserted by a B-LG pass criterion count test here; the count "
            "itself (robots_disallowed_hits) is pinned by B-LG-03"),
    # ------------------------------------------------------------------ Osei-lite and drift
    m("os-01", OS, "\"sitemap\": timedelta(days=1)", "\"sitemap\": timedelta(days=7)", ["test_b_osei.py"]),
    m("os-02", OS, "        if len(self.quarantined) < QUARANTINED_MAX:", "        if True:", ["test_b_osei.py"]),
    m("os-03", OS, "        if len(self.observations) < OBSERVATIONS_MAX:", "        if True:", ["test_b_osei.py"]),
    m("os-04", OS, "        if basis is not None and basis not in SIZE_BASES:", "        if False:", ["test_b_osei.py"]),
    m("os-05", OS, "            q[\"size_basis\"], q[\"truncated\"] = \"read_before_stop\", True",
      "            q[\"size_basis\"], q[\"truncated\"] = \"decoded\", True", ["test_b_osei.py"]),
    m("os-06", OS, "        if reason not in REASONS:", "        if False:", ["test_b_osei.py"], status="equivalent",
      proof="every caller passes a reason from REASONS (closed constants in the code); no test can pass another"),
    m("os-07", OS, "            q[\"bytes\"], q[\"sha256\"] = len(data), hashlib.sha256(data).hexdigest()",
      "            q[\"bytes\"], q[\"sha256\"] = 0, hashlib.sha256(b\"\").hexdigest()", ["test_b_osei.py"]),
    m("os-08", OS, "\"entity_record\": timedelta(days=180)", "\"entity_record\": timedelta(days=7)", ["test_b_osei.py"]),
    m("dr-01", DR, "    if ka != kb:", "    if False:", ["test_b_drift.py"]),
    m("dr-02", DR, "    ok = (\"OK\", \"PARTIAL\")", "    ok = (\"OK\", \"PARTIAL\", \"FAILED\", \"KILLED\")", ["test_b_drift.py"]),
    m("dr-03", DR, "        if oc and om:", "        if oc or om:", ["test_b_drift.py"]),
    m("dr-04", DR, "OVERTURNING = (\"MEASUREMENT_ERROR\", \"SAMPLING_NOISE\")",
      "OVERTURNING = (\"MEASUREMENT_ERROR\", \"SAMPLING_NOISE\", \"TOOL_FAILURE\")", ["test_b_drift.py"]),
    m("dr-05", DR, "        if moved and page_moved:", "        if False:", ["test_b_drift.py"]),
    m("dr-06", DR, "        if moved and changed is not None:", "        if False:", ["test_b_drift.py"]),
    m("dr-07", DR, "    if any(s not in (None, \"OK\") for s in states + [mine]):", "    if False:", ["test_b_drift.py"]),
    m("dr-08", DR, "            or code.startswith(\"PAGE_NOT_READ_\") or code in INCOMPLETE_CODES \\",
      "            or code in INCOMPLETE_CODES \\", ["test_b_drift.py"]),
    m("dr-09", DR, "    if code.endswith((\"_UNREADABLE\", \"_UNREACHABLE\", \"_UNCHECKED\", \"_INCOMPLETE\", \"_APPROXIMATE\")) \\",
      "    if code.endswith((\"_UNREADABLE\",)) \\", ["test_b_drift.py"]),
    m("dr-10", DR, "        if ca in (\"unreachable\", \"too_large\", \"incomplete\") or cb in (\"unreachable\", \"too_large\", \"incomplete\"):",
      "        if ca == \"unreachable\":", ["test_b_drift.py"]),
    m("dr-11", DR, "            if la in (None, \"unreadable\") or lb in (None, \"unreadable\"):", "            if False:",
      ["test_b_drift.py"]),
    m("dr-12", DR, "        if xa or xb or fa.get(\"robots_unchecked\") or fb.get(\"robots_unchecked\"):", "        if False:",
      ["test_b_drift.py"]),
]

GROUPS = {"robots": [R], "fetch": [F], "codes": [C, U], "selene": [S, "primitives/parse.py"], "logs": [L, SL], "delia": [D], "roman": [RO],
          "entity_check": [E], "osei_drift": [OS, DR]}
MODULES = list(GROUPS)
MIN_PER_MODULE = {}                     # every module: at least 20 (brief)


def _copy(work: Path) -> Path:
    dst = work / "svc"
    if dst.exists():
        import shutil
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    tar = work / "svc.tar"
    with tarfile.open(tar, "w") as t:
        t.add(str(SVC / "src"), arcname="src")
        t.add(str(SVC / "tests"), arcname="tests")
        t.add(str(SVC / "pytest.ini"), arcname="pytest.ini")
        t.add(str(SVC / "docs"), arcname="docs")
    with tarfile.open(tar) as t:
        t.extractall(dst, filter="data")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--only")
    ap.add_argument("--check", action="store_true", help="only check that every anchor occurs exactly once")
    a = ap.parse_args()
    if a.check:
        bad = [mu["id"] for mu in MUTANTS if (SVC / "src" / mu["file"]).read_text(encoding="utf-8")
               .count(mu["anchor"]) != 1]
        per = {g: sum(1 for mu in MUTANTS if mu["file"] in fs) for g, fs in GROUPS.items()}
        print(f"mutants {len(MUTANTS)}; per module {per}; anchors not unique {bad}")
        return 1 if bad else 0
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    survivors, killed, ran = [], 0, 0
    for mu in MUTANTS:
        if mu["status"] != "behavioural" or (a.only and mu["id"] != a.only):
            continue
        dst = _copy(work)
        f = dst / "src" / mu["file"]
        text = f.read_text(encoding="utf-8")
        if text.count(mu["anchor"]) != 1:
            print(f"{mu['id']}: ANCHOR NOT UNIQUE", flush=True)
            survivors.append(mu["id"])
            continue
        f.write_text(text.replace(mu["anchor"], mu["replacement"]), encoding="utf-8")
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "-m",
                            "not lead_gate", *mu["tests"]], cwd=dst, capture_output=True, text=True, timeout=900,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        ran += 1
        if r.returncode == 0:
            survivors.append(mu["id"])
            print(f"{mu['id']}: SURVIVED ({time.monotonic() - t0:.0f} s)", flush=True)
        elif r.returncode != 1:                   # an error (collection, usage), not a failing test: not a kill
            survivors.append(mu["id"])
            print(f"{mu['id']}: ERROR rc={r.returncode} ({time.monotonic() - t0:.0f} s)", flush=True)
        else:
            killed += 1
            print(f"{mu['id']}: killed ({time.monotonic() - t0:.0f} s)", flush=True)
    print(f"ran {ran} killed {killed} survivors {survivors}", flush=True)
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
