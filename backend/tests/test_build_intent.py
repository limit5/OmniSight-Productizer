"""W16.3 — Build-intent detection module contract tests.

Locks the public surface of ``backend.web.build_intent`` so the W16.3
coach trigger family stays binding for the orchestrator-chat
integration tests in ``test_invoke_coach_build_intent.py`` and the
W16.4 inline-preview consumer that will eventually consume the
classified ``scaffold_kind`` bucket key.

Coverage axes
─────────────

  §A  Drift guards — every frozen wire-shape constant + scaffold-
      kind enum + trigger prefix + action-keyword partition.
  §B  Detection happy paths — pure CJK, pure Latin, mixed-script,
      multi-word phrases, classifier specificity ordering.
  §C  Detection negative paths — verb-only / subject-only / empty /
      whitespace / false-positive guards (rebuilds / appointment /
      buildbot / pageant) all return zero refs.
  §D  ``BuildIntentRef`` API — ``trigger_key`` + ``scaffold_command``
      shape, frozen-dataclass invariants.
  §E  Hash determinism — same (verb, subject, kind) triple yields
      byte-identical ``intent_hash`` across calls; different triples
      diverge.
  §F  ``classify_subject_to_kind`` — table coverage + fallback.
  §G  Re-export sweep — every public symbol surfaces from the
      ``backend.web`` package.

These tests are PG-free, LLM-free, and fixture-free — pure-function
detection means every test is a single call.
"""

from __future__ import annotations

import re

import pytest

from backend import web as web_pkg
from backend.web import build_intent as bi


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  Drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDriftGuards:

    def test_hash_hex_length_pinned_at_16(self):
        assert bi.BUILD_INTENT_HASH_HEX_LENGTH == 16

    def test_max_build_intents_pinned(self):
        assert bi.MAX_BUILD_INTENTS == 3

    def test_max_display_chars_pinned(self):
        assert bi.MAX_BUILD_INTENT_DISPLAY_CHARS == 80

    def test_kinds_tuple_preserves_row_spec_order(self):
        assert bi.BUILD_INTENT_KINDS == (
            "landing", "site", "page", "app",
        )

    def test_kind_constants_match_tuple(self):
        assert bi.BUILD_INTENT_KIND_LANDING == "landing"
        assert bi.BUILD_INTENT_KIND_SITE == "site"
        assert bi.BUILD_INTENT_KIND_PAGE == "page"
        assert bi.BUILD_INTENT_KIND_APP == "app"

    def test_trigger_prefix_ends_with_colon(self):
        # Required by backend.routers.invoke._detect_coaching_triggers
        # parsing — the splitter assumes ``<prefix>:<payload>``.
        assert bi.BUILD_INTENT_TRIGGER_PREFIX == "build_intent:"

    def test_scaffold_command_pinned(self):
        assert bi.BUILD_INTENT_SCAFFOLD_COMMAND == "/scaffold"

    def test_auto_preview_flag_pinned(self):
        assert bi.BUILD_INTENT_AUTO_PREVIEW_FLAG == "--auto-preview"

    def test_action_keywords_cover_row_spec(self):
        # Row spec literal: 蓋 / 做 / 建 / make / build / create.
        # All six tokens MUST appear in the public tuple.
        for tok in ("蓋", "做", "建", "make", "build", "create"):
            assert tok in bi.BUILD_INTENT_ACTION_KEYWORDS, (
                f"action keyword {tok!r} missing"
            )

    def test_action_keyword_partition_consistency(self):
        # Internal CJK + Latin subsets must concatenate into the
        # public tuple — drift here would silently break the
        # _has_cjk_action_verb / _BUILD_INTENT_ACTION_LATIN_PATTERN
        # fast paths.
        cjk = bi._BUILD_INTENT_ACTION_KEYWORDS_CJK
        latin = bi._BUILD_INTENT_ACTION_KEYWORDS_LATIN
        assert tuple(cjk) + tuple(latin) == bi.BUILD_INTENT_ACTION_KEYWORDS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  Detection happy paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectionHappyPaths:

    def test_pure_cjk_action_plus_subject(self):
        refs = bi.detect_build_intents_in_text("蓋一個網站")
        assert len(refs) == 1
        assert refs[0].verb == "蓋"
        assert refs[0].subject == "網站"
        assert refs[0].scaffold_kind == bi.BUILD_INTENT_KIND_SITE

    def test_pure_latin_action_plus_subject(self):
        refs = bi.detect_build_intents_in_text("build a website please")
        assert len(refs) == 1
        assert refs[0].verb == "build"
        assert refs[0].subject == "website"
        assert refs[0].scaffold_kind == bi.BUILD_INTENT_KIND_SITE

    def test_mixed_script_cjk_verb_latin_subject(self):
        # "幫我蓋一個 landing page" — CJK verb + Latin subject is the
        # most common bilingual operator phrasing.
        refs = bi.detect_build_intents_in_text("幫我蓋一個 landing page")
        assert len(refs) == 1
        assert refs[0].verb == "蓋"
        assert refs[0].subject == "landing page"
        assert refs[0].scaffold_kind == bi.BUILD_INTENT_KIND_LANDING

    def test_mixed_script_latin_verb_cjk_subject(self):
        refs = bi.detect_build_intents_in_text("please build 一個 網站")
        assert len(refs) == 1
        # CJK verb absent, so Latin verb wins.
        assert refs[0].verb == "build"
        # CJK subject present, Latin "site"/"website" absent →
        # CJK subject wins.
        assert refs[0].subject == "網站"
        assert refs[0].scaffold_kind == bi.BUILD_INTENT_KIND_SITE

    def test_multi_word_landing_page_beats_bare_page(self):
        # Specificity ordering: "landing page" must classify as
        # ``landing`` not ``page`` even though both subjects substring-
        # match.
        refs = bi.detect_build_intents_in_text("create a landing page")
        assert len(refs) == 1
        assert refs[0].subject == "landing page"
        assert refs[0].scaffold_kind == bi.BUILD_INTENT_KIND_LANDING

    def test_web_app_classifies_as_app(self):
        refs = bi.detect_build_intents_in_text("make me a web app")
        assert len(refs) == 1
        assert refs[0].subject == "web app"
        assert refs[0].scaffold_kind == bi.BUILD_INTENT_KIND_APP

    def test_bare_app_classifies_as_app(self):
        refs = bi.detect_build_intents_in_text("build an app")
        assert len(refs) == 1
        assert refs[0].subject == "app"
        assert refs[0].scaffold_kind == bi.BUILD_INTENT_KIND_APP

    def test_cjk_landing_zh_tw_classifies_as_landing(self):
        refs = bi.detect_build_intents_in_text("建一個登陸頁")
        assert len(refs) == 1
        assert refs[0].subject == "登陸頁"
        assert refs[0].scaffold_kind == bi.BUILD_INTENT_KIND_LANDING

    def test_cjk_simplified_variants_classify_correctly(self):
        # 简体中文 variants — "网站" / "网页" / "应用".
        for txt, expected_kind in [
            ("做一个网站", bi.BUILD_INTENT_KIND_SITE),
            ("建一个网页", bi.BUILD_INTENT_KIND_PAGE),
            ("做一个应用", bi.BUILD_INTENT_KIND_APP),
        ]:
            refs = bi.detect_build_intents_in_text(txt)
            assert len(refs) == 1, txt
            assert refs[0].scaffold_kind == expected_kind, txt

    def test_make_create_verbs_recognised(self):
        for verb in ("make", "create"):
            refs = bi.detect_build_intents_in_text(f"please {verb} a website")
            assert len(refs) == 1
            assert refs[0].verb == verb

    def test_case_insensitive_latin_match(self):
        refs = bi.detect_build_intents_in_text("BUILD ME A WEBSITE")
        assert len(refs) == 1
        # Verb and subject normalised to lower-case in the ref.
        assert refs[0].verb == "build"
        assert refs[0].subject == "website"

    def test_double_space_between_landing_and_page_still_matches(self):
        # "\s+" in the multi-word subject pattern lets a casual paste
        # with double-space still match.
        refs = bi.detect_build_intents_in_text("build a landing  page")
        assert len(refs) == 1
        assert refs[0].scaffold_kind == bi.BUILD_INTENT_KIND_LANDING


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  Detection negative paths (false-positive / no-match)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectionNegativePaths:

    def test_empty_string(self):
        assert bi.detect_build_intents_in_text("") == []

    def test_none_input(self):
        assert bi.detect_build_intents_in_text(None) == []

    def test_whitespace_only(self):
        assert bi.detect_build_intents_in_text("   \n\t  ") == []

    def test_verb_only_without_subject(self):
        # "I am building confidence" — verb but no subject keyword.
        assert bi.detect_build_intents_in_text("I am building confidence") == []

    def test_subject_only_without_verb(self):
        # "this is a landing page that exists" — subject but no
        # action verb.
        assert bi.detect_build_intents_in_text(
            "this is a landing page that exists",
        ) == []

    def test_appointment_does_not_false_positive(self):
        # "appointment" contains "app" — must not match the bare
        # "app" subject because of the regex word boundary.
        assert bi.detect_build_intents_in_text(
            "I have an appointment to make",
        ) == []

    def test_pageant_does_not_false_positive(self):
        # "pageant" contains "page".
        assert bi.detect_build_intents_in_text(
            "build a pageant celebration",
        ) == []

    def test_buildbot_does_not_false_positive_alone(self):
        # "buildbot" contains "build" but as a sub-token — word
        # boundary should suppress it.
        assert bi.detect_build_intents_in_text("Buildbot is running") == []

    def test_rebuilds_does_not_false_positive(self):
        # "rebuilds" contains "build" as a non-word-boundary substring.
        assert bi.detect_build_intents_in_text(
            "the system rebuilds nightly",
        ) == []

    def test_cjk_verb_only_without_subject(self):
        # 做測試 — CJK verb but no recognised subject.
        assert bi.detect_build_intents_in_text("做一個測試") == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  BuildIntentRef API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildIntentRefApi:

    def test_trigger_key_uses_prefix_and_hash(self):
        ref = bi.detect_build_intents_in_text("蓋一個網站")[0]
        key = ref.trigger_key()
        assert key.startswith(bi.BUILD_INTENT_TRIGGER_PREFIX)
        assert key == f"build_intent:{ref.intent_hash}"

    def test_scaffold_command_includes_kind_and_auto_preview(self):
        ref = bi.detect_build_intents_in_text("build a landing page")[0]
        cmd = ref.scaffold_command()
        assert cmd == "/scaffold landing --auto-preview"

    def test_intent_hash_is_16_hex_chars(self):
        ref = bi.detect_build_intents_in_text("蓋一個網站")[0]
        assert len(ref.intent_hash) == 16
        assert re.fullmatch(r"[0-9a-f]{16}", ref.intent_hash)

    def test_ref_is_frozen_dataclass(self):
        ref = bi.detect_build_intents_in_text("蓋一個網站")[0]
        with pytest.raises(Exception):
            ref.verb = "make"  # frozen → FrozenInstanceError

    def test_module_level_trigger_key_helper_round_trips(self):
        ref = bi.detect_build_intents_in_text("建一個網頁")[0]
        assert bi.build_intent_trigger_key(ref) == ref.trigger_key()

    def test_trigger_keys_for_intents_helper(self):
        cmd = "請幫我蓋一個 landing page"
        refs = bi.detect_build_intents_in_text(cmd)
        keys = bi.trigger_keys_for_build_intents(refs)
        assert keys == [ref.trigger_key() for ref in refs]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  Hash determinism
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHashDeterminism:

    def test_same_intent_yields_same_hash(self):
        a = bi.detect_build_intents_in_text("蓋一個網站")[0]
        b = bi.detect_build_intents_in_text("快幫我蓋一個網站好嗎")[0]
        # Same (verb, subject, kind) → same hash.
        assert a.intent_hash == b.intent_hash

    def test_different_subject_yields_different_hash(self):
        a = bi.detect_build_intents_in_text("蓋一個網站")[0]
        b = bi.detect_build_intents_in_text("蓋一個 landing page")[0]
        assert a.intent_hash != b.intent_hash

    def test_different_verb_yields_different_hash(self):
        a = bi.detect_build_intents_in_text("蓋一個 app")[0]
        b = bi.detect_build_intents_in_text("做一個 app")[0]
        assert a.intent_hash != b.intent_hash


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  classify_subject_to_kind
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClassifySubjectToKind:

    @pytest.mark.parametrize("subject,expected", [
        ("landing page", bi.BUILD_INTENT_KIND_LANDING),
        ("landing", bi.BUILD_INTENT_KIND_LANDING),
        ("登陸頁", bi.BUILD_INTENT_KIND_LANDING),
        ("登陆页", bi.BUILD_INTENT_KIND_LANDING),
        ("website", bi.BUILD_INTENT_KIND_SITE),
        ("site", bi.BUILD_INTENT_KIND_SITE),
        ("網站", bi.BUILD_INTENT_KIND_SITE),
        ("网站", bi.BUILD_INTENT_KIND_SITE),
        ("page", bi.BUILD_INTENT_KIND_PAGE),
        ("網頁", bi.BUILD_INTENT_KIND_PAGE),
        ("頁面", bi.BUILD_INTENT_KIND_PAGE),
        ("app", bi.BUILD_INTENT_KIND_APP),
        ("webapp", bi.BUILD_INTENT_KIND_APP),
        ("web app", bi.BUILD_INTENT_KIND_APP),
        ("應用", bi.BUILD_INTENT_KIND_APP),
        ("应用", bi.BUILD_INTENT_KIND_APP),
    ])
    def test_known_subjects_classify_correctly(self, subject, expected):
        assert bi.classify_subject_to_kind(subject) == expected

    def test_unknown_subject_falls_back_to_page(self):
        # Lowest-blast-radius default — a generic page is the safest
        # scaffold for unrecognised intent.
        assert bi.classify_subject_to_kind("kiosk") == bi.BUILD_INTENT_KIND_PAGE

    def test_empty_subject_falls_back_to_page(self):
        assert bi.classify_subject_to_kind("") == bi.BUILD_INTENT_KIND_PAGE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §G  Re-export sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_W16_3_RE_EXPORTED_SYMBOLS = sorted([
    "BUILD_INTENT_ACTION_KEYWORDS",
    "BUILD_INTENT_AUTO_PREVIEW_FLAG",
    "BUILD_INTENT_HASH_HEX_LENGTH",
    "BUILD_INTENT_KINDS",
    "BUILD_INTENT_KIND_APP",
    "BUILD_INTENT_KIND_LANDING",
    "BUILD_INTENT_KIND_PAGE",
    "BUILD_INTENT_KIND_SITE",
    "BUILD_INTENT_SCAFFOLD_COMMAND",
    "BUILD_INTENT_TRIGGER_PREFIX",
    "BuildIntentRef",
    "MAX_BUILD_INTENTS",
    "MAX_BUILD_INTENT_DISPLAY_CHARS",
    "build_intent_trigger_key",
    "classify_subject_to_kind",
    "detect_build_intents_in_text",
    "trigger_keys_for_build_intents",
])


@pytest.mark.parametrize("symbol", _W16_3_RE_EXPORTED_SYMBOLS)
def test_w16_3_symbol_re_exported_from_package(symbol: str) -> None:
    assert symbol in web_pkg.__all__, (
        f"{symbol} missing from backend.web __all__"
    )
    assert getattr(web_pkg, symbol) is not None


def test_w16_3_re_export_count_pinned_at_17() -> None:
    """W16.3 introduces exactly 17 public symbols.  Drift guard
    ensures a future PR that adds / removes a symbol updates the
    package-level count guard in lockstep.
    """
    assert len(_W16_3_RE_EXPORTED_SYMBOLS) == 17
