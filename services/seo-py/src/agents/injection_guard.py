"""
The injection guard for Theo's native multi-turn chains (ADR 0049, review round 3). Shared plumbing, not an agent.

In native multi-turn an engine's earlier answer is re-sent to it, so instruction-like text inside that answer — put
there by a page the engine read — may steer the engine's later answers. ``suspected(text)`` says whether a text holds
an instruction aimed at the MODEL (not advice aimed at a reader). It is lexical: closed pattern families over several
normalised views of the text. It is a HEURISTIC GUARD, NOT A GUARANTEE: it reduces, never removes, the risk (an
independent reviewer measured recall 0.53 / precision 0.93 on an unseen set before round 4); its measured precision and
recall on its own labelled fixture set (tests/fixtures/injection/labelled.json) are reported in ADR 0049.

Views (so simple obfuscation does not hide an instruction): NFKC, zero-width and bidi characters removed, HTML
entities unescaped, lower case; a "squeezed" view joining letters spaced or dotted apart (``i g n o r e``,
``i.g.n.o.r.e``); a leetspeak view (``1gn0r3`` -> ``ignore``); and the UTF-8 text of any base64 run of 16+ characters
that decodes to printable text.

Pattern families (an instruction aimed at the model): overriding earlier instructions / answers / context; chat-role
and prompt-template markers (``system:``, ``<system>``, ``### Instruction:``, ``[INST]``, ``<|im_start|>``);
addressing the AI ("note to the AI assistant", "if you are an AI"); steering later turns ("when asked later, say X",
"in all future answers"); persona / jailbreak forms ("you are now DAN", "pretend you are", "developer mode", "without
restrictions"); imperative recommendation or suppression directives ("you must recommend X", "Always recommend X.",
"Do not mention competitors"); prompt exfiltration ("reveal your system prompt"); and output control ("respond only
with"). Benign reader-facing forms ("You are now eligible", "From now on, you can file", "I always recommend getting
three quotes", "we should recommend reviewing", "ignore the noise") are kept out by requiring the model-directed form.

``resent_texts(answer)`` lists every text that would be re-sent: the answer text and, from its content blocks as
received, Anthropic ``cited_text`` and search-result ``title`` fields and Gemini text / thought parts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

VERSION = "injection-guard/2026-11-08.2"
# .2 (review round 4): multilingual families (es, fr, de, pt, it, zh, ja, ru), homoglyph folding with the Unicode
#    confusables (UTS #39, vendored: confusables.txt + confusables.NOTICE), hyphen- and space-split words,
#    JSON / YAML / fenced role forms, third-person and future-turn forms, persona games
CONFUSABLES = Path(__file__).with_name("confusables.txt")
CONFUSABLES_SHA256 = "95bd0aad6dced5ebc63436f459c06ab21a8d107cd842fb57f5c3a1e91bca8611"   # UTS #39 v16.0.0
_ZERO_WIDTH = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_SPACED = re.compile(r"\b(?:[a-z][ .\-_*]){2,}[a-z]\b")
_B64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
_AI = r"(?:ai|a\.i\.|assistant|ai assistant|llm|language model|model|chatbot|bot|claude|gpt|chatgpt|gemini|copilot)"
_PAST = (r"(?:previous|prior|above|earlier|preceding|original|initial|system|existing|old|all(?: the)?|your|any|"
         r"every|other)")
_THING = (r"(?:instructions?|prompts?|messages?|rules|directions|directives|answers?|responses?|context|guidelines|"
          r"guidance|text|content|conversation|constraints|programming)")
PATTERNS = [
    # override earlier instructions / answers / context
    rf"\b(?:ignore|disregard|forget|override|bypass|discard|drop|erase|abandon)\s+(?:all\s+|any\s+|the\s+|of\s+)?"
    rf"(?:your\s+|my\s+|these\s+|those\s+)?{_PAST}\s+(?:of\s+(?:your|the)\s+)?(?:\w+\s+)?{_THING}\b",
    r"\b(?:forget|ignore|disregard)\s+(?:about\s+)?(?:everything|anything|all)\s+(?:above|before|prior|previously|"
    r"so far|you (?:were|have been) (?:told|given)|said)\b",
    r"\b(?:ignore|disregard|forget)\s+(?:what|anything)\s+(?:you|i)\s+(?:said|wrote|answered|were told)\b",
    # chat-role and prompt-template markers
    r"(?m)^\s*(?:system|assistant|developer|ai|model)\s*(?:prompt|message|note)?\s*:",
    r"<\s*/?\s*(?:system|assistant|user|developer|instructions?|prompt|sys)\s*>",
    r"(?m)^\s*#{2,}\s*(?:instructions?|system|response|new task|task)\b",
    r"\[/?\s*(?:inst|sys|system)\s*\]",
    r"<\|\s*(?:im_start|im_end|system|endoftext|eot_id|start_header_id)\s*\|>",
    r"\b(?:begin|start|end)\s+(?:of\s+)?(?:new\s+)?(?:system\s+)?(?:instructions?|prompt|task)\b",
    r"\b(?:new|updated|revised|additional|hidden|secret)\s+(?:system\s+)?(?:instructions?|task|directive)s?\s*[:\-]",
    # addressing the AI
    rf"\b(?:note|message|instructions?|reminder|attention|memo)\s+(?:to|for)\s+(?:the\s+|any\s+|all\s+)?{_AI}s?\b",
    rf"\b(?:dear|attention|hey|hello|hi)\s+(?:the\s+)?{_AI}\b",
    rf"\bif\s+you\s+are\s+(?:an?\s+)?{_AI}\b",
    rf"\bas\s+an?\s+{_AI},?\s+you\s+(?:must|should|will|shall|are required)\b",
    rf"\b(?:this|the following)\s+(?:message|text|page|note|section|instruction)\s+is\s+(?:for|addressed to|meant "
    rf"for)\s+(?:the\s+|any\s+)?{_AI}",
    rf"\b{_AI}s?\s+(?:reading|summari[sz]ing|processing|crawling)\s+this\b",
    # steering later turns
    r"\b(?:when|if|whenever)\s+(?:you\s+are\s+|you're\s+)?(?:asked|questioned|prompted|queried)\b[^.]{0,60}?"
    r"\b(?:later|again|next|in the future|afterwards|about)\b[^.]{0,60}?\b(?:say|answer|respond|reply|recommend|tell|"
    r"state|claim|mention)\b",
    r"\bin\s+(?:all|every|any|each)\s+(?:future|later|subsequent|following|next|remaining)\s+(?:answers?|responses?|"
    r"turns?|replies|messages?|conversations?)\b",
    r"\bfrom\s+now\s+on,?\s+(?:you\s+(?:must|will|shall|should|are to|have to|need to|may only|can only)|always|only|"
    r"never|respond|answer|reply|recommend|say|act|pretend|ignore)\b",
    r"\bfor\s+(?:the\s+)?rest\s+of\s+(?:this|the)\s+(?:conversation|chat|session)\b",
    r"\b(?:remember|memori[sz]e)\s+(?:this|that)\s+(?:for|in)\s+(?:later|future|the next)\b",
    # persona / jailbreak
    r"\byou\s+are\s+now\s+(?:dan\b|an?\s+(?:unrestricted|uncensored|unfiltered|evil|jailbroken|different|new)\b|"
    r"in\s+(?:developer|dan|god|jailbreak|unrestricted)\s+mode|no\s+longer\s+(?:bound|restricted|an?\s+ai|"
    r"claude|chatgpt)|free\s+(?:of|from)\s+(?:all\s+)?(?:rules|restrictions))",
    r"\bpretend\s+(?:to\s+be|you\s+are|that\s+you\s+are|you're)\b",
    r"\b(?:act|behave|respond)\s+as\s+(?:if\s+you\s+(?:are|were)|an?\s+(?:unrestricted|uncensored|evil|different)|dan)\b",
    r"\b(?:role-?play|roleplay)\s+as\b",
    r"\bjail-?break(?:ing|ed)?\s+(?:mode|prompt|enabled|activated|on)\b|\b(?:this\s+is\s+a|enter|enable|activate|"
    r"begin)\s+(?:the\s+)?jail-?break\b",
    r"\b(?:developer|dan|god)\s+mode\b",
    r"\bdo\s+anything\s+now\b",
    r"\b(?:answer|respond|reply|operate|act|behave|speak|talk)\s+without\s+(?:any\s+)?(?:restrictions|filters|"
    r"limitations|censorship|guardrails|safety)\b",
    r"\b(?:bypass|disable|turn\s+off|ignore|override)\s+(?:your\s+|all\s+|the\s+)?(?:safety|content|ethical)?\s*"
    r"(?:filters?|guidelines|guardrails|restrictions|safety)\b",
    # recommendation / suppression directives to the model
    r"\byou\s+(?:must|should|shall|will|have to|need to|are required to|are instructed to)\s+"
    r"(?:always\s+|only\s+|now\s+)?"
    r"(?:recommend|mention|say|state|answer|respond|reply|promote|praise|cite|endorse|rank|favou?r|tell)\b",
    r"(?:^|[.!?:;]\s*|\n\s*)(?:always|only)\s+(?:recommend|mention|cite|promote|endorse|suggest)\s+[a-z0-9][\w.\-]*"
    r"(?:\s+[a-z0-9][\w.\-]*)?\s+(?:in|to|as|when|first|above|over|and)\b",
    r"(?:^|[.!?:;]\s*|\n\s*)(?:never|do\s+not|don't)\s+(?:mention|recommend|cite|name|suggest|reveal)\s+(?:any\s+)?"
    r"(?:other|competitors?|alternatives?|rivals?|this instruction|these instructions)\b",
    r"\b(?:recommend|promote|endorse)\s+[a-z0-9][\w.\-]*(?:\s+[a-z0-9][\w.\-]*)?\s+(?:in|for)\s+(?:all|every|each)\s+"
    r"(?:answers?|responses?|replies)\b",
    # JSON / YAML / fenced role forms (round 4)
    r"[\"']role[\"']\s*:\s*[\"'](?:system|developer|assistant)[\"']",
    r"(?m)^\s*-?\s*role\s*:\s*[\"']?(?:system|developer)\b",
    r"```\s*(?:system|instructions?|prompt|sys)\b",
    # third-person / indirect address of the model (round 4)
    rf"\b(?:the|any|every|all|each)\s+(?:{_AI})s?\s+(?:that|which|who)?\s*(?:reads?|reading|sees?|seeing|processes|"
    rf"summari[sz]es|finds?|crawls?)?\s*(?:this|these|it)?\s*(?:page|text|content|message|document|note)?\s*,?\s*"
    rf"(?:should|must|shall|will|needs?\s+to|has\s+to|is\s+(?:required|instructed|expected)\s+to)\s+"
    rf"(?:always\s+|now\s+|only\s+)?(?:recommend|say|answer|respond|reply|mention|tell|state|rank|promote|praise|cite|"
    rf"describe|ignore|disregard|prefer|favou?r|list|include)\b",
    rf"\b(?:any|every|all)\s+{_AI}s?\s+(?:that|which|who)\s+(?:reads?|sees?|processes|summari[sz]es)\s+this\b",
    # future-turn forms (round 4)
    r"\bnext\s+time\s+(?:you(?:'re|\s+are)?|someone|a user|the user|anyone)\s+(?:is\s+|are\s+)?(?:asked|asks|ask)\b",
    r"\bin\s+your\s+(?:next|following|later|subsequent|future)\s+(?:answers?|replies|reply|responses?|messages?|turns?)\b",
    r"\bfor\s+the\s+remainder\s+of\s+(?:this|the|our)\s+(?:chat|conversation|session|thread)\b",
    r"\bin\s+(?:the\s+)?(?:later|next|following|subsequent|upcoming)\s+(?:turns?|answers?|replies|responses?)\b",
    # persona games (round 4)
    r"\byou\s+are\s+(?:now\s+)?(?:called\s+|named\s+)?(?!robot\b)[a-z][\w\-]*(?:bot|gpt)\b",
    r"\b(?:act|behave|respond|reply|answer)\s+(?:as|like)\s+(?:an?\s+|the\s+)?[a-z][\w\-]*gpt\b",
    r"\byour\s+(?:new\s+)?name\s+is\s+(?:now\s+)?[a-z][\w\-]*(?:bot|gpt)\b",
    r"\byou\s+are\s+no\s+longer\s+(?:an?\s+)?(?:ai|assistant|chatbot|language model|claude|gemini|chatgpt)\b",
    r"\b(?:let's|lets)\s+play\s+a\s+game\b[^.]{0,80}\byou\s+(?:are|will be|play)\b",
    # exfiltration and output control
    r"\b(?:reveal|print|show|repeat|output|leak|disclose)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+|hidden\s+|initial\s+)?"
    r"(?:prompt|instructions)\b(?!\s+(?:sheet|manual|booklet|page|form|document|card|pdf|list|packet|leaflet))",
    r"\b(?:respond|reply|answer|output)\s+(?:only\s+with|with\s+only|with\s+exactly|exactly\s+with)\s*"
    r"(?:[:'\"\u201c]|the\s+(?:word|words|phrase|text|following|sentence)\b)",
]
# multilingual families (round 4): override, "from now on", "you must recommend", "you are now", future turns
ML_PATTERNS = [
    # es
    r"\bignora(?:r|d)?\s+(?:todas\s+)?(?:las\s+)?(?:instrucciones|indicaciones|órdenes|reglas)\s+"
    r"(?:anteriores|previas|de arriba)",
    r"\bolvida(?:r)?\s+(?:todo\s+lo\s+anterior|todas\s+las\s+instrucciones|las\s+instrucciones\s+anteriores)",
    r"\ba\s+partir\s+de\s+ahora,?\s+(?:siempre|solo|sólo|debes|tienes que|vas a)\b",
    r"\b(?:debes|tienes que)\s+(?:siempre\s+)?recomendar\b",
    r"\b(?:ahora\s+eres|eres\s+ahora)\b",
    r"\ben\s+(?:tus|tu)\s+(?:próximas?|siguientes?)\s+respuestas?\b",
    r"\bnota\s+para\s+(?:el|la)\s+(?:ia|asistente|modelo)\b",
    # fr
    r"\bignor(?:e|ez|er)\s+(?:toutes\s+)?les\s+(?:instructions|consignes|règles)\s+(?:précédentes|antérieures|ci-dessus)",
    r"\boubli(?:e|ez|er)\s+(?:tout\s+ce\s+qui\s+précède|toutes\s+les\s+(?:instructions|consignes)|les\s+consignes)",
    r"\bà\s+partir\s+de\s+maintenant,?\s+(?:tu|vous)\s+(?:dois|devez|vas|allez)\b",
    r"\b(?:tu\s+dois|vous\s+devez)\s+(?:toujours\s+)?recommander\b",
    r"\b(?:tu\s+es|vous\s+êtes)\s+maintenant\b",
    r"\bnote\s+(?:à|pour)\s+l['’](?:ia|assistant)\b",
    r"\bdans\s+(?:tes|vos)\s+prochaines?\s+réponses?\b",
    # de
    r"\bignorier(?:e|en|t)?\s+(?:alle\s+)?(?:vorherigen|bisherigen|obigen|früheren|vorigen)\s+(?:anweisungen|instruktionen|"
    r"befehle|regeln)",
    r"\bvergiss\s+(?:alles\s+(?:bisherige|vorherige|oben)|alle\s+(?:vorherigen\s+)?anweisungen)",
    r"\bab\s+(?:jetzt|sofort)\s+(?:musst|sollst|wirst)\s+du\b",
    r"\bdu\s+(?:musst|sollst)\s+(?:immer\s+)?[^.]{0,40}\bempfehlen\b",
    r"\bdu\s+bist\s+(?:jetzt|nun|ab sofort)\b",
    r"\bhinweis\s+(?:an|für)\s+(?:die|den)\s+(?:ki|assistenten)\b",
    r"\bin\s+deinen\s+(?:nächsten|folgenden)\s+antworten\b",
    # pt
    r"\bignor(?:e|a|ar)\s+(?:todas\s+)?as\s+instruções\s+(?:anteriores|prévias|acima)",
    r"\besqueç(?:a|er)\s+(?:tudo\s+(?:o\s+que\s+foi\s+dito\s+)?(?:acima|antes)|todas\s+as\s+instruções)",
    r"\ba\s+partir\s+de\s+agora,?\s+(?:você|voce|tu)\s+(?:deve|vai|precisa)\b",
    r"\b(?:você|voce)\s+(?:deve|precisa)\s+(?:sempre\s+)?recomendar\b",
    r"\b(?:você|voce)\s+agora\s+é\b",
    r"\bnas\s+suas\s+próximas\s+respostas\b",
    # it
    r"\bignora\s+(?:tutte\s+)?le\s+istruzioni\s+precedenti",
    r"\bda\s+ora\s+in\s+poi,?\s+(?:devi|dovrai)\b",
    # ru
    r"(?:игнорируй|проигнорируй|забудь)\s+(?:все\s+)?(?:предыдущие|прежние|прошлые)?\s*(?:инструкции|указания|правила)",
    r"\bс\s+этого\s+момента\s+ты\b",
    r"\bты\s+(?:теперь|должен\s+рекомендовать)\b",
    # zh / ja (no word boundaries)
    r"(?:忽略|忽视|无视|不要理会)(?:之前|以前|上面|先前|前面|所有|上述)(?:的)?(?:所有)?(?:的)?(?:指令|指示|说明|提示|规则)",
    r"忘(?:记|掉)(?:之前|以上|上面|所有)(?:的)?(?:内容|指令|指示)?",
    r"从现在(?:开始|起)[，,]?\s*(?:你|请)",
    r"你(?:现在)?(?:必须|应该|一定要)(?:始终|总是)?推荐",
    r"你现在是",
    r"系统(?:提示|指令)[:：]",
    r"在(?:你)?(?:接下来|之后|以后)的(?:回答|回复)中",
    r"(?:以前|前|上記)の(?:指示|命令)を(?:無視|忘れ)",
    r"これから(?:は)?(?:常に|必ず)",
]
# letters-only phrases matched on a view with every non-letter removed (catches hyphen / space / dot splitting)
SQUASHED = ["ignoreallpreviousinstructions", "ignorepreviousinstructions", "ignoreallpriorinstructions",
            "ignorepriorinstructions", "disregardallpreviousinstructions", "disregardpreviousinstructions",
            "disregardpriorinstructions", "forgetallpreviousinstructions", "forgeteverythingabove",
            "ignoreyourpreviousinstructions", "ignoreyourinstructions", "ignoreyourearlieranswer",
            "ignorethesystemprompt", "revealyoursystemprompt", "youarenowdan", "developermodeenabled"]
_RX = [re.compile(p, re.IGNORECASE) for p in PATTERNS + ML_PATTERNS]
_NONLETTER = re.compile(r"[^a-z]+")
_HYPHEN_SPLIT = re.compile(r"(?<=[a-z])[\-\u2010\u2011\u00b7]+\s*(?=[a-z])")


@lru_cache(maxsize=1)
def _confusables() -> dict:
    """Non-ASCII character -> its ASCII skeleton (UTS #39 confusables, MA table), single code points only. The file is
    pinned by SHA-256; another file is refused (fail closed)."""
    raw = CONFUSABLES.read_bytes()
    if hashlib.sha256(raw).hexdigest() != CONFUSABLES_SHA256:
        raise RuntimeError("agents/confusables.txt is not the pinned UTS #39 v16.0.0 file")
    out = {}
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2 or " " in parts[0]:
            continue
        src = chr(int(parts[0], 16))
        dst = "".join(chr(int(c, 16)) for c in parts[1].split())
        if not src.isascii() and dst.isascii():
            out[src] = dst
    return out


def fold(text: str) -> str:
    m = _confusables()
    return "".join(m.get(ch, ch) for ch in text)


def _b64_texts(text: str) -> list:
    out = []
    for m in _B64.findall(text)[:20]:
        try:
            raw = base64.b64decode(m + "=" * (-len(m) % 4), validate=True)
            dec = raw.decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        if dec and sum(ch.isprintable() or ch.isspace() for ch in dec) / len(dec) > 0.95:
            out.append(dec)
    return out


def views(text: str) -> list:
    t = unicodedata.normalize("NFKC", text or "")
    t = html.unescape(_ZERO_WIDTH.sub("", t)).replace("’", "'")
    low = t.lower()
    folded = fold(t).lower()
    squeezed = _SPACED.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group(0)), folded)
    out = [low, folded, squeezed, squeezed.translate(_LEET), _HYPHEN_SPLIT.sub("", folded)]
    for dec in _b64_texts(t):
        out.append(unicodedata.normalize("NFKC", dec).lower())
    return out


def suspected(text: str) -> bool:
    if not text:
        return False
    vs = views(text[:100_000])
    if any(rx.search(v) for v in vs for rx in _RX):
        return True
    squashed = [_NONLETTER.sub("", v) for v in vs[1:4]]
    # a homoglyph-folded capital I can come out as "l": compare with l and i made one letter
    squashed += [sq.replace("l", "i") for sq in squashed]
    return any(p in sq or p.replace("l", "i") in sq for sq in squashed for p in SQUASHED)


def resent_texts(answer) -> list:
    """Every text an answer would carry back in native mode (answer text, cited_text, result titles, Gemini parts)."""
    out = [getattr(answer, "text", "") or ""]

    def walk(x, depth=0):
        if depth > 6:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("text", "cited_text", "title") and isinstance(v, str):
                    out.append(v)
                elif isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x[:256]:
                walk(v, depth + 1)
    walk(getattr(answer, "turn_content", None))
    return out
