"""W16.5 — Edit-while-preview coaching trigger integration tests.

Locks the W16.5 wiring in ``backend/routers/invoke.py`` that detects
edit verbs / modifiers + UI-element targets in the operator's INVOKE
command (``"header 大一點"``, ``"改 button 顏色"``, ``"make the hero
font bigger"``) and surfaces the three-option apply menu (apply now /
dry-run / chat) backed by the ``/edit-preview`` slash command.

Coverage axes
─────────────

  §A  ``_detect_coaching_triggers`` emits one
      ``edit_while_preview:<hash16>`` trigger when an edit verb /
      modifier + target co-occur, honours per-intent ``suppress``,
      and stays a no-op for edit-free commands.
  §B  ``_edit_in_message_hashes`` round-trips the hashes out of the
      trigger key (sibling to ``_build_intent_in_message_hashes``).
  §C  ``_build_templated_coach_message`` renders the 1-of-1 apply
      menu with the three bilingual options + the operator's raw
      excerpt threaded into the slash command, and overrides the
      legacy ``empty_workspace`` framing because the operator
      declared edit intent.
  §D  ``_build_templated_coach_message`` keeps the
      ``missing_toolchain`` banner first when both fire, appends the
      edit menu as a final section.
  §E  ``_build_templated_coach_message`` renders the edit menu first
      and the build_intent menu second when both edit + build_intent
      triggers co-fire — edit signal is the most direct.
  §F  ``_build_coach_context`` (LLM-driven path) hands the LLM a
      single edit_while_preview bullet that pre-renders the slash
      commands so the LLM never has to invent the syntax.
  §G  ``_resolve_edit_intent_for_trigger`` falls back gracefully when
      ``edit_intent_refs`` is missing / mismatched.
  §H  Coach system prompt mentions the edit_while_preview trigger
      family (drift guard).

These tests are PG-free and LLM-free — the helpers under test are
all pure functions.

Module-global / cross-worker state audit (per
docs/sop/implement_phase_step.md Step 1): the W16.5 trigger detection
relies on :mod:`backend.web.edit_intent` whose drift guard already
pins the frozen contract; the planner forwards refs through the
action-dict (no module-level cache), so cross-worker concern is N/A
(Answer #1).
"""

from __future__ import annotations

from backend.routers import invoke as inv
from backend.web import edit_intent as ei


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _empty_state(installed=frozenset()):
    return {
        "agents": [],
        "tasks": [],
        "running_agents": [],
        "idle_agents": [],
        "installed_entries": installed,
    }


def _detect_first_edit_hash(command: str) -> str:
    refs = ei.detect_edit_intents_in_text(command)
    assert refs, "test fixture should produce at least one edit intent"
    return refs[0].edit_hash


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  _detect_coaching_triggers — emit / suppress / no-op
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectCoachingTriggersEditIntent:

    def test_modifier_only_intent_emits_trigger(self):
        cmd = "header 大一點"
        h = _detect_first_edit_hash(cmd)
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=cmd,
        )
        assert f"edit_while_preview:{h}" in triggers

    def test_cjk_verb_intent_emits_trigger(self):
        cmd = "改 button 顏色"
        h = _detect_first_edit_hash(cmd)
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=cmd,
        )
        assert f"edit_while_preview:{h}" in triggers

    def test_latin_verb_intent_emits_trigger(self):
        cmd = "change the footer color"
        h = _detect_first_edit_hash(cmd)
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=cmd,
        )
        assert f"edit_while_preview:{h}" in triggers

    def test_edit_free_command_emits_no_edit_trigger(self):
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command="just a text command",
        )
        assert all(
            not t.startswith(ei.EDIT_INTENT_TRIGGER_PREFIX)
            for t in triggers
        )

    def test_per_edit_suppress(self):
        # Operator dismissed this edit intent earlier → re-emitting
        # must NOT re-coach.  A different intent in the same session
        # still fires.
        cmd = "header 大一點"
        h = _detect_first_edit_hash(cmd)
        suppress = frozenset({f"edit_while_preview:{h}"})
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), suppress, command=cmd,
        )
        assert f"edit_while_preview:{h}" not in triggers

    def test_command_with_url_does_not_suppress_edit_intent(self):
        # Operator pasted a URL AND said "改 button" — both triggers
        # should fire (planner picks one to lead via the priority
        # chain in the coach renderer).
        cmd = "改 the button on https://example.com"
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=cmd,
        )
        url_count = sum(1 for t in triggers if t.startswith("url_in_message:"))
        edit_count = sum(
            1 for t in triggers if t.startswith(ei.EDIT_INTENT_TRIGGER_PREFIX)
        )
        assert url_count >= 1
        assert edit_count == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  _edit_in_message_hashes — extract helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEditInMessageHashes:

    def test_extract_hashes_preserves_order(self):
        triggers = [
            "empty_workspace",
            "edit_while_preview:aaaaaaaaaaaaaaaa",
            "url_in_message:https://example.com",
            "edit_while_preview:bbbbbbbbbbbbbbbb",
        ]
        out = inv._edit_in_message_hashes(triggers)
        assert out == ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]

    def test_no_edit_triggers_returns_empty(self):
        assert inv._edit_in_message_hashes(
            ["empty_workspace", "stale_pep"],
        ) == []

    def test_blank_hash_skipped(self):
        out = inv._edit_in_message_hashes([
            "edit_while_preview:",
            "edit_while_preview:VALID0123456789",
        ])
        assert out == ["VALID0123456789"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  _build_templated_coach_message — single intent leads
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachEditIntentSingle:

    def test_single_edit_overrides_empty_workspace_with_three_options(self):
        ref = ei.detect_edit_intents_in_text("header 大一點")[0]
        msg = inv._build_templated_coach_message(
            triggers=[
                "empty_workspace",
                f"edit_while_preview:{ref.edit_hash}",
            ],
            pending_count=0,
            edit_intent_refs=[ref],
        )
        # All three bilingual apply options must render as bullets.
        assert "(a) 直接套用 / Apply now" in msg
        assert "(b) 預覽影響範圍 / Dry-run" in msg
        assert "(c) 改用對話 / Send to chat" in msg
        # Apply slash command carries the verbatim excerpt.
        assert "/edit-preview <workspace_id>" in msg
        assert "header 大一點" in msg
        # Dry-run flag rendered.
        assert "--dry" in msg
        # empty_workspace framing must NOT appear.
        assert "/tour" not in msg
        assert "工作台是空的喔" not in msg

    def test_single_edit_without_refs_falls_back_to_generic(self):
        # When edit_intent_refs is omitted (legacy caller / unit
        # test), the renderer must fall back to a generic placeholder
        # rather than crash.
        msg = inv._build_templated_coach_message(
            triggers=["edit_while_preview:DEAD0000BEEF1111"],
            pending_count=0,
        )
        # Slash commands still rendered.
        assert "/edit-preview <workspace_id>" in msg
        assert "--dry" in msg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  _build_templated_coach_message — toolchain leads, edit trails
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachEditWithToolchain:

    def test_missing_toolchain_leads_edit_appended(self):
        ref = ei.detect_edit_intents_in_text("header 大一點")[0]
        msg = inv._build_templated_coach_message(
            triggers=[
                "missing_toolchain:nodejs-lts-20",
                f"edit_while_preview:{ref.edit_hash}",
            ],
            pending_count=0,
            edit_intent_refs=[ref],
        )
        nodejs_idx = msg.find("Node.js LTS 20")
        edit_intro_idx = msg.find(
            "裝完 toolchain 之後，你想改的 UI 也可以一鍵 apply",
        )
        assert 0 <= nodejs_idx < edit_intro_idx, (
            "Node.js display name must appear before the edit appendix intro"
        )
        # Apply menu rendered with full slash command + verbatim excerpt.
        assert "/edit-preview <workspace_id>" in msg
        assert "header 大一點" in msg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  _build_templated_coach_message — edit leads when build_intent co-fires
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachEditPlusBuildIntent:

    def test_edit_first_build_intent_second_with_separator(self):
        from backend.web import build_intent as bi
        edit_ref = ei.detect_edit_intents_in_text("header 大一點")[0]
        # Force a co-firing build intent in the same message.
        build_ref = bi.detect_build_intents_in_text("蓋一個 landing page")[0]
        msg = inv._build_templated_coach_message(
            triggers=[
                f"edit_while_preview:{edit_ref.edit_hash}",
                f"build_intent:{build_ref.intent_hash}",
            ],
            pending_count=0,
            edit_intent_refs=[edit_ref],
            build_intent_refs=[build_ref],
        )
        edit_cmd_idx = msg.find("/edit-preview <workspace_id>")
        build_intro_idx = msg.find("或者你想從零 scaffold 一個全新的")
        build_cmd_idx = msg.find("/scaffold landing --auto-preview")
        # Edit menu rendered FIRST (most direct intent signal).
        assert edit_cmd_idx >= 0
        # Scaffold menu rendered SECOND with intro separator.
        assert build_intro_idx > edit_cmd_idx
        assert build_cmd_idx > build_intro_idx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  _build_coach_context — LLM-driven path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildCoachContextEditIntent:

    def test_edit_block_pre_renders_slash_commands(self):
        ref = ei.detect_edit_intents_in_text("header 大一點")[0]
        block = inv._build_coach_context(
            triggers=[f"edit_while_preview:{ref.edit_hash}"],
            pending_count=0,
            edit_intent_refs=[ref],
        )
        # The LLM must see the full slash-command syntax + dry-run
        # variant so it never has to invent the names.
        assert "/edit-preview <workspace_id>" in block
        assert "--dry" in block
        # Trigger keyword + target + raw excerpt all surface.
        assert ref.trigger in block
        assert ref.target in block
        assert ref.raw_excerpt in block
        # Hash rendered for correlation.
        assert ref.edit_hash in block

    def test_edit_block_without_refs_falls_back_to_generic(self):
        block = inv._build_coach_context(
            triggers=["edit_while_preview:0123456789ABCDEF"],
            pending_count=0,
        )
        assert "/edit-preview <workspace_id>" in block
        # Hash still surfaces.
        assert "0123456789ABCDEF" in block


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §G  _resolve_edit_intent_for_trigger — graceful degradation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResolveEditIntentForTrigger:

    def test_match_returns_trigger_target_excerpt(self):
        ref = ei.detect_edit_intents_in_text("header 大一點")[0]
        trigger_kw, target, excerpt = inv._resolve_edit_intent_for_trigger(
            ref.edit_hash, [ref],
        )
        assert trigger_kw == "大一點"
        assert target == "header"
        assert "header 大一點" in excerpt

    def test_no_match_falls_back(self):
        ref = ei.detect_edit_intents_in_text("header 大一點")[0]
        trigger_kw, target, excerpt = inv._resolve_edit_intent_for_trigger(
            "NOTPRESENT0000000", [ref],
        )
        assert trigger_kw == "edit"
        assert target == "ui"
        assert excerpt == "edit ui"

    def test_none_refs_falls_back(self):
        trigger_kw, target, excerpt = inv._resolve_edit_intent_for_trigger(
            "anyhash", None,
        )
        assert trigger_kw == "edit"
        assert target == "ui"
        assert excerpt == "edit ui"

    def test_empty_refs_falls_back(self):
        trigger_kw, target, excerpt = inv._resolve_edit_intent_for_trigger(
            "anyhash", [],
        )
        assert trigger_kw == "edit"
        assert target == "ui"
        assert excerpt == "edit ui"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §H  Coach prompt mentions the edit_while_preview trigger family
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCoachSystemPromptAwareness:

    def test_system_prompt_describes_edit_trigger(self):
        # Drift guard — if a future PR renames the trigger key or
        # slash-command shape without updating the persona prompt,
        # the LLM will receive the trigger with no instructions on
        # how to render it.
        assert "edit_while_preview:<hash16>" in inv._COACH_SYSTEM_PROMPT
        assert "/edit-preview" in inv._COACH_SYSTEM_PROMPT
        assert "vite HMR" in inv._COACH_SYSTEM_PROMPT

    def test_system_prompt_pins_priority_relative_to_other_intents(self):
        # Drift guard for the W16.5 priority rule — edit_while_preview
        # leads when present even with URL / image / build_intent.
        assert "edit_while_preview" in inv._COACH_SYSTEM_PROMPT
        # Priority paragraph must mention that edit leads.
        assert (
            "render the edit menu FIRST" in inv._COACH_SYSTEM_PROMPT
            or "most direct" in inv._COACH_SYSTEM_PROMPT
        )
