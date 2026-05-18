---
id: L-OP-1493
ticket: OP-1493
title: A bare `docker run` is an island — graceful adapters turn it into a 4-day silent failure
date: 2026-05-18
tags: [devops, docker, compose, deployment, silent-failure, anti-pattern]
---

# A bare `docker run` is an island — graceful adapters turn it into a 4-day silent failure

**Situation**: On 2026-05-14 an operator stood up Neo4j as a quick
unblock with `docker run -d --name omnisight-neo4j …` (no compose,
no `--network omnisight-productizer_default`, no labels). The container
ran fine in isolation on Docker's default `bridge` network (172.17.0.2)
and the operator moved on. Four days later, during the 2026-05-18
evening Graphiti deploy (Task B in
`docs/operations/2026-05-18-evening-operator-tasks.md`), the operator
discovered that `MATCH (n) RETURN count(n)` returned `0` — the Cognee
"structural axis" feature shipped under OP-1456 had never written a
node end-to-end. Root cause: backend services live on
`omnisight-productizer_default`, and Docker DNS is per-network, so
`bolt://neo4j:7687` did not resolve from the backend's network
namespace. Every Cognee call raised `CogneeNeo4jUnavailable`, the
adapter degraded to its B8/B10 baselines exactly as designed
(`docs/operations/cognee-runbook.md` §5), and no alert fired because
the degraded path is a *contract*, not an exception. Standalone data
also sat at the divergent `${HOME}/.local/share/omnisight/neo4j/...`
path instead of the FHS-aligned compose mount at
`/var/lib/omnisight/neo4j/...` — even if the network had matched, the
state was on the wrong volume.

**Why it kept hiding**: every individual signal told the truth.
`docker ps` showed `omnisight-neo4j` up. The healthcheck inside the
standalone container passed. `cypher-shell` from the host worked. The
backend logs even mentioned cognee — just the lines that say "falling
back to B8". The defect was *between* signals: the network membership
mismatch lived in the gap between "container healthy" and "callable
from the service that needs it", and the graceful-fallback contract
turned the missing dependency into a quiet performance regression
instead of a loud failure. It is anti-pattern #13
(shipped-but-not-deployed, [`L-OP-976`](L-OP-976-shipped-not-deployed.md))
re-instantiated one layer down: the install recipe *was* run, the
container *was* live — what failed was the *integration shape*. The
4-AC discipline catches "merged but never started"; it does not catch
"started but on a network nobody else uses".

**Fix**: OP-1493 retired the standalone container and switched to the
compose-managed `neo4j` service at `docker-compose.yml:115-145`
(profile `cognee`), aligning the data root to
`/var/lib/omnisight/neo4j/...`. The path migration runbook
(`docs/operations/neo4j-path-migration-2026-05-18.md`) records the
per-host cutover steps, the bootstrap requirement for new hosts
(`sudo mkdir -p && sudo chown user:user` *before* first
`docker compose --profile cognee up`), and the validation matrix that
mirrors the OP-1493 4-AC. The compose service was already correctly
defined — the gap was entirely runtime/operator, not code. No
docker-compose.yml or Cognee adapter change was needed.

**Verification**: AC #2 (Deploy) — `docker ps --filter "name=neo4j"`
shows `omnisight-productizer-neo4j-1` on
`omnisight-productizer_default` and the bare `omnisight-neo4j` row is
gone. AC #3 (Integration) — `cypher-shell ... MATCH (n) RETURN
count(n)` ≥ 1 after the first Cognee write event, plus
`curl http://neo4j:7474` reachable from
`omnisight-productizer-backend-a-1`. AC #4 (Exercised) — 24 h of
stable compose-managed Neo4j with at least one successful Cognee write,
and the archival tar of the standalone snapshot at
`~/pg-backups/standalone-neo4j-snapshot-2026-05-18.tar.gz`. Operator
evidence lives in the OP-1493 ticket comments, not in this file.

**Generalisation**: **Any service brought up outside compose is invisible
to compose's service graph and to the operator's mental model of "the
stack".** Bare `docker run` for a dependency that other compose
services need to reach is a silent landmine even when each individual
piece is healthy — the failure mode lives in the gap between the
container's own readiness and its *integration* readiness. Three forcing
functions help:

1. **Compose-or-nothing for shared dependencies**: if a service has any
   in-stack consumer (backend, Graphiti, worker, …), it must be defined
   in `docker-compose.yml` under a profile. A `docker run` is acceptable
   only for an ephemeral one-shot the operator runs and stops in the
   same session — *never* for a persistent dependency. This applies
   equally to Postgres, Redis, vector stores, and any future memory
   tier.
2. **Tripwire the graceful-fallback contract**: a fallback that is
   contractually silent (Cognee → B8/B10, memory tool → in-memory,
   queues → local) needs a periodic *liveness* signal that the
   *primary* path is being used — a Prometheus counter like
   `cognee_primary_path_total` whose ratio to
   `cognee_fallback_total` is alerted when it inverts. Without that
   counter, "the system works on the fallback" looks identical to "the
   system works on the primary" until a node-count or latency probe
   says otherwise.
3. **FHS-align state paths on first boot**: divergent state roots
   (`~/.local/share/…` vs `/var/lib/…`) compound the cost of a
   cutover — the migration becomes "move the data *and* re-stand-up the
   container" instead of just the latter. Pick the FHS-aligned path
   the first time, document the bootstrap requirement, and the cutover
   becomes a network swap, not a volume copy.

Adjacent lessons: [`L-OP-976`](L-OP-976-shipped-not-deployed.md)
(shipped-but-not-deployed, the lifecycle-wide parent of this
integration-shape failure mode) and `L-OP-1024`
(`L-OP-1024-lesson-surface-meta-mechanism.md` — write-down-and-forget
is the same defect class as a quiet fallback, applied to the lessons
corpus itself).
