"""W16.5 — Edit-while-preview detection contract tests.

Locks the public surface of ``backend.web.edit_intent`` so the W16.5
``edit_while_preview:<hash16>`` trigger key stays binding for the
:mod:`backend.routers.invoke` planner and the future ``/edit-preview``
slash router.

Coverage axes
─────────────

  §A  Drift guards — every frozen wire-shape constant + cap +
      partition + table sanity invariant.
  §B  Detection happy paths — CJK / Latin / mixed-script / modifier-
      only / verb-only.
  §C  Detection negative paths — no edit verb, no target, false-
      positive guards on word-boundary Latin tokens.
  §D  ``EditIntentRef`` API — ``trigger_key`` / ``slash_command`` /
      hash determinism / dataclass-frozen.
  §E  ``edit_intent_trigger_key`` + ``trigger_keys_for_edit_intents``
      convenience wrappers.
  §F  Re-export sweep — every public symbol surfaces from the
      ``backend.web`` package.

These tests are PG-free, LLM-free, and pure ``str → list``.
"""

from __future__ import annotations

import pytest

from backend import web as web_pkg
from backend.web import edit_intent as ei


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  Drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDriftGuards:

    def test_hash_hex_length_pinned(self):
        assert ei.EDIT_INTENT_HASH_HEX_LENGTH == 16

    def test_max_edit_intents_pinned(self):
        assert ei.MAX_EDIT_INTENTS == 3

    def test_max_display_chars_pinned(self):
        assert ei.MAX_EDIT_INTENT_DISPLAY_CHARS == 80

    def test_trigger_prefix_ends_in_colon(self):
        assert ei.EDIT_INTENT_TRIGGER_PREFIX.endswith(":")
        assert ei.EDIT_INTENT_TRIGGER_PREFIX == "edit_while_preview:"

    def test_slash_command_pinned(self):
        assert ei.EDIT_INTENT_SLASH_COMMAND == "/edit-preview"
        assert ei.EDIT_INTENT_SLASH_COMMAND.startswith("/")

    def test_dry_run_flag_pinned(self):
        assert ei.EDIT_INTENT_DRY_RUN_FLAG == "--dry"
        assert ei.EDIT_INTENT_DRY_RUN_FLAG.startswith("--")

    def test_verb_keywords_non_empty(self):
        assert ei.EDIT_INTENT_VERB_KEYWORDS

    def test_modifier_keywords_non_empty(self):
        assert ei.EDIT_INTENT_MODIFIER_KEYWORDS

    def test_target_keywords_non_empty(self):
        assert ei.EDIT_INTENT_TARGET_KEYWORDS

    def test_target_normalised_lowercase(self):
        # Bucket key downstream consumers branch on must be lower-case
        # ASCII so the FE / slash router can switch on a string
        # constant without locale-folding.
        for (_kw, normalised, _is_cjk) in ei.EDIT_INTENT_TARGET_KEYWORDS:
            assert normalised
            assert normalised == normalised.lower(), normalised


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  Detection happy paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectionHappyPaths:

    def test_modifier_only_cjk_with_latin_target(self):
        # The row-spec example: "header 大一點" — modifier carries the
        # implicit verb ("make X bigger"); target is the Latin "header".
        refs = ei.detect_edit_intents_in_text("header 大一點")
        assert len(refs) == 1
        assert refs[0].trigger == "大一點"
        assert refs[0].target == "header"

    def test_cjk_verb_with_cjk_target(self):
        refs = ei.detect_edit_intents_in_text("改一下標題列")
        assert len(refs) == 1
        assert refs[0].trigger == "改"
        assert refs[0].target == "header"  # 標題列 normalises to header

    def test_cjk_verb_with_latin_target(self):
        refs = ei.detect_edit_intents_in_text("改 button 顏色")
        assert len(refs) == 1
        assert refs[0].trigger == "改"
        assert refs[0].target == "button"

    def test_latin_modifier_with_latin_target(self):
        refs = ei.detect_edit_intents_in_text("make the header bigger")
        assert len(refs) == 1
        assert refs[0].trigger == "bigger"
        assert refs[0].target == "header"

    def test_latin_verb_with_latin_target(self):
        refs = ei.detect_edit_intents_in_text("change the footer color")
        assert len(refs) == 1
        assert refs[0].trigger == "change"
        assert refs[0].target == "footer"

    def test_navbar_classifies_before_nav(self):
        # Multi-word / compound targets are walked first so "navbar"
        # classifies as the bucket "nav" without false-firing as a
        # bare "nav" prefix mid-word.
        refs = ei.detect_edit_intents_in_text("change the navbar background")
        assert len(refs) == 1
        assert refs[0].target == "nav"

    def test_mixed_script_phrasing(self):
        # Bilingual operator: CJK modifier + Latin target.
        refs = ei.detect_edit_intents_in_text("button 顏色換成藍色")
        assert len(refs) == 1
        assert refs[0].target == "button"

    def test_raw_excerpt_preserved(self):
        cmd = "make the hero font bigger please"
        refs = ei.detect_edit_intents_in_text(cmd)
        assert len(refs) == 1
        assert cmd in refs[0].raw_excerpt


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  Detection negative paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectionNegativePaths:

    def test_empty_string_returns_empty(self):
        assert ei.detect_edit_intents_in_text("") == []

    def test_none_returns_empty(self):
        assert ei.detect_edit_intents_in_text(None) == []

    def test_text_without_verb_returns_empty(self):
        # "header" is a valid target, but no edit verb / modifier
        # co-occurs — stays a no-op so the planner doesn't surface a
        # coach card on a passing mention.
        assert ei.detect_edit_intents_in_text("the header looks fine") == []

    def test_text_without_target_returns_empty(self):
        # Edit verb with no UI-element target → no edit intent.
        assert ei.detect_edit_intents_in_text("change my mind") == []

    def test_build_intent_keywords_do_not_false_fire(self):
        # "build me a website" is W16.3 (build_intent), not W16.5
        # (edit_while_preview).  The "make" verb intentionally lives
        # in build_intent's keyword table; we should NOT produce an
        # edit intent here because there is no edit-style target
        # noun.
        assert ei.detect_edit_intents_in_text("build me a website") == []

    def test_word_boundary_blocks_substring_false_positives(self):
        # "fixate" contains "fix" — but the regex word boundary
        # blocks it; "appointment" contains "point" — no false fire;
        # "rebuilds" contains "build" — that's a build verb anyway,
        # not an edit verb.
        for s in (
            "fixate on the design",
            "make an appointment",
            "rebuilds the project",
        ):
            assert ei.detect_edit_intents_in_text(s) == [], s

    def test_no_text_no_intent(self):
        assert ei.detect_edit_intents_in_text("   ") == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  EditIntentRef API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEditIntentRefAPI:

    def test_trigger_key_format(self):
        refs = ei.detect_edit_intents_in_text("header 大一點")
        key = refs[0].trigger_key()
        assert key.startswith(ei.EDIT_INTENT_TRIGGER_PREFIX)
        # Suffix must be the 16-hex hash exactly.
        suffix = key[len(ei.EDIT_INTENT_TRIGGER_PREFIX):]
        assert len(suffix) == ei.EDIT_INTENT_HASH_HEX_LENGTH
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_slash_command_apply(self):
        refs = ei.detect_edit_intents_in_text("header 大一點")
        cmd = refs[0].slash_command("ws-42")
        assert cmd.startswith(ei.EDIT_INTENT_SLASH_COMMAND)
        assert "ws-42" in cmd
        assert "header 大一點" in cmd

    def test_slash_command_dry_run(self):
        refs = ei.detect_edit_intents_in_text("header 大一點")
        cmd = refs[0].slash_command("ws-42", dry_run=True)
        assert cmd.endswith(ei.EDIT_INTENT_DRY_RUN_FLAG)

    def test_hash_determinism_same_pair(self):
        # Re-typing the same edit intent in the same session must
        # produce a byte-identical hash so per-intent suppress works.
        h1 = ei.detect_edit_intents_in_text("header 大一點")[0].edit_hash
        h2 = ei.detect_edit_intents_in_text("header 大一點")[0].edit_hash
        assert h1 == h2

    def test_hash_changes_when_target_changes(self):
        h_header = ei.detect_edit_intents_in_text("header 大一點")[0].edit_hash
        h_footer = ei.detect_edit_intents_in_text("footer 大一點")[0].edit_hash
        assert h_header != h_footer

    def test_hash_changes_when_trigger_changes(self):
        h_bigger = ei.detect_edit_intents_in_text("header 大一點")[0].edit_hash
        h_smaller = ei.detect_edit_intents_in_text("header 小一點")[0].edit_hash
        assert h_bigger != h_smaller

    def test_dataclass_frozen(self):
        ref = ei.detect_edit_intents_in_text("header 大一點")[0]
        with pytest.raises((AttributeError, Exception)):
            ref.target = "footer"  # type: ignore[misc]

    def test_excerpt_truncation(self):
        long_text = "header 大一點 " + "x" * 200
        refs = ei.detect_edit_intents_in_text(long_text)
        assert len(refs) == 1
        assert len(refs[0].raw_excerpt) <= ei.MAX_EDIT_INTENT_DISPLAY_CHARS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  Convenience wrappers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConvenienceWrappers:

    def test_edit_intent_trigger_key_round_trip(self):
        ref = ei.detect_edit_intents_in_text("header 大一點")[0]
        assert ei.edit_intent_trigger_key(ref) == ref.trigger_key()

    def test_trigger_keys_for_edit_intents_preserves_order(self):
        ref_a = ei.detect_edit_intents_in_text("header 大一點")[0]
        ref_b = ei.detect_edit_intents_in_text("footer 顏色換")[0]
        keys = ei.trigger_keys_for_edit_intents([ref_a, ref_b])
        assert keys == [ref_a.trigger_key(), ref_b.trigger_key()]

    def test_trigger_keys_for_edit_intents_empty(self):
        assert ei.trigger_keys_for_edit_intents([]) == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  Re-export sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_W16_5_EDIT_INTENT_SYMBOLS = (
    "EDIT_INTENT_DRY_RUN_FLAG",
    "EDIT_INTENT_HASH_HEX_LENGTH",
    "EDIT_INTENT_MODIFIER_KEYWORDS",
    "EDIT_INTENT_SLASH_COMMAND",
    "EDIT_INTENT_TARGET_KEYWORDS",
    "EDIT_INTENT_TRIGGER_PREFIX",
    "EDIT_INTENT_VERB_KEYWORDS",
    "EditIntentRef",
    "MAX_EDIT_INTENTS",
    "MAX_EDIT_INTENT_DISPLAY_CHARS",
    "detect_edit_intents_in_text",
    "edit_intent_trigger_key",
    "trigger_keys_for_edit_intents",
)


@pytest.mark.parametrize("symbol", _W16_5_EDIT_INTENT_SYMBOLS)
def test_w16_5_edit_intent_symbol_re_exported(symbol: str) -> None:
    assert symbol in web_pkg.__all__, f"{symbol} missing from backend.web.__all__"
    assert getattr(web_pkg, symbol) is getattr(ei, symbol)


def test_w16_5_edit_intent_module_all_count() -> None:
    assert len(ei.__all__) == len(_W16_5_EDIT_INTENT_SYMBOLS)
    assert set(ei.__all__) == set(_W16_5_EDIT_INTENT_SYMBOLS)


def test_total_re_export_count_matches_w16_5_baseline() -> None:
    # Bumped from 345 (W16.4 baseline) → 374 (W16.5 +13 edit_intent
    # +16 preview_hmr_reload).  Drift here means the W16 epic
    # added/removed surface without the lock-step bump landing across
    # all neighbour test files.
    assert len(web_pkg.__all__) == 374
