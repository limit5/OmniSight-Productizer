# OmniSight reverse proxy — upstream health check + automatic eject

> G2 / HA-02 deliverable row 1348. This README is the **operator-facing
> contract** for how Caddy detects an unhealthy backend replica, how
> long it takes to eject one, and how the eject budget lines up with
> `scripts/deploy.sh` rolling restart and `backend/lifecycle.py`
> SIGTERM drain.
>
> Scope: `deploy/reverse-proxy/Caddyfile`. Everything below is derived
> from that single file — if it says one thing and the Caddyfile says
> another, the Caddyfile wins and this doc is a bug.

---

## 1. Why this document exists

The Caddyfile shipped in G2 #1 (`deploy/reverse-proxy/Caddyfile`)
already configures both an active health probe and a passive eject
policy. G2 #4 (TODO row 1348) is the **contract + documentation + test
coverage** for that configuration — without this README an operator
who needs to reason about "how long after a replica dies does traffic
stop hitting it?" has to reverse-engineer the answer from Caddyfile
comments. With this README they have one page to read.

If you are triaging a production incident and came here from the
runbook index: jump to **§5 Timing budget** and **§7 Triage**.

---

## 2. The two eject mechanisms (and why we have both)

Caddy gives us two independent ways to pull an upstream from the load
balancer pool. We run both simultaneously because they cover
non-overlapping failure modes.

| Mechanism | Directive family | What it watches | Eject when |
|-----------|------------------|-----------------|------------|
| **Active probe** | `health_uri`, `health_interval`, `health_fails`, … | Proactive `GET /readyz` every N seconds | `health_fails` consecutive probe failures |
| **Passive eject** | `fail_duration`, `max_fails`, `unhealthy_*` | Real client traffic in-flight | `max_fails` bad responses within `fail_duration`, OR one response slower than `unhealthy_latency`, OR `unhealthy_request_count` 5xx bursts |

Active catches "replica is gone / migrating / draining" — the
backend's `/readyz` endpoint returns 503 during SIGTERM drain
(`backend/lifecycle.py`) and during DB startup migration, so a
draining replica self-announces before client traffic ever hits it.

Passive catches "replica answers the probe fine but production traffic
is broken" — e.g. a DB blip that only affects write paths,
a replica that's answering /readyz from cache while its worker pool
is saturated, or a corrupt container that 200s /readyz but 5xxs every
real request.

**The combined guarantee**: an upstream is ejected within **at most
6 seconds** of becoming unhealthy (3 failed 2 s probes) and, if it's
serving real traffic while broken, even sooner (passive eject fires
as soon as `max_fails` + `fail_duration` threshold crosses).

---

## 3. Active probe — how it works

```
health_uri /readyz
health_port 0              # probe the same port the LB is proxying to
health_method GET
health_interval 2s         # one probe every 2 s per replica
health_timeout 2s          # probe that takes >2 s counts as a failure
health_status 2xx          # only 2xx counts as success
health_passes 1            # 1 good probe re-admits a drained replica
health_fails 3             # 3 bad probes eject it
health_follow_redirects false  # /readyz must answer 200/503 directly
```

**Timing diagram** (steady state):

```
t=0s    Replica A starts answering 503 (drain / crash)
t=2s    Caddy probe #1 → 503     (fails[A]=1, still in pool)
t=4s    Caddy probe #2 → 503     (fails[A]=2, still in pool)
t=6s    Caddy probe #3 → 503     (fails[A]=3, A ejected)
t=6s+   All new traffic → backend-b only
...
t=N     Replica A recovers and starts answering 200
t=N+2s  Probe → 200               (passes[A]=1, A re-admitted)
```

`health_passes 1` is deliberately aggressive — during a rolling
restart we WANT the replica back in rotation the moment it's healthy,
because the other replica is about to be drained next.

**Why `health_follow_redirects false`**: `/readyz` is contractually a
flat 200 or 503 endpoint (`backend/api/v1/health.py`). Any 3xx
response is a configuration bug (e.g. accidental auth middleware
injection) and should eject the replica rather than silently mask the
problem by following the redirect.

---

## 4. Passive eject — how it works ("fail_timeout" equivalent)

Caddy v2's `fail_duration` is the direct analogue of nginx's
`fail_timeout`: it's BOTH the observation window AND the eject
duration. If `max_fails` 5xx responses arrive within `fail_duration`
seconds, the upstream is ejected — for another `fail_duration`
seconds.

```
fail_duration 30s              # nginx-style fail_timeout
max_fails 3                    # 5xx count threshold
unhealthy_request_count 50     # burst threshold (see below)
unhealthy_status 5xx           # only 5xx counts
unhealthy_latency 10s          # single >10 s response also ejects
```

### Three independent trip wires

Any ONE of these will eject the replica:

1. **`max_fails` within `fail_duration`** — classic. 3 × 5xx within
   30 s → eject for 30 s.
2. **`unhealthy_latency`** — one response taking longer than 10 s is
   a signal the replica is sick even if it eventually returns 200.
   Ejected immediately.
3. **`unhealthy_request_count`** — if 50 requests are in flight
   against this replica while a partial outage is happening, eject
   regardless of the per-request status. Protects against the
   thundering-herd pattern where one slow replica collects
   connections.

### Why not just rely on the active probe?

The active probe is a synthetic request. It asks `/readyz`, which
checks DB connectivity and migration state — but it can't check "is
the tenant-rate-limiter queue saturated for user 42's writes?".
Passive eject observes real traffic, so it catches failure modes
that are invisible to the probe.

---

## 5. Timing budget — how this aligns with rolling restart

The health/eject config is tuned to the constraint that
`scripts/deploy.sh rolling` must be able to drain a replica and bring
it back without ever leaving zero healthy upstreams.

| Step | Driver | Budget | Source |
|------|--------|--------|--------|
| SIGTERM sent → `/readyz` returns 503 | `backend/lifecycle.py` | < 1 s | lifecycle.py is_ready flag |
| 503 observed → replica ejected from pool | Caddy active probe | ≤ 6 s | `health_interval 2s × health_fails 3` |
| Replica container stopped | `docker compose stop --timeout` | ≤ 35 s | `OMNISIGHT_ROLL_DRAIN_TIMEOUT` (default 35) |
| Replica container recreated | `docker compose up -d --force-recreate` | ~5 s | image pull + start |
| New container → `/readyz` 200 → re-admitted | `OMNISIGHT_ROLL_READY_TIMEOUT` | ≤ 120 s | deploy.sh poll |
| Re-admit into pool | Caddy active probe | ≤ 2 s | `health_passes 1 × health_interval 2s` |

**Invariant**: at any moment during a rolling restart, at least one
replica is answering the active probe with 200. The two replicas are
touched serially (A fully healthy before B is drained — enforced by
`deploy.sh` ordering), so the pool is never empty.

**Worst-case traffic gap**: 6 s (the active-probe eject latency after
SIGTERM). During this 6 s window, Caddy has `lb_try_duration 5s` +
`lb_try_interval 250ms` configured, which means a request that lands
on the draining replica is retried on the healthy one within 250 ms
— so clients see a single re-queue, not a 5xx. This is the mechanism
that delivers "0 × 5xx during deploy" from TODO row 1349.

---

## 6. Operator knobs

The ejection parameters are tuned for the default production load
profile. If you need to adjust them for a different environment
(staging with flaky network, dev box with slow disk), edit the
Caddyfile in place — they are NOT env-var overridable today by
design. The contract tests in
`backend/tests/test_reverse_proxy_health_eject.py` pin the literal
values so that any change to the tuning is visible in the diff and
in CI.

What IS env-var overridable (G2 #1 contract):

| Variable | Default | Purpose |
|----------|---------|---------|
| `OMNISIGHT_UPSTREAM_A` | `backend-a:8000` | Override A replica address |
| `OMNISIGHT_UPSTREAM_B` | `backend-b:8001` | Override B replica address |
| `OMNISIGHT_PUBLIC_HOSTNAME` | (unset) | ACME hostname; unset ⇒ `:443` + internal CA |
| `OMNISIGHT_ACME_EMAIL` | (unset) | ACME registration email |

What is NOT env-var overridable (on purpose):

- `health_interval`, `health_timeout`, `health_fails`, `fail_duration`,
  `max_fails`, `unhealthy_latency`, `unhealthy_request_count` —
  changing these changes the safety contract and should go through
  code review, not a `docker run -e`.

---

## 7. Triage — "Caddy is ejecting my replica, why?"

1. **Is `/readyz` returning 200?**
   `curl -sf http://backend-a:8000/readyz | jq` — if this is 503, the
   active probe is doing its job. Go look at the backend logs for DB
   migration failure / lifecycle drain state.

2. **Are real requests 5xx?**
   `docker compose -f docker-compose.prod.yml logs --tail=200 caddy | grep '"status":5'`
   — if there's a burst of 5xx from a specific replica, the passive
   eject (`fail_duration`/`max_fails`) is firing. Check the replica's
   backend logs for the underlying error.

3. **Is the replica slow?**
   Look for `"duration":1[0-9]\.` or higher in Caddy logs. If a
   replica's p99 crosses 10 s, `unhealthy_latency` kicks it — this
   is usually a GC pause, a saturated worker pool, or a hanging
   upstream (LLM provider timing out while holding the worker).

4. **Is the pool flapping?**
   If you see repeated eject→readmit→eject cycles, `health_passes
   1` might be too aggressive for your environment. It should only
   flap if `/readyz` itself is intermittent — investigate DB
   connectivity under load rather than raising `health_passes`.

### Smoke validation

```bash
# Validate the Caddyfile parses cleanly (no live reload required).
caddy validate --config deploy/reverse-proxy/Caddyfile --adapter caddyfile

# Dry-run format check (catches indentation bugs that cause silent
# scope drift on reverse_proxy directives).
caddy fmt deploy/reverse-proxy/Caddyfile

# Run the contract tests (pinned semantics):
python3 -m pytest backend/tests/test_reverse_proxy_caddyfile.py \
                  backend/tests/test_reverse_proxy_health_eject.py -q
```

---

## 8. References

- G1: `backend/lifecycle.py` — SIGTERM drain coordinator (`/readyz` flips to 503)
- G1: `/healthz` (liveness) vs `/readyz` (readiness) split
- G2 #1: `deploy/reverse-proxy/Caddyfile` — this file's subject
- G2 #2: `docker-compose.prod.yml` — dual-replica topology
- G2 #3: `scripts/deploy.sh` rolling mode
- G2 #5: soak-test for "0 × 5xx during deploy" (TODO row 1349)
- Contract tests: `backend/tests/test_reverse_proxy_caddyfile.py` + `backend/tests/test_reverse_proxy_health_eject.py`
