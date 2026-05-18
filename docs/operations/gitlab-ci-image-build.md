# GitLab CI Image Build

## Goal

`.gitlab-ci.yml` mirrors `.github/workflows/build-images.yml` for the
three production runtime images:

| Image path | Dockerfile |
|------------|------------|
| `${CI_REGISTRY_IMAGE}/backend` | `Dockerfile.backend` |
| `${CI_REGISTRY_IMAGE}/frontend` | `Dockerfile.frontend` |
| `${CI_REGISTRY_IMAGE}/bridge` | `Dockerfile.bridge` |

The pipeline runs only for `v*` tag pushes. It publishes these tags for
each image:

| Tag | Rule |
|-----|------|
| `${CI_COMMIT_TAG}` | every `v*` tag pipeline |
| `sha-${CI_COMMIT_SHORT_SHA}` | every `v*` tag pipeline |
| `latest` | every `v*` tag pipeline |

Branch pushes do not run this pipeline and do not move `latest`.

## Stages

| Stage | Purpose |
|-------|---------|
| `build` | Build and push backend, frontend, and bridge multi-arch images. |
| `sign` | Sign the pushed image digest with cosign keyless using GitLab OIDC. |
| `sbom` | Generate a CycloneDX SBOM with syft for each image digest. |
| `attest` | Attach an in-toto predicate with `cosign attest`. |
| `audit-emit` | Emit `image-audit-${name}.json` as a 90-day artifact. |

## GitLab CI Variable Contract

GitLab provides these variables automatically when the project container
registry is enabled:

| Variable | Required use |
|----------|--------------|
| `CI_REGISTRY` | Registry host used for login and audit metadata. |
| `CI_REGISTRY_IMAGE` | Base namespace for `backend`, `frontend`, and `bridge`. |
| `CI_REGISTRY_USER` | Registry push username. |
| `CI_REGISTRY_PASSWORD` | Registry push token/password. |
| `CI_COMMIT_TAG` | Release tag, required to match `v*`. |
| `CI_COMMIT_SHORT_SHA` | Immutable `sha-<short>` tag suffix. |
| `CI_COMMIT_SHA` | OCI revision label and audit metadata. |
| `CI_PIPELINE_ID` | Audit run identifier. |
| `CI_PIPELINE_URL` | Audit run URL and attestation predicate metadata. |

The pipeline defines these defaults:

| Variable | Default |
|----------|---------|
| `COSIGN_VERSION` | `v2.4.1` |
| `SYFT_VERSION` | `v1.17.0` |
| `COSIGN_CERT_IDENTITY_REGEXP` | `^https://sora\.services:49154/omnisight/OmniSight-Productizer//\.gitlab-ci\.yml@refs/tags/v.*$` |
| `COSIGN_CERT_OIDC_ISSUER` | `https://sora.services:49154` |

Override `COSIGN_CERT_IDENTITY_REGEXP` or `COSIGN_CERT_OIDC_ISSUER` in
GitLab CI/CD variables if the self-managed GitLab instance advertises a
different OIDC issuer URL or project identity claim.

## Runner Requirements

The runner that executes this pipeline needs:

- Docker-in-Docker support with privileged mode enabled.
- Network egress to the GitLab Container Registry.
- Network egress to `github.com` for cosign and syft installer downloads.
- GitLab OIDC `id_tokens` support for the `SIGSTORE_ID_TOKEN` token with
  audience `sigstore`.

## Canary Procedure

1. Push a dummy canary tag:

   ```bash
   git tag v0.5.0-rc3-canary
   git push origin v0.5.0-rc3-canary
   ```

2. Watch the GitLab pipeline for the `build`, `sign`, `sbom`, `attest`,
   and `audit-emit` stages.

3. Pull and verify from the production host:

   ```bash
   docker login sora.services:49154
   docker pull sora.services:49154/omnisight/OmniSight-Productizer/backend:v0.5.0-rc3-canary
   cosign verify \
     --certificate-identity-regexp '^https://sora\.services:49154/omnisight/OmniSight-Productizer//\.gitlab-ci\.yml@refs/tags/v.*$' \
     --certificate-oidc-issuer 'https://sora.services:49154' \
     sora.services:49154/omnisight/OmniSight-Productizer/backend:v0.5.0-rc3-canary
   ```

4. Compare the pulled digest with the signed digest emitted by the
   `sign-image: [backend, Dockerfile.backend]` job.
