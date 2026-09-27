"""
Strict type check of every answer a port or thin client gives Legal (AEGIS N17-3, swept from finance-py).

Every answer is checked BEFORE anything derived from it is staged or anchored: ``Op.call`` (every port call of an
operation) and the Compliance proposal delivery / row confirmation that run outside the lock. A malformed answer --
a wrong type in any field (a string where a bool belongs, a list where an id belongs, an unknown status, a
non-integer HTTP status) -- is refused as a whole: the port's fail-closed fallback is used and the refusal is
recorded on the ledger (``adapter_answer_refused``: port, action, the problem's TYPE only). No coercion (pydantic
strict mode), no partial apply.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from ports import ComplianceRow, Delivery, EnvelopeAnswer, ProposalAnswer

IdS = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
Reason = Annotated[str, StringConstraints(max_length=2000)]


class _S(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class DeliveryM(_S):
    delivered: bool
    reason: Reason = ""
    reference: Optional[IdS] = None


class ProposalAnswerM(_S):
    status: Literal["created", "refused", "unavailable"]
    compliance_proposal_id: Optional[IdS] = None
    http_status: Optional[Annotated[int, Field(ge=100, le=599)]] = None


class ComplianceRowM(_S):
    available: Literal[True]
    obligation_id: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]{1,39}$")]
    effective_status: Literal["verified", "unverified", "expired", "superseded"]
    register_version: Optional[Annotated[int, Field(ge=0, le=10 ** 9)]] = None


class EnvelopeAnswerM(_S):
    available: Literal[True]
    envelope_id: IdS


MODELS = {Delivery: DeliveryM, ProposalAnswer: ProposalAnswerM, ComplianceRow: ComplianceRowM,
          EnvelopeAnswer: EnvelopeAnswerM}
_AVAILABLE = (ComplianceRow, EnvelopeAnswer)


class Malformed(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = re.sub(r"[^A-Za-z0-9._:-]", "-", kind)[:64]


def check(ans: Any, typ: type, fallback: Any) -> Any:
    """``ans`` if it is a well-formed ``typ``; an unavailable answer becomes ``fallback``; raises ``Malformed``."""
    if type(ans) is not typ:
        raise Malformed(f"wrong_type:{type(ans).__name__}")
    raw = {f.name: getattr(ans, f.name) for f in dataclasses.fields(ans)}
    if typ in _AVAILABLE:
        if raw.get("available") is False:
            return fallback
        if raw.get("available") is not True:
            raise Malformed("available_not_bool")
    try:
        MODELS[typ].model_validate(raw)
    except ValidationError as exc:
        errs = exc.errors(include_url=False, include_input=False)
        first = errs[0] if errs else {}
        raise Malformed(f"{'.'.join(str(x) for x in first.get('loc', ()))}:{first.get('type', 'invalid')}") from None
    except Exception as exc:  # noqa: BLE001
        raise Malformed(f"unvalidatable:{type(exc).__name__}") from None
    return ans
