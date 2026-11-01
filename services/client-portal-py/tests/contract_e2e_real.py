"""
End-to-end contract (ADR 0028): the REAL legal-py app, the REAL client acceptance page and the REAL finance-py app,
in one process, no network (each app's ASGI transport; Finance reaches Legal through its real thin client
``clients.HttpLegal``, the page through its real ``legal_client.LegalClient``). Run as a script by
``test_e2e_real.py`` in a subprocess: the three services have modules with the same names (config, api, helpers ...),
so each is imported, kept by reference, and purged from ``sys.modules`` before the next. Prints one JSON object of
observations; the test asserts on it.

Path proven:
  Andre: LG-20 rules, CQ-19 founder determination, the MSA (standard form) and a client's SOW fill founder-approved
  -> Andre invites client:acme-1 to the MSA + SOW (a single-use link)
  -> the client opens the page, ticks both boxes, types name/title/company, submits (Origin = the page's origin)
  -> Legal holds two evidence-sufficient acceptances recorded by the page
  -> Finance: billing readiness allowed, and Andre's invoice approval ISSUES the invoice (FIN-29 legal gate).
Negatives: the used link (page and resubmission), another client's invoice with acme's acceptance, a tampered
document hash (at the page's Legal route and in Finance's contract reference), a link for another client naming
acme's fill, a SOW template retired after an invitation, and an expired link.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys

sys.dont_write_bytecode = True
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve()
PORTAL = HERE.parents[1]
SERVICES = PORTAL.parent
LEGAL, FINANCE = SERVICES / "legal-py", SERVICES / "finance-py"


def _refuse(*a, **k):
    raise RuntimeError("network access attempted (not allowed)")


socket.socket.connect = _refuse
socket.create_connection = _refuse


def use(service: Path) -> None:
    """Put one service's src and tests first on sys.path, after purging every module loaded from another service."""
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if f.startswith(str(SERVICES)) and not f.startswith(str(service)):
            del sys.modules[name]
    sys.path[:] = [p for p in sys.path if not p.startswith(str(SERVICES))]
    sys.path[:0] = [str(service / "src"), str(service / "tests")]


MSA = "Z Best Media Master Services Agreement (test text): the same terms for every client.\n"
SOW_TEMPLATE = "Statement of Work for {{client_legal_name}}: SEO audit, fee {{fee_usd}} USD, due in 15 days.\n"
SOW_VARS = {"client_legal_name": {"type": "string", "max_length": 200},
            "fee_usd": {"type": "string", "max_length": 20}}
RATIONALE = "Founder approval (ADR 0022): ZBM's client documents as approved by me as founder."
DISPLAY = {"client_legal_name": "Acme LLC", "notice_email": "legal@acme.example", "contact_first_name": "Pat",
           "sow_number": "SOW-2026-001", "sow_title": "SEO and AI Visibility Audit"}


def main() -> int:
    out: dict = {}
    # ---------------------------------------------------------------- 1) legal-py (Andre's side)
    use(LEGAL)
    os.environ["LEGAL_SERVICE_TOKEN"] = "test-legal-service-token-do-not-use-0123"
    import helpers as LH                                    # noqa: E402  legal-py's harness
    x = LH.Harness()
    x.approve_rules()

    def fap(doc_id, version, digest):
        x.ok(x.apost(f"/legal/v1/documents/{doc_id}/versions/{version}/founder-approval",
                     {"request_id": LH.rid("fap"), "version_sha256": digest, "rationale": RATIONALE,
                      "founder_confirms": True}))

    def fill(party, fee):
        f = x.ok(x.apost("/legal/v1/documents/sow_seo_audit/versions",
                         {"request_id": LH.rid("fill"), "entity": "zbm", "party_ref": party,
                          "variables": {"client_legal_name": "Acme LLC", "fee_usd": fee}}), 201)
        fap("sow_seo_audit", f["version"], f["sha256"])
        return f

    def invite(party, docs, code=201):
        return x.ok(x.apost("/legal/v1/client-acceptance-invitations",
                            {"request_id": LH.rid("cinv"), "party_ref": party, "documents": docs,
                             "display": dict(DISPLAY)}), code)

    x.ok(x.apost("/legal/v1/register/CQ-19/founder-determination",
                 {"request_id": LH.rid("fdt"), "founder_confirms": True,
                  "rationale": "Founder determination (ADR 0022): clients accept on the page by unticked boxes."}))
    m = x.upload("client_msa", MSA, "1.0", "zbm")
    fap("client_msa", "1.0", m["sha256"])
    t = x.upload("sow_seo_audit", SOW_TEMPLATE, "1.0", "zbm", template_variables=SOW_VARS)
    fap("sow_seo_audit", "1.0", t["sha256"])
    sow = fill("client:acme-1", "1500.00")
    msa_ref = {"doc_id": "client_msa", "version": "1.0", "doc_sha256": m["sha256"]}
    sow_ref = {"doc_id": "sow_seo_audit", "version": sow["version"], "doc_sha256": sow["sha256"]}
    link = invite("client:acme-1", [msa_ref, sow_ref])
    out["link_page"] = link["page"]
    transport = x.client._transport
    legal_token, portal_caller, finance_caller = LH.SERVICE_TOKEN, LH.CALLERS["client_portal"], LH.CALLERS["finance_31"]
    lclock = x.clock
    import clock as legal_clock                                    # noqa: E402

    # ---------------------------------------------------------------- 2) finance-py, its real Legal thin client
    use(FINANCE)
    os.environ["FIN_SERVICE_TOKEN"] = "test-fin-service-token-do-not-use-0123456"
    import helpers as FH                                    # noqa: E402  finance-py's harness
    from clients import HttpLegal                           # noqa: E402
    hr = FH.Harness().ready()
    hr.svc.ports.legal = HttpLegal("http://testserver", legal_token, finance_caller, transport=transport, timeout=5,
                                   now=lclock.now)

    def billing_profile(client, acc_id, ref=msa_ref):
        return hr.put(f"/fin/v1/clients/{client}/billing-profile",
                      {"request_id": FH.rid(), "entity": "zbm", "payment_method": "ach",
                       "msa": {**ref, "acceptance_id": acc_id}}, andre=FH.ANDRE_TOKEN)

    import clock as finance_clock                           # noqa: E402
    import ports as finance_ports                           # noqa: E402

    def invoice(client, ref, acc_id):
        inv = hr.ok(hr.post("/fin/v1/invoices", {
            "request_id": FH.rid(), "entity": "zbm", "client_id": client, "kind": "service",
            "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "1500.00"}],
            "payment_methods": ["ach"], "legal_ref": {**ref, "acceptance_id": acc_id}}, caller="onboarding"), 201)["invoice"]
        r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                    {"request_id": FH.rid(), "content_sha256": inv["content_sha256"], "decision": "approve"},
                    andre=FH.ANDRE_TOKEN)
        return r.status_code, r.json()

    # ---------------------------------------------------------------- 3) the client acceptance page
    use(PORTAL)
    # Legal and Finance both import ``clock`` inside functions (``from clock import parse_iso``) at request time, and
    # their calls interleave (Finance's thin client runs Legal's app inside a Finance request). The two parse_iso are
    # byte-identical (asserted), so one ``clock`` module serves both; Finance's lazy ``ports`` import is kept too.
    import inspect
    assert inspect.getsource(legal_clock.parse_iso) == inspect.getsource(finance_clock.parse_iso)
    sys.modules["clock"], sys.modules["ports"] = legal_clock, finance_ports
    import helpers as PH                                    # noqa: E402  the page's own test helpers
    from app import PortalApp                               # noqa: E402
    from legal_client import LegalClient                    # noqa: E402
    settings = PH.settings()
    page_app = PortalApp(settings, LegalClient("http://testserver", legal_token, portal_caller, transport=transport))
    tok = link["token"]
    out["finance_before_acceptance"] = invoice("acme-1", sow_ref, "lg-acc-" + "0" * 26)[0]   # no acceptance: refused
    page = PH.call(page_app, "GET", f"/accept/{tok}")
    out["page_status"] = page.status
    out["page_shows_texts"] = MSA in page.text and "fee 1500.00 USD" in page.text
    done = PH.form_post(page_app, token=tok, get=page)
    out["submit_status"] = done.status
    out["confirmed"] = "Thank you. Your acceptance is recorded." in done.text
    refs = re.findall(r"Acceptance reference: (lg-acc-[0-9A-Z]{26})", done.text)
    accs = {a["doc_id"]: a for a in x.svc.acceptances.values()}
    out["legal_acceptances"] = sorted((a["doc_id"], a["entry_by"], a["evidence_sufficient"]) for a in accs.values())
    out["refs_match_legal"] = sorted(refs) == sorted(a["acceptance_id"] for a in accs.values())
    msa_acc, sow_acc = accs["client_msa"]["acceptance_id"], accs["sow_seo_audit"]["acceptance_id"]
    out["ip_hmac_only"] = all(len(a["portal_evidence"]["ip_hmac_sha256"]) == 64 for a in accs.values()) \
        and "203.0.113.7" not in json.dumps(list(accs.values()))
    # reuse: the page is gone; the SAME form again (a refresh, a lost response) gets the confirmation from Legal's
    # receipt (AEGIS M-2), a different submission gets the 404; nothing more is recorded
    out["reuse_page"] = PH.call(page_app, "GET", f"/accept/{tok}").status
    again = PH.form_post(page_app, token=tok, get=page)
    out["reuse_submit"] = [again.status, "Thank you. Your acceptance is recorded." in again.text,
                           sorted(re.findall(r"Acceptance reference: (lg-acc-[0-9A-Z]{26})", again.text)) == sorted(refs)]
    lclock.advance(minutes=30)                                    # beyond Legal's 15-minute idempotency window
    out["reuse_submit_later"] = PH.form_post(page_app, token=tok, get=page).status
    out["other_submit"] = PH.form_post(page_app, token=tok, get=page, fields={"title": "CEO"}).status
    out["acceptance_count_after_reuse"] = len(x.svc.acceptances)

    # ---------------------------------------------------------------- 4) Finance recognises the page's acceptances
    out["billing_profile"] = billing_profile("acme-1", msa_acc).status_code
    ready = hr.ok(hr.get("/fin/v1/clients/acme-1/billing-readiness", caller="onboarding"))
    out["billing_ready"] = ready["allowed"]
    code, body = invoice("acme-1", sow_ref, sow_acc)
    out["invoice_issue"] = [code, body.get("invoice", {}).get("status")]
    # wrong client: globex presents acme's acceptance
    code, body = invoice("globex-2", sow_ref, sow_acc)
    out["wrong_client_invoice"] = [code, "LEGAL_NOT_CURRENT" in json.dumps(body)]
    # tampered document hash in Finance's contract reference
    code, body = invoice("acme-1", {**sow_ref, "doc_sha256": "f" * 64}, sow_acc)
    out["tampered_hash_invoice"] = [code, "LEGAL_NOT_CURRENT" in json.dumps(body)]

    # ---------------------------------------------------------------- 5) negatives at the link
    # another client's link naming acme's fill: Legal refuses to issue it
    out["wrong_client_link"] = [r["code"] for r in invite("client:globex-2", [msa_ref, sow_ref], code=409)["reasons"]]
    # a tampered document hash at the page's Legal route (the portal caller itself cannot record it)
    sow2 = fill("client:acme-1", "2500.00")
    sow2_ref = {"doc_id": "sow_seo_audit", "version": sow2["version"], "doc_sha256": sow2["sha256"]}
    link2 = invite("client:acme-1", [msa_ref, sow2_ref])
    body = {"request_id": "e2e-tamper-1", "token": link2["token"],
            "documents": [{**msa_ref, "presented_sha256": msa_ref["doc_sha256"], "affirmative_act": True},
                          {**sow2_ref, "doc_sha256": "e" * 64, "presented_sha256": "e" * 64, "affirmative_act": True}],
            "presentation": "inline", "page_sha256": LH.sha(b"p"), "page_text_b64": LH.b64(b"p"),
            "signatory": {"full_name": "Mallory", "title": "CEO", "company": "Acme LLC"},
            "ip_hmac_sha256": "1" * 64, "user_agent_hmac_sha256": "2" * 64, "hmac_key_id": "0" * 16,
            "form_sha256": "3" * 64}
    r = x.post("/legal/v1/portal/acceptances", body, caller="client_portal")
    out["tampered_hash_accept"] = [r.status_code, x.svc.client_invitations[link2["invitation_id"]]["used_at"]]
    # AEGIS M-5: the portal caller cannot record a page Legal did not render, nor another company
    good_docs = [{**msa_ref, "presented_sha256": msa_ref["doc_sha256"], "affirmative_act": True},
                 {**sow2_ref, "presented_sha256": sow2_ref["doc_sha256"], "affirmative_act": True}]
    fake = {**body, "request_id": "e2e-fake-page", "documents": good_docs,
            "signatory": {"full_name": "Mallory", "title": "CEO", "company": "Totally Different Inc"},
            "page_sha256": LH.sha(b"anything at all"), "page_text_b64": LH.b64(b"anything at all")}
    r = x.post("/legal/v1/portal/acceptances", fake, caller="client_portal")
    out["fabricated_page_accept"] = [r.status_code, sorted(i["code"] for i in r.json().get("reasons", []))]
    # the SOW template retired after the invitation: the page (opened before) cannot be submitted, nor reopened
    page2 = PH.call(page_app, "GET", f"/accept/{link2['token']}")
    x.ok(x.apost("/legal/v1/documents/sow_seo_audit/versions/1.0/decision",
                 {"request_id": LH.rid(), "decision": "retire", "version_sha256": t["sha256"]}))
    out["template_changed"] = [page2.status, PH.form_post(page_app, token=link2["token"], get=page2).status,
                               PH.call(page_app, "GET", f"/accept/{link2['token']}").status]
    # the invoice under the SOW acme already accepted still issues (ADR 0022: a recorded acceptance stays valid)
    out["invoice_after_template_retired"] = invoice("acme-1", sow_ref, sow_acc)[0]
    # an expired link: an MSA-only link (Legal issues it; the page never serves it), then 14 days pass
    link3 = invite("client:acme-1", [msa_ref])
    out["msa_only_page"] = PH.call(page_app, "GET", f"/accept/{link3['token']}").status
    resolve3 = lambda: x.post("/legal/v1/portal/invitations/resolve", {"token": link3["token"]},   # noqa: E731
                              caller="client_portal").status_code
    before = resolve3()
    lclock.advance(days=14)
    out["expired_resolve"] = [before, resolve3()]
    out["acceptances_total"] = len(x.svc.acceptances)
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
