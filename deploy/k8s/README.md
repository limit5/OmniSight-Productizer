# OmniSight — Kubernetes manifests

Plain YAML manifests for operators who run `kubectl apply -f` directly.
The Helm chart (G5 #5, TODO row 1373) under `deploy/helm/omnisight/`
renders these same manifests through templates for release-managed
workflows. Two surfaces, one truth.

## Charter

See `docs/ops/orchestration_selection.md` (G5 #1). Key commitments
locked in §7:

- `deploy/k8s/` ships plain YAML; `deploy/helm/omnisight/` ships a chart.
- `PodDisruptionBudget` uses `policy/v1` — lands in G5 #3 (row 1371).
- Probes use `httpGet` against the G1 `/readyz` and `/livez` endpoints —
  wired in G5 #4 (row 1372).
- HPA uses `autoscaling/v2` with `targetCPUUtilizationPercentage: 70`.
- Deployment RollingUpdate with `maxUnavailable: 0` and `maxSurge: 1`.
- Ingress defaults to `ingressClassName: nginx`; Gateway-API is an
  explicit Helm toggle (not silent auto-detect).
- CI smoke runs against `kind` 1.29 to pin the minimum version claim.

## Files

| File | Kind | API version | Source of truth |
|---|---|---|---|
| `00-namespace.yaml` | Namespace | v1 | charter §3.2 |
| `10-deployment-backend.yaml` | Deployment | apps/v1 | charter §7.4 |
| `15-pdb-backend.yaml` | PodDisruptionBudget | policy/v1 | charter §7.2 |
| `20-service-backend.yaml` | Service | v1 | charter §7 |
| `30-ingress.yaml` | Ingress | networking.k8s.io/v1 | charter §7.6 |
| `40-hpa-backend.yaml` | HorizontalPodAutoscaler | autoscaling/v2 | charter §7.4 |

Numeric prefixes encode apply order — `kubectl apply -f deploy/k8s/`
walks the directory in lexical order, so the namespace exists before
any namespaced object tries to land.

## Apply

```bash
kubectl apply -f deploy/k8s/
```

To override the image for a real cluster (the default
`ghcr.io/your-org/omnisight-backend:latest` is a placeholder):

```bash
kubectl -n omnisight set image deployment/omnisight-backend \
  backend=ghcr.io/${OMNISIGHT_GHCR_NAMESPACE}/omnisight-backend:${OMNISIGHT_IMAGE_TAG}
```

Or use the Helm chart (G5 #5, row 1373) which accepts
`--set image.repository=…` / `--set image.tag=…`.

## CI smoke

A `kind` 1.29 cluster is the minimum version target per charter §7.7.
Every manifest must render + apply cleanly:

```bash
kind create cluster --image kindest/node:v1.29.0
kubectl apply -f deploy/k8s/
kubectl -n omnisight wait deploy/omnisight-backend --for=condition=available --timeout=120s
```

The G5 #6 delivery bundle (row 1374) will land the CI job that runs
this against each PR.

## Scope — what this bundle does NOT include

- CI smoke workflow + kind harness → G5 #6 row 1374.
- `deploy/nomad/` or `deploy/swarm/` — out of scope per charter §7.8.

`PodDisruptionBudget` (G5 #3 row 1371) is part of the bundle —
ships as `15-pdb-backend.yaml` with `policy/v1` and `minAvailable: 1`.

The Helm chart (G5 #5 row 1373) ships under `deploy/helm/omnisight/`
with split `values-staging.yaml` / `values-prod.yaml`. Charter §7.5 —
two surfaces, one truth: the chart is intended to render byte-faithfully
to the manifests above for the fields the chart owns, so
`helm template … | kubectl diff -f - -f deploy/k8s/` is a no-op for
those fields.

Readiness / liveness probes (G5 #4 row 1372) are wired into the
Deployment's backend container: `httpGet` against the G1 `/readyz`
and `/livez` endpoints (both served on the named `http` port 8000).
`/livez` is a byte-identical alias of `/healthz` added in G1 so the
K8s probes can follow the charter spelling with zero payload drift.
