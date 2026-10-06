"""Influencers: creator applications (with the 18+ attestation), researched prospects, discovery imports through the
NOT_BUILT ports, verified first names (ADR 0015 decisions 6-8). A mixin of InfluencerService.

A record holds a display name, an email, platform handles, niches and bands, a country, its source and evidence, the
18+ attestation FLAG with the attestation text's version and SHA-256 (never a date of birth or an age), a ``blocked``
flag, and the keyed hashes that dedupe, suppression, holds, the ledger and the export use."""

from __future__ import annotations

from typing import Optional

import models as m
import textguard
from errors import Conflict, Forbidden, Invalid, Unavailable
from intelligences import i01_intake, i02_identity, i03_fit, i04_suppression, i05_templates
from ledger import derived_id
from ports import NotWired
from pydantic import ValidationError
from reasons import R


class PeopleMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_influencer_created(self, d, at):
        inf = {k: d.get(k) for k in ("influencer_id", "source", "display_name", "email", "email_hash", "niches",
                                     "follower_band", "engagement_band", "country", "evidence_ref", "fit")}
        inf.update(handles=[dict(h) for h in d["handles"]], adult_attested=False, attestation=None, attested_at=None,
                   email_confirmed=False, blocked=None, blocked_prior=None, verified_first_name=None, created_at=at,
                   updated_at=at, tax=None, payee={"payee_ref": None, "status": "none", "checked_at": None})
        self.influencers[d["influencer_id"]] = inf
        self._index(inf)

    def _index(self, inf: dict) -> None:
        if inf.get("email_hash"):
            self.email_index.setdefault(inf["email_hash"], inf["influencer_id"])
        for h in inf["handles"]:
            self.handle_index.setdefault(h["handle_hash"], inf["influencer_id"])

    def _apply_application(self, inf: dict, d: dict, at: str) -> None:
        """A confirmed application (the token came back from the address): the 18+ attestation and the handles that
        no other record holds take effect, and the address is confirmed."""
        inf.update(adult_attested=True, attestation=d["attestation"], attested_at=at, email_confirmed=True,
                   updated_at=at)
        have = {h["platform"] for h in inf["handles"]}
        inf["handles"] += [dict(h) for h in d.get("add_handles", ()) if h["platform"] not in have
                           and h["handle_hash"] not in self.handle_index]
        self._index(inf)

    def _a_influencer_blocked(self, d, at):
        """A declared minor: the record is frozen at once (no outreach, brief, deal, contract, content or payout) and
        everything queued to it is cancelled; Andre then confirms (permanent suppression) or releases it. A flag only:
        no age, no date of birth."""
        inf = self.influencers[d["influencer_id"]]
        inf["blocked_prior"] = {k: inf.get(k) for k in ("adult_attested", "attestation", "attested_at")}
        inf.update(blocked=d["reason"], adult_attested=False, updated_at=at)
        self._cancel_queued(set(), at, "INFLUENCER_BLOCKED", inf["influencer_id"])
        for dr in self.dm_drafts.values():
            if dr["influencer_id"] == inf["influencer_id"] and dr["status"] == "draft":
                dr.update(status="cancelled", updated_at=at)

    def _a_minor_review_decided(self, d, at):
        inf = self.influencers[d["influencer_id"]]
        if d["decision"] == "confirm_minor":
            inf.update(blocked="MINOR_CONFIRMED", updated_at=at)
            self._a_suppression_added({"hashes": d["hashes"], "reason": "minor_declared", "actor": d["actor"]}, at)
        else:
            # not_a_minor (AEGIS R1-L4): released with the attestation the creator had made BEFORE the false
            # declaration, if any (a record that never attested stays unattested: the creator applies again)
            prior = inf.get("blocked_prior") or {}
            inf.update(blocked=None, adult_attested=bool(prior.get("adult_attested")),
                       attestation=prior.get("attestation"), attested_at=prior.get("attested_at"),
                       blocked_prior=None, updated_at=at)

    def _a_first_name_verified(self, d, at):
        self.influencers[d["influencer_id"]].update(verified_first_name=d["first_name"], updated_at=at)

    # ------------------------------------------------------------------------------------------------ helpers

    def _handles(self, items: list) -> list[dict]:
        """Canonical handles with their keyed hashes; at most one per platform. A handle that cannot be canonicalised
        is refused, never altered."""
        out, seen = [], set()
        for h in items:
            canon = i02_identity.handle(h["platform"], h["handle"])
            if canon is None:
                raise Invalid(R("HANDLE_INVALID"))
            if h["platform"] in seen:
                raise Invalid(R("HANDLE_PLATFORM_REPEATED"))
            seen.add(h["platform"])
            out.append({"platform": h["platform"], "handle": canon,
                        "handle_hash": i02_identity.handle_hash(self.pii_key, h["platform"], canon)})
        return out

    def _email(self, raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if raw is None:
            return None, None
        e = i02_identity.email(raw)
        if e is None:
            raise Invalid(R("EMAIL_INVALID"))
        return e, i02_identity.email_hash(self.pii_key, e)

    def _record(self, source: str, body: dict, email: Optional[str], eh: Optional[str], handles: list) -> dict:
        return {"source": source, "display_name": body["display_name"], "email": email, "email_hash": eh,
                "handles": handles, "niches": list(body.get("niches") or []),
                "follower_band": body.get("follower_band"), "engagement_band": body.get("engagement_band"),
                "country": body.get("country"), "evidence_ref": body.get("evidence_ref"),
                "fit": i03_fit.score(body.get("niches"), body.get("follower_band"), body.get("engagement_band"))}

    def _existing(self, eh: Optional[str], handles: list) -> Optional[str]:
        if eh and eh in self.email_index:
            return self.email_index[eh]
        for h in handles:
            if h["handle_hash"] in self.handle_index:
                return self.handle_index[h["handle_hash"]]
        return None

    @staticmethod
    def _created_events(iid: str, source: str, actor: str, rk: str) -> list:
        return [("influencer_recorded", f"influencer:{iid}", {"influencer_id": iid, "source": source,
                                                              "adult_attested": False}, (actor, rk))]

    # ------------------------------------------------------------------------------------------------ applications

    def apply(self, caller: str, body: dict) -> dict:
        """The creator application form. Only the creator's own form attests (i01), and only once the creator proves
        the address (AEGIS R1-M1/M2): an application changes NO identity field of any record — no attestation, no
        handle — until the confirmation token mailed to that address comes back (``POST /inf/v1/confirmations``). A
        new address gets a bare record (no handle, not attested, address unconfirmed).

        A declared minor is refused and nothing about them is kept; when the address matches a record we already hold,
        that record is FROZEN at once (a flag only: no age, no date of birth) and waits for Andre's minor review. It is
        not suppressed: anyone can type anyone's address into a public form, so a stranger can freeze a record but
        never permanently opt a creator out (sales-py S5-L2's reasoning)."""
        problem = i01_intake.source_problem("inbound_application", caller)
        if problem:
            raise Forbidden(R(problem))
        with self.lock:
            self._gate()
            email, eh = self._email(body["email"])
            rk = self._rk("apply", eh, body)
            prev = self._idem(caller, rk, body)
            if prev:
                if (prev[1] or {}).get("refused"):
                    raise Invalid(R(prev[1]["refused"]))
                return self._application_answer(prev[1]["influencer_id"], prev[1]["confirmation_id"])
            existing_id = self.email_index.get(eh)
            existing = self.influencers.get(existing_id) if existing_id else None
            age = i01_intake.attestation_problem(body.get("adult_18_plus"))
            if age == "MINOR_REFUSED":
                if existing is not None and not existing.get("blocked"):
                    iid = existing["influencer_id"]
                    self._commit("influencer_blocked", self._req(
                        {"influencer_id": iid, "reason": "MINOR_DECLARED"}, caller, rk, body,
                        {"refused": "MINOR_REFUSED"}), caller,
                        evidence=("influencer_blocked", f"influencer:{iid}",
                                  {"influencer_id": iid, "reason": "MINOR_DECLARED"}, (caller, rk)))
                raise Invalid(R("MINOR_REFUSED"))
            if age:
                raise Invalid(R(age))
            if existing is not None and existing.get("blocked"):
                raise Forbidden(R("INFLUENCER_BLOCKED"))
            handles = self._handles(body.get("handles") or [])
            payload = {"attestation": {"text_version": body["attestation_text_version"],
                                       "text_sha256": body["attestation_text_sha256"], "source": "creator_form"},
                       "add_handles": handles}
            record = None
            ev = []
            if existing is None:
                iid = derived_id("inf", caller, rk)
                record = {"influencer_id": iid, **self._record("inbound_application", body, email, eh, [])}
                ev += self._created_events(iid, "inbound_application", caller, rk)
            else:
                iid = existing["influencer_id"]
            conf, msg, cev = self._confirmation(caller, rk, iid, "application", email, eh, payload)
            self._commit("application_received", self._req(
                {"influencer_id": iid, "record": record, "confirmation": conf, "message": msg}, caller, rk, body,
                {"influencer_id": iid, "confirmation_id": conf["conf_id"]}), caller, evidence=ev + cev)
            return self._application_answer(iid, conf["conf_id"])

    def _application_answer(self, iid: str, conf_id: str) -> dict:
        return {**self.influencer_view(self.influencers[iid]), "confirmation_id": conf_id,
                "confirmation_status": self.confirmations[conf_id]["status"]}

    def _a_application_received(self, d, at):
        if d.get("record"):
            self._a_influencer_created(d["record"], at)
        self._a_confirmation_requested(d, at)

    def create_prospect(self, caller: str, body: dict) -> dict:
        """A profile a person at Andre's console researched. Not attested: it can be contacted (email under the
        outreach rules, DMs only with Andre's approval) but gets no brief, deal, content, contract or payout until the
        creator applies with the same address."""
        problem = i01_intake.source_problem("manual_research", caller)
        if problem:
            raise Forbidden(R(problem))
        with self.lock:
            self._gate()
            email, eh = self._email(body.get("email"))
            handles = self._handles(body["handles"])
            rk = self._rk("prospect", handles[0]["handle_hash"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.influencer_view(self.influencers[prev[1]["influencer_id"]])
            dup = self._existing(eh, handles)
            if dup:
                raise Conflict(R("INFLUENCER_EXISTS"), influencer_id=dup)
            iid = derived_id("inf", caller, rk)
            self._commit("influencer_created", self._req(
                {"influencer_id": iid, **self._record("manual_research", body, email, eh, handles)},
                caller, rk, body, {"influencer_id": iid}), caller,
                evidence=self._created_events(iid, "manual_research", caller, rk))
            return self.influencer_view(self.influencers[iid])

    def import_discovery(self, caller: str, body: dict) -> dict:
        """Public-profile and paid-database discovery go through their ports only (no scraping code here). The
        stand-ins refuse 503 ``SOURCE_NOT_WIRED``. A wired source's records are validated like a prospect; one that
        fails is skipped and counted, one we already hold is a duplicate."""
        with self.lock:
            self._gate()
            rk = self._rk("import", body["source"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(prev[1])
        port = self.ports.sources[body["source"]]
        self._begin(rk)
        try:
            try:
                records = port.fetch({"niches": list(body.get("niches") or [])}, body["limit"])
            except NotWired:
                raise Unavailable(R("SOURCE_NOT_WIRED")) from None
            except Exception:  # noqa: BLE001 - a source that fails is unavailable; its text is dropped
                raise Unavailable(R("SOURCE_UNAVAILABLE")) from None
            return self._import_records(caller, rk, body, records)
        finally:
            self._end(rk)

    def _import_records(self, caller: str, rk: str, body: dict, records) -> dict:
        created, skipped, duplicates = [], 0, 0
        with self.lock:
            self._gate()
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(prev[1])
            for i, raw in enumerate((records or [])[:body["limit"]]):
                try:
                    # a source's record passes the same gates as a request body: no tax id, no date of birth or age,
                    # the strict prospect model (unknown fields refused)
                    if not isinstance(raw, dict) or textguard.problem(raw):
                        raise ValueError
                    rec = m.ProspectIn.model_validate({**raw, "request_id": f"{body['request_id']}.{i}"}) \
                        .model_dump(mode="json")
                    email, eh = self._email(rec.get("email"))
                    handles = self._handles(rec["handles"])
                except (ValueError, ValidationError, Invalid):
                    skipped += 1
                    continue
                if self._existing(eh, handles):
                    duplicates += 1
                    continue
                iid = derived_id("inf", caller, rk, i)
                self._commit("influencer_created", {"influencer_id": iid, **self._record(
                    body["source"], rec, email, eh, handles)}, caller,
                    evidence=self._created_events(iid, body["source"], caller, f"{rk}#{i}"))
                created.append(iid)
            answer = {"source": body["source"], "created": created, "skipped": skipped, "duplicates": duplicates}
            self._commit("job_ran", self._req({"job": "discovery-import"}, caller, rk, body, answer), caller)
            return answer

    def verify_first_name(self, caller: str, influencer_id: str, body: dict) -> dict:
        """The only value ``{{first_name}}`` renders: set by a person at the console (sales-py S2-H1)."""
        with self.lock:
            self._gate()
            rk = self._rk("first_name", influencer_id, body)
            if self._idem(caller, rk, body):
                return self.influencer_view(self.influencers[influencer_id])
            inf = self._get(self.influencers, influencer_id, "INFLUENCER_NOT_FOUND")
            if not i05_templates.merge_value_ok(body["first_name"]):
                raise Invalid(R("MERGE_FIELD_REFUSED"))
            self._commit("first_name_verified", self._req({"influencer_id": influencer_id,
                                                           "first_name": body["first_name"]}, caller, rk, body,
                                                          influencer_id), caller,
                         evidence=("first_name_verified", f"influencer:{influencer_id}",
                                   {"influencer_id": influencer_id}, (caller, rk)))
            return self.influencer_view(inf)

    def decide_minor_review(self, influencer_id: str, body: dict) -> dict:
        """Andre's decision on a record blocked by a declared minor: ``confirm_minor`` keeps it blocked for good and
        suppresses every address and handle of it; ``not_a_minor`` releases it WITHOUT an attestation (the creator
        applies again and attests on their own form)."""
        with self.lock:
            self._gate()
            rk = self._rk("minor_review", influencer_id, body)
            if self._idem("andre", rk, body):
                return self.influencer_view(self.influencers[influencer_id])
            inf = self._get(self.influencers, influencer_id, "INFLUENCER_NOT_FOUND")
            if inf.get("blocked") != "MINOR_DECLARED":
                raise Conflict(R("NO_MINOR_REVIEW"))
            hashes = sorted(i04_suppression.hashes_of(inf))
            ev = [("minor_review_decided", f"influencer:{influencer_id}",
                   {"influencer_id": influencer_id, "decision": body["decision"]}, ("andre", rk))]
            if body["decision"] == "confirm_minor":
                ev.append(self._suppression_evidence(hashes, "minor_declared", "andre", rk))
            self._commit("minor_review_decided", self._req({"influencer_id": influencer_id,
                                                            "decision": body["decision"], "hashes": hashes},
                                                           "andre", rk, body, influencer_id), "andre", evidence=ev)
            return self.influencer_view(inf)

    # ------------------------------------------------------------------------------------------------ views

    def influencer_view(self, inf: dict) -> dict:
        out = {k: v for k, v in inf.items() if k not in ("handles", "tax", "payee", "blocked_prior")}
        out["handles"] = [dict(h) for h in inf["handles"]]
        out["suppressed"] = i04_suppression.suppressed(self.suppression, i04_suppression.hashes_of(inf))
        out["held"] = self._held(inf)
        out["tax_profile"] = None if not inf.get("tax") else {k: inf["tax"][k] for k in (
            "tax_form", "legal_form", "country", "tax_ref_sha256", "recorded_at")}
        out["payee"] = {"status": inf["payee"]["status"], "registered": bool(inf["payee"]["payee_ref"]),
                        "checked_at": inf["payee"]["checked_at"]}
        return out

    def influencer(self, influencer_id: str) -> dict:
        with self.lock:
            return self.influencer_view(self._get(self.influencers, influencer_id, "INFLUENCER_NOT_FOUND"))

    def influencers_view(self, source: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.influencer_view(x) for x in self.influencers.values()
                    if source is None or x["source"] == source][:1000]
