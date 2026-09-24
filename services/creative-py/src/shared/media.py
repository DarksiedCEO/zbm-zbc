"""
Plug points for the approved media building blocks — NOT integrated.

Approved (Sep 24 2026) but deliberately not integrated in this build:
Kinocut (cutting), PySceneDetect (scene boundaries), faster-whisper /
WhisperX (transcription), c2pa-rs (Content Credentials), Chromaprint
(audio fingerprinting). No video/audio library is imported anywhere in
this service. Each interface below is where one will plug in; each
stand-in reports `integrated=False` and does nothing. Licences must be
checked at integration time (AGPL needs Legal 37 sign-off per the spec).

Where they plug in:
- SceneDetector, Transcriber  -> zbc/source_mining (today: segments are
  supplied by the caller, already timestamped).
- ClipCutter                  -> zbc/campaign_kit seed clips.
- AudioFingerprinter          -> zbc/clip_review raw-repost / music checks
  (today: transformation elements and watermark flag are DECLARED by the
  submitter, not detected).
- ProvenanceStamper           -> zbm/rights_provenance Content Credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class NotIntegrated:
    component: str
    integrated: bool = False

    @property
    def reason(self) -> str:
        return f"{self.component} is not integrated in this build (interface only)"


class SceneDetector(Protocol):
    def detect(self, media_ref: str) -> list[tuple[float, float]] | NotIntegrated: ...


class Transcriber(Protocol):
    def transcribe(self, media_ref: str) -> list[dict] | NotIntegrated: ...


class ClipCutter(Protocol):
    def cut(self, media_ref: str, start: float, end: float) -> str | NotIntegrated: ...


class AudioFingerprinter(Protocol):
    def fingerprint(self, media_ref: str) -> str | NotIntegrated: ...


class ProvenanceStamper(Protocol):
    def stamp(self, media_ref: str, manifest: dict) -> str | NotIntegrated: ...


class PySceneDetectStandIn:
    def detect(self, media_ref: str) -> NotIntegrated:
        return NotIntegrated("PySceneDetect")


class WhisperStandIn:
    def transcribe(self, media_ref: str) -> NotIntegrated:
        return NotIntegrated("faster-whisper/WhisperX")


class KinocutStandIn:
    def cut(self, media_ref: str, start: float, end: float) -> NotIntegrated:
        return NotIntegrated("Kinocut")


class ChromaprintStandIn:
    def fingerprint(self, media_ref: str) -> NotIntegrated:
        return NotIntegrated("Chromaprint")


class C2paStandIn:
    def stamp(self, media_ref: str, manifest: dict) -> NotIntegrated:
        return NotIntegrated("c2pa-rs")
