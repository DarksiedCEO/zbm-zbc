# Dependency maintenance

The repository's Dependabot configuration requests weekly version checks for service Python manifests, the shared deployment Python input/hash lock, Delivery's uv graph, the ledger's Cargo graph, the orchestrator's Go graph, the dashboard's npm graph, and GitHub Actions. Version updates are limited to one open PR per configured update entry; security updates use GitHub's separate limit. Minor/patch version updates are grouped within each ecosystem; major versions remain eligible for individual review. Security groups have no semantic-version filter.

The file takes effect on the default branch. A reviewed configuration is not evidence that a hosted update has run successfully. After merging, inspect the Dependency graph / Dependabot update logs and verify every configured location is discovered. Repository-level dependency graph, alerts and security-update settings are separate prerequisites; this change does not enable or attest them. No automatic approval or merge is configured.

## Review an update

1. Confirm the PR's exact head, manifest/lock consistency and release notes. Delivery has both requirements.txt and uv.lock, so changes to shared dependencies may need coordinated updates. Deployment Python requirements.in and requirements.lock.txt must remain consistent, including all distribution hashes.
2. Re-run the affected service suites through the hygiene wrapper, relevant live fixtures, dependency audits and image builds. Preserve count checks and existing digest/ADR assertions. Do not weaken a gate because a bot authored the change.
3. Apply the normal independent review and deployment prerequisites. A successful registry advisory scan does not establish safety of a pinned Git revision; review those changes separately.

## Deliberate limits

Toolchain binaries, Docker base images and checksum build arguments are maintained through the existing digest-pin/ADR/build checks and explicit reviewed updates; they are not covered by these package-manager entries. Delivery's toy fixtures and archived evidence manifests are excluded from this production dependency schedule. Dependabot cannot establish runtime behavior, image provenance, compatibility of updated lock formats with the CI-pinned package manager, or availability of backups and rollback. The first hosted run and update PR remain required operational evidence.

References: [GitHub configuration options](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference), [supported ecosystems](https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories), and [security-update configuration](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-security-updates).
