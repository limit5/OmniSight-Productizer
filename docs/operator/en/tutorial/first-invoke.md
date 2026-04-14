# Tutorial · First Invoke (10 minutes)

> **source_en:** 2026-04-14 · authoritative

This tutorial walks you from a freshly launched dashboard to your
first **Singularity Sync / Invoke** — the global action that tells the
orchestrator "look at the system, decide what to do next, do it". By
the end you'll recognise every element the AI lit up and you'll know
what to click to intervene.

## Before you start

- Backend on `http://localhost:8000` (or wherever your `BACKEND_URL`
  points). Check with `curl http://localhost:8000/api/v1/health`.
- Frontend on `http://localhost:3000`.
- At least one LLM provider key in `.env`, OR no key (rule-based
  fallback still works, agents just produce template responses).

## 1 · Orient yourself

Open `http://localhost:3000`. The **5-step first-run tour** starts
automatically on a new browser (Skip/Next at the bottom of each card).
When it finishes the dashboard is yours. Glance at the top bar:

- **MODE** pill — currently SUPERVISED by default. That means routine
  AI actions auto-run and risky ones will wait for you.
  [→ details](../reference/operation-modes.md)
- **`?` help icon** next to MODE — click any time you forget what
  something does.
- **Decision Queue** (right-side tile) — empty for now. This is where
  decisions the AI can't auto-execute will land.

## 2 · Pick the simplest possible task

Open the **Orchestrator AI** panel (middle on desktop, swipe to it on
mobile). In the input box type:

```
/invoke list which hardware devices are attached
```

Hit Enter.

## 3 · Watch the pipeline light up

Several things happen in quick succession — this is normal:

1. The **REPORTER VORTEX** log stream on the left prints
   `[INVOKE] singularity_sync: ...`.
2. An agent in the **Agent Matrix** panel turns `active`. Its
   thought-chain updates line by line.
3. One or more **Tool progress** events show file reads / shell calls.
4. In SUPERVISED mode, if the agent proposes anything `risky` or
   `destructive`, a **Toast** pops top-right and the item also lands
   in the **Decision Queue**.

For this "read-only list" invocation, no decision should surface — the
AI just answers in-chat.

## 4 · Read the answer

The orchestrator prints a message back in the panel. You should see a
list of attached devices (may be empty if you're on a dev laptop
without a camera plugged in — that's fine).

## 5 · Try a riskier invoke

```
/invoke create a git branch called tutorial-sandbox in the current workspace
```

This time, in SUPERVISED mode, you should see a **Decision Queue**
entry with severity `risky`. The toast shows A / R / Esc keyboard
hints and a countdown.

- Press **A** (or click APPROVE) — the AI creates the branch.
- Press **R** — the AI stands down.
- Wait the countdown out — resolves to the safe default (usually
  "stand down").

If you don't see a decision, your agent likely auto-executed it
because of a rule or because you changed MODE to FULL_AUTO /
TURBO. Check the `?` help inside the Decision Queue panel for the
severity matrix.

## 6 · Try MANUAL mode

Click the MODE pill → MANUAL. Re-run the branch-create invoke. Now
*every* step enters the Decision Queue, including routine reads. This
is the right mode for "I want to see what the AI is about to do before
it does anything."

Flip back to SUPERVISED when you're done exploring.

## Where to go next

- [Handling a decision](handling-a-decision.md) — the full lifecycle
  of a risky/destructive decision including undo.
- [Operation Modes](../reference/operation-modes.md) — severity × mode
  matrix in detail.
- [Budget Strategies](../reference/budget-strategies.md) — if your
  token bill worried you during this tutorial.
- [Troubleshooting](../troubleshooting.md) — when something didn't
  light up as described.
