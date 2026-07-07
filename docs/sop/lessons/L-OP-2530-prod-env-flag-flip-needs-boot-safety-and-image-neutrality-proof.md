---
id: L-OP-2530
ticket: OP-2530
title: Prod env-flag flips need a boot-safety + image-neutrality proof BEFORE the recreate
date: 2026-07-07
tags: [deploy, prod, compose, pydantic, feature-flags, operations]
related_tickets: [OP-1579, OP-1734]
---

# Prod env-flag flips need a boot-safety + image-neutrality proof BEFORE the recreate

**Situation**: Arming P5 supervised execution required adding `OMNISIGHT_P5_EXECUTE=1` to
prod `.env` and force-recreating both backend replicas. Two latent ways this "trivial" flip
could take prod down or silently change it:

1. Backend `Settings` runs `extra='forbid'`. If the new key reached a source pydantic
   *parses* (a dotenv file), boot dies with `extra_forbidden` at collection — the exact trap
   already documented for local pytest (`OMNISIGHT_DOTENV_FILE=.env.test`). Compose
   `env_file: .env` injects the ENTIRE prod `.env` into the container environment, so every
   undeclared key in that file is a candidate landmine.
2. `docker compose up --force-recreate` re-resolves `image:`. If the ref floats on a tag,
   the recreate can silently roll prod onto a different image than the one validated.

**Fix** — the four-step preflight that made the flip provably safe, in order:

1. **Locate the READ path of the flag.** `is_execute_enabled()` reads `os.environ` directly;
   the key is NOT a declared `Settings` field. Only Settings-*parsed* sources can brick boot.
2. **Prove boot-safety empirically, not by argument.** The *running* container already
   carries undeclared keys from the same `env_file` (`COMPOSE_PROJECT_NAME`,
   `OMNISIGHT_IMAGE_TAG`, `OMNISIGHT_REGISTRY`…) while healthy → pydantic-settings ignores
   undeclared `os.environ` keys under `extra='forbid'`. Then close the dotenv hole: verify
   `OMNISIGHT_DOTENV_FILE` is unset in-container AND no `/app/.env` exists — the dotenv
   parser is the one source that *does* reject unknown keys.
3. **Prove the recreate is image-neutral.** `docker compose config` must resolve `image:` to
   the digest-pinned ref (`OMNISIGHT_BACKEND_IMAGE_REF=<repo>@sha256:…`) that equals the
   running container's image. Digest-pinned ⇒ a recreate cannot roll the image.
4. **Roll one replica at a time with a readiness gate**, then verify the flag at the CODE
   predicate (`is_execute_enabled() is True` in-container), not merely `env | grep`.

**Verification**: 2026-07-07 prod flip — `backend-a` then `backend-b` recreated on the
identical digest `sha256:c43a1208…`, each ready in 15 s, public health `online` throughout;
`is_execute_enabled() == True` on both replicas; running image unchanged. Operation record:
`docs/operations/2026-07-07-p5-supervised-execution-first-restart.md`.

**Generalisation**: Before flipping any env-file-delivered runtime flag on prod:
(a) find the flag's read path — `os.environ`-read keys are safe under `extra='forbid'`, while
dotenv-parsed or Settings-declared sources can refuse boot; prove it with the running
container's own env, not reasoning; (b) prove the recreate is image-neutral (digest-pinned
ref; `compose config` == running image) so a config-only change stays config-only; (c) roll
per-replica behind a readiness gate; (d) verify at the code predicate. A flag flip is a
deploy — give it a deploy's preflight.
