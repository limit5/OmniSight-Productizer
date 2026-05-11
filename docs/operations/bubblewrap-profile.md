# Bubblewrap Profile Audit (OP-862)

OP-845 added the session-level runner sandbox in
`backend/agents/runner_sandbox.py`. This audit documents the tool surface
that runs during a normal JIRA Story completion, whether each surface needs
paths outside the ticket worktree, and how operators should diagnose missing
binds without weakening the sandbox.

## Scope

Audited code paths:

* `auto-runner-jira.py` pickup, CLI invocation, post-CLI Gerrit push path.
* `backend/agents/jira_dispatch.py` git, ssh, Gerrit, and JIRA helpers used
  by the runner.
* `backend/agents/runner_workspace_safety.py` OP-836 sentinel checks.
* Runner-exposed Bash handlers in `backend/agents/runner_handlers.py` and
  `backend/agents/tool_dispatcher.py`.
* Existing OP-845 sandbox tests plus OP-862 escape probes.

Out of scope for this document: Docker Tier-1/Tier-2 agent sandboxes,
frontend web preview sandboxes, database migrations, and embedded targets.

## Current Profile

Linux uses `bwrap` and macOS uses `sandbox-exec`; unsupported platforms log
`sandbox=disabled` and run the raw command unless enforcement is enabled.

Linux mount surface:

| Mount | Mode | Why it exists |
|---|---:|---|
| `/usr` | RO | host programs, dynamic libraries, Python, git, ssh, bwrap dependencies |
| `/lib` | RO | dynamic loader and shared libraries on Debian/Ubuntu-style hosts |
| `/lib64` | RO | dynamic loader on distros that use a separate lib64 path |
| `/etc` | RO | NSS, certificates, shell/runtime config needed by host tools |
| `/bin` | RO | shell and POSIX utilities |
| `/sbin` | RO | distro utility path when present |
| ticket worktree absolute path | RW | only durable place the CLI may edit |
| `/tmp/runner-<ticket-key>` | RW | scratch path for subprocesses and probes |
| linked worktree git metadata dirs | RW | enables `git commit` inside `git worktree` checkouts |
| `/proc` | virtual | process metadata for normal tool startup |
| `/dev` | virtual | standard device nodes such as `/dev/null` |

Environment shaping:

| Variable | Value inside sandbox | Reason |
|---|---|---|
| `HOME` | ticket worktree absolute path | prevents probing or writing the operator's real home |
| `TMPDIR` | `/tmp/runner-<ticket-key>` | keeps scratch writes out of host `/tmp` |
| cwd | ticket worktree absolute path | keeps relative paths inside the allowed tree |

Network is denied by default with `--unshare-net`. Operators may set
`OMNISIGHT_RUNNER_SANDBOX_NETWORK_ALLOW=<reason>` for one pickup; the
override is logged with the ticket key and must not be baked into systemd.

## Story Completion Inventory

Typical Story completion has three phases:

1. Runner preflight and worktree preparation.
2. Wrapped CLI execution (`claude` or `codex`) and its in-process tools.
3. Runner postflight, Change-Id normalization, Gerrit push, and JIRA comment.

### Preflight and Pickup

| Invocation | Location | Runs inside bwrap? | Outside-worktree requirement | Required bind/profile note |
|---|---|---:|---|---|
| JIRA REST via `urllib.request` | `jira_dispatch.make_client`, fetch/pickup helpers | no | network and bot token file under `~/.config/omnisight` | not part of the CLI sandbox; do not bind JIRA credentials into bwrap |
| Gerrit open-PS query through `ssh ... gerrit query` | `jira_dispatch.open_ps_count_for` | no | `ssh`, bot private key under `~/.config/omnisight`, network | not part of the CLI sandbox; keep Gerrit SSH keys outside bwrap |
| merged-sibling query through `ssh ... gerrit query` | `auto-runner-jira.already_merged_in_gerrit` | no | same as Gerrit query above | not part of the CLI sandbox |
| `git config --get extensions.worktreeConfig` | `jira_dispatch.assert_worktree_config_enabled` | no | repository `.git/config` | not part of bwrap; validates runner host setup |
| `git fetch`, `git worktree add`, `git switch`, `git reset`, `git clean` | `jira_dispatch.sync_to_gerrit_develop` | no | Gerrit SSH key, shared git common dir, ephemeral worktree base | not part of bwrap; do not bind main repo into the CLI sandbox |
| `git rev-parse --git-common-dir` and `git rev-parse --show-toplevel` | `runner_workspace_safety` | no | git metadata | not part of bwrap; verifies OP-836 cwd isolation before launch |
| `.runner-cwd-sentinel` write | `runner_workspace_safety.write_workspace_sentinel` | no | ticket worktree | worktree RW bind is sufficient for later CLI visibility |

### Wrapped CLI

| Invocation | Location | Runs inside bwrap? | Outside-worktree requirement | Required bind/profile note |
|---|---|---:|---|---|
| `claude --dangerously-skip-permissions -p <prompt>` | `auto-runner-jira._invoke_cli` | yes | host `claude` binary and runtime files | `/usr`, `/bin`, `/lib*`, `/etc` RO are required; no home bind |
| `codex exec --cd <worktree> --yolo` | `auto-runner-jira._invoke_cli` | yes | host `codex` binary and runtime files | `/usr`, `/bin`, `/lib*`, `/etc` RO are required; no home bind |
| Built-in Bash foreground commands | `runner_handlers.bash_handler` | yes, when called by wrapped CLI process | `/bin/bash`, POSIX tools, language runtimes | `/bin`, `/usr`, `/lib*`, `/etc` RO plus worktree/TMPDIR RW |
| Persistent Bash session | `tool_dispatcher.PersistentBashSession` | yes, when constructed by wrapped CLI process | `/bin/bash` | same as Bash foreground commands |
| `git` from Bash or CLI internals | CLI tool calls | yes | worktree `.git` file points to shared git metadata outside linked worktrees | bind the worktree-specific git dir plus common git dir; do not bind the main checkout |
| `python3 -m pytest ...` from Bash | CLI tool calls | yes | Python binary, site-packages, repo files | `/usr`, `/bin`, `/lib*`, `/etc` RO and worktree RW are sufficient when deps are installed system-wide or in the worktree |
| `gh` from Bash | CLI tool calls | yes | `gh` binary plus credentials | binary can be read through `/usr` if installed, but credentials in real home are intentionally unavailable; prefer runner-side Gerrit/JIRA helpers |
| MCP servers launched as subprocesses | external tool registry | yes if launched by wrapped CLI; no if runner-host daemon | binary/runtime plus any server-specific state | bind only documented server state paths; do not bind operator home wholesale |

### Postflight

| Invocation | Location | Runs inside bwrap? | Outside-worktree requirement | Required bind/profile note |
|---|---|---:|---|---|
| `git rev-parse HEAD`, `git merge-base --is-ancestor` | `runner_workspace_safety.verify_workspace_sentinel` | no | git metadata | runner-host postflight check; not part of bwrap |
| `git rev-list`, `git status`, `git rebase --exec git commit --amend --no-edit` | `jira_dispatch.ensure_change_ids` | no | git metadata, commit-msg hook | runner-host postflight check; not part of bwrap |
| `git log -1 --format=%B` | `jira_dispatch._head_change_id` | no | git metadata | runner-host postflight check |
| `git push ... HEAD:refs/for/develop` | `jira_dispatch.push_to_gerrit_for_review` | no | Gerrit SSH key, network | runner-owned forward path; not part of bwrap |
| JIRA comment and transition REST calls | `jira_dispatch.add_comment`, runner finalization | no | network and bot token | runner-owned; forward transitions remain runner-owned |

## Bind Matrix

| Tool or script | Needs outside-worktree path? | Path class | Profile decision |
|---|---:|---|---|
| `bwrap` | yes | sandbox binary | resolved before entering sandbox |
| `claude` CLI | yes | host binary/runtime | covered by RO system binds |
| `codex` CLI | yes | host binary/runtime | covered by RO system binds |
| `/bin/bash` | yes | host binary/runtime | covered by RO system binds |
| `git` inside wrapped CLI | yes for linked worktrees | worktree-specific git dir and common git dir | OP-862 adds the narrow metadata binds so `git commit` works without binding the main checkout |
| `git` runner postflight | yes | host git metadata and network remote | outside bwrap by design |
| `ssh` for Gerrit | yes | `~/.config/omnisight/gerrit-*-ed25519` | outside bwrap by design; never bind into CLI sandbox |
| JIRA REST helpers | yes | token file and network | outside bwrap by design |
| `python3` / `pytest` | yes | host runtime/dependencies | covered by RO system binds when dependencies are installed outside the worktree |
| `gh` | yes | host binary and credentials | binary readable through RO system binds; credentials intentionally unavailable |
| MCP subprocess servers | maybe | server-specific binaries/state | document each server state path before adding a bind |
| `/etc/shadow` | no | sensitive host file | OP-862 escape test must fail |
| `/proc/1/environ` | no | sensitive host process metadata | OP-862 escape test must fail |
| `~/.ssh/` | no | operator credentials | `HOME` is worktree; OP-862 escape test must fail |

## Correctness Regression Audit

OP-862 added `backend/tests/test_bubblewrap_escape.py` and a CI job that
installs bubblewrap before running it. The test asserts that a wrapped CLI
cannot read `/etc/shadow`, `/proc/1/environ`, or `~/.ssh/`.

Drift check performed for this audit:

| Scenario | Baseline | Sandboxed | Result |
|---|---|---|---|
| runner sandbox argv construction | existing `test_runner_sandbox.py` unit coverage | wrapper now includes linked-worktree git metadata binds | bind shape pinned by `test_argv_shape_linux_binds_linked_git_metadata` |
| linked worktree git commit | unsandboxed `git commit` can update worktree metadata | `test_e2e_git_commit_in_linked_worktree_inside_jail` commits inside bwrap | missing git metadata bind fixed |
| sensitive host-path access | host can name the paths | wrapped probe must print `BLOCKED` | enforced by CI job `runner-bubblewrap-escape-regressions` |
| synthetic Story completion diff parity | unsandboxed shell writes, py-compiles, and commits a module | same script runs inside bwrap | `test_sandboxed_story_completion_matches_unsandboxed_baseline` compares `git show --numstat` and clean status |

Local note: this workstation does not have `bwrap` installed, so real
runtime probes skip locally. CI installs `bubblewrap` explicitly, which
turns the same probes into hard checks on Ubuntu.

## Error Catalog

| Error | Meaning | Required response |
|---|---|---|
| `BubblewrapBindMissing` | A tool needs a path that is not in the profile | document the tool, exact path, command, stderr, then add the narrowest bind |
| `BubblewrapSandboxEscapePossible` | A sensitive host-path probe succeeded | stop the runner, treat as critical, revert or tighten the profile before further pickup |
| `BubblewrapDriftFromBaseline` | Sandboxed and unsandboxed Story completion produced different diff or test result | keep the ticket in TODO/blocked state until drift is explained |

## Troubleshooting: Tool Needs a Bind

Use this pattern when a wrapped CLI fails only inside bubblewrap:

1. Capture the exact command and stderr from the wrapped run.
2. Re-run the same command outside bubblewrap from the same worktree.
3. If outside succeeds and inside fails with `No such file`, `Permission
   denied`, `EROFS`, or missing credentials, classify it as
   `BubblewrapBindMissing`.
4. Identify the smallest stable path class:
   * host binary/runtime: prefer existing RO system binds;
   * work output: write under the worktree or `/tmp/runner-<ticket>`;
   * credentials: keep runner-owned credentials outside bwrap unless the
     ticket explicitly requires a reviewed credential path;
   * service state: add a server-specific bind, not `$HOME`.
5. Add or update a test that fails without the bind.
6. Update this matrix with the new tool and path.

Example:

```text
Symptom: `pytest` inside the wrapped CLI fails with "python3: not found".
Classification: BubblewrapBindMissing.
Narrow fix: verify `/usr` and `/bin` RO binds are present; do not bind
the operator home. Add an argv-shape test if the bind was missing.
```

Credential example:

```text
Symptom: `gh auth status` fails inside the wrapped CLI.
Classification: expected denial, not a profile bug.
Action: use runner-owned Gerrit/JIRA helpers outside bwrap, or create a
ticket-specific reviewed credential bind. Do not bind `~/.config` or
`~/.ssh` wholesale.
```

## CI Hook

`.github/workflows/ci.yml` includes `runner-bubblewrap-escape-regressions`.
That job:

1. checks out the repository;
2. installs Python dependencies from the locked requirement files;
3. installs `bubblewrap` via apt; and
4. runs `backend/tests/test_bubblewrap_escape.py`.

The dedicated job exists so the escape probes are not hidden in a shard that
might skip on an image without `bwrap`.
