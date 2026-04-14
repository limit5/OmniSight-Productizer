---
name: patch_protocol
description: >-
  Code-modification protocol fragment. Bootstraps into prompt_versions
  at startup (Phase 56-DAG-C S3) and can be canary-promoted via
  prompt_registry. Consumers that need to edit existing files should
  prepend this fragment to their system prompt.
schema_version: 1
---

## ⚡ Code Modification Protocol

You must NOT re-emit a full source file when you want to change a few
lines. Wasting output tokens invites hallucination and slows every
turn. Use the dedicated tools:

- `create_file(path, content)` — for **new** files only. Uncapped.
- `patch_file(path, patch_kind, payload)` — for **existing** files.
  `patch_kind ∈ {"search_replace", "unified_diff"}`.
- `write_file(path, content)` — **legacy**. Still accepted for
  first-time writes. For *overwriting existing files* it refuses
  when the new body exceeds `OMNISIGHT_PATCH_MAX_INLINE_LINES`
  (default 50).

### SEARCH / REPLACE format

```python
<<<<<<< SEARCH
    def init_gpio(pin_number):
        # Initialize the hardware pin
        setup_pin(pin_number, MODE_IN)
=======
    def init_gpio(pin_number):
        # Initialize the hardware pin with Pull-Up resistor
        setup_pin(pin_number, MODE_IN, PULL_UP)
        verify_pin_state(pin_number)
>>>>>>> REPLACE
```

Hard requirements (enforced server-side):

1. SEARCH must carry **≥ 3 non-blank lines of context**. One-line
   SEARCH blocks are rejected — almost no real source file has a
   unique single line.
2. SEARCH must match the file **exactly once**. Zero matches →
   `PatchNotFound`. Multiple matches → `PatchAmbiguous`. Add more
   surrounding context to disambiguate.
3. Line endings are preserved — don't normalise CRLF to LF, the
   patcher does that for you.

Multiple SEARCH/REPLACE blocks per payload are allowed; they apply in
order, each against the **result** of the previous block.

### Unified diff alternative

```
--- a/src/driver.c
+++ b/src/driver.c
@@ -42,3 +42,3 @@
 // context line
-return init_bus(BUS_I2C);
+return init_bus(BUS_I2C | BUS_FAST_MODE);
 // context line
```

Use this when you already have a tool that produces unified diffs
(e.g. from `git diff`). Otherwise prefer SEARCH/REPLACE — it's easier
to emit correctly.

### What happens if you fail the protocol

- Rejected `write_file` calls feed the IIS quality window
  (Phase 63-A). Repeat violations push the agent to L1 calibrate
  (re-inject this fragment) and eventually L2 route (switch model).
- Patch failures (`[PATCH-FAILED]`) also count. If you see one, fix
  the SEARCH context and retry — don't fall back to `write_file` on
  the existing file, you'll be rejected again.
