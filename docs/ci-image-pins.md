# Build image digest provenance

Observed 2026-11-01 UTC. These are multi-platform OCI index digests, not mutable tag-only references. Versions and runtime bases are unchanged.

| Reference | OCI index digest | Use |
|---|---|---|
| `golang:1.27.3-bookworm` | `sha256:d9b6a1beb9474adc2a1ce842573639b44b2a0dd49b6669f51d3821baf9ec37c9` | orchestrator and stripe-gateway |
| `rust:1.99.1-slim-bookworm` | `sha256:24707e70a4f1e23d9a7b25202ec43126d28a52294b96aad5c670df1fd1f1d400` | ledger |
| `ghcr.io/astral-sh/uv:0.8.17` | `sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1` | delivery uv tool |

Go and Rust digests match the completed successful [railway-images CI job](https://github.com/DarksiedCEO/zbm-zbc/actions/runs/41266956603/job/124731749436) and independently fetched public registry descriptors. The uv descriptor was fetched directly from the public GHCR image; that CI job did not build the full delivery service image.

Reproduce each registry observation with `docker buildx imagetools inspect <reference> --format '{{json .Manifest}}'`. The descriptor includes its digest and platform manifests. The deployment configuration tests require digest pins for every declared Railway runtime/build stage and for delivery's external uv tool image. This does not establish image provenance signatures, vulnerability freedom, or a successful build of the new commit; its CI must validate the actual pinned build.

When bumping a version, review the new descriptor and update the digest in the same change. Preserve the versioned tag for readability. The separate resilience branch's comprehensive Docker-reference scanner remains separate work; this change closes the four known unpinned references without importing that branch's unfinished runtime changes.
