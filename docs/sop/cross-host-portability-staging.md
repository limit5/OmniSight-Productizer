# Cross-Host Portability for Staging — Port-Based, Not Subdomain-Based

**Status**: Living SOP. Read it before you (a) add a new artifact under
`deploy/staging/`, `infra/staging/`, or `deploy/systemd/staging-*`, or
(b) move the staging stack to a different host.

**Origin**: AUDIT-29 Phase 4 — "Port-based staging (drop the
`staging.sora.services` subdomain)". Sub-META **OP-991** (AUDIT-29d); this
doc is **OP-1031** (AUDIT-29d-3). It codifies, as a standing rule, the
portability discipline that the one-off 5a→5c migration runbook
(`docs/operations/staging-migration-5a-to-5c-runbook.md`, OP-974)
discovered the hard way, plus the one change AUDIT-29d makes on top of it:
**a host-named URL for staging is itself a host coupling — address staging
by a port, not a subdomain.**

Related:

* [`docs/operations/staging-migration-5a-to-5c-runbook.md`](../operations/staging-migration-5a-to-5c-runbook.md)
  — the concrete weekend migration recipe + the portability audit (§1
  there). This SOP is the *generalised* version of that runbook's §1.
* [`docs/operations/staging-environment-runbook.md`](../operations/staging-environment-runbook.md)
  — the systemd-wrapped 5a stack (AUDIT-19a / OP-971).
* [`docs/operations/staging-runbook.md`](../operations/staging-runbook.md)
  — the OP-878 blue-green flow + the active-color port table.
* [`docs/operations/staging-environment.md`](../operations/staging-environment.md)
  — OP-767 stack topology + the `main_promoted` auto-deploy worker.
* [`docs/sop/architecture-anti-patterns.md`](architecture-anti-patterns.md)
  — the cookbook; the "host-named endpoint as hidden coupling" failure
  belongs in the same family as #13 ("dead inventory") and the
  shipped-but-not-deployed pattern.
* `tests/test_staging_migration_5a_to_5c.py` + `scripts/portability-audit.sh`
  — the executable form of the checklist in §3. The pytest module is the
  source of truth; the shell CLI mirrors it (the `ScriptDriftsFromPytest`
  guard in `tests/test_portability_audit_sh.py` keeps them in sync).
* ADR-0006 (TLS termination via the Synology reverse proxy) and ADR-0013
  (docs-site DNS/TLS) — prior art on why a publicly-resolvable name is an
  operationally heavy thing to attach to a service, especially one that
  must *not* be Internet-reachable.

---

## TL;DR

1. **Staging is reached by a port on whatever host runs it** — the
   active-color Caddy port (`18080` blue / `28080` green by default),
   surfaced through `OMNISIGHT_STAGING_URL`. On the staging host that is
   `http://localhost:<port>`; from the prod host it is
   `http://<staging-host-lan-ip>:<port>`. There is no `staging.sora.services`
   DNS record, no ACME cert for a non-public name, no split-horizon
   resolver entry.
2. **Every host-specific value is an env var with a sane default.** Paths
   are relative or `${VAR:-default}`; host ports are
   `${STAGING_*_PORT:-default}`; the only place a literal `host:port` may
   live is `deploy/staging/.env.local` (gitignored).
3. **The aggregate resource ceiling is a percentage, not a byte count** —
   so it re-derives from the new box automatically.
4. **The portability audit must return 0 hits** before any host move.
   `pytest -q tests/test_staging_migration_5a_to_5c.py -k portability` or
   `scripts/portability-audit.sh` (exit `0`).

If you internalise one sentence: *the staging stack must be moveable to
another host with `git clone` + fill in `deploy/staging/.env` + one `sed`
pass on the systemd unit paths + open one firewall port — nothing else.*

---

## 1. The pattern: a subdomain is a host coupling in disguise

`staging.sora.services` *looks* host-independent — it's a name, names are
abstractions, moving the box is "just a DNS change". In practice a
public-ish subdomain attaches a chain of host-coupled obligations to the
staging stack:

* **A DNS record** that must be re-pointed on every move (and its TTL
  managed around the cut-over, or consumers cache the old IP).
* **An ACME challenge path** — Caddy wants a real cert for the name. For a
  name that must *not* be Internet-reachable (staging holds anonymized but
  still-sensitive prod-shaped data) that means an internal CA (step-ca) or
  a long-lived mounted cert — extra machinery that travels with the host,
  not the repo.
* **A split-horizon resolver entry** so the LAN view points at the
  staging box while the public view points nowhere — one more piece of
  host-side config to recreate.
* **A larger blast radius** — a publicly-resolvable name is one
  misconfiguration away from being publicly reachable. Same LAN ≠
  isolated; a public name makes "accidentally exposed" cheaper.
* **A name that drifts into tracked files as a literal** — once
  `https://staging.sora.services` is the default of `OMNISIGHT_STAGING_URL`
  and the host-matcher in `caddy.json` and the frontend build args, it is
  *everywhere*, and "move the box" silently became "audit every file for
  the name".

None of that buys staging anything a port doesn't. Staging has exactly two
classes of caller — the on-host gate units (`staging-gate-{canary,smoke}.service`,
which probe `localhost`) and the prod-host `release_milestone_checker`
path (which reads the JSONL the gate units write, and at most `curl`s
staging for debugging). Neither needs a memorable name; both are perfectly
served by `http://<host>:<port>`.

**Rule**: staging is addressed by a **port**. The only DNS name that may
appear in a tracked staging artifact is one that resolves *identically on
every host the stack can land on* (i.e. effectively none — `localhost`
doesn't count as a "name" for this purpose). A host-specific name belongs
in `deploy/staging/.env.local`, like any other host-specific literal.

> This does **not** ban a *prod*-side convenience name. If an operator
> wants `staging.internal` in their LAN resolver for `curl` ergonomics,
> that's a host-side nicety living in *their* resolver, never a default in
> a tracked file and never something the gate units depend on.

---

## 2. The port-based staging contract

| Concern | Port-based form | What it replaces |
|---|---|---|
| On-host probe (gate units) | `http://localhost:${STAGING_HTTP_PORT:-18080}` (active color) | `https://staging.sora.services` |
| Cross-host probe (prod → staging, debugging) | `http://<staging-host-lan-ip>:${STAGING_HTTP_PORT:-18080}` | same name over split-horizon DNS |
| The env knob | `OMNISIGHT_STAGING_URL` — default `http://localhost:18080`, override per host in `~/.config/omnisight/staging-compose.env` (or `.env.local`) | default `https://staging.sora.services` |
| Active-color indirection | read `/var/lib/omnisight/staging/active-upstream.caddy` / `active_color`; **never hard-code 18080 vs 28080** — the blue-green flow flips it | a single fixed name pointing at "whichever color is live" |
| In-stack reverse proxy | Caddy with **path** routing only (`/api/*`, `/readyz`, `/healthz` → backends; everything else → frontend). **No `host:` / `@host` matcher.** Caddy listens on the published port; TLS is the host's business (plain HTTP on a firewalled LAN segment, or an internal cert mounted in — not public Let's Encrypt) | Caddy with a `staging.sora.services` site block + ACME |
| Frontend build args | `VITE_API_BASE` / `NEXT_PUBLIC_API_URL` point at the **relative** `/api` path (the proxy is co-located) or at `${OMNISIGHT_STAGING_URL}/api` | hard-coded `https://staging.sora.services/api/...` |

Why a port survives the move and a name doesn't: a port is a property of
*the service* (which port the stack publishes), not *the host* (which IP
it's at). `git clone` carries the `${STAGING_HTTP_PORT:-18080}` default;
the only host-side fact left is "the staging box is at LAN IP X", which is
one firewall rule (`prod-host → X:18080 allow`) and one `OMNISIGHT_STAGING_URL`
override — both things the migration runbook already touches, neither
requiring a DNS provider, an ACME client, or a resolver edit.

---

## 3. The 5a→5c portability checklist

This is the standing version of the migration runbook's §1.2 audit, with a
fifth facet added by AUDIT-29d (the port-not-subdomain rule). **All five
must hold; the audit must return 0 hits before any host move.** Each maps
to a check in `tests/test_staging_migration_5a_to_5c.py` (canonical) /
`scripts/portability-audit.sh` (mirror).

| # | Check | Rule | How it's enforced |
|---|---|---|---|
| 1 | **Paths relative or via env var; volumes named** | No `bind` mount or config path in `docker-compose.yml`/`caddy.json` may be an absolute host path unless it's `${VAR:-default}` form. Data volumes must be **named**, never host-path bind mounts. | audit check #1 |
| 2 | **No hard-coded `localhost:<port>` outside `.env.local`** | A literal `host:port` of any staging *dependency* must come from an env var or live only in `deploy/staging/.env.local`. (A container's healthcheck against its *own* port — `localhost:8000/readyz` — is fine; that's the container's port, not a host coupling.) | audit check #2 |
| 3 | **All published ports via env** | Every `ports:` mapping is `"${STAGING_*_PORT:-default}:container"`, never a bare literal host port. | audit check #3 |
| 4 | **Aggregate cgroup ceiling as a fraction** | The `omnisight-staging-compose.service` `MemoryMax`/`CPUQuota` is a **percentage** (`NN%`), so it re-derives from the new box's RAM/CPU. (`IOWeight` is a relative weight — also fine.) | audit check #4 |
| 5 | **Addressed by port, not by a host-specific name** | No tracked staging artifact (`docker-compose.yml`, `caddy.json`, the systemd units, `infra/staging/*`, the frontend build args, the `OMNISIGHT_STAGING_URL` / `STAGING_HOSTNAME` defaults) may contain a host-specific DNS name as a literal or as an env-var *default*. The default is a port-based `localhost` URL; a host name is an `.env.local` / `~/.config/omnisight/*.env` override. Caddy has no `host:` matcher. | audit check #5 (added by AUDIT-29d-1, OP-1029) |

Run it:

```bash
# canonical, CI-enforced:
pytest -q tests/test_staging_migration_5a_to_5c.py -k portability

# standalone shell CLI — same checks, exit-code contract, no pytest:
scripts/portability-audit.sh            # human report; exit 0 = 0 hits
scripts/portability-audit.sh --json     # one JSON object for CI gates
```

A green pytest run / `scripts/portability-audit.sh` exit `0` == "0
portability hits" == the stack is host-portable.

If the audit returns a *new* hit (someone added a bare literal host port,
an absolute bind mount, a non-`%` cgroup limit, a divergent systemd path
prefix, or a host-specific name), fixing it is an `area:devops` change on
the offending artifact — file it as a blocker on whatever migration ticket
surfaced it and re-run the audit before proceeding.

---

## 4. By-design exceptions (carried from the migration runbook §1.3)

These look like portability hits but are intentional; the audit whitelists
them and the migration runbook handles them explicitly:

* **Absolute paths in the systemd `[Service]` sections** —
  `WorkingDirectory=`, `ExecStart=`, `EnvironmentFile=-`,
  `StandardOutput=append:`. systemd *requires* `ExecStart` to be an
  absolute path; there is no relative form. The migration handles this
  with **one `sed` pass** that rewrites the repo/log prefix to wherever
  the operator cloned on the new box (runbook §3 step 5). The audit
  asserts these prefixes are **uniform** (one `OMNISIGHT_REPO_PREFIX`
  worth of editing), not that they're absent.
* **Per-container `mem_limit: 1g|2g|512m|128m`** — these are *workload*
  ceilings (a backend process needs ~2 GB regardless of box size), not
  host fractions; Docker Compose has no "percent of host RAM" form for
  `mem_limit`. On a bigger dedicated box they simply leave more slack. The
  *aggregate* auto-scaling lever is the systemd `MemoryMax=NN%` (check #4).
* **`127.0.0.1:55432` (staging Postgres' published port) defaults in
  `snapshot-restore.sh` / `staging-pg-snapshot.service`** — the *loopback*
  address of the staging PG's published port *on the staging host*; on the
  new box that's still `127.0.0.1:55432` from inside it (the snapshot
  orchestrator runs on the same box as staging PG). Env-var overridable;
  nothing to change unless you republish PG on a non-default port.

`localhost:18080` (and `28080`) are likewise not violations of check #2:
they are the staging stack's *own* published Caddy port, read on the host
that runs the stack — exactly the port-based form check #5 *wants*. The
violation check #2/#5 catch is a *DNS name* (`staging.sora.services`) or a
*remote* `host:port` baked into a tracked file.

---

## 5. Migration recipe — pointer, not duplicate

The step-by-step host-move procedure lives in
[`docs/operations/staging-migration-5a-to-5c-runbook.md`](../operations/staging-migration-5a-to-5c-runbook.md)
— WSL2 gotchas, the Friday-evening cut-over, the Saturday soak, the
verification test plan, rollback. Do not duplicate it here.

The **delta** this SOP introduces over that runbook (once AUDIT-29d-1 has
removed the host-matcher and flipped the `OMNISIGHT_STAGING_URL` default):

* **Drop the DNS step** (runbook §4 / §5.2) — there is no
  `staging.sora.services` record to re-point.
* **Drop the ACME step** (runbook §5.2 last bullet) — Caddy serves the
  published port over plain HTTP on the firewalled LAN segment, or over an
  internal cert the operator mounts; no public Let's Encrypt for a
  non-public name.
* **The firewall rule becomes port-explicit**: `prod-host →
  <staging-host>:18080 allow` (the gate-unit / `curl` path), plus the
  snapshot-pull path; default-deny everything else between segments. (The
  §2.5 "same LAN ≠ isolated" rule is unchanged — a docker network is not
  isolation.)
* **The only per-host edits left** are: fill in `deploy/staging/.env`
  (+ `.env.local` if you have host-specific literals), one `sed` pass on
  the systemd unit paths, set `OMNISIGHT_STAGING_URL=http://<new-host>:18080`
  in `~/.config/omnisight/staging-compose.env`, open the one firewall
  port. That's the whole "is it portable?" claim.

---

## 6. When you add a new staging artifact

Before merging anything under `deploy/staging/`, `infra/staging/`, or
`deploy/systemd/staging-*`:

1. Run `scripts/portability-audit.sh` (or the pytest) — it must stay at
   **0 hits**. If you genuinely need a new by-design exception, add it to
   the audit's whitelist *and* document it in §4 here *and* in the
   migration runbook §1.3 — in the same patch.
2. Any new published port → `"${STAGING_<NAME>_PORT:-<default>}:<container>"`.
3. Any new endpoint a service must reach → an env var with a port-based
   `localhost` default, never a host name literal.
4. Any new resource ceiling that should track box size → a `%` on the
   systemd unit, not a byte count.
5. Consider wiring `scripts/portability-audit.sh` into a pre-commit hook
   for the staging paths — it's a dependency-free bash exec with a clean
   exit code, far lighter than invoking pytest.

The `tests/test_staging_migration_5a_to_5c.py` module runs in CI on every
change to the staging artifacts, so a regression can't land silently — but
the cheapest place to catch it is before you push.

---

## Change log

| Date | Ticket | Change |
|---|---|---|
| 2026-05-13 | OP-1031 | Initial SOP — codifies the port-based staging pattern (§1–§2), the 5a→5c portability checklist as a standing rule with the new "addressed by port, not subdomain" facet (§3 check #5), the by-design exceptions (§4), and the migration delta vs. the OP-974 runbook (§5). AUDIT-29d Phase 4. |
