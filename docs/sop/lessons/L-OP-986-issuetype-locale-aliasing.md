---
id: L-OP-986
ticket: OP-986
title: Locale-dependent display strings are not keys — normalize at the boundary or key on a locale-agnostic ID
date: 2026-05-12
tags: [jira, runner, capability-matrix, i18n, post-mortem]
related_tickets: [OP-855, OP-980, OP-981, OP-985]
---

# Locale-dependent display strings are not keys

**Situation**: OP-855's capability matrix (`config/capability_matrix.yaml`) keys
each `(ticket_type × area × tier)` row on the JIRA issuetype *name* — `Story`,
`Bug`, `Task`. The runner reads `issuetype.name` off the issue fetch and looks it
up. That worked in every test and on the operator's English-locale account, so it
shipped. On 2026-05-12 the first tickets that actually exercised the post-OP-855
`gerrit_push` gate (the AUDIT-26 children OP-980/981/985) failed with
`[runner-capability-blocked] 'gerrit_push' not permitted`. Root cause:
soraapp.atlassian.net is configured in the Japanese display locale, so JIRA
returns `issuetype.name = 'ストーリー'`, not `'Story'`. `entries.get('ストーリー')`
→ `None` → the runner fell back to `read_only_default = [mcp_search,
memory_recall]` → no `code_edit`, no `gerrit_push`, no `jira_update` for *any*
Story-typed pickup. The failure was invisible until then because earlier tickets
either didn't reach the `gerrit_push` code path, carried operator override
labels, or predated the runtime check being wired.

The trap: `issuetype.name` *looks* like a stable identifier in the API response
and in JQL (`issuetype = Story` still works because JQL resolves names
case-/locale-insensitively server-side), but the value returned in the issue
*payload* is the localized display name. A map keyed on it is a map keyed on a
user-facing string that changes per JIRA instance.

**Fix**: Normalize at the boundary, keep one source of truth.
`backend/agents/capability_matrix.py` gained `canonical_issuetype()` plus an
`_ISSUETYPE_ALIASES` table (`ストーリー→Story`, `バグ→Bug`, `タスク→Task`, …);
`CapabilityMatrix._lookup` runs every incoming `ticket_type` through it before
indexing `self.entries`. The YAML stays English-only — adding a locale is a
one-line code change, not a YAML fork. Unmapped names pass through unchanged so a
genuinely missing row still surfaces as `CapabilityMatrixMissingEntry` rather than
being silently rewritten. The per-ticket `capability:enable=*` override labels
applied as the 2026-05-12 20:25 workaround on OP-980/981/985 were removed once the
fix landed (Phase 2).

The fully locale-agnostic option — key the matrix on the numeric `issuetype.id`
(`"10001"` …) — was considered and deferred: it needs an ID-based YAML migration
and a per-instance canary because the ID itself can differ between JIRA instances
(`IssueTypeIDDrift` in OP-986's error catalog). The alias table is the pragmatic
middle: locale-agnostic *enough* for the one instance we have, with the migration
path documented.

**Verification**:
`backend/tests/test_capability_matrix.py::test_resolve_japanese_issuetype_alias_maps_to_story`
pins `resolve('ストーリー', 'backend', 'M') == resolve('Story', 'backend', 'M')`
through both `resolve` and `resolve_for_areas`, and asserts the alias never leaks
into `known_ticket_types()`. `::test_canonical_issuetype_normalizes_known_and_passes_through_unknown`
pins the pass-through semantics. `::test_resolve_other_locales_documented_in_comment`
asserts the `_ISSUETYPE_ALIASES` block catalogues zh-TW/ko/de/fr and names the
`issuetype.id` escape hatch so the next operator extends it correctly.
`backend/tests/test_auto_runner_prompt_builder.py::test_japanese_issuetype_resolves_full_capabilities`
drives `auto-runner-jira.py::_build_prompt` with a mocked
`issuetype.name='ストーリー'` and asserts the resolved set includes `gerrit_push`
and is *not* `read_only_default` — the end-to-end runner integration check.

**Generalisation**: Before using any string from an external system as a map key
or equality token, ask whether it's an *identifier* or a *display value*. JIRA
issuetype/status/priority *names*, OS locale strings, timezone display names, and
HTTP `reason-phrase` text are all display values — they vary by locale, instance
config, or library version. If you must consume the display value, normalize it to
a canonical form at the system boundary (one alias table, one place to extend) and
keep your internal data keyed on the canonical form; better still, find the
locale-agnostic ID the API also exposes (`issuetype.id`, status `id`, IANA tz
name) and key on that. A lookup that returns `None` and quietly degrades to a
"safe" default is the worst shape for this bug: it doesn't crash, it doesn't log
an error loud enough to block release, and the degraded behaviour (read-only) looks
plausible until something downstream needs the capability that got silently
dropped.
