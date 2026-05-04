# tools_patch

**Purpose**: Host-side patcher that applies SEARCH/REPLACE blocks and unified diffs to file contents, so the agent can emit small edits instead of regenerating whole files (Phase 67-B S1, "Engine 2" of the lossless acceleration design).

**Key types / public surface**:
- `apply_search_replace_payload(source, raw)` — apply a chain of SEARCH/REPLACE blocks to a string.
- `find_search_replace_match(source, search)` — resolve a SEARCH block through the WP.3.1 cascade.
- `append_diff_validation_confidence_ledger(event)` — append the selected WP.3 cascade layer + confidence score to the N10 ledger without storing patch payload bytes.
- `apply_unified_diff(source, diff)` — apply one file's worth of unified diff to a string.
- `apply_to_file(path, patch_kind, payload)` — convenience wrapper: read, apply, atomic temp-file rename.
- `SearchReplaceBlock`, `CascadeMatch`, `DiffValidationLedgerEvent`, `Hunk` — dataclasses for parsed payloads / match results / ledger rows; `parse_search_replace` exposes parsing.
- Exception hierarchy: `PatchError` → `PatchNotFound`, `PatchAmbiguous`, `PatchMalformed`.

**Key invariants**:
- SEARCH resolution walks the WP.3 cascade: exact match (`1.000`) → indent-agnostic (`0.980`) → prefix-tail rescue (`0.940`) → Jaro-Winkler actual score when ≥ `0.900`. Each layer must produce exactly one match; zero or multiple matches raise rather than guess — silent wrong-occurrence application is the failure mode being prevented.
- `OMNISIGHT_WP_DIFF_VALIDATION_ENABLED=false` disables the fuzzy cascade and keeps Layer 1 exact-match only; the knob is read lazily per call so workers derive the same policy from their process environment.
- Successful file-level SEARCH/REPLACE patches append one N10 `Diff Validation Confidence` row per block, with path, patch kind, selected layer, score, and non-sensitive notes. Raw patch payloads and file contents are never written to the ledger.
- SEARCH blocks need ≥ `MIN_SEARCH_CONTEXT_LINES` (3) non-blank lines; this threshold is "locked by design".
- Line endings are preserved: `apply_unified_diff` sniffs CRLF in the first 4 KB and rejoins with the original terminator, and trailing-newline state is tracked across `splitlines()`.
- Unified-diff hunks are applied last-to-first (so earlier line numbers stay valid), but error messages still cite the original hunk index.
- `apply_to_file` refuses to create new files — new-file creation is explicitly delegated to a separate `create_file` tool.

**Cross-module touchpoints**:
- Pure stdlib (`re`, `dataclasses`, `pathlib`, `logging`); no internal backend imports.
- Intended caller is the Phase 67-B S2 layer that owns atomic writes and tool dispatch — not visible in this module, so the exact call site is unclear from the source alone.
