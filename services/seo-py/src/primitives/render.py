"""
Primitive: render — a SEPARATE port from fetch, with explicit combined states (ADR 0017 decision 11).

No headless renderer is wired in Wave 1 (config.NOT_BUILT: SEO_RENDERER), so production always reports
``RAW_OK_RENDER_NOT_CONNECTED`` for a page that fetched: the audit reads the raw HTML only and says that
JavaScript-inserted content was not seen. When a renderer port is connected (a later wave), it returns
``{"state": "OK", "html": ...}`` or ``{"state": "FAILED"}`` and the combined states below apply.

States:
  RAW_FAILED                     the fetch itself failed (no render attempted)
  RAW_OK_RENDER_NOT_CONNECTED    raw HTML only
  RAW_OK_RENDER_FAILED           the renderer was asked and failed
  RAW_OK_RENDER_OK               both, and they agree on the essentials
  JS_DEPENDENT                   essentials (title, h1, canonical, most of the text) only appear after rendering
"""

from __future__ import annotations

from typing import Optional

from primitives import Killed
from primitives.parse import parse_html

STATES = ("RAW_FAILED", "RAW_OK_RENDER_NOT_CONNECTED", "RAW_OK_RENDER_FAILED", "RAW_OK_RENDER_OK", "JS_DEPENDENT")
JS_TEXT_RATIO = 0.5


def render_state(fetch_result, raw_extract: Optional[dict], renderer, guard=None) -> dict:
    if not fetch_result.ok or raw_extract is None:
        return {"state": "RAW_FAILED"}
    if renderer is None or not getattr(renderer, "connected", False):
        return {"state": "RAW_OK_RENDER_NOT_CONNECTED"}
    if guard is not None:
        guard(capability="render")
    try:
        out = renderer.render(fetch_result.final_url or fetch_result.url, fetch_result.text())
    except Killed:
        raise
    except Exception as exc:                      # a renderer port failure is a state, never a crash
        return {"state": "RAW_OK_RENDER_FAILED", "detail": type(exc).__name__}
    if not isinstance(out, dict) or out.get("state") != "OK" or not isinstance(out.get("html"), str):
        return {"state": "RAW_OK_RENDER_FAILED"}
    rendered = parse_html(out["html"], fetch_result.final_url or fetch_result.url)
    missing = []
    if rendered.get("title") and not raw_extract.get("title"):
        missing.append("title")
    if any(x["level"] == 1 for x in rendered["headings"]) and not any(x["level"] == 1 for x in raw_extract["headings"]):
        missing.append("h1")
    if rendered.get("canonicals") and not raw_extract.get("canonicals"):
        missing.append("canonical")
    rt, tt = rendered["text_length"], raw_extract["text_length"]
    if rt > 0 and tt / rt < JS_TEXT_RATIO:
        missing.append("text")
    if missing:
        return {"state": "JS_DEPENDENT", "only_after_render": missing, "rendered_extract": rendered}
    return {"state": "RAW_OK_RENDER_OK", "rendered_extract": rendered}
