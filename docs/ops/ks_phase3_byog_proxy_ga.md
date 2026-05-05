# KS Phase 3 BYOG Proxy GA Evidence

> Phase 3 Definition of Done for Tier 3 BYOG proxy. This page is an
> evidence index: it does not replace the narrower KS.3 runbooks or the
> Go/Python contract tests that enforce each row.

## Scope

Phase 3 covers the customer-side `omnisight-proxy` and the SaaS
fail-fast path that routes Tier 3 LLM traffic through it. It does not
cover KS.4 cross-cutting mitigations, incident response, pentest, SOC 2,
or the final all-KS three-knob rollout rows.

## GA Evidence Matrix

| Phase 3 requirement | Required evidence | Guard |
|---|---|---|
| `omnisight-proxy` GA, < 100 MB | `Dockerfile.omnisight-proxy` builds a stripped static Go binary into `gcr.io/distroless/static-debian12:nonroot`; `.github/workflows/docker-publish.yml` publishes `omnisight-proxy`; live image size is checked by `OMNISIGHT_TEST_PROXY_IMAGE=1`. | `backend/tests/test_omnisight_proxy_image.py` |
| p95 latency overhead < 50 ms | Go proxy tests run proxied requests through an mTLS server, require connection reuse, and fail when proxy-hop p95 is `>= 50ms`. CI runs `go test ./...` in `omnisight-proxy`. | `omnisight-proxy/internal/server/server_test.go`, `.github/workflows/ci.yml` |
| mTLS handshake matrix | Auth tests cover valid client cert, pinned-cert mismatch, expired cert, and self-signed cert. | `omnisight-proxy/internal/auth/auth_test.go` |
| Replay protection | Auth tests reject reused nonces and verify a bad signature does not consume the nonce. SaaS client tests ensure signed nonces differ across requests. | `omnisight-proxy/internal/auth/auth_test.go`, `backend/tests/test_byog_proxy_fail_fast.py` |
| HD.21.5 self-hosted edition shared image | Self-hosted SOP requires the same GHCR `omnisight-proxy` image, digest match evidence, no self-hosted proxy fork, and mode-specific heartbeat/audit URLs. | `docs/ops/self_hosted_byog_proxy_alignment.md`, `backend/tests/test_ks39_self_hosted_byog_alignment.py` |
| Strict zero-trust, proxy unreachable does not fallback | SaaS proxy client raises BYOG-specific errors on transport/mTLS/auth failures and exposes no direct-provider fallback hook. Upgrade and self-hosted runbooks explicitly forbid hosted fallback after Tier 3 cutover/purge. | `backend/byog_proxy_client.py`, `backend/tests/test_byog_proxy_fail_fast.py`, `docs/ops/tier2_to_tier3_byog_proxy_upgrade.md` |

## Operator Cutover Gate

Before a Tier 3 tenant is marked deployed-active, the operator must
record this evidence in the customer ticket:

- Immutable proxy image tag and GHCR digest.
- Customer registry digest when the image is mirrored.
- mTLS CA, pinned client certificate fingerprint, and certificate expiry.
- Signed-nonce HMAC key location or customer KMS / Vault reference.
- `GET /api/v1/byog/proxies/<proxy_id>/health` showing
  `connected=true` and `stale=false`.
- One successful proxied request per provider, including streaming when
  the provider supports it.
- Replayed nonce rejected by the proxy.
- Customer audit log contains full prompt/response; OmniSight audit
  metadata contains no prompt or response payload.
- SaaS-side provider key material cleared after customer import, with a
  sampled decrypt of old Tier 2 credential ids failing as expected.

## Runtime State Notes

`omnisight-proxy` is a single Go process. Its replay cache is
process-local by design and is protected by a mutex; no module-global
mutable state is shared across SaaS workers. SaaS-side proxy health and
metadata stores coordinate through Redis in production, while the
in-memory stores are intentionally dev/test-only and per worker.

## Production Status

Current status is `dev-only`: code, docs, CI contracts, and local tests
are present, but no live customer proxy image digest or tenant cutover
ticket is recorded in this repository.

Next gate is `deployed-active`: publish an immutable `omnisight-proxy`
release image, deploy it for the first Tier 3 customer or staging
tenant, record the cutover evidence above, and run a no-fallback smoke
where proxy unreachable returns a BYOG error instead of direct provider
egress.
