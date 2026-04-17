# OmniSight Helm chart (G5 #5, TODO row 1373)

Templates the same six K8s objects that `deploy/k8s/*.yaml` ships
(Namespace, Deployment, Service, Ingress, PodDisruptionBudget, HPA) for
release-managed workflows. Two surfaces, one truth — chart output is
intended to be `kubectl diff`-clean against the plain manifests for the
fields the chart owns.

## Charter

`docs/ops/orchestration_selection.md` (G5 #1). The §7 commitments this
chart enforces:

- §7.1 — Two surfaces (`deploy/k8s/` plain YAML + `deploy/helm/omnisight/`
  chart); chart renders to the same K8s shapes.
- §7.2 — `PodDisruptionBudget` uses `policy/v1`. Toggle via `pdb.enabled`.
- §7.3 — Probes use `httpGet` against G1 `/readyz` + `/livez`.
- §7.4 — HPA uses `autoscaling/v2` with
  `targetCPUUtilizationPercentage: 70`. Deployment RollingUpdate with
  `maxUnavailable: 0` and `maxSurge: 1`.
- §7.5 — Environment overrides split into `values-staging.yaml` /
  `values-prod.yaml`, NOT inline conditionals in `values.yaml`.
- §7.6 — Gateway-API selection is an EXPLICIT toggle
  (`ingress.gatewayApi.enabled`) — the chart never auto-detects.
- §7.7 — CI smoke against `kind` 1.29 (landed in G5 #6, row 1374, at
  `.github/workflows/k8s-helm-smoke.yml`).

## Files

| Path | Purpose |
|---|---|
| `Chart.yaml` | Chart metadata; `kubeVersion >= 1.29.0-0`. |
| `values.yaml` | Defaults — production-leaning, byte-faithful to `deploy/k8s/*.yaml`. |
| `values-staging.yaml` | Staging overrides (smaller resources, max=4). |
| `values-prod.yaml` | Prod overrides (larger resources, max=10, topology-spread). |
| `.helmignore` | Standard packaging excludes. |
| `templates/_helpers.tpl` | `omnisight.fullname` / `omnisight.labels` helpers. |
| `templates/namespace.yaml` | Namespace doc; gated by `createNamespace`. |
| `templates/deployment.yaml` | Deployment with probes + downward-API env. |
| `templates/service.yaml` | ClusterIP Service; targetPort uses named `http`. |
| `templates/ingress.yaml` | Ingress OR Gateway-API HTTPRoute (explicit toggle). |
| `templates/pdb.yaml` | `policy/v1` PDB; gated by `pdb.enabled`. |
| `templates/hpa.yaml` | `autoscaling/v2` HPA; gated by `autoscaling.enabled`. |
| `templates/NOTES.txt` | `helm install` output summary. |

## Install

Staging:

```bash
helm upgrade --install omnisight deploy/helm/omnisight \
  -f deploy/helm/omnisight/values.yaml \
  -f deploy/helm/omnisight/values-staging.yaml \
  --set image.repository=ghcr.io/<your-namespace>/omnisight-backend \
  --set image.tag=<staging-sha>
```

Production:

```bash
helm upgrade --install omnisight deploy/helm/omnisight \
  -f deploy/helm/omnisight/values.yaml \
  -f deploy/helm/omnisight/values-prod.yaml \
  --set image.repository=ghcr.io/<your-namespace>/omnisight-backend \
  --set image.tag=<release-tag>
```

The default `image.repository=ghcr.io/your-org/omnisight-backend` is an
obvious placeholder — operators MUST override.

## Diff against the plain manifests

The G5 #6 CI smoke (`.github/workflows/k8s-helm-smoke.yml`, row 1374)
runs `helm template … | kubectl apply --dry-run=server -f -` against
both default and overlay values on every PR that touches the chart or
the plain manifests. To reproduce locally — or to surface a textual
diff before pushing — run:

```bash
helm template omnisight deploy/helm/omnisight \
  --namespace omnisight \
  --set image.repository=ghcr.io/your-org/omnisight-backend \
  --set image.tag=latest \
  | kubectl diff -f - -f deploy/k8s/ || true
```

## Toggles

The four toggles operators reach for most:

- `pdb.enabled` — defaults `true`. Flip to `false` only on
  single-replica dev clusters where voluntary disruption budgets make
  no sense.
- `autoscaling.enabled` — defaults `true`. When disabled the chart
  emits no HPA and Deployment.replicas stays at `replicaCount`.
- `ingress.enabled` — defaults `true`. When disabled the chart emits
  neither Ingress nor HTTPRoute (operators may use a separate manager).
- `ingress.gatewayApi.enabled` — defaults `false` (renders
  `networking.k8s.io/v1` Ingress). Flip to `true` to render
  `gateway.networking.k8s.io/v1` HTTPRoute instead. Charter §7.6
  explicitly forbids silent auto-detection.

## Scope — what this chart does NOT include

- `deploy/nomad/` or `deploy/swarm/` — out of scope per charter §7.8.
- Optional sub-charts (Postgres-HA, Prometheus Operator) — those ship
  as their own deploy bundles under `deploy/postgres-ha/` and
  `deploy/prometheus/` and stay first-class. The OmniSight chart is
  application-scoped, not platform-scoped.
