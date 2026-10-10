"""
Crawler / bot families Selene checks robots.txt against — versioned DATA, not code (ADR 0017 decision 14).

SOURCE NOTE: compiled from each operator's own public crawler documentation as known to this service's authors on
the version date below. Operators add, rename and re-purpose tokens; some entries are robots.txt CONTROL TOKENS
that no crawler sends as a User-Agent (Google-Extended, Applebot-Extended). This list is NOT complete and is not a
claim about how any operator uses what it fetches. Update it by adding a new version; never edit a past version's
meaning in place.
"""

from __future__ import annotations

VERSION = "2026-10-09.1"
SOURCE_NOTE = ("Operators' public crawler documentation as known on 2026-10-09; not complete; tokens and purposes "
               "change; control tokens are robots.txt-only and never appear as a User-Agent")

# purpose: search | ai_training | ai_search | ai_user_fetch | training_control | dataset
FAMILIES = (
    {"token": "Googlebot", "operator": "Google", "purpose": "search", "engine": "google"},
    {"token": "Google-Extended", "operator": "Google", "purpose": "training_control", "engine": "google",
     "note": "control token for use of content in Google's AI models; not a crawler User-Agent"},
    {"token": "Bingbot", "operator": "Microsoft", "purpose": "search", "engine": "bing"},
    {"token": "GPTBot", "operator": "OpenAI", "purpose": "ai_training", "engine": "openai"},
    {"token": "OAI-SearchBot", "operator": "OpenAI", "purpose": "ai_search", "engine": "openai"},
    {"token": "ChatGPT-User", "operator": "OpenAI", "purpose": "ai_user_fetch", "engine": "openai"},
    {"token": "ClaudeBot", "operator": "Anthropic", "purpose": "ai_training", "engine": "anthropic"},
    {"token": "Claude-SearchBot", "operator": "Anthropic", "purpose": "ai_search", "engine": "anthropic"},
    {"token": "Claude-User", "operator": "Anthropic", "purpose": "ai_user_fetch", "engine": "anthropic"},
    {"token": "PerplexityBot", "operator": "Perplexity", "purpose": "ai_search", "engine": "perplexity"},
    {"token": "Perplexity-User", "operator": "Perplexity", "purpose": "ai_user_fetch", "engine": "perplexity"},
    {"token": "Applebot", "operator": "Apple", "purpose": "search", "engine": "apple"},
    {"token": "Applebot-Extended", "operator": "Apple", "purpose": "training_control", "engine": "apple",
     "note": "control token for use of content in Apple's AI models; not a crawler User-Agent"},
    {"token": "CCBot", "operator": "Common Crawl", "purpose": "dataset", "engine": None},
)
SEARCH_TOKENS = ("Googlebot", "Bingbot")
ENGINE_SEARCH_BOT = {"google": "Googlebot", "bing": "Bingbot", "openai": "OAI-SearchBot",
                     "anthropic": "Claude-SearchBot", "perplexity": "PerplexityBot"}


def by_token(token: str) -> dict:
    for f in FAMILIES:
        if f["token"].lower() == token.lower():
            return f
    raise KeyError(token)
