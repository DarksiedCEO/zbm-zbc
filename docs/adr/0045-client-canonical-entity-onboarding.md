# ADR 0045 — Founder-authorized client canonical entity creation

Status: implementation candidate; independent review and hosted gates pending. No production activation or factual verification claim.

## Problem

A client tenant could read its canonical entity and an existing entity could be updated, but only the own-tenant seed had a creation path. An ordinary client therefore received ENTITY_NOT_FOUND and could not provide canonical facts for an audit through the service API. This change adds that missing interface without changing paid-audit, Compliance, renderer or provider behavior.

## Interface and ownership

`POST /seo/v1/tenants/{tid}/entity` requires the existing service bearer, dashboard caller and separate Andre approval. The route selects an existing tenant of kind `client`. The body cannot select a tenant or entity ID. Own tenants and the `ent-zbm` seed remain on their existing path.

The strict body contains `request_id` and `fields`, a nonempty list of at most five strict records. Each record contains `field`, `value`, `source`, `provenance` and `authority`. `field` is one of name, street_address, locality, region, telephone; duplicate names are refused. Every value uses the existing update validation, including printable ASCII/200-character bounds, source vocabulary shape, four authority values and ten-digit normalized telephone semantics. Omissions remain absent; no address, country, hours, phone or verification status is inferred.

A list matches the existing `field: telephone` update convention. The global personal-data key scanner remains unchanged: a body key named telephone is still refused. Creation is not an exemption for arbitrary personal-data keys or a provider identity check.

## State and evidence

The stable ID is `ent-{tenant_id}`: existing tenant validation bounds its suffix to40characters and the existing entity-ID grammar accepts it. Under the same service lock, creation checks the service/integrity gate, global/write/tenant/entity_write kills, client kind, existing tenant binding and any occupied entity ID. A collision refuses rather than overwriting or reassigning ownership.

Creation reuses `_req` and `_commit('entity_created',...)`. Typed ledger evidence contains IDs and a hash of supplied fields; the existing local journal holds the factual record. No in-memory publication precedes the existing evidence/anchor/local append contract. Existing pending-line roll-forward handles anchor or append uncertainty; this change adds no retry policy or recovery machinery. Initial version is1, each supplied field gets the committed observation time and empty history.

Idempotency uses actorAndre plus operation, route tenant and request ID, bound to the full submitted body hash. Exact replay returns the current entity view, consistent with existing field-update replay; it does not reset later versions/history. Reordering the field list under the same request ID conservatively counts as changed content and conflicts. A changed body under that key returns REQUEST_ID_REUSED; a new creation request for an already-bound tenant or occupied ID returns ENTITY_EXISTS.

## Boundaries

Factual input does not require billing or Compliance approval. It cannot bind Finance clients, establish payment, activate a client or bypass paid-audit gates. A supplied authority/provenance label remains a supplied assertion, not an independently verified business fact. No outbound request, listing write, report delivery or human review is performed by this route.

No journal kind/schema migration is introduced: existing entity_created and request metadata are reused. Older readers understand the representation but lack this creation route; no external rollback enforcement or readiness certification is claimed. Runtime scope is models.py, api.py, svc_entity.py and two registered fixed reasons in reasons.py. Full tests and private proof results are separately recorded at the immutable candidate.
