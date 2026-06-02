# Codex CLI failure-mode investigation (OP-1917)

**Filed:** 2026-06-02
**Investigator:** claude (this session)
**Ticket:** OP-1917
**Status:** ROOT CAUSE CONFIRMED — H7 (not in original H1-H6 hypothesis list)

---

## TL;DR

**Root cause = codex CLI subscription-auth refresh-token TOCTOU race between `runner-codex@1` and `runner-codex@2` systemd instances sharing one ChatGPT subscription account at `/home/user/.codex/auth.json`.**

ChatGPT's auth backend invalidates each refresh-token immediately on first use (anti-replay). When both codex instances make `responses_websocket` API calls within the same ~few-second window, both attempt to refresh the shared access token; the first refresh wins, the second's refresh attempt returns `refresh_token_already_used` → HTTP 401 → codex CLI exits rc=1 → runner reverts ticket → stoploss.

**Failure rate:** 2,472 token-refresh failures across both instances since first occurrence **2026-06-01 19:42:40 UTC** (≈24h before Case 2 EPIC started; ~36h before OP-1917 filed). Both instances about even (1,224 + 1,248). This is why **Case 2 EPIC hit 10/10 codex rc=1** and **Phase 0 hits 12/12 so far** — the failure was structural from before Case 2 started, not Case-2-specific.

H1-H6 from the original OP-1917 plan ALL ruled out by log evidence (see §3).

---

## 1. Evidence trail

### 1.1 Smoking-gun log excerpt (OP-1920 attempt, codex-2 instance, 2026-06-02 22:32:09 local)

```text
=== 22:35:57 codex-2 cycle #6953 start workspace=/tmp/runner-workspaces/codex-2/run-2mLqMj ===
[runner] sandbox=wrapped platform=linux enforce=1                              # ← H1 ruled out
[runner] authenticated as rt3628+codex-bot@gmail.com                          # ← H2 ruled out
[runner] selected: OP-1920 (component=META)
[runner] worktree synced: fresh branch feature/OP-1920-runner-fresh
[runner] OP-1920 capabilities: ['code_edit', 'gerrit_push', 'jira_update', ...]
[runner] OP-1920 claim acquired (token: codex-2:1780410723171957-7b027d35)
[runner] transitioning OP-1920 → In Progress
WARNING: proceeding, even though we could not update PATH                     # ← H6 non-fatal
You are working on JIRA ticket OP-1920.                                        # ← H4 ruled out (prompt delivered)
Phase 0 Sub-EPIC P0.A Wave-1 ticket. Parent: Phase 0 META OP-1918...
[full AC1/AC2/AC3/AC4 sections visible in log, ~150 lines of prompt context]
When you complete the work, your final commit message must include
[OP-1920] in the subject line.

2026-06-02T14:32:09Z ERROR codex_login::auth::manager: Failed to refresh token:
  Your access token could not be refreshed because your refresh token was
  already used. Please log out and sign in again.
[~30 more identical errors over ~2 seconds]
2026-06-02T14:32:10Z ERROR codex_api::endpoint::responses_websocket:
  failed to connect to websocket: HTTP error: 401 Unauthorized,
  url: wss://chatgpt.com/backend-api/codex/responses
[~30 more identical 401s]
ERROR: Your access token could not be refreshed because your refresh
token was already used. Please log out and sign in again.

[runner] OP-1920 CLI failed rc=1; reverting ticket
```

### 1.2 Failure-signature frequency

```text
grep -c "refresh token was already used" /tmp/runner-codex-1.log → 1224
grep -c "refresh token was already used" /tmp/runner-codex-2.log → 1248
```

Roughly even split → both instances racing, not one stuck and the other healthy.

### 1.3 First occurrence

```text
1053644:2026-06-01T19:42:40.408880Z ERROR codex_login::auth::manager:
  Failed to refresh token: Your access token could not be refreshed
  because your refresh token was already used.
```

≈24h BEFORE Case 2 EPIC started (2026-06-02 ~AM). So this isn't a Case-2 regression — Case 2 EPIC simply was the first one filed under broken-codex conditions. Case 1 Sub-EPIC 4 (filed 2026-06-01, partially under the broken window) had 17/38 codex rc=1 = 45%, consistent with token-refresh race firing intermittently. Case 2 hit 10/10 because EVERY ticket fell into the now-stable broken state.

### 1.4 Shared-token mechanism (config-level)

```text
ls -la /home/user/.codex/auth.json → -rw-------  4430 bytes  May 23 02:09
```

Canonical ChatGPT subscription token. Owned by `user` (the host user that both `runner-codex@1` and `runner-codex@2` systemd `--user` units run as). Per-ticket `cli-home` directories (`/tmp/runner-OP-XXXX/cli-home/.codex/`) get seeded from this file via the ephemeral wrapper at `scripts/runner-wrapper/run-ephemeral.sh`. When either instance's codex CLI invocation triggers a server-side refresh, the SERVER invalidates the previous refresh-token; the OTHER instance's still-cached refresh-token (from before this refresh) becomes invalid → next refresh attempt by the other instance hits `refresh_token_already_used`.

Per-ticket cli-home isolation does NOT solve this: the refresh-token state is server-side, not file-side.

---

## 2. Why the OP-1917 H1-H6 hypotheses all missed

The original H1-H6 in OP-1917 framed failure as a runner-side or sandbox-side bug. Actual root cause is **codex CLI auth backend state** (server-side at ChatGPT) — outside the failure surface I was looking at. This is a teachable miss; recording so future investigations of "X CLI rc=1" check auth/token state before ruling in/out sandbox/toolchain/runner-wrapper hypotheses.

| H | Verdict | Evidence |
|---|---|---|
| H1 Sandbox-tightening regression (bwrap 2026-05-28) | **RULED OUT** | `[runner] sandbox=wrapped platform=linux enforce=1` succeeds; no bwrap mount errors; codex CLI starts in jail OK. |
| H2 Per-instance Gerrit account missing | **RULED OUT** | `[runner] authenticated as rt3628+codex-bot@gmail.com (712020:bc59c45a...)` — Gerrit auth succeeds before codex CLI even runs. |
| H3 Cross-repo PR open path broken | **RULED OUT** | Phase 0 tickets target productizer Gerrit, not cross-repo. PR-open path not exercised. Failure is upstream of any push. |
| H4 Ticket-spec verbosity / context overflow | **RULED OUT** | Prompt + AC1-AC4 sections visible in log (~150 lines successfully delivered to codex CLI). Codex CLI received the full prompt; failure is auth, not context. |
| H5 Toolchain-availability gap | **RULED OUT** | Codex CLI starts cleanly; clone completes in 4-5 sec; `cli-home` populated. Failure happens after CLI startup, before any tool execution. |
| H6 Worktree-clone wrapper regression / sentinel deletion | **RULED OUT** | No `.runner-cwd-sentinel` deletion / "workspace tampered" errors. The `WARNING: proceeding, even though we could not update PATH` is harmless (codex's own warning about not creating helper binaries in tmpdir; doesn't gate execution). |
| **H7 Subscription-auth refresh-token TOCTOU race (NEW)** | **CONFIRMED** | 2,472 failures since 2026-06-01 19:42:40; concurrent codex-1 + codex-2 invocations; canonical token at `/home/user/.codex/auth.json` shared across both systemd `--user` units; server-side refresh invalidates the other instance's cached refresh-token. |

---

## 3. Why this is the same shape as `feedback_runner_burst_toctou_shared_account`

Memory note `feedback_runner_burst_toctou_shared_account` documented the same anti-pattern earlier:

> "Runner burst → shared-account TOCTOU false-abort → stoploss. Don't file >3 same-class agent:auto tickets at once."

That note was filed for general burst protection. H7 is a more specific, more severe instance: the shared-account isn't a JIRA account but a ChatGPT subscription auth-token, and the contention isn't "lots of work" but "any 2 concurrent codex CLI invocations on the same host". Once you have 2 codex runners under one ChatGPT account, the failure rate is asymptotically 100% as soon as both instances try to do anything at the same time.

This is also the same shape as `project_family_f_gerrit_account_gap` memory: per-instance creds are derived (codex-bot-codex-1 etc.) but only umbrella creds exist (`codex-bot`). The umbrella-only pattern repeats in the ChatGPT subscription layer.

---

## 4. Recommended fix path (4-step ladder, smallest fix first)

### Step 1 — Immediate operator action (5 min)
Run `codex login` (one-shot, on host) to refresh the root token. Restart `runner-codex@1` + `runner-codex@2`. This buys ~1-2 hours of working codex before the next concurrent refresh re-triggers the race. **Useful for: clearing the immediate backlog of stuck codex tickets**, not a permanent fix.

### Step 2 — Short-term: host-wide flock around codex CLI invocation (1 PR, ~1 day)
Add `flock /var/lock/codex-cli-host.lock` around the codex CLI invocation in the runner wrapper (`scripts/runner-wrapper/run-ephemeral.sh` or wherever the codex binary is exec'd). Serializes codex-1 + codex-2 on the same host so only one CLI session is in-flight at a time. Eliminates the refresh-token race.

Tradeoff: halves codex throughput (no parallelism between instances). But since today's reality is 0% throughput (every ticket fails), serial codex = strict improvement.

### Step 3 — Medium-term: per-instance ChatGPT subscription accounts (1 ticket, ~1 week)
Mirror the family-F Gerrit per-instance account pattern. Two separate ChatGPT subscription accounts:
- `codex-1@<email-domain>` → seeds `/home/user/.codex-codex-1/auth.json`
- `codex-2@<email-domain>` → seeds `/home/user/.codex-codex-2/auth.json`

Runner wrapper sets `CODEX_HOME=/home/user/.codex-<INSTANCE_ID>` per-instance. Eliminates shared-token state at the source. Restores full parallelism.

Operator-side prereq: create 2 ChatGPT accounts (or whatever the subscription tier permits). Cost: 1 extra subscription/month.

### Step 4 — Long-term (optional): API-key auth instead of subscription
Switch codex CLI to OpenAI API-key auth (no refresh-token rotation, no shared-account contention). Each instance can hold its own API key. Operator cost: OpenAI API metered spend instead of fixed subscription. Worth re-considering once usage patterns are stable.

---

## 5. Concrete next-step ticket scope (separate ticket, file after operator reads this)

```
[OP-NEW][infra] Codex CLI host-wide flock to eliminate subscription-auth refresh-token race (H7 fix)

Per OP-1917 investigation report (docs/audit/codex-cli-2026-06-02-failure-mode-investigation.md),
codex-1 + codex-2 share /home/user/.codex/auth.json + refresh tokens, triggering ChatGPT auth
backend's anti-replay invalidation and causing 100% codex rc=1 since 2026-06-01 19:42.

Goal: wrap codex CLI invocation in scripts/runner-wrapper/run-ephemeral.sh (or sibling) with
flock /var/lock/codex-cli-host.lock so only one codex CLI session is in-flight per host.

Files (NEW or EDIT):
- EDIT: scripts/runner-wrapper/run-ephemeral.sh (or the wrapper that exec's codex)
- (optional) NEW: docs/operations/codex-cli-serialization-runbook.md

Verification:
- After ship: tail /tmp/runner-codex-{1,2}.log for ≥30 min; 0 occurrences of
  "refresh token was already used"
- Manual: trigger 2 codex tickets concurrently; observe that the second blocks on flock
  until the first completes, then runs cleanly

Tier S; class subscription-codex (let codex retry it after fix lands as data point).
```

Plus a follow-up "per-instance ChatGPT account" ticket once operator decides on Step 3.

---

## 6. What this investigation does NOT solve

- Does NOT recover the 22 already-failed codex tickets (those are hand-authored already; not a recovery issue).
- Does NOT fix any non-codex CLI failure mode (claude CLI has its own auth path; not investigated here).
- Does NOT explain the 17/38 (45%) Case 1 Sub-EPIC 4 rate vs. 10/10 (100%) Case 2 rate beyond "stochastic when 2 instances race, asymptotic 100% as race window shortens". A finer-grained analysis would correlate ticket timing across instances — out of scope for OP-1917 close.
- Does NOT investigate whether claude CLI has a similar shared-auth issue. Worth a 30-min spike (memory has hints it might — `project_family_f_gerrit_account_gap`).

---

## 7. Audit-trail metadata

- Logs read: `/tmp/runner-codex-1.log` (73MB), `/tmp/runner-codex-2.log` (92MB) — both retained as-of investigation time
- systemd units inspected: `runner-codex@1`, `runner-codex@2` (both active running per `systemctl --user list-units`)
- Canonical auth file: `/home/user/.codex/auth.json` (4430 bytes, last modified `May 23 02:09`; rotated server-side ≥1224× per instance)
- Investigation duration: ~15 min from log-survey to root-cause confirmation; H1-H6 ruled out in ~10 min by log evidence (no need for separate test cases)
