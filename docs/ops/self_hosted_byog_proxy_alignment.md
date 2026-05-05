# Self-hosted BYOG Proxy Alignment SOP (KS.3.9)

> Alignment contract between KS.3 BYOG proxy and HD.21.5.2 self-hosted
> edition. The two deployment modes stay independent, but they share one
> `omnisight-proxy` container image and one operator checklist.

## 1. Scope

KS.3 and HD.21.5.2 solve different customer requests:

| Mode | Customer asks for | Runtime boundary |
|---|---|---|
| KS.3 BYOG proxy | Keep LLM provider keys inside customer VPC while OmniSight SaaS remains hosted by us. | Only `omnisight-proxy` runs customer-side. |
| HD.21.5.2 self-hosted edition | Run the whole OmniSight stack inside customer VPC or air-gapped infrastructure. | Backend, frontend, storage, and `omnisight-proxy` run customer-side. |

The shared artifact is only the proxy image. Do not couple BYOG SaaS
registration, HD license activation, or air-gapped bundle export into a
single control plane.

## 2. Shared Image Contract

Both paths pull the exact image published by `.github/workflows/docker-publish.yml`:

```text
ghcr.io/${OMNISIGHT_GHCR_NAMESPACE:-your-org}/omnisight-proxy:${OMNISIGHT_IMAGE_TAG:-latest}
```

Rules:

- Build source is `Dockerfile.omnisight-proxy`; no self-hosted forked
  Dockerfile is allowed.
- The release pipeline publishes `omnisight-proxy` beside
  `omnisight-backend` and `omnisight-frontend`.
- Self-hosted bundles may mirror the image into an internal registry,
  but the mirrored digest must match the GHCR release digest recorded in
  the customer ticket.
- Do not publish `omnisight-proxy-self-hosted`,
  `omnisight-selfhosted-proxy`, or customer-specific proxy images.
- Pin `OMNISIGHT_IMAGE_TAG` to an immutable release tag for production;
  `latest` is only acceptable for local evaluation.

Digest evidence example:

```bash
docker buildx imagetools inspect \
  ghcr.io/${OMNISIGHT_GHCR_NAMESPACE}/omnisight-proxy:${OMNISIGHT_IMAGE_TAG}

docker inspect --format '{{index .RepoDigests 0}}' \
  ${CUSTOMER_REGISTRY}/omnisight-proxy:${OMNISIGHT_IMAGE_TAG}
```

Record both digests in the deployment ticket before traffic is enabled.

## 3. Self-hosted Deployment SOP

Use the same runtime knobs as the KS.3 proxy runbooks. In self-hosted
mode, the SaaS heartbeat and metadata audit URLs point at the local
OmniSight backend service, not the public SaaS domain.

Minimal compose override:

```yaml
services:
  omnisight-proxy:
    image: ghcr.io/${OMNISIGHT_GHCR_NAMESPACE:-your-org}/omnisight-proxy:${OMNISIGHT_IMAGE_TAG:-latest}
    pull_policy: missing
    restart: unless-stopped
    ports:
      - "8443:8443"
    environment:
      - OMNISIGHT_PROXY_ADDR=:8443
      - OMNISIGHT_PROXY_AUTH_ENABLED=true
      - OMNISIGHT_PROXY_ID=proxy-selfhosted-prod
      - OMNISIGHT_PROXY_TENANT_ID=t-selfhosted
      - OMNISIGHT_PROXY_TLS_CERT_FILE=/run/certs/proxy-server.crt
      - OMNISIGHT_PROXY_TLS_KEY_FILE=/run/certs/proxy-server.key
      - OMNISIGHT_PROXY_CLIENT_CA_FILE=/run/certs/omnisight-client-ca.pem
      - OMNISIGHT_PROXY_PINNED_CLIENT_CERT_SHA256=sha256:<64-hex-fingerprint>
      - OMNISIGHT_PROXY_NONCE_HMAC_KEY_FILE=/run/secrets/nonce-hmac.key
      - OMNISIGHT_PROXY_PROVIDER_CONFIG_FILE=/etc/omnisight-proxy/providers.json
      - OMNISIGHT_PROXY_SAAS_HEARTBEAT_URL=http://backend-a:8000/api/v1/byog/proxies/proxy-selfhosted-prod/heartbeat
      - OMNISIGHT_PROXY_SAAS_AUDIT_URL=http://backend-a:8000/api/v1/byog/proxies/proxy-selfhosted-prod/audit
      - OMNISIGHT_PROXY_CUSTOMER_AUDIT_LOG_FILE=/var/log/omnisight-proxy/audit.ndjson
    volumes:
      - ./self-hosted/proxy/certs:/run/certs:ro
      - ./self-hosted/proxy/secrets:/run/secrets:ro
      - ./self-hosted/proxy/providers.json:/etc/omnisight-proxy/providers.json:ro
      - ./self-hosted/proxy/audit:/var/log/omnisight-proxy
```

Air-gapped installs use the same service block after importing the image
tarball into the customer registry:

```bash
docker load -i omnisight-proxy-${OMNISIGHT_IMAGE_TAG}.tar
docker tag \
  ghcr.io/${OMNISIGHT_GHCR_NAMESPACE}/omnisight-proxy:${OMNISIGHT_IMAGE_TAG} \
  ${CUSTOMER_REGISTRY}/omnisight-proxy:${OMNISIGHT_IMAGE_TAG}
docker push ${CUSTOMER_REGISTRY}/omnisight-proxy:${OMNISIGHT_IMAGE_TAG}
```

## 4. BYOG SaaS Deployment SOP

For hosted OmniSight tenants, follow
`docs/ops/tier2_to_tier3_byog_proxy_upgrade.md`. The image reference is
the same, but heartbeat and metadata audit URLs point at the hosted
OmniSight SaaS API:

```text
OMNISIGHT_PROXY_SAAS_HEARTBEAT_URL=https://ai.sora-dev.app/api/v1/byog/proxies/<proxy_id>/heartbeat
OMNISIGHT_PROXY_SAAS_AUDIT_URL=https://ai.sora-dev.app/api/v1/byog/proxies/<proxy_id>/audit
```

Do not reuse the self-hosted annual license activation token as BYOG
proxy authentication. BYOG proxy authentication remains mTLS plus signed
nonce.

## 5. Cutover Checklist

- [ ] Release tag pinned in `OMNISIGHT_IMAGE_TAG`.
- [ ] `Dockerfile.omnisight-proxy` is the only proxy Dockerfile used.
- [ ] GHCR digest and customer registry digest match.
- [ ] Self-hosted bundle includes the same proxy image tarball as the
      BYOG release artifact.
- [ ] mTLS, cert pinning, and signed nonce are enabled.
- [ ] `providers.json` uses one of the KS.3.3 key sources:
      `local_file`, `kms`, or `vault`.
- [ ] Heartbeat reaches the correct backend for the mode:
      public SaaS for KS.3, local backend for HD.21.5.2.
- [ ] Customer audit log path is mounted and writable.
- [ ] OmniSight-side audit metadata contains no prompt or response
      payload.
- [ ] Ticket records release tag, GHCR digest, mirrored digest, proxy id,
      tenant id, certificate fingerprint, and operator.

## 6. HD.21.5 Shared Image Confirmation

HD.21.5 self-hosted edition is confirmed to share the same customer-side
proxy image as KS.3 BYOG SaaS tenants:

- Canonical image: `ghcr.io/${OMNISIGHT_GHCR_NAMESPACE:-your-org}/omnisight-proxy:${OMNISIGHT_IMAGE_TAG:-latest}`.
- Canonical build source: `Dockerfile.omnisight-proxy`.
- Canonical release path: `.github/workflows/docker-publish.yml` matrix
  entry `image: omnisight-proxy`.
- Self-hosted bundles may include `omnisight-proxy-${OMNISIGHT_IMAGE_TAG}.tar`
  or mirror it to a customer registry, but the digest evidence must
  match the GHCR release digest.
- There is no self-hosted-only proxy Dockerfile, package, image name, or
  registry repository. Keep `omnisight-proxy-self-hosted`,
  `omnisight-selfhosted-proxy`, and customer-specific proxy images out
  of release automation.

This is an artifact-sharing confirmation only. KS.3 BYOG SaaS
registration, HD.21.5.2 self-hosted annual license activation, air-gapped
bundle export, heartbeat URL selection, and rollback decisions remain
mode-specific.

## 7. Rollback Boundary

Image rollback is allowed by pinning `OMNISIGHT_IMAGE_TAG` back to the
last known-good release and redeploying the same compose service.

Configuration rollback differs by mode:

- KS.3 BYOG SaaS tenant: disable proxy mode before SaaS-side key purge,
  as described in the KS.3.8 runbook.
- HD.21.5.2 self-hosted tenant: keep the full stack inside the customer
  VPC and roll back only the proxy service. Do not route provider calls
  out to OmniSight SaaS as a fallback.

After KS.3 SaaS-side key purge, OmniSight must not recreate provider
keys from backups. After HD.21.5.2 self-hosted cutover, OmniSight must
not request customer provider keys for hosted fallback.
