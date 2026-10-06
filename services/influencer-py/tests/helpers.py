"""Test harness: an in-process service over the real ASGI stack, a fake ledger with ledger-rust's contract (copied from
sales-py's tests/helpers.py), recording fakes for every port (they live here only, never importable from src/), and
builders for the common objects. Every token and key here is DERIVED (sha256 of a test-only label), never written out,
so no secret-shaped literal reaches the secret scanner."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock
from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, Recorder, payload_sha256
from ports import ContractCheck, ContractSend, PayeeAnswer, PayeeStatus, PayoutAnswer, Ports, SendResult
from service import InfluencerService
from store import RecordLog


def _derived(label: str) -> str:
    return hashlib.sha256(f"influencer-py test-only {label}".encode()).hexdigest()


SERVICE_TOKEN = "svc-" + _derived("service token")
ANDRE = "andre-" + _derived("andre approval token")
CALLERS = {c: f"{c}-" + _derived(f"caller {c}") for c in config_mod.KNOWN_CALLERS}
PII_KEY_HEX = _derived("pii key")
OUTREACH = "creators-outreach.test"
ZBM_DOMAIN, ZBC_DOMAIN = "zbestmedia.test", "zbestclips.test"
POSTAL = "123 Test Street, Suite 4, Los Angeles, CA 90001"
ATTEST_SHA = hashlib.sha256(b"I confirm I am 18 or older.").hexdigest()
MEDIA = [hashlib.sha256(b"media-1").hexdigest()]
# a fixed instant; NO test depends on the hour of day (influencer outreach has no quiet-hours rule)
T0 = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
P = "/inf/v1"
TAX_REF = "stripe:acct_TESTabcdefghijklmnop"


def rid() -> str:
    return "r-" + uuid.uuid4().hex


class FakeLedger:
    """ledger-rust's contract: idempotent on identical content, 409 on a different record under the same id;
    entries() in ledger order with the persisted fields."""

    def __init__(self):
        self.events: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.fail = False
        self.fail_reads = False
        self.fail_types: set = set()
        self.verify_result = True
        self.verify_calls = 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        if self.fail or event_type in self.fail_types:
            raise LedgerNotRecorded("fake ledger down")
        e = {"kind": "event", "seq": len(self.events) + 1, "event_id": event_id, "department": department,
             "event_type": event_type, "actor": actor, "subject_id": subject_id,
             "payload_sha256": payload_sha256(payload), "summary": summary, "_payload": payload}
        prev = self.by_id.get(event_id)
        if prev is not None:
            if {k: prev[k] for k in e if k not in ("seq", "_payload")} != \
                    {k: e[k] for k in e if k not in ("seq", "_payload")}:
                raise LedgerConflict("409")
            return
        self.events.append(e)
        self.by_id[event_id] = e

    def entries(self) -> list[dict]:
        if self.fail_reads:
            raise LedgerQueryFailed("fake ledger unreadable")
        return [{k: v for k, v in e.items() if k != "_payload"} for e in self.events]

    def verify(self) -> bool:
        self.verify_calls += 1
        return self.verify_result and not self.fail_reads

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == t]


class FakeEmail:
    wired = True

    def __init__(self, status: str = "accepted"):
        self.sent: list = []
        self.status = status

    def send(self, message_id, to, msg):
        self.sent.append((message_id, to, msg))
        return SendResult(self.status, f"prov-{len(self.sent)}")


class FakeDm:
    wired = True

    def __init__(self):
        self.sent: list = []

    def send(self, message_id, platform, handle, text):
        self.sent.append((message_id, platform, handle, text))
        return SendResult("accepted", f"dm-{len(self.sent)}")


class FakeSource:
    wired = True

    def __init__(self, records=None):
        self.records = records or []

    def fetch(self, query, limit):
        return list(self.records)


class FakeLegal:
    wired = True

    def __init__(self, send: str = "sent", status: str = "in_force"):
        self.send, self.status = send, status
        self.calls: list = []

    def send_contract(self, deal_id, influencer_id, deal_sha256, brief_sha256, disclosure):
        self.calls.append(("send", deal_id, deal_sha256, disclosure))
        return ContractSend(self.send, f"env-{deal_id[-8:]}" if self.send == "sent" else None)

    def contract_status(self, deal_id, envelope_ref, deal_sha256):
        self.calls.append(("status", deal_id, envelope_ref))
        return ContractCheck(self.status)


class FakeFinance:
    """Finance (31) as a recording fake. Its per-person key is a keyed hash of the "matched TIN": by default one per tax
    reference; ``same_person`` lists references Finance matched to ONE TIN; ``person_keys=False`` returns none."""
    wired = True

    def __init__(self, register: str = "registered", status: str = "verified", payout: str = "accepted",
                 person_keys: bool = True, same_person=()):
        self.register, self.status, self.payout = register, status, payout
        self.person_keys, self.same_person = person_keys, set(same_person)
        self.registered: list = []
        self.payouts: list = []
        self.keys: dict = {}

    def _person(self, tax_ref):
        if not self.person_keys:
            return None
        basis = "one-person" if tax_ref in self.same_person else tax_ref
        return "pk-" + hashlib.sha256(basis.encode()).hexdigest()[:24]

    def register_payee(self, payee_id, tax_ref, tax_form, legal_form, country, identity_ref):
        self.registered.append((payee_id, tax_ref, tax_form, legal_form, country, identity_ref))
        ref = f"payee-{payee_id[-8:]}" if self.register == "registered" else None
        self.keys[ref] = self._person(tax_ref)
        return PayeeAnswer(self.register, ref, self.keys[ref])

    def payee_status(self, payee_ref):
        return PayeeStatus(self.status, self.keys.get(payee_ref))

    def request_payout(self, payout_id, payee_ref, amount, currency, brand, deal_id):
        self.payouts.append((payout_id, payee_ref, amount, currency, brand, deal_id))
        return PayoutAnswer(self.payout, f"fin-{len(self.payouts)}" if self.payout == "accepted" else None)


def wired_ports(**over) -> Ports:
    p = Ports(FakeEmail(), FakeDm(), {"public_profile": FakeSource(), "paid_database": FakeSource()}, FakeLegal(),
              FakeFinance())
    for k, v in over.items():
        setattr(p, k, v)
    return p


def write_key(path) -> str:
    import os
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, PII_KEY_HEX.encode())
    os.close(fd)
    return str(path)


def base_env(**over) -> dict:
    env = {"INF_SERVICE_TOKEN": SERVICE_TOKEN, "INF_CALLER_TOKENS": json.dumps(CALLERS), "INF_NON_PRODUCTION": "1",
           "INF_ANDRE_APPROVAL_TOKEN": ANDRE, "INF_OUTREACH_DOMAIN": OUTREACH, "INF_ZBM_DOMAIN": ZBM_DOMAIN,
           "INF_ZBC_DOMAIN": ZBC_DOMAIN, "INF_POSTAL_ADDRESS": POSTAL}
    env.update({k: v for k, v in over.items() if v is not None})
    for k in [k for k, v in over.items() if v is None]:
        env.pop(k, None)
    return env


class Harness:
    def __init__(self, tmp, data_dir: Optional[str] = None, ledger: Optional[FakeLedger] = None,
                 clock: Optional[FixedClock] = None, ports: Optional[Ports] = None, **env_over):
        self.tmp = tmp
        extra = {}
        if data_dir:
            extra["INF_DATA_DIR"] = data_dir
            key = tmp / "pii.key"
            extra["INF_PII_HASH_KEY_FILE"] = str(key) if key.exists() else write_key(key)
        self.env = base_env(**{**extra, **env_over})
        self.settings = config_mod.load(self.env)
        self.ledger = ledger or FakeLedger()
        self.clock = clock or FixedClock(T0)
        self.ports = ports or Ports.default()
        lock = self.settings.data_dir_lock
        token = lock.claim() if lock is not None else None   # as api.build: before the log is opened
        try:
            self.svc = InfluencerService(self.settings, Recorder(self.ledger), RecordLog(self.settings.data_dir),
                                         self.ports, self.clock, lock_token=token)
        except BaseException:
            if lock is not None:
                lock.release_claim(token)
            raise
        self.client = TestClient(api._wrap(api.create_app(self.svc, self.settings)), raise_server_exceptions=False)

    def restart(self, **env_over) -> "Harness":
        self.svc.close()
        return Harness(self.tmp, self.settings.data_dir, self.ledger, self.clock, self.ports, **env_over)

    # --- http
    def headers(self, caller: Optional[str] = "dashboard", andre=False) -> dict:
        h = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        if caller:
            h[api.CALLER_HEADER] = CALLERS[caller]
        if andre:
            h[api.FOUNDER_HEADER] = ANDRE if andre is True else andre
        return h

    def post(self, path: str, body: Optional[dict] = None, caller: Optional[str] = "dashboard", andre=False):
        return self.client.post(P + path, json=body if body is not None else {}, headers=self.headers(caller, andre))

    def get(self, path: str, caller: Optional[str] = "dashboard", **kw):
        return self.client.get(P + path, headers=self.headers(caller), **kw)

    @staticmethod
    def ok(resp, code: int = 200):
        assert resp.status_code == code, (resp.status_code, resp.text)
        return resp.json()

    @staticmethod
    def code(resp, status: int, detail: str):
        assert resp.status_code == status and resp.json().get("detail") == detail, (resp.status_code, resp.text)

    def job(self, name: str):
        return self.post(f"/jobs/{name}/run", {"request_id": rid()}, caller="scheduler")

    # --- builders
    def application(self, email: str = "creator@example.test", handles=(("instagram", "@creator.one"),),
                    adult=True, **extra) -> dict:
        body = {"request_id": rid(), "display_name": "Creator One", "email": email,
                "handles": [{"platform": p, "handle": hd} for p, hd in handles], "niches": ["gaming", "comedy"],
                "follower_band": "mid", "engagement_band": "high", "country": "US",
                "attestation_text_version": "age-v1", "attestation_text_sha256": ATTEST_SHA, **extra}
        if adult is not None:
            body["adult_18_plus"] = adult
        return self.post("/applications", body, caller="hub")

    def confirm(self, conf_id: str):
        return self.post("/confirmations", {"request_id": rid(), "token": self.svc.confirmation_token(conf_id)},
                         caller="hub")

    def creator(self, email: str = "creator@example.test", handles=(("instagram", "@creator.one"),),
                first_name: Optional[str] = "Casey") -> dict:
        app = self.ok(self.application(email, handles), 201)
        self.ok(self.confirm(app["confirmation_id"]))
        inf = self.ok(self.get(f"/influencers/{app['influencer_id']}"))
        if first_name:
            self.ok(self.post(f"/influencers/{inf['influencer_id']}/first-name",
                              {"request_id": rid(), "first_name": first_name}))
        return inf

    def prospect(self, handles=(("tiktok", "@found.one"),), email: Optional[str] = "found@example.test") -> dict:
        body = {"request_id": rid(), "display_name": "Found One", "handles": [{"platform": p, "handle": hd}
                                                                              for p, hd in handles],
                "niches": ["ecommerce"], "evidence_ref": "ev-1"}
        if email:
            body["email"] = email
        return self.ok(self.post("/influencers", body), 201)

    def template(self, brand="zbm", name="intro", subject="Working together, {{first_name}}?",
                 body="Hi {{first_name}}, we would love to work with you.", approve=True) -> dict:
        t = self.ok(self.post("/templates", {"request_id": rid(), "brand": brand, "name": name, "subject": subject,
                                             "body": body}, caller="influencer_agent"), 201)
        if approve:
            v = t["versions"][-1]
            t = self.ok(self.post(f"/templates/{t['template_id']}/versions/{v['version']}/approve",
                                  {"request_id": rid(), "content_sha256": v["content_sha256"]}, andre=True))
        return t

    def email(self, inf: dict, t: dict, version: int = 1):
        return self.post("/outreach/email", {"request_id": rid(), "influencer_id": inf["influencer_id"],
                                             "template_id": t["template_id"], "version": version},
                         caller="influencer_agent")

    def campaign(self, brand="zbm", kind="influencer", partner=None) -> dict:
        body = {"request_id": rid(), "brand": brand, "name": "Fall launch", "kind": kind}
        if partner:
            body["partner"] = partner
        return self.ok(self.post("/campaigns", body, caller="influencer_agent"), 201)

    def brief(self, campaign: dict, disclosure="#ad", approve=True,
              text="Show how you use the product in a normal day. Three key points: speed, price, support.") -> dict:
        b = self.ok(self.post("/briefs", {"request_id": rid(), "campaign_id": campaign["campaign_id"],
                                          "title": "Fall launch brief", "text": text, "disclosure": disclosure},
                              caller="influencer_agent"), 201)
        if approve:
            b = self.ok(self.post(f"/briefs/{b['brief_id']}/approve",
                                  {"request_id": rid(), "content_sha256": b["content_sha256"]}, andre=True))
        return b

    def deal(self, inf: dict, campaign: dict, brief: dict, fee="1000.00", product="0.00", platform="instagram",
             caller="influencer_agent"):
        return self.post("/deals", {"request_id": rid(), "influencer_id": inf["influencer_id"],
                                    "campaign_id": campaign["campaign_id"], "brief_id": brief["brief_id"],
                                    "deliverables": [{"platform": platform, "kind": "reel", "quantity": 1}],
                                    "fee": fee, "product_value": product}, caller=caller)

    def setup_deal(self, fee="1000.00", **kw) -> tuple[dict, dict, dict, dict]:
        inf = self.creator(**kw)
        c = self.campaign()
        b = self.brief(c)
        d = self.ok(self.deal(inf, c, b, fee=fee), 201)
        return inf, c, b, d

    def contract(self, deal: dict) -> dict:
        self.ok(self.post(f"/deals/{deal['deal_id']}/contract", {"request_id": rid()}, caller="influencer_agent"))
        return self.ok(self.post(f"/deals/{deal['deal_id']}/contract/confirm", {"request_id": rid()},
                                 caller="influencer_agent"))

    def content(self, deal: dict, caption="#ad Loving this new tool from Z Best Media. #gaming", platform="instagram",
                label=True):
        return self.post("/contents", {"request_id": rid(), "deal_id": deal["deal_id"], "platform": platform,
                                       "caption": caption, "media_sha256": MEDIA, "platform_label_on": label},
                         caller="influencer_agent")

    def approve_content(self, c: dict) -> dict:
        return self.ok(self.post(f"/contents/{c['content_id']}/approve",
                                 {"request_id": rid(), "content_sha256": c["content_sha256"]}, andre=True))

    def live(self, c: dict):
        return self.post(f"/contents/{c['content_id']}/live", {"request_id": rid(),
                                                               "content_sha256": c["content_sha256"],
                                                               "post_ref": "post-1"}, caller="influencer_agent")

    def tax(self, inf: dict, ref=TAX_REF, form="w9", country="US", legal="individual"):
        """Asks for the change; it takes effect only once confirmed (``tax_confirmed``)."""
        return self.post("/tax-profiles", {"request_id": rid(), "influencer_id": inf["influencer_id"],
                                           "tax_form": form, "tax_ref": ref, "legal_form": legal,
                                           "country": country}, caller="hub")

    def tax_confirmed(self, inf: dict, ref=TAX_REF, **kw) -> dict:
        conf = self.ok(self.tax(inf, ref=ref, **kw), 201)
        return self.ok(self.confirm(conf["conf_id"]))

    def verify(self, inf: dict):
        return self.post(f"/payees/{inf['influencer_id']}/verify", {"request_id": rid()}, caller="influencer_agent")

    def payout(self, deal: dict, amount: str, content_ids, caller="influencer_agent"):
        return self.post("/payouts", {"request_id": rid(), "deal_id": deal["deal_id"], "amount": amount,
                                      "content_ids": list(content_ids)}, caller=caller)

    def paid_ready(self, fee="1000.00") -> tuple[dict, dict, dict]:
        """A creator with a contracted deal, one live approved content, and a verified payee."""
        inf, c, b, d = self.setup_deal(fee=fee)
        self.contract(d)
        content = self.ok(self.content(d), 201)
        self.approve_content(content)
        self.ok(self.live(content))
        self.tax_confirmed(inf)
        self.ok(self.verify(inf))
        return inf, d, content
