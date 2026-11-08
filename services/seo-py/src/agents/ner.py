"""
Optional named-entity recognition for Idris (ADR 0048 review round 3): a self-hosted open-source model as ONE scoring
signal for emerging-competitor candidates. Never a network call, never a paid service, never required.

The model is spaCy (MIT) with the pinned small English pipeline ``en_core_web_sm`` 3.8.0 (MIT; its meta names
OntoNotes 5 as training data, "commercial (licensed by Explosion)", and WordNet 3.0). Both are installed from
``services/seo-py/requirements-ner.txt`` (exact versions; the model wheel pinned by sha256) and pass the repository's
licence gate (``services/delivery-py/devtools/licence_gate.py --site-packages <env>``; ADR 0048 records the run). They
are NOT in ``requirements.txt``: the default install and the service image do not carry them.

OFF unless ``SEO_IDRIS_NER=1``. When ON and the packages are not installed, the service refuses to start (fail
closed: a configuration that asks for NER never silently runs without it). Loading is local (``spacy.load`` of the
installed package, the parser and lemmatizer excluded); text is processed in memory and dropped. The model's output
can only label names the deterministic extractor already found: ORG / PRODUCT is evidence, PERSON / GPE / EVENT /
DATE ... counter-evidence (agents/idris.py); it never adds a candidate.
"""

from __future__ import annotations

from typing import Optional

MODEL, MODEL_VERSION = "en_core_web_sm", "3.8.0"
MAX_CHARS = 20000                    # an answer longer than this is labelled on its first MAX_CHARS characters only


class SpacyNer:
    """``ner(text) -> [(start, end, label)]``. Loaded once; thread-compatible for the service's use (one call at a
    time per run under the citation-run flow)."""

    def __init__(self, nlp=None):
        if nlp is None:
            import spacy                          # noqa: PLC0415 - optional dependency (requirements-ner.txt)
            nlp = spacy.load(MODEL, exclude=["parser", "lemmatizer", "attribute_ruler"])
            if nlp.meta.get("version") != MODEL_VERSION:
                raise RuntimeError(f"{MODEL} {MODEL_VERSION} is required (found {nlp.meta.get('version')})")
        self.nlp = nlp
        self.describe = {"model": MODEL, "version": (nlp.meta or {}).get("version"),
                         "licence": (nlp.meta or {}).get("license")}

    def __call__(self, text: str) -> list:
        doc = self.nlp((text or "")[:MAX_CHARS])
        return [(e.start_char, e.end_char, e.label_) for e in doc.ents]


def load(enabled: bool) -> Optional[SpacyNer]:
    """The NER port, or None when OFF. ON without the packages raises (the service refuses to start)."""
    if not enabled:
        return None
    try:
        return SpacyNer()
    except ImportError as exc:
        raise RuntimeError("SEO_IDRIS_NER=1 needs spaCy and en_core_web_sm 3.8.0 (services/seo-py/"
                           "requirements-ner.txt)") from exc
