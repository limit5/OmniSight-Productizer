# Neo4j path migration — standalone → compose-managed (OP-1493)

**Status**: Operator migration runbook. Diagnosed 2026-05-18 evening during
Task B (Graphiti deploy) in
[`docs/operations/2026-05-18-evening-operator-tasks.md`](2026-05-18-evening-operator-tasks.md);
formally tracked under OP-1493.

**Audience**: prod-host operator (the WSL/Linux machine running the
OmniSight-Productizer compose stack).

---

## 1. What changed

| Property | Old (standalone, 4 days) | New (canonical, FHS-aligned) |
|---|---|---|
| Container | `omnisight-neo4j` (bare `docker run`) | `omnisight-productizer-neo4j-1` (compose) |
| Compose definition | none | [`docker-compose.yml:115-145`](../../docker-compose.yml), profile `cognee` |
| Network | default `bridge` (172.17.0.2) — alone | `omnisight-productizer_default` (shared with backend + Graphiti) |
| Data root | `${HOME}/.local/share/omnisight/neo4j/...` | `/var/lib/omnisight/neo4j/...` |
| Lifecycle | manual `docker run` / `docker stop` | `docker compose --profile cognee up -d neo4j` |
| Reachable from backend? | **No** (network mismatch) | **Yes** (same compose network) |

The compose-managed paths line up with the [`cognee-runbook`](cognee-runbook.md) §2 statement that "Persistent storage is pinned to `/var/lib/omnisight/neo4j/`" and with the Filesystem Hierarchy Standard (FHS) convention for variable application state owned by a daemon.

## 2. Why this matters (root cause of the 4-day silent failure)

The `omnisight-neo4j` container was a 2026-05-14 quick-deploy: a bare `docker run` to unblock a local experiment, never converted to a compose service. It lived on Docker's default `bridge` network, isolated from `omnisight-productizer_default` where every other service in the stack runs. The backend's Cognee adapter (`backend/agents/cognee_integration.py`) resolves the Bolt URI through Docker DNS as `bolt://neo4j:7687`, but DNS is per-network — from the backend's network namespace there was no `neo4j` host. Each Cognee lookup raised `CogneeNeo4jUnavailable`, the adapter degraded to B8/B10 baselines (the explicit contract in [`cognee-runbook`](cognee-runbook.md) §5), and the operator never saw a hard failure. Result: `MATCH (n) RETURN count(n)` returned `0` for four days while the "structural axis" feature ([memory `project_v0_5_0_hotfix_marathon_2026_05_18`](../../) — OP-1456) appeared shipped but was never exercised end-to-end. This is anti-pattern #13 (shipped-but-not-deployed, [`L-OP-976`](../sop/lessons/L-OP-976-shipped-not-deployed.md)) at the runtime-config layer rather than the install-recipe layer.

## 3. Canonical path — the rule going forward

**Canonical path**: `/var/lib/omnisight/neo4j/{data,logs,import,plugins}`

  * Owned by `user:user` (the unprivileged service account that runs the compose stack).
  * Mode `0755` on the parent; subdirectories created by the operator (no sudo needed once the parent is user-owned).
  * Referenced verbatim in `docker-compose.yml:127-131` — operators must **not** edit the compose mounts; align the host instead.

**Deprecated path**: `${HOME}/.local/share/omnisight/neo4j/...`

  * Was the standalone container's data root. As of 2026-05-18 evening this directory is **abandoned** — archive and remove (see §6).
  * Future operators MUST NOT recreate the `~/.local/share/omnisight/neo4j/` layout. If you see it on a host, that host is mid-migration or has a stale snapshot; check the JIRA ticket for OP-1493 status before acting.

The `~/.local/share/omnisight/backups/` *backup* path (`docs/runbook/post-deploy-recovery.md:573`) is unrelated and remains valid — only the Neo4j data root moved.

## 4. Host bootstrap (new prod hosts)

Before the first `docker compose --profile cognee up`, the prod-host operator MUST create the canonical parent with the right ownership. The compose service runs as the Neo4j image's default uid and writes to the bind-mounted host paths; the parent must already exist and be writable by `user` for the subdirectory creation in §5 to succeed without sudo.

```bash
# One-time, run as a user with sudo (the only sudo step in this migration):
sudo mkdir -p /var/lib/omnisight/neo4j
sudo chown user:user /var/lib/omnisight/neo4j
sudo chmod 0755 /var/lib/omnisight/neo4j

# Verify
stat -c '%U:%G %a %n' /var/lib/omnisight/neo4j
# Expected: user:user 755 /var/lib/omnisight/neo4j
```

This step is a one-shot per host. On the 2026-05-18 prod host the operator pre-created the parent before OP-1493 filed — re-running is idempotent.

## 5. Migration procedure (existing prod host, 2026-05-18)

Run from `/home/user/work/sora/OmniSight-Productizer` as the unprivileged `user` account. Each step prints a verification command; do not proceed past a step whose verification fails.

### 5.1 Create the four data subdirectories

```bash
# Parent must already be user-owned (§4). No sudo needed here.
for sub in data logs import plugins; do
  mkdir -p /var/lib/omnisight/neo4j/$sub
done

# Verify
ls -ld /var/lib/omnisight/neo4j/{data,logs,import,plugins}
# Expected: four lines, all owned user:user, mode 0755
```

### 5.2 Stop and remove the standalone container

```bash
# Confirm it is the only "omnisight-neo4j" name in use (vs. compose-managed `omnisight-productizer-neo4j-1`)
docker ps -a --filter "name=^omnisight-neo4j$" --format "{{.Names}}\t{{.Networks}}\t{{.Status}}"
# Expected: one row, network=bridge

docker stop omnisight-neo4j
docker rm omnisight-neo4j

# Verify
docker ps -a --filter "name=^omnisight-neo4j$" --format "{{.Names}}"
# Expected: empty
```

### 5.3 Bring up compose-managed Neo4j

```bash
cd /home/user/work/sora/OmniSight-Productizer

# NEO4J_PASSWORD must be set in .env (per docker-compose.yml:122 — refuses the default literal "neo4j")
grep -q '^NEO4J_PASSWORD=' .env || { echo "NEO4J_PASSWORD missing from .env"; exit 1; }

docker compose --profile cognee up -d neo4j

# Wait for the healthcheck (start_period=30s, then up to 5×30s retries)
for i in 1 2 3 4 5 6; do
  sleep 30
  state=$(docker inspect --format='{{.State.Health.Status}}' omnisight-productizer-neo4j-1 2>/dev/null)
  echo "$(date +%H:%M:%S) health=$state"
  [[ "$state" == "healthy" ]] && break
done

# Verify network + path
docker ps --filter "name=omnisight-productizer-neo4j-1" \
  --format "{{.Names}}\t{{.Networks}}\t{{.Status}}"
# Expected: omnisight-productizer-neo4j-1 ... omnisight-productizer_default ... Up (healthy)

ls /var/lib/omnisight/neo4j/data/ | head
# Expected: neo4j metadata files appear after first healthy start (databases/, server_id, store_lock, ...)
```

### 5.4 Verify backend can reach Neo4j on the shared network

```bash
# DNS + HTTP browser (browser is optional in prod but cheap to sanity-check)
docker exec omnisight-productizer-backend-a-1 sh -c 'curl -sS http://neo4j:7474 | head -5'
# Expected: HTML / JSON response with a "neo4j_version" key

# Cognee write path — pick the runtime verifier that fits your shell
docker exec omnisight-productizer-backend-a-1 \
  python -m scripts.cognee_healthcheck
# Expected: healthcheck OK, exits 0 (per cognee-runbook §3.1 step 5)

# Spot-check the node count after a Cognee write event
docker exec omnisight-productizer-neo4j-1 \
  cypher-shell -a bolt://localhost:7687 -u neo4j -p "$NEO4J_PASSWORD" \
  'MATCH (n) RETURN count(n) AS n'
# Expected: n ≥ 1 after the first Cognee ingest cycle
```

### 5.5 Bring up Graphiti (previously blocked by the network mismatch)

```bash
# Pre-pull (image namespace already fixed by the 2026-05-18 evening Graphiti commit)
docker pull zepai/graphiti:latest

docker compose --profile graphiti up -d --no-deps graphiti

sleep 30
docker ps --filter "name=graphiti" --format "{{.Names}}\t{{.Status}}"
# Expected: omnisight-productizer-graphiti-1 ... Up (healthy)

# Inside-container health probe (per docker-compose.yml:165-169)
docker exec omnisight-productizer-graphiti-1 \
  python -c "import urllib.request; r=urllib.request.urlopen('http://localhost:8000/api/v1/health'); print(r.status)"
# Expected: 200
```

### 5.6 Archive the standalone data root

Even though the standalone instance held zero nodes, take a forensic snapshot before removing — the audit trail (and a sliver of "what if we missed something") justifies a few MB of compressed state.

```bash
mkdir -p ~/pg-backups
tar czf ~/pg-backups/standalone-neo4j-snapshot-2026-05-18.tar.gz \
  -C ~ .local/share/omnisight/neo4j

# Verify
ls -lh ~/pg-backups/standalone-neo4j-snapshot-2026-05-18.tar.gz
# Expected: a non-empty .tar.gz file with today's date

# Only after the compose instance has been Healthy for at least one full
# Cognee write cycle (cf. AC #4 — 24 h exercised), remove the old root:
rm -rf ~/.local/share/omnisight/neo4j/
```

## 6. Validation matrix (mirrors the OP-1493 4-AC)

| AC | Check | Pass criterion |
|---|---|---|
| Code | `git log --oneline -- docs/operations/neo4j-path-migration-2026-05-18.md` | At least one commit referencing `[OP-1493]` |
| Deploy | `docker ps --filter "name=neo4j" --format "{{.Names}} {{.Networks}}"` | Shows `omnisight-productizer-neo4j-1` on `omnisight-productizer_default`; no row for `omnisight-neo4j` |
| Deploy | `ls /var/lib/omnisight/neo4j/data/` | Neo4j metadata files present after first healthy start |
| Integration | `docker exec omnisight-productizer-backend-a-1 sh -c 'curl -s http://neo4j:7474'` | Returns Neo4j HTTP UI HTML |
| Integration | `cypher-shell ... 'MATCH (n) RETURN count(n)'` after one Cognee write | Result ≥ 1 |
| Integration | `docker ps --filter "name=graphiti"` | Healthy |
| Exercised | 24 h elapsed + ≥1 successful Cognee write event + archive tar present | All three observed |

## 7. Rollback (compose Neo4j → standalone)

Not recommended — the standalone path is divergent from the compose contract and re-introduces the network-isolation defect. If a compose-managed bring-up fails hard (e.g. healthcheck never settles), prefer:

1. `docker compose --profile cognee stop neo4j` (the backend Cognee adapter degrades to B8/B10 per [`cognee-runbook`](cognee-runbook.md) §6 "Pause Cognee at runtime").
2. Investigate the compose-side root cause (password, port collision, mem_limit pressure) before reverting.
3. Only if a hard rollback is unavoidable: extract the archived `standalone-neo4j-snapshot-2026-05-18.tar.gz` back to `~/.local/share/omnisight/neo4j/` and re-run the original `docker run` recipe — but open a follow-up ticket the same day to redo the migration.

## 8. References

* `docker-compose.yml:115-145` — canonical `neo4j` service definition (profile `cognee`).
* [`docs/operations/cognee-runbook.md`](cognee-runbook.md) §2-3 — Neo4j storage pin + compose bring-up sequence.
* [`docs/operations/graphiti-mcp-runbook.md`](graphiti-mcp-runbook.md) §2 — Graphiti bring-up that depended on this migration.
* [`docs/operations/2026-05-18-evening-operator-tasks.md`](2026-05-18-evening-operator-tasks.md) Task B — origin diagnosis.
* [`docs/sop/lessons/L-OP-976-shipped-not-deployed.md`](../sop/lessons/L-OP-976-shipped-not-deployed.md) — anti-pattern #13 (the lesson this incident re-instantiates).
* [`docs/sop/lessons/L-OP-1493-bare-docker-run-is-an-island.md`](../sop/lessons/L-OP-1493-bare-docker-run-is-an-island.md) — generalisable lesson from this ticket.
