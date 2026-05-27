# Runner Sandbox (OP-845)

Bubblewrap (Linux) / seatbelt (macOS) jail around the runner's CLI
invocations. Complements **OP-836** (sentinel + cwd-perm detection)
with kernel-enforced prevention: even an injection-driven write to
`/home/user/.ssh/authorized_keys` or `curl <attacker>` is refused
before reaching userland.

> "Sandbox is the only defense that survives prompt injection."
> — Anthropic Claude Code sandboxing post (2026-05-06)

## Threat model

The runner spawns `claude` / `codex` against a ticket-derived prompt.
That prompt may itself include hostile text exfiltrated through earlier
JIRA comments, Gerrit reviews, or untrusted source files. Detection
layers (OP-836 sentinel, OP-832 area-label gate) catch the *result* of
an injection — sandbox refuses the action itself.

## How it works

`backend/agents/runner_sandbox.py` exposes `wrap_in_bubblewrap` which
returns a wrapped argv:

| Platform        | Mechanism                       | Network default |
|-----------------|---------------------------------|-----------------|
| Linux           | `bwrap` (bubblewrap user-NS)    | `--unshare-net` |
| macOS (Darwin)  | `sandbox-exec` (seatbelt SBPL)  | `(deny network*)` |
| anything else   | identity (warn `sandbox=disabled`) | n/a          |

Mounts inside the jail:

* **RO**: `/usr`, `/lib`, `/lib64`, `/etc`, `/bin`, `/sbin` (system
  toolchain — readable, but writes return `EROFS`).
* **RW**: the ticket's assigned worktree (`OMNISIGHT_CLAUDE_WORKTREE` or
  the runner-instance per-bot path), and `/tmp/runner-<ticket-key>/`
  (scratch dir; persists across the pickup so the CLI can stage
  intermediate files).
* `HOME` is pinned to the worktree absolute path so a CLI that probes
  `$HOME` for a writable scratch location finds the worktree, not the
  operator's real home.

Everything else is invisible / unwritable.

## Operator install

### Linux production

```bash
sudo apt install bubblewrap            # provides /usr/bin/bwrap
which bwrap                            # /usr/bin/bwrap
```

Then enable enforcement in the systemd unit:

```ini
# ~/.config/systemd/user/runner-claude@.service
Environment=OMNISIGHT_RUNNER_SANDBOX_ENFORCE=1
```

```bash
systemctl --user daemon-reload
systemctl --user restart runner-claude@default
journalctl --user -u runner-claude@default | grep 'sandbox=wrapped'
# → [runner] sandbox=wrapped platform=linux enforce=1
```

### macOS dev

`sandbox-exec` ships with the base OS — no install needed.

```bash
which sandbox-exec                     # /usr/bin/sandbox-exec
export OMNISIGHT_RUNNER_SANDBOX_ENFORCE=1
```

### Dev laptops without the binary

Leave `OMNISIGHT_RUNNER_SANDBOX_ENFORCE` unset. The runner logs:

```
[runner] sandbox=degraded platform=linux enforce=0
[sandbox-binary-missing] bwrap not on PATH; sandbox=degraded — invoking raw cmd
```

…and continues with un-jailed invocations. This is intentional so a
contributor without root access to `apt install` isn't blocked.

## Environment variables

| Name | Default | Effect |
|---|---|---|
| `OMNISIGHT_RUNNER_SANDBOX_ENFORCE` | unset (off) | When `1`, missing sandbox binary raises `SandboxBinaryMissing` and aborts the pickup. When unset, the runner degrades to raw invocation. |
| `OMNISIGHT_RUNNER_SANDBOX_NETWORK_ALLOW` | unset | When non-empty, value is the audit reason for permitting network. Logged as `[sandbox-network-override]` with ticket key + reason. |

## Network policy override

Network deny by default. For tickets that genuinely need network (PyPI
fetch outside the cache, fetching a hostile-looking-but-actually-fine
README from GitHub), set per-pickup:

```bash
OMNISIGHT_RUNNER_SANDBOX_NETWORK_ALLOW="OP-XYZ needs PyPI fetch for chardet" \
  python3 auto-runner-jira.py
```

Every override is logged with the ticket key + reason so the audit
trail records who needed it and why. Don't bake the env var into the
systemd unit — that turns the default into "always-on network" and
defeats half the threat model.

## OP-836 interaction

The OP-836 sentinel lives **inside** the worktree
(`.runner-cwd-sentinel`), which the bubblewrap profile RW-binds. The
sequence becomes:

1. Runner writes sentinel into worktree (pre-launch).
2. Runner builds wrapped argv via `wrap_in_bubblewrap`.
3. CLI runs inside the jail — sees worktree as the only writable space
   plus `/tmp/runner-<ticket>/`.
4. CLI exits.
5. Runner verifies sentinel + HEAD descendant relationship (unchanged
   from OP-836).

If the CLI attempts to write outside the worktree, bubblewrap returns
`EACCES`/`EROFS`, the CLI sees the error, and the OP-836 sentinel
verification continues to pass (the CLI never tampered with the
worktree).

## Verifying the deploy

After enabling `OMNISIGHT_RUNNER_SANDBOX_ENFORCE=1`:

```bash
# 1. Startup line in journal
journalctl --user -u runner-claude@default --since '5min ago' | grep sandbox=

# expected: [runner] sandbox=wrapped platform=linux enforce=1

# 2. Next pickup logs the wrap for each invocation
journalctl --user -u runner-claude@default --since '5min ago' | grep sandbox-wrapped

# expected: [sandbox-wrapped] sandbox=wrapped ticket=OP-XXX argv0=claude network=False ...

# 3. Manual smoke test: try a write outside the worktree inside the jail
python3 - <<'EOF'
from pathlib import Path
from backend.agents.runner_sandbox import wrap_in_bubblewrap
import subprocess, tempfile
with tempfile.TemporaryDirectory() as wt:
    argv = wrap_in_bubblewrap(
        ["sh", "-c", "touch /etc/runner-poison && echo WROTE || echo BLOCKED"],
        worktree_path=Path(wt), ticket_key="OP-SMOKE",
    )
    out = subprocess.run(argv, capture_output=True, text=True)
    print(out.stdout)
EOF
# expected: BLOCKED
```

## Performance

The bwrap cold-start overhead measured against `true` is **< 500ms**
on the production host (AC#6). Benchmark lives at
`backend/tests/test_runner_sandbox.py::test_overhead_under_500ms_cold_start`.

If the overhead grows past budget, profile with:

```bash
strace -f -c -ttt bwrap --die-with-parent --new-session true 2>&1 | head -20
```

## Error catalog

| Exception | When | Operator action |
|---|---|---|
| `SandboxBinaryMissing(platform, binary)` | ENFORCE=1 + binary absent | `apt install bubblewrap` (or unset ENFORCE for dev) |
| `SandboxNetworkPolicyOverride(ticket_key, reason)` | informational — every override logged | review audit log; revoke env var when ticket merges |
| `SandboxPermissionDenied(path)` | CLI attempted write outside jail | this is the **win condition** — review CLI logs + injection vector, do not "fix" by relaxing the sandbox |

## DoD

* `apt install bubblewrap` on the production host.
* Set `OMNISIGHT_RUNNER_SANDBOX_ENFORCE=1` in the active runner systemd
  unit(s).
* Restart the runner. Verify next ticket pickup logs `sandbox=wrapped`
  on the bootstrap line + `[sandbox-wrapped]` on the CLI invocation.

## Agent-CLI toolchain + config binding (OP-1803 / OP-1783 P1)

Wrapping the *agent* CLI (`claude` / `codex`) needs more than the system
RO-mounts: the interpreter and the CLI shims live under nvm, and the CLIs
read their config from `CLAUDE_CONFIG_DIR` / `CODEX_HOME`. P1 binds these so
the jailed CLI can `execvp` and read its config (fixing the execvp failure):

* **Toolchain (§2a)** — `_build_bubblewrap_argv` resolves the live node via
  `which node` (falling back to `$NVM_DIR`'s default-aliased version) and
  RO-binds the **specific** `~/.nvm/versions/node/<ver>/` subtree (node +
  the `claude`/`codex` shims + their `node_modules`). It does **not** bind
  all of `~/.nvm`. A distro `/usr/bin/node` needs no extra mount — it's
  under the existing `/usr` RO-bind.
* **CLI config (§2b, Option B)** — `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are
  in `ENV_ALLOWLIST` (so the degraded/raw spawn forwards them too) and are
  RO-bound at their host path + `--setenv`'d to that bound path. The CLI
  must **not** resolve config relative to the jail's HOME (pinned to the
  worktree), hence the explicit bind+setenv rather than a HOME-relative dir.
* **Egress (§2c, v1)** — the agent-CLI wrap in `auto-runner-jira.py` passes
  `network=True` (blanket-allow for v1) so the jailed CLI reaches the model
  API + git remote. This is the one call site that opts out of the
  deny-by-default network policy; the env-var override path is unchanged.

> ⚠️ **INERT until bwrap is re-enabled.** This shipped while bwrap is
> disabled (`/usr/bin/bwrap.disabled-*`), so `wrap_in_bubblewrap` returns
> the raw cmd and nothing above takes effect at runtime. Re-enabling bwrap
> fleet-wide + the live canary (one `claude` + one `codex` pickup) is a
> **separate operator step**, not part of OP-1803.

## Lineage

* OP-836 — sentinel detection (the L2 layer this complements).
* OP-827 — typed exceptions (`NoCommitsOnBranchError` etc.) — sandbox
  errors follow the same shape so the runner's failure classifier can
  route them.
* Anthropic 2026-05-06 conference talk on Claude Code sandboxing —
  reference profile in
  [`anthropic-experimental/sandbox-runtime`](https://github.com/anthropic-experimental/sandbox-runtime).
