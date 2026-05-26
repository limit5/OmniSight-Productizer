---
id: L-OP-1736
ticket: OP-1736
title: Fail-closed CLI gates must self-locate their tools before command -v
date: 2026-05-26
tags: [devops, deploy, tooling]
---

# Fail-closed CLI gates must self-locate their tools before `command -v`

**Situation**: The v0.6.2 promote ran under a clean env
(`env -i PATH=/usr/bin:/bin:/usr/local/bin`). That minimal PATH dropped
`cosign` (installed in `~/bin`), so the inline
`scripts/verify_image_signature.sh` `command -v cosign` check exited 2 and
aborted the promote mid-retag.

**Fix**: OP-1736 made the verifier self-locate cosign before the
`command -v` gate — honour an explicit `$COSIGN_BIN`, else probe the known
install dirs (`~/bin`, `/usr/local/bin`, `~/go/bin`) and prepend the hit to
PATH — while still failing closed (exit 2, never skipping verification) with
a message that names the searched dirs and the `COSIGN_BIN` override. The
release-train runbook dropped the `env -i` clean-env habit in favour of the
normal shell + `OMNISIGHT_TOOLING_TOLERATE_EXTRA_ENV=1` (OP-1702) for the
gate step.

**Verification**: `tests/test_verify_image_signature_sh.py` covers
discovery via `$COSIGN_BIN`, discovery via `~/bin` under a minimal PATH,
a hard-fail (exit 2) when cosign is truly absent, and a hard-fail when
`$COSIGN_BIN` is set but not executable.

**Generalisation**: A `command -v <tool>` gate inherits whatever PATH the
caller hands it, and "clean env" wrappers are a common operator habit, so a
script that fail-closes on a missing tool should self-locate it first. And
before adding an `env -i` wrapper as a workaround, confirm the failure it
"fixes" is real on the path you run — here the clean-env habit was a stale
workaround for a pydantic `extra_forbidden` raise that the promote path
(`promote_image_bundle.py`, which never imports `backend.config`) does not
even hit.
