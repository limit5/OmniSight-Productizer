# Multi-node orchestration selection — K8s vs Nomad vs docker swarm (G5 / HA-05)

> G5 #1 (TODO row 1369). Opening deliverable of the G5 HA-05 bucket.
> Decision scope: which container orchestrator OmniSight should target
> for the G5 production manifests (rows 1370–1373) and the Helm / job
> bundle (row 1374).
>
> Siblings (not yet delivered):
> - G5 #2 row 1370 — Deployment (replicas=2, maxUnavailable=0) / Service / Ingress / HPA (CPU 70%)
> - G5 #3 row 1371 — PodDisruptionBudget (minAvailable=1)
> - G5 #4 row 1372 — readiness / liveness probe wiring to the G1 endpoint
> - G5 #5 row 1373 — Helm chart `deploy/helm/omnisight/` (values.yaml staging / prod)
> - G5 #6 row 1374 — delivery bundle `deploy/k8s/` (or `deploy/nomad/`) + this decision doc

This document is the **charter** for G5. Rows 1370–1374 land artefacts
in the directory chosen below; changing the decision later means rewriting
those artefacts, so the rationale is captured here exhaustively — not
just the outcome.

---

## 1. TL;DR — Decision

| Field | Value |
|---|---|
| Chosen orchestrator | **Kubernetes** (vanilla — no distro lock-in) |
| Manifest home | `deploy/k8s/` (plain YAML) + `deploy/helm/omnisight/` (chart) |
| Minimum target version | Kubernetes 1.29 (GA of sidecar-container lifecycle, required by G5 #4 readiness sequencing) |
| Node footprint | 3-node control plane (managed) + ≥ 2 worker nodes |
| Runtime on each node | containerd (Docker-shim is dead since 1.24) |
| Runner of record for staging | Any CNCF-certified distro; repo assumes EKS / GKE / AKS / kind parity |
| Alternatives considered | HashiCorp Nomad (1.7+), Docker Swarm (classic mode) |
| Decision reversibility | **Medium.** Helm values are generic enough to port to Nomad `template` blocks; Caddy + Postgres-HA already live under `deploy/` as first-class manifests so the container contract is orchestrator-agnostic. |
| Primary trade-off accepted | K8s operational-burden tax (control plane, RBAC, CNI, storage classes) in exchange for ecosystem depth — HPA, PDB, probes, Ingress controller choice, Helm catalog, operator pattern |

**Why not the lighter options.**
- Docker Swarm — classic mode has been in **maintenance since 2020**; Mirantis Swarm Enterprise is a different product with a different support story. We need a multi-year runway.
- Nomad — the closest peer. Lower ops overhead and genuinely elegant, but **the OmniSight stack we already ship has more first-class K8s integrations** (Prometheus Operator / kube-state-metrics / Caddy Ingress / cert-manager / external-secrets), and the team's hiring funnel skews K8s.

---

## 2. Evaluation axes

The G5 TODO row literally calls out **比較運維負擔** (compare operational burden).
We widen that to the five axes below — burden is not just install effort, it
is the ongoing cost of running, observing, upgrading, and recovering.

| Axis | What we are measuring |
|---|---|
| **install-burden** | Bootstrapping a fresh 3-node cluster (control plane + workers), CNI, storage class, Ingress controller. Measured in hours of a qualified SRE. |
| **day2-burden** | Routine ops: RBAC changes, secret rotation, CNI upgrades, node draining, certificate renewal, version skew handling. Measured in hours / month. |
| **observability-burden** | How much work to expose the G7 HA signals (`omnisight_backend_instance_up`, `rolling_deploy_5xx_rate`, `replica_lag_seconds`, `readyz_latency`) and Alertmanager rules. Lower is better. |
| **upgrade-burden** | Orchestrator upgrades, breaking-change absorption (CRD migrations, API removals). Frequency and blast-radius. |
| **recovery-burden** | DR drill complexity (G6). How hard is it to rebuild the control plane + replay state from the Postgres-HA pair? |

Burden is scored `low` / `medium` / `high`. Scoring is relative
between the three candidates — a `high` on Kubernetes is still
survivable for a product that already ships a dedicated Postgres-HA
deploy bundle.

---

## 3. Option A — Kubernetes (vanilla)

**Reference version**: Kubernetes 1.29 (sidecar GA, the last 1.x API
deprecation wave for `autoscaling/v2beta*` already completed in 1.26).

### 3.1 Install-burden — **high**

A fresh 3-node control plane needs: etcd, kube-apiserver, kube-scheduler,
kube-controller-manager on each control node, then a CNI (Calico / Cilium
/ Flannel), a CSI driver for persistent volumes, and an Ingress controller
(ingress-nginx, Traefik, or a Gateway-API compatible one). Budget
**½ day** with a managed control plane (EKS / GKE / AKS), **2–3 days**
self-hosted (kubeadm / k3s / RKE2). OmniSight targets the managed path
for staging / prod and `kind` for CI smoke — see §6 for the matrix.

### 3.2 Day-2-burden — **medium-to-high**

RBAC has non-trivial complexity (Roles, ClusterRoles, ServiceAccounts,
namespace boundaries). Secret rotation needs external-secrets-operator
or sealed-secrets to avoid human error. CNI upgrades are the single
largest day-2 incident source on most on-prem clusters. The bright
side: the OmniSight workload (backend replicas + Redis + Postgres pair)
fits comfortably inside a **single namespace with two ingresses**, so
the RBAC surface stays small.

### 3.3 Observability-burden — **low**

This is the single largest reason we pick K8s. Prometheus Operator +
kube-state-metrics + node-exporter deliver every G7 HA signal out of
the box, and the existing `deploy/prometheus/orchestration_alerts.rules.yml`
is already expressed in PromQL — it ports 1:1. HPA is a first-class
object, the `CPU 70%` target the G5 #2 row calls for is a literal
`targetCPUUtilizationPercentage: 70` field.

### 3.4 Upgrade-burden — **medium**

Managed control planes handle the N-3 version skew automatically.
Breaking-change absorption is the risk: `PodDisruptionBudget` moved from
`policy/v1beta1` to `policy/v1` in 1.21, `HorizontalPodAutoscaler` moved
from `autoscaling/v2beta2` to `autoscaling/v2` in 1.26. The Helm chart
pins API versions explicitly to make these bumps a conscious edit, not
a silent deprecation.

### 3.5 Recovery-burden — **medium**

etcd backup / restore is a well-trodden recipe and most managed control
planes snapshot it for us. The stateful piece (Postgres) is already
isolated in `deploy/postgres-ha/` so a full cluster wipe + rebuild is
**manifests re-apply + restore from Postgres basebackup**. G6 DR drill
will rehearse this quarterly.

---

## 4. Option B — HashiCorp Nomad

**Reference version**: Nomad 1.7.x (service discovery + native workload
identity, both required to avoid a Consul hard dependency).

### 4.1 Install-burden — **low**

A single static binary per node. No etcd, no kube-apiserver, no CNI
plugin dance. A 3-node Nomad server + 2 clients cluster is **~2 hours**
for an SRE who has never seen Nomad before. This is its headline
advantage.

### 4.2 Day-2-burden — **low-to-medium**

ACL + namespace model is genuinely simpler than K8s RBAC. Secret
handling requires Vault (another HashiCorp service) unless you accept
flat-file secrets — OmniSight's O10 security baseline doesn't allow the
latter in prod. So "no Vault" is **not** a real option → day-2 cost
creeps up once Vault is in the picture (Vault seal / unseal, root-token
rotation, Vault backup).

### 4.3 Observability-burden — **medium**

Nomad exports native metrics to Prometheus, but there is no equivalent
to kube-state-metrics — we'd re-implement `omnisight_backend_instance_up`
by scraping Nomad's `/v1/allocations` directly. The PromQL alerting rules
still work, but the labels differ (`alloc_id` vs `pod`). Every G7
dashboard + alert needs a Nomad-specific variant. That's real work.

### 4.4 Upgrade-burden — **low**

In-place rolling upgrade of the Nomad binary is a well-documented recipe
and the API surface has been extremely stable. HashiCorp's LTS story is
better than K8s for small ops teams.

### 4.5 Recovery-burden — **low**

Raft snapshot of the Nomad servers + the same Postgres restore path.
The Nomad server ships `nomad operator raft snapshot save`; replay is
a documented command, not an etcd byte-surgery exercise.

### 4.6 Why not Nomad anyway

Three reasons, in order of weight:

1. **Ecosystem gravity**: every G7 observability building block we
   plan to consume (Prometheus Operator, Grafana's k8s data sources,
   cert-manager ACME, Vault-on-K8s-Operator even if we keep Vault itself)
   is K8s-native first. Nomad variants exist but are always one version
   behind and community-maintained, not vendor-maintained.
2. **Hiring funnel**: the operator pool we recruit from skews
   heavily K8s. Not a technical argument, but operational burden
   includes training time for new oncall.
3. **Vendor concentration risk**: adopting Nomad + Vault + Consul is a
   full HashiCorp bet. The IBM acquisition of HashiCorp (announced 2024)
   doesn't immediately change anything but does raise the concentration
   risk score on a single vendor's roadmap.

---

## 5. Option C — Docker Swarm (classic)

**Reference version**: Swarm-mode built into Docker Engine (NOT
Mirantis Swarm Enterprise — that is a different product).

### 5.1 Install-burden — **very-low**

`docker swarm init` on one node, `docker swarm join` on the others.
The lowest install cost by a wide margin. If simplicity were our only
axis, Swarm would win.

### 5.2 Day-2-burden — **low short-term, high long-term**

Short-term, Swarm day-2 is `docker service update --image=…` and a
handful of `docker stack deploy` recipes — deeply familiar to anyone
who uses `docker-compose.prod.yml` (i.e. the current OmniSight baseline).

Long-term, the day-2 burden grows because **classic Swarm has been in
maintenance mode since 2020**. Bug fixes land; new features do not.
This means every new requirement (zero-downtime secret rotation,
multi-cluster federation, Gateway-API-style routing) requires a
hand-rolled workaround.

### 5.3 Observability-burden — **high**

No equivalent to `kube-state-metrics`. Prometheus service discovery
against Swarm is possible via `dockerswarm_sd_configs` but missing the
fine-grained metadata K8s / Nomad provide. Every G7 signal needs a
bespoke exporter.

### 5.4 Upgrade-burden — **low short-term, unknown long-term**

Docker Engine upgrades are the orchestrator upgrades. No drift risk.
The unknown is what happens when Swarm eventually reaches end-of-life —
the migration path is effectively "rebuild on K8s or Nomad", and we'd
be doing that migration under duress.

### 5.5 Recovery-burden — **medium-to-high**

Swarm's state is in a raft store on the managers, but the tooling for
snapshot / restore is thinner than either K8s etcd backup or Nomad
raft snapshot. In practice, DR means re-initialising the swarm and
re-applying stack files — which is fine, but you want that rehearsed
(see G6).

### 5.6 Why Swarm is a **no-go**

The single disqualifier is **Docker Inc.'s product direction**. Classic
swarm-mode has had no major-feature work in 5 years; Mirantis owns the
enterprise fork on a different roadmap. Shipping a 2026 greenfield
deployment on a maintenance-mode orchestrator is future-debt we do not
want to take on — especially when the migration tax to K8s later is
larger than the install tax of K8s today.

---

## 6. Scoring summary

| Axis | Kubernetes | Nomad | Docker Swarm |
|---|---|---|---|
| install-burden | high | low | very-low |
| day2-burden | medium-to-high | low-to-medium (+Vault) | low short-term, high long-term |
| observability-burden | **low** | medium | high |
| upgrade-burden | medium | low | unknown long-term |
| recovery-burden | medium | low | medium-to-high |
| **ecosystem depth** | **deep** (ingress, operators, Prometheus stack, cert-manager, externalsecrets) | medium | shallow + shrinking |
| **roadmap risk** | low (CNCF) | medium (single vendor, IBM concentration) | **high (maintenance-mode)** |

Kubernetes wins on ecosystem depth + roadmap risk, loses on install
burden, and ties on recovery burden.

**Decision**: accept the install-burden tax. It is a one-time cost;
ecosystem depth compounds over the product's life.

---

## 7. Consequences

This decision locks the following commitments for rows 1370–1374:

1. Manifests land under `deploy/k8s/` (plain YAML, for `kubectl apply -f`
   workflows) AND `deploy/helm/omnisight/` (chart, for release-managed
   workflows). Two surfaces, one truth — the Helm templates render to
   the plain YAML for diffability.
2. G5 #3 PodDisruptionBudget uses `policy/v1` API version.
3. G5 #4 probes wire to the G1 `/readyz` and `/livez` endpoints via
   `httpGet` probes (not exec, not tcpSocket — the G1 endpoint is HTTP).
4. G5 #2 HPA uses `autoscaling/v2` API version with
   `targetCPUUtilizationPercentage: 70`. The Deployment's rollout
   strategy is `RollingUpdate` with `maxUnavailable: 0` and
   `maxSurge: 1` — the only configuration that delivers zero-downtime
   rollouts on a 2-replica backend and matches the G2 rolling-restart
   semantics we already ship.
5. The Helm chart `values.yaml` splits staging / prod overrides into
   `values-staging.yaml` / `values-prod.yaml` rather than one mega-file
   with environment conditionals (keeps rendered diffs readable).
6. Ingress picks the Gateway-API Ingress class when the cluster has
   one; otherwise falls back to `ingressClassName: nginx` — this is an
   explicit Helm toggle (`ingress.gatewayApi.enabled`) rather than a
   silent auto-detect.
7. CI smoke uses `kind` (Kubernetes IN Docker) for parity: every G5
   manifest must render + apply cleanly against a vanilla `kind` 1.29
   cluster — this pins the minimum version claim.
8. Nomad and Swarm bundles are **explicitly out-of-scope** for G5.
   If requirements change (e.g. a customer mandates non-K8s), we open a
   fresh G-bucket, not silently add a second orchestrator to G5.

---

## 8. Open questions (not blockers for G5 #1)

These questions do **not** block landing this decision doc, but they
are flagged so the subsequent G5 rows know what still needs a call:

- **Ingress controller choice**: ingress-nginx vs Traefik vs Gateway-API
  (Envoy Gateway / Contour). Default in G5 #2 manifests: ingress-nginx
  (most broadly deployed). The Helm chart will make this a toggle.
- **PersistentVolumeClaim provisioner**: depends on the target cluster's
  StorageClass. Helm chart takes `persistence.storageClassName` as a
  value with no default.
- **Which `replica=2, maxUnavailable=0` strategy** — `Recreate` or
  `RollingUpdate`? G5 #2 row says `maxUnavailable=0`, which implies
  RollingUpdate with `maxUnavailable: 0` and `maxSurge: 1` (one extra
  pod comes up before any existing pod terminates). This is the only
  configuration that delivers zero-downtime rollouts on a 2-replica
  backend and matches the G2 rolling-restart semantics we already ship.
- **Multi-tenant isolation**: I-series multi-tenancy (RLS,
  statement_timeout, role-scoped grants) lands database-side.
  Orchestrator-side, each tenant is a namespace OR a Helm release — TBD
  at the I-series kickoff. G5 manifests deliberately do not
  pre-decide this to avoid rework.

---

## 9. Cross-references

- Upstream context: `TODO.md` row 1369 (this G5 #1 item), row 1370–1374
  (G5 #2–#6 follow-on artefacts).
- G1 readiness endpoint contract: `backend/routers/health.py` + the G1
  rows in TODO.md — probes in G5 #4 wire to exactly those paths.
- G2 rolling-restart semantics: `docs/ops/blue_green_runbook.md` §1
  "Why blue-green" explains the rolling-restart baseline on top of which
  blue-green and (now) K8s Deployments sit.
- G4 Postgres-HA deploy bundle: `deploy/postgres-ha/` is orchestrator-
  agnostic (plain docker-compose). In K8s, the Postgres pair can run
  either as a StatefulSet pair OR outside-of-cluster (RDS-like managed
  service). Default: managed (simpler DR story in G6).
- G6 DR runbook (future): will absorb the K8s-specific etcd backup
  step in addition to the Postgres basebackup flow.
- G7 HA observability (future): the PromQL alert rules in
  `deploy/prometheus/orchestration_alerts.rules.yml` port 1:1 to K8s;
  the service-discovery labels are the only diff.
- O8 distributed orchestration: `docs/ops/orchestration_migration.md`
  — the OmniSight *application* has a monolith↔distributed flip
  independent of the *cluster* orchestrator. G5 is about the cluster;
  O8 is about the application. Don't conflate them.
