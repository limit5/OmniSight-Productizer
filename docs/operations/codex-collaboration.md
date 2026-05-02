# Codex Collaboration — Install & Run Guide

> **Status**: 2026-05-02 initial setup. Operator-facing how-to for
> running OpenAI Codex (`codex` CLI) alongside Claude on this repo.
> **Related**: `AGENTS.md` (Codex rules), `coordination.md` (ownership
> matrix), `docs/operations/runner-strategy.md` (architectural context).

---

## TL;DR

```bash
# 1. Install codex CLI (one of these)
npm install -g @openai/codex                # Linux / macOS / Windows
# brew install --cask codex                 # macOS only

# 2. Sign in (interactive, one-time)
codex                                       # → "Sign in with ChatGPT" → log in via browser
# (after sign-in, exit with /exit or Ctrl+C; auth persists)

# 3. Worktree is already set up for you (Claude did this on 2026-05-02):
ls /home/user/work/sora/OmniSight-codex-worktree   # should exist on codex-work branch

# 4. Launch Codex runner (Tier B = default, runs from worktree)
cd /home/user/work/sora/OmniSight-Productizer
OMNISIGHT_CODEX_FILTER=FS python3 auto-runner-codex.py

# 5. Meanwhile, Claude runners run in parallel from main checkout
cd /home/user/work/sora/OmniSight-Productizer
OMNISIGHT_RUNNER_FILTER=BP.A python3 auto-runner.py     # subscription
# or
OMNISIGHT_RUNNER_FILTER=BP.J python3 auto-runner-sdk.py # API
```

---

## Step 1 — Install codex CLI

The `codex` CLI is OpenAI's interactive coding assistant (analogous to
`claude` CLI). Install via npm or Homebrew.

### Linux / Windows / macOS (npm)

```bash
npm install -g @openai/codex
codex --version    # confirm install
```

If you don't have npm:

```bash
# Debian / Ubuntu
sudo apt install -y nodejs npm

# Or via nvm (recommended on long-lived dev machines)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
nvm install --lts
npm install -g @openai/codex
```

### macOS (Homebrew)

```bash
brew install --cask codex
codex --version
```

### Direct binary

If neither npm nor Homebrew is convenient, download the prebuilt binary
from <https://github.com/openai/codex/releases>, extract, and put on
PATH.

---

## Step 2 — Authenticate

```bash
codex
```

Interactive UI launches. Pick **"Sign in with ChatGPT"** and complete
the OAuth flow in your browser. Your **OpenAI Pro / Plus / Business /
Edu / Enterprise** subscription (you mentioned Pro 5x) provides the
quota — no API key needed for subscription mode.

After signing in:

```
> /exit
```

The auth persists in `~/.codex/` (or platform equivalent). Subsequent
`codex` invocations use it automatically.

### Verify

```bash
codex exec --cd /tmp "echo hello world from codex"
```

Should run cleanly and print a response. If it asks for permissions,
the install / auth is fine but `--yolo` flag may be needed for
non-interactive auto-runner mode (the runner adds it by default).

---

## Step 3 — Worktree (already set up)

When Claude added Codex collaboration on 2026-05-02, it ran:

```bash
git -C /home/user/work/sora/OmniSight-Productizer branch codex-work master
git -C /home/user/work/sora/OmniSight-Productizer worktree add \
    /home/user/work/sora/OmniSight-codex-worktree codex-work
```

Result: `/home/user/work/sora/OmniSight-codex-worktree/` is a separate
working directory rooted at the same `.git` but on the `codex-work`
branch. You don't need to do this again.

Check it's there:

```bash
git -C /home/user/work/sora/OmniSight-Productizer worktree list
```

Should show:

```
/home/user/work/sora/OmniSight-Productizer    <sha> [master]
/home/user/work/sora/OmniSight-codex-worktree <sha> [codex-work]
```

(plus any pre-existing agent worktrees the OmniSight system manages).

---

## Step 4 — Run Codex against the TODO

### The standard launch (Tier B — recommended default)

```bash
cd /home/user/work/sora/OmniSight-Productizer
OMNISIGHT_CODEX_FILTER=FS python3 auto-runner-codex.py
```

What this does:
  * Reads TODO.md from the **main** checkout (not the worktree — TODO
    is shared)
  * Filters to sections matching `FS` (Full-Stack adapter scaffolds —
    Codex's domain per `coordination.md`)
  * For each pending `- [ ] item`, drives `codex exec --cd
    /home/user/work/sora/OmniSight-codex-worktree --yolo "<prompt>"`
  * Codex's commits land on the `codex-work` branch, NOT master
  * On completion, marks `- [x][G]` in TODO.md

When Codex finishes a batch, you (or Claude) review and merge:

```bash
cd /home/user/work/sora/OmniSight-Productizer
git log --oneline master..codex-work    # see what codex committed
git diff master..codex-work             # full diff
# If you're happy:
git merge codex-work                    # fast-forward / merge
# If not:
git checkout codex-work
git reset --hard master                 # discard codex's work
git checkout master
```

### Tier A launch (rare — pattern-replication explicitly approved)

For tasks you've **personally vetted** as pure pattern-replication
(e.g., "add the SES adapter following the Resend adapter shape
exactly"):

```bash
cd /home/user/work/sora/OmniSight-Productizer
OMNISIGHT_CODEX_TIER=A \
OMNISIGHT_CODEX_FILTER=FS \
OMNISIGHT_CODEX_TARGET_ITEM='FS.4.1 Resend' \
python3 auto-runner-codex.py
```

What changes:
  * cwd switches to main checkout (master), not worktree
  * Codex's commits land directly on master alongside Claude's

⚠️ **Use Tier A sparingly.** It bypasses the human-review safety net.
Default to Tier B unless you've already verified the task type works
well for Codex on at least 2-3 prior runs.

---

## Step 5 — Run Claude runners in parallel

Two patterns work today:

### Multiple Claude runners (subscription)

```bash
# Terminal 1
OMNISIGHT_RUNNER_FILTER=BP.A python3 auto-runner.py

# Terminal 2 (different section, no overlap)
OMNISIGHT_RUNNER_FILTER=BP.J python3 auto-runner.py

# Terminal 3
OMNISIGHT_RUNNER_FILTER=KS.1 python3 auto-runner.py
```

Per `coordination.md` same-branch safety contract: same TODO file,
no overlap because `OMNISIGHT_RUNNER_FILTER` is disjoint, no commit
conflicts because file-level non-overlap.

### Mixing Claude (subscription) + Claude (API) + Codex

```bash
# Terminal 1 — Claude subscription, big multi-subsystem epic
OMNISIGHT_RUNNER_FILTER=W14 python3 auto-runner.py

# Terminal 2 — Claude API, single-file work
unset OMNISIGHT_SDK_TARGET_ITEM
OMNISIGHT_RUNNER_FILTER=BP.J \
OMNISIGHT_SDK_DAILY_BUDGET=20 \
OMNISIGHT_SDK_MAX_PER_ITEM_USD=8 \
python3 auto-runner-sdk.py

# Terminal 3 — Codex, pattern-heavy adapter epic
OMNISIGHT_CODEX_FILTER=FS python3 auto-runner-codex.py
```

⚠️ **All FILTER env vars must be disjoint** — if Terminal 1 and 3 both
get tasks in `BP.J`, you'll have race conditions on TODO.md / HANDOFF.md.
Read `coordination.md` "Section ownership" before running concurrent
runners.

---

## Environment variables — reference

`auto-runner-codex.py` honours these:

| Env var | Default | Purpose |
|---|---|---|
| `OMNISIGHT_CODEX_BIN` | `codex` | Path to codex CLI (override if installed somewhere unusual) |
| `OMNISIGHT_CODEX_TIER` | `B` | `A` or `B` per coordination.md |
| `OMNISIGHT_CODEX_WORKTREE` | `../OmniSight-codex-worktree` | Tier B working directory |
| `OMNISIGHT_CODEX_FILTER` | _(empty = all)_ | Same syntax as `OMNISIGHT_RUNNER_FILTER` |
| `OMNISIGHT_CODEX_TARGET_ITEM` | _(empty)_ | Lock to a single item by substring match |
| `OMNISIGHT_CODEX_MODEL` | _(codex default)_ | Override model (e.g., `gpt-5.5`) |
| `OMNISIGHT_CODEX_APPROVAL` | `yolo` | `yolo` (full-auto) / `auto` / `read-only` |
| `OMNISIGHT_CODEX_EXTRA_FLAGS` | _(empty)_ | Any extra `codex exec` flags space-separated |
| `OMNISIGHT_CODEX_TIMEOUT_S` | `1800` | Per-item timeout (30 min) |
| `OMNISIGHT_CODEX_MAX_RETRIES` | `2` | Retry per item |
| `OMNISIGHT_CODEX_COOLDOWN` | `5` | Sleep between items (seconds) |
| `OMNISIGHT_CODEX_SECTION_COOLDOWN` | `10` | Sleep when crossing sections |

---

## Recovery — when Codex goes off-rails

Symptoms:
  * Commit message style violates `AGENTS.md` Rule 5 (missing tier
    marker / wrong Co-Authored-By)
  * Code style inconsistent with rest of codebase (didn't mirror
    existing pattern)
  * Scope creep — Codex changed unrelated files
  * Tests broken (Codex skipped running them)

Recovery options (in increasing severity):

### 1. Single bad commit on `codex-work` (Tier B)

```bash
cd /home/user/work/sora/OmniSight-codex-worktree
git log --oneline -5                       # find the bad commit
git revert <bad-commit-sha>                # creates a revert commit
# OR for a clean reset:
git reset --hard HEAD~1                    # discards last commit
```

Since Tier B never touches master, no other developer is affected.

### 2. Multiple bad commits on `codex-work`

```bash
cd /home/user/work/sora/OmniSight-codex-worktree
git reset --hard master                    # nuke all codex-work changes
# then re-launch codex with stricter task scope
```

### 3. Bad commit landed on `master` (Tier A)

```bash
cd /home/user/work/sora/OmniSight-Productizer
git log --oneline                          # find the bad SHA
git revert <bad-sha>                       # safe — creates a new commit
# DO NOT git reset master — other runners may have committed since
```

If you see any sign that Tier A is repeatedly producing bad commits,
**stop using Tier A entirely**. The Tier classification was wrong;
move that task type to Tier B.

### 4. AGENTS.md needs adjustment

If a class of error keeps recurring (e.g., Codex repeatedly violates
some rule), update `AGENTS.md` to be more explicit about that rule.
Do this manually (don't let Codex edit AGENTS.md per Rule 8).

---

## Throughput expectations

Empirically (per `runner-strategy.md` data):

| Setup | Throughput vs solo Claude |
|---|---|
| Solo Claude (subscription, 1 runner) | 100% baseline |
| Solo Claude (subscription, 4 parallel runners on disjoint sections) | 280-320% |
| Above + Claude API runner (1) on Tier-A-eligible items | +20-30% |
| Above + Codex Tier B (1 worktree, separate sections) | +30-50% |
| All four together, well-routed | 380-440% — but coordination overhead grows |

Coordination overhead becomes meaningful past 4 concurrent runners.
Beyond that, the bottleneck is human review capacity, not LLM
throughput.

---

## When NOT to run Codex

  * **Anthropic subscription has plenty of headroom** AND no Codex-
    suitable tasks queued — just run more Claude runners
  * **You're about to do a major architectural change** — let Claude
    handle in isolation first; Codex can come in for follow-up
    pattern-fill work
  * **Tasks ahead in TODO are ALL `[!]` failures from previous Codex
    runs** — diagnose the AGENTS.md gap before launching another run

---

## When TO run Codex

  * **`FS.*` epic is pending** — Codex's sweet spot
  * **Documentation backlog needs clearing** — markdown is GPT's
    strength
  * **`B9` ESLint cleanup queued** — high-volume mechanical fixes,
    Codex thrives
  * **You're at Anthropic subscription quota** but have OpenAI
    headroom — Codex absorbs work
  * **You've vetted at least one Tier B run end-to-end** and are
    comfortable with the output quality

---

## Quick troubleshooting

| Symptom | Fix |
|---|---|
| `❌ 找不到 codex CLI` at startup | `npm install -g @openai/codex` then re-login `codex` once |
| Codex prompts for permission mid-task | `OMNISIGHT_CODEX_APPROVAL=yolo` (default) — should already be set; check `codex --version` is recent |
| `❌ Tier B 需要 worktree` exit | Run worktree setup commands in Step 3 |
| Codex hangs / no progress | Ctrl+C once → graceful stop; if Ctrl+C 3 times no effect, `pkill -KILL -f auto-runner-codex` from another terminal |
| Codex's commits don't have Co-Authored-By trailers | Codex didn't follow AGENTS.md Rule 5 → check the task scope was clear; consider tightening AGENTS.md |
| Race condition on TODO.md | `OMNISIGHT_CODEX_FILTER` and the Claude runners' filters overlapped — fix and restart |
| `[Tier-A]` marker missing on Codex commit | Codex skipped Rule 5 — review the commit, consider stricter prompt template |

---

## Future evolution

When `BP.A2A` (Agent-to-Agent Protocol Integration) lands, the three
runners' roles will collapse into:

  * Single OmniSight Orchestrator that publishes A2A AgentCards
  * Each TODO task auto-routes to the right backend (Claude / API /
    Codex / external) based on capability descriptors
  * Operator picks "this task" not "which runner"

Until then, manual routing via `OMNISIGHT_*_FILTER` environment
variables is the contract.
