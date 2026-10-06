"""Response and pitch assembly (ADR 0016 decision 10).

Decides: the exact document of a bid / RFP / RFQ response or a pitch, and the SHA-256 Andre's approval binds. A
response is an ordered list of parts, each either an APPROVED boilerplate block (cited by id and version, with the
content hash Andre approved) or custom text, which is approved only as part of Andre's approval of the whole
response. A block that is not approved, or whose current content no longer hashes to what Andre approved, refuses the
assembly. The document hash covers the pursuit, brand, kind, response version, every part and every sensitivity flag,
so ANY change after approval (an edited block, an edited custom paragraph, a reordered part) is a different hash and
the approval no longer matches. Never: writes text, approves, or submits."""

from __future__ import annotations

import hashlib
import json
from typing import Optional

NUMBER = 4
NAME = "response_assembly"
DECIDES = "the exact response document and the content hash an approval binds"

MAX_PARTS = 120
MAX_TOTAL_CHARS = 400_000


class AssemblyProblem(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def block_sha256(brand: str, title: str, text: str) -> str:
    return sha({"brand": brand, "title": title, "text": text})


def block_usable(block: dict, version: dict, brand: str) -> Optional[str]:
    """None when this block version is approved, unedited since, and usable for ``brand``."""
    if block["brand"] not in (brand, "both"):
        return "BLOCK_BRAND_MISMATCH"
    if version["status"] != "approved" or not version.get("approved_sha256"):
        return "BLOCK_NOT_APPROVED"
    if version["approved_sha256"] != version["content_sha256"] or \
            block_sha256(block["brand"], version["title"], version["text"]) != version["approved_sha256"]:
        return "BLOCK_HASH_MISMATCH"
    return None


def assemble(pursuit: dict, response_version: int, parts: list, blocks: dict, flags: list) -> dict:
    """The canonical document. ``parts`` as stored: ``{"block_id", "version"}`` or ``{"custom": text}``."""
    if not parts or len(parts) > MAX_PARTS:
        raise AssemblyProblem("RESPONSE_EMPTY" if not parts else "RESPONSE_TOO_LARGE")
    out, total = [], 0
    for p in parts:
        if "custom" in p:
            text = p["custom"]
            total += len(text)
            out.append({"type": "custom", "text": text, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
            continue
        b = blocks.get(p["block_id"])
        v = (b or {}).get("versions", {}).get(str(p["version"]))
        if b is None or v is None:
            raise AssemblyProblem("BLOCK_NOT_FOUND")
        problem = block_usable(b, v, pursuit["brand"])
        if problem:
            raise AssemblyProblem(problem)
        total += len(v["text"])
        out.append({"type": "block", "block_id": p["block_id"], "version": p["version"],
                    "content_sha256": v["approved_sha256"]})
    if total > MAX_TOTAL_CHARS:
        raise AssemblyProblem("RESPONSE_TOO_LARGE")
    doc = {"pursuit_id": pursuit["pursuit_id"], "brand": pursuit["brand"], "kind": pursuit["kind"],
           "response_version": response_version, "parts": out, "flags": sorted(set(flags))}
    return {"doc": doc, "content_sha256": sha(doc)}


def rendered_text(doc: dict, blocks: dict) -> str:
    """The text a submission carries: each part in order, blocks at their approved version."""
    chunks = []
    for p in doc["parts"]:
        if p["type"] == "custom":
            chunks.append(p["text"])
        else:
            chunks.append(blocks[p["block_id"]]["versions"][str(p["version"])]["text"])
    return "\n\n".join(chunks)
