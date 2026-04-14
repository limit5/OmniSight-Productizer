# Tier-1 Sandbox — Operator Guide

> Phase 64-A. Audience: ops / SRE installing or running OmniSight in
> staging or production.

The Tier-1 sandbox is the docker container that wraps every agent's
shell command. It enforces:

| Control | Knob | Default |
|---|---|---|
| User-space kernel (gVisor) | `OMNISIGHT_DOCKER_RUNTIME` | `runsc` |
| Egress whitelist | `OMNISIGHT_T1_ALLOW_EGRESS` + `OMNISIGHT_T1_EGRESS_ALLOW_HOSTS` | air-gap |
| Image trust | `OMNISIGHT_DOCKER_IMAGE_ALLOWED_DIGESTS` | open mode |
| Wall-clock kill | `OMNISIGHT_SANDBOX_LIFETIME_S` | 2700 (45 min) |
| Per-exec output cap | `OMNISIGHT_SANDBOX_MAX_OUTPUT_BYTES` | 10000 (10 KB) |

Everything below is opt-in for dev (sane fallbacks) and required for
prod.

---

## 1. Install gVisor (runsc)

The default runtime is `runsc`. Without it the backend silently falls
back to `runc`, and the container shares the host kernel — escape
resistance is **downgraded**.

### Linux (Debian/Ubuntu)

```bash
ARCH=$(uname -m)
URL=https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}
wget -q ${URL}/runsc ${URL}/runsc.sha512 \
     ${URL}/containerd-shim-runsc-v1 ${URL}/containerd-shim-runsc-v1.sha512
sha512sum -c runsc.sha512 containerd-shim-runsc-v1.sha512
chmod a+rx runsc containerd-shim-runsc-v1
sudo mv runsc containerd-shim-runsc-v1 /usr/local/bin/

sudo /usr/local/bin/runsc install         # registers runtime in dockerd
sudo systemctl restart docker
docker info | grep -i runtime             # must list "runsc"
```

### macOS / WSL2 (dev only)

gVisor needs a real Linux kernel. On these platforms set:

```
OMNISIGHT_DOCKER_RUNTIME=runc
```

…and accept the reduced isolation. CI / prod must run on Linux.

### Verify the runtime selection

Trigger one container launch (any agent task) and tail the logs for:

```
sandbox_runtime_fallback   # = silent downgrade to runc
```

If you see this in prod, gVisor is missing. Fix the host before
shipping.

---

## 2. Egress whitelist (DOUBLE GATE)

Default: `--network none`. To open egress for e.g. `git clone github`
you must set **both**:

```bash
export OMNISIGHT_T1_ALLOW_EGRESS=true
export OMNISIGHT_T1_EGRESS_ALLOW_HOSTS=github.com,gerrit.internal:29418
```

Either one missing → still air-gapped, with a loud warning. The
backend creates the docker bridge `omnisight-egress-t1` automatically.
Then on each host run **once**:

```bash
sudo OMNISIGHT_T1_EGRESS_ALLOW_HOSTS=github.com,gerrit.internal:29418 \
    scripts/setup_t1_egress_iptables.sh
```

This installs an `OMNISIGHT-T1-EGRESS` iptables chain that ACCEPTs the
resolved IPs and DROPs everything else. Re-run after the allow-list
changes (the script flushes its own chain idempotently). The script
**refuses** an empty allow-list to avoid a silent-DROP-all foot-gun.

Gotchas:

- DNS rotation: the iptables ACCEPT IPs are snapshot-at-install-time.
  If a host moves IP, re-run the script. The Python side caches DNS
  for 5 min purely so the bridge name decision is fast.
- Internal registries: add the registry's hostname to the allow-list
  *if* you want builds to pull from it. Otherwise pre-mount the
  artefacts.

---

## 3. Image immutability (`docker_image_allowed_digests`)

Defaults to **open mode** (no check) so dev and CI keep working.
In prod, pin the trusted image digest:

```bash
docker image inspect --format '{{.Id}}' omnisight-agent:<your-tag>
# → sha256:abcdef...   (the LOCAL content digest, not RepoDigest)

export OMNISIGHT_DOCKER_IMAGE_ALLOWED_DIGESTS=sha256:abcdef...
```

Multiple digests are accepted (CSV) so you can ship a new build
alongside the previous one before retiring the old digest. After
deployment, verify rejections appear when you intentionally swap the
image:

```
omnisight_sandbox_image_rejected_total{image="..."} > 0
```

…and the audit log carries `sandbox_image_rejected` rows.

---

## 4. Wall-clock kill (`sandbox_lifetime_s`)

The watchdog SIGKILLs any container older than this regardless of
in-progress commands. Defaults to **2700 s (45 min)** as per the
tiered-sandbox spec.

```bash
export OMNISIGHT_SANDBOX_LIFETIME_S=2700  # 45 min, prod default
export OMNISIGHT_SANDBOX_LIFETIME_S=0     # disable (NOT for prod)
```

Tune up for legitimately long builds; never set 0 unless you have a
different killswitch upstream.

When the watchdog fires you'll see:

- `omnisight_sandbox_lifetime_killed_total{tier="t1"}` increment
- audit `sandbox_killed reason=lifetime`
- SSE `container.killed` + `agent.error` events

---

## 5. Observability — single Grafana panel cheat-sheet

| Symptom | Metric / log to check |
|---|---|
| Builds suddenly fail to run | `omnisight_sandbox_launch_total{result="error"}` rate |
| Untrusted image swapped in | `omnisight_sandbox_image_rejected_total` non-zero |
| Builds being killed mid-run | `omnisight_sandbox_lifetime_killed_total` rate |
| Silent gVisor downgrade | `sandbox_runtime_fallback` SSE / structlog event |
| Successful launches | `omnisight_sandbox_launch_total{result="success"}` |

Audit chain (Phase 53) carries the per-launch row:

```
action=sandbox_launched   actor=agent:<id>
  after={tier, runtime, image, network, container_id}
action=sandbox_killed     actor=system:lifetime-watchdog
action=sandbox_image_rejected  actor=agent:<id>
```

`audit verify_chain` continues to detect any tampering.

---

## 6. Pre-prod checklist

- [ ] `runsc` installed and `docker info` lists it
- [ ] `OMNISIGHT_DOCKER_RUNTIME=runsc` (or omit — same default)
- [ ] `OMNISIGHT_DOCKER_IMAGE_ALLOWED_DIGESTS` set to your build's
      sha256
- [ ] If egress is required: both env vars set + iptables script run
      on every sandbox host
- [ ] `OMNISIGHT_SANDBOX_LIFETIME_S` matches your longest legitimate
      build (default 2700 fits ~95% of cases)
- [ ] Grafana panels for the four `sandbox_*` counters
- [ ] Alert on any `sandbox_image_rejected_total` increase
- [ ] Alert on `sandbox_runtime_fallback` SSE in prod

---

## 7. Tier 2 — Networked Sandbox (Phase 64-B)

Tier 2 inverts T1's policy: **public internet is reachable, private
RFC1918 / link-local / ULA addresses are DROPped** at iptables. Use
this for MLOps data pulls, third-party API tests, and Phase 65
training-data exfil.

There is **no env double-gate** for T2 — the Python entry point is
the gate:

```python
from backend.container import start_networked_container
info = await start_networked_container(agent_id, workspace_path)
```

The caller is responsible for any Decision Engine approval (planned
`kind=sandbox/networked`, severity=`risky`) before touching this API.

### Install (once per host)

```bash
sudo scripts/setup_t2_network.sh
```

This requires the `omnisight-egress-t2` bridge to exist; the backend
creates it the first time `start_networked_container` runs, so you
can either (a) launch one T2 container then run the script, or (b)
pre-create with `docker network create --driver bridge omnisight-egress-t2`.

### Defended against

- Prompt-injected agent → curl `http://10.0.0.1/admin` → DROP
- Same agent → `nslookup metadata.google.internal` → DROP (link-local)
- Same agent → `pip install pkg` from public PyPI → ACCEPT

### Observability

Same metrics as T1, just `tier="networked"`:

- `omnisight_sandbox_launch_total{tier="networked",result="success"}`
- `omnisight_sandbox_lifetime_killed_total{tier="networked"}`
- `omnisight_sandbox_output_truncated_total{tier="networked"}`

Audit row carries `after.tier="networked"`, `after.network=omnisight-egress-t2`.

---

## Related

- `docs/design/tiered-sandbox-architecture.md` — design rationale
- `scripts/setup_t1_egress_iptables.sh` — egress hardening script
- `backend/sandbox_net.py` — Python-side egress decision
- `backend/container.py::_lifetime_killswitch` — wall-clock killer
