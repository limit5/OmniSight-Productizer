# Anthropic SDK Upgrade — 2026-05

Purpose: upgrade OmniSight runners to an Anthropic Python SDK that supports
Managed Agents memory stores, outcomes, and the Dreaming research preview
without regressing the existing Messages API and Batch API paths.

## Version Floor

Use `anthropic==0.100.0`.

Evidence:

- `0.97.0` introduced the CMA Memory public beta.
- `0.100.0` introduced Managed Agents outcomes, multiagent support,
  webhooks, and vault validation.
- The Managed Agents beta header is `managed-agents-2026-04-01`; Anthropic's
  Managed Agents docs describe it as enabling agents, sessions, outcomes,
  vaults, credentials, and memory stores.
- Dreaming is available on Claude Managed Agents as a research preview and
  builds on sessions plus memory stores rather than a separate SDK namespace.

Keep the effective ceiling below `1.0.0` until a major-version audit lands.
`langchain-anthropic==1.4.0` already constrains `anthropic<1.0.0,>=0.85.0`.

## Operator Upgrade

1. Confirm the target runner is clean:

   ```bash
   git status --short
   ```

2. Install the locked production and dev dependencies:

   ```bash
   python3 -m pip install --require-hashes -r backend/requirements.txt
   python3 -m pip install --require-hashes -r backend/requirements-dev.txt
   ```

3. Verify the installed SDK version:

   ```bash
   python3 - <<'PY'
   import importlib.metadata
   print(importlib.metadata.version("anthropic"))
   PY
   ```

   Expected output: `0.100.0`.

4. Run the SDK audit gate:

   ```bash
   python3 -m pytest backend/tests/test_sdk_version_pin.py -q
   ```

5. Run the Anthropic client regression tests:

   ```bash
   python3 -m pytest \
     backend/tests/test_anthropic_native_client.py \
     backend/tests/test_batch_client.py \
     backend/tests/test_sdk_auto_escalate.py \
     -q
   ```

6. Exercise one sandbox runner ticket in dry-run mode:

   ```bash
   python3 scripts/run_s1_via_anthropic_sdk.py --dry-run --pilot OP-863
   ```

   This should not spend Anthropic tokens. It validates launcher wiring, cost
   guard setup, and the local tool loop shape.

## Deprecated API Audit

Stable Messages calls remain on:

```python
client.messages.create(...)
client.messages.batches.create(...)
```

Beta feature calls must use:

```python
client.beta.messages.create(...)
```

The OmniSight wrapper now routes requests with beta built-in tools
(`bash_*`, `code_execution_*`, `computer_*`, `memory_*`, `text_editor_*`) or
`mcp_servers` through `client.beta.messages.create()`. Plain text prompts and
standard OmniSight tool schemas continue to use the stable namespace.

Batch submission remains on `client.messages.batches.*` because the Batch API
wraps `messages.create()` params and the repo's batch client does not create
Managed Agents resources directly.

## Multi-Version Compatibility

The CI gate verifies two surfaces:

- SDK N: `0.100.0`, the pinned current version.
- SDK N+1 probe: `0.101.0`, simulated in unit tests by requiring the same
  generated beta namespaces and types.

If a real `0.101.x` install later removes or renames a required namespace,
`SDKBreakingChangeDetected` is the expected failure. In that case:

1. Pin the ceiling to the last known-good version.
2. File a follow-up ticket with the missing namespace or generated type.
3. Do not deploy the upgraded fleet until the wrapper is adapted.

## Error Catalog

`SDKVersionTooOld`

The installed `anthropic` package is below `0.100.0`. Install from the locked
requirements files and re-run `test_sdk_version_pin.py`.

`SDKDeprecatedAPIUsed`

A beta feature was about to use `client.messages.create()` instead of
`client.beta.messages.create()`. Update the request routing in
`backend/agents/anthropic_native_client.py`.

`SDKBreakingChangeDetected`

The SDK is at or above the audited floor but no longer exposes a required
Managed Agents beta namespace or generated type. Pin a ceiling and open a
follow-up migration ticket.

## Rollback

Rollback is configuration-only:

1. Revert the `anthropic` lock change in `backend/requirements.in` and
   `backend/requirements.txt`.
2. Reinstall dependencies from the reverted lockfile.
3. Re-run the SDK gate to confirm the expected failure mode is understood.

Do not remove the audit tests during rollback; they document why the older SDK
cannot satisfy Memory Tool / Outcomes / Dreaming compatibility.
