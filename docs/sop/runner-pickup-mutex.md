# Runner ticket-pickup mutex — fencing-token claim (AUDIT-24 / OP-977)

**Status**: shipped 2026-05-12, OP-977 (supersedes OP-838)
**Owner**: runner / orchestration on-call
**Module**: `backend/agents/jira_dispatch.py` — `claim_ticket_atomic`,
`release_ticket_claim`, `_lowest_uuid_claim_winner`
**Tests**: `backend/tests/test_atomic_claim_race.py` (7-case race suite),
`backend/tests/test_jira_dispatch_mutex.py` (mechanism unit tests)

## 1. Why this exists

The pickup JQL (`docs/sop/jira-ticket-conventions.md` §16) selects tickets
that are `To Do` AND `assignee is EMPTY`. Between that query and the
`transition_to_in_progress` + assign call there are several seconds of
worktree prep and pre-pickup gates. Two runner processes ticking on
similar wall-clock minutes can both see the same ticket as pickable, both
walk every gate, and both launch a CLI on it in different worktrees. The
consequences observed in production:

- **duplicate Gerrit pushes** — two changes with different subjects, one
  merged + one abandoned (OP-836 #356 / OP-837 #358, 2026-05-11);
- **wrongful revert** — the *loser* finishes second, hits a recovery path
  (e.g. `NoCommitsOnBranchError`) and transitions the ticket back to `To
  Do`, undoing the *winner's* already-Under-Review work (OP-974,
  2026-05-12 16:20–16:45 — operator rescue required);
- wasted runner tokens (2× CLI invocations) every time it fires.

### 1.1 Why OP-838's `claim:{instance_id}` label "mutex" did not fix it

OP-838 added a single label `claim:{instance_id}` written via
`update.labels.add` and read back. It serialises the **cross-bot** shape
(`codex-bot` vs `claude-bot`) because the *assignee* field is single-valued
and last-writer-wins — only one bot account survives the readback. But it
**cannot** serialise two runners that share an `instance_id`:

```
t=0    runner-A: JQL → OP-974 (no claim label)              → candidate
t=0+ε  runner-B: JQL → OP-974 (still no claim label)        → candidate
t=1    runner-A: PUT labels.add = "claim:default"
t=1+ε  runner-B: PUT labels.add = "claim:default"   ← IDEMPOTENT set-union
t=2    runner-A: GET → sees "claim:default" → "I won"
t=2+ε  runner-B: GET → sees "claim:default" → "I won" too
```

Atlassian's `labels.add` is a **set-union**, not a compare-and-swap. Both
PUTs "succeed", both readbacks show the label, both proceed. This is the
canonical "the fix shipped but didn't actually fix the underlying problem"
anti-pattern (see `docs/sop/lessons/L-OP-977-idempotent-label-add-is-not-a-mutex.md`).

## 2. The fencing-token mechanism

Each claim attempt mints a **unique-per-tick fencing token**:

```
token = f"{epoch_us:016d}-{uuid4().hex[:8]}"          # e.g. 0001715518859123456-3af9c1d0
label = f"claim:{instance_id}:{token}"                # e.g. claim:default:0001715518859123456-3af9c1d0
```

- the **16-digit zero-padded microsecond Unix epoch** prefix makes
  *lexicographic* order over tokens equal *chronological* order — so
  "lowest token" == "earliest claimer";
- the **8-hex-char uuid** suffix breaks ties between two runners that mint
  within the same microsecond.

State transitions (`claim_ticket_atomic` → `_claim_ticket_atomic_fenced`):

```
1. pre-GET  /issue/{key}?fields=assignee,labels
     • a *live* foreign-instance fenced claim, or a foreign assignee → return
       ClaimLost(winner=...)  (cheap fast-fail, no write)
     • collect stale fenced tokens + pre-AUDIT-24 bare labels to GC
2. PUT      /issue/{key}
     fields.assignee = bot_account_id
     update.labels   = [ {add: f"claim:{instance_id}:{token}"} ] + [ {remove: l} for l in stale ]
3. post-GET /issue/{key}?fields=assignee,labels
     • retried up to 3× (first immediate, then 200ms apart) until our label
       is visible — Atlassian PUT→GET is occasionally lagged ~100ms
     • if never visible within the budget → ClaimLost("claim-label-missing-from-readback")
4. decide:
     • post-GET assignee != our bot account            → ClaimLost(assignee:<id>)   (cross-bot)
     • a live foreign-instance fenced claim crept in    → ClaimLost(<that label>)
     • winning_token = min( live tokens for our instance )   # _lowest_uuid_claim_winner
       if winning_token != our token                   → ClaimLost(claim:{inst}:{winning_token})
     • else                                            → ClaimWon(token)
```

The key property: the winner is decided by a **total order over the
readback set**, not by "did my idempotent write land". Every runner that
shares `instance_id` runs `min(...)` over the same set of labels and
therefore agrees on exactly one winner. The loser **does not delete its
own label** — it leaves it for GC, so the winner re-running `min(...)`
never flips.

## 3. GC strategy

There are three kinds of leftover claim labels and one invariant:

| Leftover | When it appears | Who removes it |
| --- | --- | --- |
| **Winner's own token** | normal completion | `release_ticket_claim(client, key, instance_id, token)` — removes *every* `claim:{instance_id}:*` (its token + any same-instance loser leftover) + the legacy bare `claim:{instance_id}`, in one GET-then-PUT |
| **Loser's token (same instance)** | a same-instance race | the winner's `release_ticket_claim` GCs it (it matches `claim:{instance_id}:*`); failing that, the next claim's pre-GET stale-sweep once it ages out |
| **Orphan token (crashed runner, any instance)** | runner killed mid-ticket | the next claim's pre-GET removes it once `now - epoch_us > 2 × CLI-timeout` (`OMNISIGHT_RUNNER_STALE_CLAIM_MAX_AGE_S`, default 7200s) — see error catalog `OrphanClaimLabel` |
| **Pre-AUDIT-24 bare `claim:{instance_id}`** | tickets touched before this rollout | treated as *already expired*; removed on the next claim's pre-GET, in the same PUT as the new fenced label — see `BackwardCompatStaleClaim` |

`release_ticket_claim` is **best-effort** — transport failures are logged
and swallowed. The claim label is audit/coordination state, not
load-bearing for correctness: the assignee field is the real "this ticket
is taken" signal (it drops the ticket out of the pickup JQL), and the
stale-sweep bounds label accumulation regardless.

> **Wiring note**: post-CLI `auto-runner-jira.py` claim release shipped
> 2026-05-14 by SP-B-X-003 / OP-1061. The runner releases only after a
> successful claim acquisition and treats pre-claim exits as no-ops.

### 3.1 Single-flag rollback

`OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY=1` reverts `claim_ticket_atomic` to
the OP-838 bare-label path (`_claim_ticket_atomic_legacy`) — an
acknowledged-racey-but-known-working-for-cross-bot baseline. The two label
formats are mutually forward-compatible: legacy code reading a new
`claim:default:0001..-ab12` label sees instance `default` (it never
mistakes the token suffix for a live `instance_id`, so it correctly treats
a *foreign-instance* fenced claim as foreign and skips), and AUDIT-24 code
treats a legacy bare label as expired. So a rollback (or a forward-roll) is
safe even with mixed labels in flight.

### 3.2 The OP-783 1:1 invariant

The deepest residual race is two runners that *both* (a) share an
`instance_id`, (b) mint tokens within the same microsecond, *and* (c) one
of them wedges for >600 ms between its pre-GET and PUT so the other's
window has fully closed. Closing that requires honouring the OP-783
operator invariant: **one bot account ↔ one `instance_id`**. With distinct
bot accounts the single-valued assignee readback (step 4, cross-bot guard)
is the backstop; with distinct `instance_id`s the fenced labels never
collide. Operators MUST NOT run two runner processes with the same
`instance_id` *and* the same bot account.

## 4. Error catalog

| Name | Meaning | Handling |
| --- | --- | --- |
| `MultiClaimDetected` | post-GET shows our token is not the lowest among `claim:{instance_id}:*` | back off — `ClaimLost(<lowest label>)`; the runner logs `[runner-mutex-lost]` and retries next tick. Exported as `RunnerMutexLost` for programmatic callers; the API returns `ClaimResult(ok=False)` rather than raising on the happy path. |
| `OrphanClaimLabel` | a fenced token from a crashed runner outlives it | the next claim's pre-GET removes any `claim:{inst}:{token}` with `now - epoch_us > OMNISIGHT_RUNNER_STALE_CLAIM_MAX_AGE_S × 1e6` (default 7200s = 2× CLI timeout) and proceeds. |
| `BackwardCompatStaleClaim` | a pre-AUDIT-24 bare `claim:{instance_id}` label | treated as expired; removed in the same PUT as the new fenced label. |
| `JIRAPutEventualConsistencyDelay` | PUT returns 200 but the next GET still shows the old labels (~100ms) | post-GET is retried up to `_CLAIM_READBACK_RETRIES` (3) times, `_CLAIM_READBACK_DELAY_S` (200ms) apart, before deciding. Worst-case added latency ≈ 0.4s. If it still doesn't converge → `ClaimLost("claim-label-missing-from-readback")`, retry next tick. |
| `RunnerMutexAPIError` | transport / HTTP failure during the claim sequence | raised (not a `ClaimResult`) so the caller can distinguish "JIRA unreachable" from "lost the race"; `auto-runner-jira.py` converts both to a clean `return 0`. |

## 5. Config knobs

| Env var | Default | Effect |
| --- | --- | --- |
| `OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY` | unset (= `0`) | `1`/`true`/`yes`/non-empty → use the OP-838 bare-label path |
| `OMNISIGHT_RUNNER_STALE_CLAIM_MAX_AGE_S` | `7200` | age past which a fenced token is swept as an orphan |

(`_CLAIM_READBACK_RETRIES` / `_CLAIM_READBACK_DELAY_S` are module constants;
tests monkeypatch the delay to 0.)

## 6. Production verification (AC #5)

After deploy, on the runner host:

1. Pick a low-stakes test ticket in `To Do` with `class:subscription-claude`.
2. Start two `claude` tmux runners **with the same `instance_id`** (the
   pathological config) pointed at it, ticking concurrently.
3. Expect: exactly **one** logs `claim acquired (token: …)` and proceeds to
   `transition_to_in_progress`; the other logs
   `[runner-mutex-lost] … lost claim to claim:<inst>:<lower-token>` and
   exits `0` without invoking the CLI.
4. Confirm only one Gerrit change is produced and the ticket is not
   bounced back to `To Do` by a loser's recovery path.
5. Reset the test ticket; restore the normal 1:1 account↔instance config.

## 7. References

- OP-838 — superseded; same problem, incomplete fix.
- OP-974 incident 2026-05-12 — root-cause evidence + operator-rescue comment.
- OP-836 / OP-837 2026-05-11 — the original duplicate-push incident.
- `docs/sop/jira-ticket-conventions.md` §16 — pickup JQL (unchanged by this fix).
- `docs/sop/architecture-anti-patterns.md` — "fix shipped but didn't fix the root cause".
- `docs/sop/lessons/L-OP-977-idempotent-label-add-is-not-a-mutex.md`.
