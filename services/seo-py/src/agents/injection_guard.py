"""
The injection guard for Theo's native multi-turn chains (ADR 0049, review round 3). Shared plumbing, not an agent.

In native multi-turn an engine's earlier answer is re-sent to it, so instruction-like text inside that answer — put
there by a page the engine read — may steer the engine's later answers. ``suspected(text)`` says whether a text holds
an instruction aimed at the MODEL (not advice aimed at a reader). It is lexical: closed pattern families over several
normalised views of the text. It is an AUXILIARY FLAG, NOT A GUARANTEE (review round 5): on a fresh independent set it
scored recall 0.246 / precision 0.789, so pattern detection does not generalise. The basis of Theo's native-pattern
claim is the structural check (theo._stacked_check); this flag only adds caution. Numbers: ADR 0049.

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
# review round 5 (ReDoS): every repetition in every pattern is bounded (tested structurally on the parsed patterns),
# whitespace runs are collapsed before matching, and a text longer than MAX_SCAN_CHARS is not scanned but flagged
MAX_SCAN_CHARS = 20_000
CONFUSABLES = Path(__file__).with_name("confusables.txt")
CONFUSABLES_SHA256 = "95bd0aad6dced5ebc63436f459c06ab21a8d107cd842fb57f5c3a1e91bca8611"   # UTS #39 v16.0.0
_ZERO_WIDTH = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_SPACED = re.compile(r"\b(?:[a-z][ .\-_*]){2,60}[a-z]\b")
_B64 = re.compile(r"[A-Za-z0-9+/]{16,4096}={0,2}")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
_AI = r"(?:ai|a\.i\.|assistant|ai assistant|llm|language model|model|chatbot|bot|claude|gpt|chatgpt|gemini|copilot)"
_PAST = (r"(?:previous|prior|above|earlier|preceding|original|initial|system|existing|old|all(?: the)?|your|any|"
         r"every|other)")
_THING = (r"(?:instructions?|prompts?|messages?|rules|directions|directives|answers?|responses?|context|guidelines|"
          r"guidance|text|content|conversation|constraints|programming)")
PATTERNS = [
    # override earlier instructions / answers / context
    rf"\b(?:ignore|disregard|forget|override|bypass|discard|drop|erase|abandon)\s{{1,8}}(?:all\s{{1,8}}|any"
    rf"\s{{1,8}}|the\s{{1,8}}|of\s{{1,8}})?"
    rf"(?:your\s{{1,8}}|my\s{{1,8}}|these\s{{1,8}}|those\s{{1,8}})?{_PAST}\s{{1,8}}(?:of\s{{1,8}}(?:your|the)"
    rf"\s{{1,8}})?(?:\w{{1,40}}\s{{1,8}})?{_THING}\b",
    r"\b(?:forget|ignore|disregard)\s{1,8}(?:about\s{1,8})?(?:everything|anything|all)\s{1,8}"
    r"(?:above|before|prior|previously|"
    r"so far|you (?:were|have been) (?:told|given)|said)\b",
    r"\b(?:ignore|disregard|forget)\s{1,8}(?:what|anything)\s{1,8}(?:you|i)\s{1,8}(?:said|wrote|answered|were told)\b",
    # chat-role and prompt-template markers
    r"(?m)^\s{0,8}(?:system|assistant|developer|ai|model)\s{0,8}(?:prompt|message|note)?\s{0,8}:",
    r"<\s{0,8}/?\s{0,8}(?:system|assistant|user|developer|instructions?|prompt|sys)\s{0,8}>",
    r"(?m)^\s{0,8}#{2,8}\s{0,8}(?:instructions?|system|response|new task|task)\b",
    r"\[/?\s{0,8}(?:inst|sys|system)\s{0,8}\]",
    r"<\|\s{0,8}(?:im_start|im_end|system|endoftext|eot_id|start_header_id)\s{0,8}\|>",
    r"\b(?:begin|start|end)\s{1,8}(?:of\s{1,8})?(?:new\s{1,8})?(?:system\s{1,8})?(?:instructions?|prompt|task)\b",
    r"\b(?:new|updated|revised|additional|hidden|secret)\s{1,8}(?:system\s{1,8})?(?:instructions?|task|directive)s?"
    r"\s{0,8}[:\-]",
    # addressing the AI
    rf"\b(?:note|message|instructions?|reminder|attention|memo)\s{{1,8}}(?:to|for)\s{{1,8}}(?:the\s{{1,8}}|any"
    rf"\s{{1,8}}|all\s{{1,8}})?{_AI}s?\b",
    rf"\b(?:dear|attention|hey|hello|hi)\s{{1,8}}(?:the\s{{1,8}})?{_AI}\b",
    rf"\bif\s{{1,8}}you\s{{1,8}}are\s{{1,8}}(?:an?\s{{1,8}})?{_AI}\b",
    rf"\bas\s{{1,8}}an?\s{{1,8}}{_AI},?\s{{1,8}}you\s{{1,8}}(?:must|should|will|shall|are required)\b",
    rf"\b(?:this|the following)\s{{1,8}}(?:message|text|page|note|section|instruction)\s{{1,8}}is\s{{1,8}}"
    rf"(?:for|addressed to|meant "
    rf"for)\s{{1,8}}(?:the\s{{1,8}}|any\s{{1,8}})?{_AI}",
    rf"\b{_AI}s?\s{{1,8}}(?:reading|summari[sz]ing|processing|crawling)\s{{1,8}}this\b",
    # steering later turns
    r"\b(?:when|if|whenever)\s{1,8}(?:you\s{1,8}are\s{1,8}|you're\s{1,8})?(?:asked|questioned|prompted|queried)"
    r"\b[^.]{0,60}?"
    r"\b(?:later|again|next|in the future|afterwards|about)\b[^.]{0,60}?\b(?:say|answer|respond|reply|recommend|tell|"
    r"state|claim|mention)\b",
    r"\bin\s{1,8}(?:all|every|any|each)\s{1,8}(?:future|later|subsequent|following|next|remaining)\s{1,8}"
    r"(?:answers?|responses?|"
    r"turns?|replies|messages?|conversations?)\b",
    r"\bfrom\s{1,8}now\s{1,8}on,?\s{1,8}(?:you\s{1,8}"
    r"(?:must|will|shall|should|are to|have to|need to|may only|can only)|always|only|"
    r"never|respond|answer|reply|recommend|say|act|pretend|ignore)\b",
    r"\bfor\s{1,8}(?:the\s{1,8})?rest\s{1,8}of\s{1,8}(?:this|the)\s{1,8}(?:conversation|chat|session)\b",
    r"\b(?:remember|memori[sz]e)\s{1,8}(?:this|that)\s{1,8}(?:for|in)\s{1,8}(?:later|future|the next)\b",
    # persona / jailbreak
    r"\byou\s{1,8}are\s{1,8}now\s{1,8}(?:dan\b|an?\s{1,8}"
    r"(?:unrestricted|uncensored|unfiltered|evil|jailbroken|different|new)\b|"
    r"in\s{1,8}(?:developer|dan|god|jailbreak|unrestricted)\s{1,8}mode|no\s{1,8}longer\s{1,8}(?:bound|restricted|an?"
    r"\s{1,8}ai|"
    r"claude|chatgpt)|free\s{1,8}(?:of|from)\s{1,8}(?:all\s{1,8})?(?:rules|restrictions))",
    r"\bpretend\s{1,8}(?:to\s{1,8}be|you\s{1,8}are|that\s{1,8}you\s{1,8}are|you're)\b",
    r"\b(?:act|behave|respond)\s{1,8}as\s{1,8}(?:if\s{1,8}you\s{1,8}(?:are|were)|an?\s{1,8}"
    r"(?:unrestricted|uncensored|evil|different)|dan)\b",
    r"\b(?:role-?play|roleplay)\s{1,8}as\b",
    r"\bjail-?break(?:ing|ed)?\s{1,8}(?:mode|prompt|enabled|activated|on)\b|\b(?:this\s{1,8}is"
    r"\s{1,8}a|enter|enable|activate|"
    r"begin)\s{1,8}(?:the\s{1,8})?jail-?break\b",
    r"\b(?:developer|dan|god)\s{1,8}mode\b",
    r"\bdo\s{1,8}anything\s{1,8}now\b",
    r"\b(?:answer|respond|reply|operate|act|behave|speak|talk)\s{1,8}without\s{1,8}(?:any\s{1,8})?"
    r"(?:restrictions|filters|"
    r"limitations|censorship|guardrails|safety)\b",
    r"\b(?:bypass|disable|turn\s{1,8}off|ignore|override)\s{1,8}(?:your\s{1,8}|all\s{1,8}|the\s{1,8})?"
    r"(?:safety|content|ethical)?\s{0,8}"
    r"(?:filters?|guidelines|guardrails|restrictions|safety)\b",
    # recommendation / suppression directives to the model
    r"\byou\s{1,8}(?:must|should|shall|will|have to|need to|are required to|are instructed to)\s{1,8}"
    r"(?:always\s{1,8}|only\s{1,8}|now\s{1,8})?"
    r"(?:recommend|mention|say|state|answer|respond|reply|promote|praise|cite|endorse|rank|favou?r|tell)\b",
    r"(?:^|[.!?:;]\s{0,8}|\n\s{0,8})(?:always|only)\s{1,8}(?:recommend|mention|cite|promote|endorse|suggest)"
    r"\s{1,8}[a-z0-9][\w.\-]{0,40}"
    r"(?:\s{1,8}[a-z0-9][\w.\-]{0,40})?\s{1,8}(?:in|to|as|when|first|above|over|and)\b",
    r"(?:^|[.!?:;]\s{0,8}|\n\s{0,8})(?:never|do\s{1,8}not|don't)\s{1,8}(?:mention|recommend|cite|name|suggest|reveal)"
    r"\s{1,8}(?:any\s{1,8})?"
    r"(?:other|competitors?|alternatives?|rivals?|this instruction|these instructions)\b",
    r"\b(?:recommend|promote|endorse)\s{1,8}[a-z0-9][\w.\-]{0,40}(?:\s{1,8}[a-z0-9][\w.\-]{0,40})?\s{1,8}(?:in|for)"
    r"\s{1,8}(?:all|every|each)\s{1,8}"
    r"(?:answers?|responses?|replies)\b",
    # JSON / YAML / fenced role forms (round 4)
    r"[\"']role[\"']\s{0,8}:\s{0,8}[\"'](?:system|developer|assistant)[\"']",
    r"(?m)^\s{0,8}-?\s{0,8}role\s{0,8}:\s{0,8}[\"']?(?:system|developer)\b",
    r"```\s{0,8}(?:system|instructions?|prompt|sys)\b",
    # third-person / indirect address of the model (round 4)
    rf"\b(?:the|any|every|all|each)\s{{1,8}}(?:{_AI})s?\s{{1,8}}(?:that|which|who)?\s{{0,8}}"
    rf"(?:reads?|reading|sees?|seeing|processes|"
    rf"summari[sz]es|finds?|crawls?)?\s{{0,8}}(?:this|these|it)?\s{{0,8}}(?:page|text|content|message|document|note)?"
    rf"\s{{0,8}},?\s{{0,8}}"
    rf"(?:should|must|shall|will|needs?\s{{1,8}}to|has\s{{1,8}}to|is\s{{1,8}}(?:required|instructed|expected)"
    rf"\s{{1,8}}to)\s{{1,8}}"
    rf"(?:always\s{{1,8}}|now\s{{1,8}}|only\s{{1,8}})?"
    rf"(?:recommend|say|answer|respond|reply|mention|tell|state|rank|promote|praise|cite|"
    rf"describe|ignore|disregard|prefer|favou?r|list|include)\b",
    rf"\b(?:any|every|all)\s{{1,8}}{_AI}s?\s{{1,8}}(?:that|which|who)\s{{1,8}}"
    rf"(?:reads?|sees?|processes|summari[sz]es)\s{{1,8}}this\b",
    # future-turn forms (round 4)
    r"\bnext\s{1,8}time\s{1,8}(?:you(?:'re|\s{1,8}are)?|someone|a user|the user|anyone)\s{1,8}(?:is\s{1,8}|are"
    r"\s{1,8})?(?:asked|asks|ask)\b",
    r"\bin\s{1,8}your\s{1,8}(?:next|following|later|subsequent|future)\s{1,8}"
    r"(?:answers?|replies|reply|responses?|messages?|turns?)\b",
    r"\bfor\s{1,8}the\s{1,8}remainder\s{1,8}of\s{1,8}(?:this|the|our)\s{1,8}(?:chat|conversation|session|thread)\b",
    r"\bin\s{1,8}(?:the\s{1,8})?(?:later|next|following|subsequent|upcoming)\s{1,8}"
    r"(?:turns?|answers?|replies|responses?)\b",
    # persona games (round 4)
    r"\byou\s{1,8}are\s{1,8}(?:now\s{1,8})?(?:called\s{1,8}|named\s{1,8})?(?!robot\b)[a-z][\w\-]{0,40}(?:bot|gpt)\b",
    r"\b(?:act|behave|respond|reply|answer)\s{1,8}(?:as|like)\s{1,8}(?:an?\s{1,8}|the\s{1,8})?[a-z][\w\-]{0,40}gpt\b",
    r"\byour\s{1,8}(?:new\s{1,8})?name\s{1,8}is\s{1,8}(?:now\s{1,8})?[a-z][\w\-]{0,40}(?:bot|gpt)\b",
    r"\byou\s{1,8}are\s{1,8}no\s{1,8}longer\s{1,8}(?:an?\s{1,8})?"
    r"(?:ai|assistant|chatbot|language model|claude|gemini|chatgpt)\b",
    r"\b(?:let's|lets)\s{1,8}play\s{1,8}a\s{1,8}game\b[^.]{0,80}\byou\s{1,8}(?:are|will be|play)\b",
    # exfiltration and output control
    r"\b(?:reveal|print|show|repeat|output|leak|disclose)\s{1,8}(?:me\s{1,8})?(?:your|the)\s{1,8}(?:system"
    r"\s{1,8}|hidden\s{1,8}|initial\s{1,8})?"
    r"(?:prompt|instructions)\b(?!\s{1,8}(?:sheet|manual|booklet|page|form|document|card|pdf|list|packet|leaflet))",
    r"\b(?:respond|reply|answer|output)\s{1,8}(?:only\s{1,8}with|with\s{1,8}only|with\s{1,8}exactly|exactly"
    r"\s{1,8}with)\s{0,8}"
    r"(?:[:'\"\u201c]|the\s{1,8}(?:word|words|phrase|text|following|sentence)\b)",
]
# multilingual families (round 4): override, "from now on", "you must recommend", "you are now", future turns
ML_PATTERNS = [
    # es
    r"\bignora(?:r|d)?\s{1,8}(?:todas\s{1,8})?(?:las\s{1,8})?(?:instrucciones|indicaciones|órdenes|reglas)\s{1,8}"
    r"(?:anteriores|previas|de arriba)",
    r"\bolvida(?:r)?\s{1,8}(?:todo\s{1,8}lo\s{1,8}anterior|todas\s{1,8}las\s{1,8}instrucciones|las"
    r"\s{1,8}instrucciones\s{1,8}anteriores)",
    r"\ba\s{1,8}partir\s{1,8}de\s{1,8}ahora,?\s{1,8}(?:siempre|solo|sólo|debes|tienes que|vas a)\b",
    r"\b(?:debes|tienes que)\s{1,8}(?:siempre\s{1,8})?recomendar\b",
    r"\b(?:ahora\s{1,8}eres|eres\s{1,8}ahora)\b",
    r"\ben\s{1,8}(?:tus|tu)\s{1,8}(?:próximas?|siguientes?)\s{1,8}respuestas?\b",
    r"\bnota\s{1,8}para\s{1,8}(?:el|la)\s{1,8}(?:ia|asistente|modelo)\b",
    # fr
    r"\bignor(?:e|ez|er)\s{1,8}(?:toutes\s{1,8})?les\s{1,8}(?:instructions|consignes|règles)\s{1,8}"
    r"(?:précédentes|antérieures|ci-dessus)",
    r"\boubli(?:e|ez|er)\s{1,8}(?:tout\s{1,8}ce\s{1,8}qui\s{1,8}précède|toutes\s{1,8}les\s{1,8}"
    r"(?:instructions|consignes)|les\s{1,8}consignes)",
    r"\bà\s{1,8}partir\s{1,8}de\s{1,8}maintenant,?\s{1,8}(?:tu|vous)\s{1,8}(?:dois|devez|vas|allez)\b",
    r"\b(?:tu\s{1,8}dois|vous\s{1,8}devez)\s{1,8}(?:toujours\s{1,8})?recommander\b",
    r"\b(?:tu\s{1,8}es|vous\s{1,8}êtes)\s{1,8}maintenant\b",
    r"\bnote\s{1,8}(?:à|pour)\s{1,8}l['’](?:ia|assistant)\b",
    r"\bdans\s{1,8}(?:tes|vos)\s{1,8}prochaines?\s{1,8}réponses?\b",
    # de
    r"\bignorier(?:e|en|t)?\s{1,8}(?:alle\s{1,8})?(?:vorherigen|bisherigen|obigen|früheren|vorigen)\s{1,8}"
    r"(?:anweisungen|instruktionen|"
    r"befehle|regeln)",
    r"\bvergiss\s{1,8}(?:alles\s{1,8}(?:bisherige|vorherige|oben)|alle\s{1,8}(?:vorherigen\s{1,8})?anweisungen)",
    r"\bab\s{1,8}(?:jetzt|sofort)\s{1,8}(?:musst|sollst|wirst)\s{1,8}du\b",
    r"\bdu\s{1,8}(?:musst|sollst)\s{1,8}(?:immer\s{1,8})?[^.]{0,40}\bempfehlen\b",
    r"\bdu\s{1,8}bist\s{1,8}(?:jetzt|nun|ab sofort)\b",
    r"\bhinweis\s{1,8}(?:an|für)\s{1,8}(?:die|den)\s{1,8}(?:ki|assistenten)\b",
    r"\bin\s{1,8}deinen\s{1,8}(?:nächsten|folgenden)\s{1,8}antworten\b",
    # pt
    r"\bignor(?:e|a|ar)\s{1,8}(?:todas\s{1,8})?as\s{1,8}instruções\s{1,8}(?:anteriores|prévias|acima)",
    r"\besqueç(?:a|er)\s{1,8}(?:tudo\s{1,8}(?:o\s{1,8}que\s{1,8}foi\s{1,8}dito\s{1,8})?(?:acima|antes)|todas\s{1,8}as"
    r"\s{1,8}instruções)",
    r"\ba\s{1,8}partir\s{1,8}de\s{1,8}agora,?\s{1,8}(?:você|voce|tu)\s{1,8}(?:deve|vai|precisa)\b",
    r"\b(?:você|voce)\s{1,8}(?:deve|precisa)\s{1,8}(?:sempre\s{1,8})?recomendar\b",
    r"\b(?:você|voce)\s{1,8}agora\s{1,8}é\b",
    r"\bnas\s{1,8}suas\s{1,8}próximas\s{1,8}respostas\b",
    # it
    r"\bignora\s{1,8}(?:tutte\s{1,8})?le\s{1,8}istruzioni\s{1,8}precedenti",
    r"\bda\s{1,8}ora\s{1,8}in\s{1,8}poi,?\s{1,8}(?:devi|dovrai)\b",
    # ru
    r"(?:игнорируй|проигнорируй|забудь)\s{1,8}(?:все\s{1,8})?(?:предыдущие|прежние|прошлые)?\s{0,8}"
    r"(?:инструкции|указания|правила)",
    r"\bс\s{1,8}этого\s{1,8}момента\s{1,8}ты\b",
    r"\bты\s{1,8}(?:теперь|должен\s{1,8}рекомендовать)\b",
    # zh / ja (no word boundaries)
    r"(?:忽略|忽视|无视|不要理会)(?:之前|以前|上面|先前|前面|所有|上述)(?:的)?(?:所有)?(?:的)?(?:指令|指示|说明|提示|规则)",
    r"忘(?:记|掉)(?:之前|以上|上面|所有)(?:的)?(?:内容|指令|指示)?",
    r"从现在(?:开始|起)[，,]?\s{0,8}(?:你|请)",
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
_NONLETTER = re.compile(r"[^a-z]{1,256}")
_HYPHEN_SPLIT = re.compile(r"(?<=[a-z])[\-\u2010\u2011\u00b7]{1,4}[ \t]{0,2}(?=[a-z])")


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


def _collapse(t: str) -> str:
    """Runs of spaces / tabs become one space and runs of line breaks one newline (a linear split, no regex), so no
    pattern ever sees long whitespace (review round 5)."""
    return "\n".join(" ".join(line.split()) for line in t.splitlines() if line.strip())


def views(text: str) -> list:
    t = unicodedata.normalize("NFKC", text or "")
    t = _collapse(html.unescape(_ZERO_WIDTH.sub("", t)).replace("\u2019", "'"))
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
    if len(text) > MAX_SCAN_CHARS:                     # too long to scan within the bound: flagged (fail closed)
        return True
    vs = views(text)
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
