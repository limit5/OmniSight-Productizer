"""W16.2 — Image-attachment coaching trigger integration tests.

Locks the W16.2 wiring in ``backend/routers/invoke.py`` that detects
inline ``data:image/<mime>;base64,…`` pastes and ``[image: <name>]``
upload markers in the operator's INVOKE command and turns them into
the three-option coach menu (component / 整頁 / brand reference)
backed by the vision-LLM-driven downstream agents.

Coverage axes
─────────────

  §A  ``_detect_coaching_triggers`` emits one
      ``image_in_message:<hash16>`` trigger per detected attachment
      (paste-order preserved), honours per-attachment ``suppress``,
      and stays a no-op for image-free commands.
  §B  ``_image_in_message_hashes`` round-trips the hashes out of the
      trigger key (sibling to ``_url_in_message_urls``).
  §C  ``_build_templated_coach_message`` renders the 1-of-1 image
      menu with the three bilingual options + slash commands carrying
      the hash, and overrides the legacy ``empty_workspace`` framing
      because the operator declared intent by attaching.
  §D  ``_build_templated_coach_message`` renders the multi-image menu
      under per-image sub-headings while keeping the three options
      for each, falling back to a generic label when the planner-
      forwarded ``image_refs`` is empty.
  §E  ``_build_templated_coach_message`` keeps the
      ``missing_toolchain`` banner first when both fire, appends the
      image menu as a secondary section, and still appends the
      stale-PEP reminder.
  §F  ``_build_templated_coach_message`` renders the URL menu first
      and the image menu second when both URL + image triggers
      co-fire — operators usually paste a URL as the canonical
      source and a screenshot as supporting reference.
  §G  ``_build_coach_context`` (LLM-driven path) hands the LLM a
      single image bullet that pre-renders the slash commands so the
      LLM never has to invent the syntax.
  §H  ``_resolve_image_label_for_trigger`` falls back gracefully
      when ``image_refs`` is missing / mismatched (defensive against
      drift).

These tests are PG-free and LLM-free — the planner / coach-renderer
helpers under test are all pure functions.

Module-global / cross-worker state audit (per
docs/sop/implement_phase_step.md Step 1): the W16.2 trigger detection
relies on :mod:`backend.web.image_attachment` whose drift guard
already pins the frozen contract; the planner forwards refs through
the action-dict (no module-level cache), so cross-worker concern is
N/A (Answer #1).
"""

from __future__ import annotations

from backend.routers import invoke as inv
from backend.web import image_attachment as ia


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


def _detect_first_image_hash(command: str) -> str:
    refs = ia.detect_image_attachments_in_text(command)
    assert refs, "test fixture should produce at least one image"
    return refs[0].image_hash


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  _detect_coaching_triggers — emit / suppress / no-op
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectCoachingTriggersImage:

    def test_marker_in_command_emits_trigger(self):
        cmd = "look at this design [image: hero-mock.png] and tell me"
        h = _detect_first_image_hash(cmd)
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=cmd,
        )
        assert f"image_in_message:{h}" in triggers

    def test_data_url_in_command_emits_trigger(self):
        cmd = "see data:image/png;base64,SAMPLE for the layout"
        h = _detect_first_image_hash(cmd)
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=cmd,
        )
        assert f"image_in_message:{h}" in triggers

    def test_image_free_command_emits_no_image_trigger(self):
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command="just a text command",
        )
        assert all(
            not t.startswith(ia.IMAGE_COACH_TRIGGER_PREFIX) for t in triggers
        )

    def test_per_attachment_suppress(self):
        # Operator dismissed a specific image earlier — re-emitting
        # that exact hash must NOT re-coach. A fresh image in the same
        # command still fires because suppress is per-hash, not per-
        # trigger-family.
        cmd = "compare [image: a.png] against [image: b.png] please"
        refs = ia.detect_image_attachments_in_text(cmd)
        assert len(refs) == 2
        hash_a, hash_b = refs[0].image_hash, refs[1].image_hash
        suppress = frozenset({f"image_in_message:{hash_a}"})
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), suppress, command=cmd,
        )
        assert f"image_in_message:{hash_a}" not in triggers
        assert f"image_in_message:{hash_b}" in triggers

    def test_multiple_images_emit_in_paste_order(self):
        cmd = (
            "ref [image: alpha.png] then "
            "data:image/jpeg;base64,BBBB and "
            "[image: charlie.png]"
        )
        refs = ia.detect_image_attachments_in_text(cmd)
        assert len(refs) == 3
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=cmd,
        )
        image_triggers = [
            t for t in triggers if t.startswith(ia.IMAGE_COACH_TRIGGER_PREFIX)
        ]
        assert image_triggers == [
            f"image_in_message:{r.image_hash}" for r in refs
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  _image_in_message_hashes — extract helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestImageInMessageHashes:

    def test_extract_hashes_preserves_order(self):
        triggers = [
            "empty_workspace",
            "image_in_message:aaaaaaaaaaaaaaaa",
            "url_in_message:https://example.com",
            "image_in_message:bbbbbbbbbbbbbbbb",
        ]
        out = inv._image_in_message_hashes(triggers)
        assert out == [
            "aaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbb",
        ]

    def test_no_image_triggers_returns_empty(self):
        assert inv._image_in_message_hashes(
            ["empty_workspace", "stale_pep"],
        ) == []

    def test_blank_hash_skipped(self):
        # Defensive — a malformed trigger key with no hash payload
        # should not produce an empty bullet.
        out = inv._image_in_message_hashes([
            "image_in_message:",
            "image_in_message:VALID0123456789",
        ])
        assert out == ["VALID0123456789"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  _build_templated_coach_message — single image leads
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachImageSingle:

    def test_single_image_overrides_empty_workspace_with_three_options(self):
        # Image menu lands BETWEEN missing_toolchain and empty_workspace
        # in priority. With only empty_workspace + image co-firing,
        # the image menu leads and the empty-workspace prompts are
        # skipped.
        ref = ia.detect_image_attachments_in_text(
            "[image: hero.png]",
        )[0]
        msg = inv._build_templated_coach_message(
            triggers=[
                "empty_workspace",
                f"image_in_message:{ref.image_hash}",
            ],
            pending_count=0,
            image_refs=[ref],
        )
        # All three bilingual options must be rendered as bullets.
        assert "(a) 元件 / Component" in msg
        assert "(b) 整頁 / Full page" in msg
        assert "(c) 品牌參考 / Brand reference" in msg
        # Slash commands carry the hash — operators copy-paste the
        # bullet into the chat.
        assert f"/clone-image {ref.image_hash} --as=component" in msg
        assert f"/clone-image {ref.image_hash} --as=page" in msg
        assert f"/brand-image {ref.image_hash}" in msg
        # Operator-facing label appears in the headline.
        assert "hero.png" in msg
        # empty_workspace framing must NOT appear.
        assert "工作台是空的喔" not in msg
        assert "/tour" not in msg

    def test_single_image_without_refs_falls_back_to_generic_label(self):
        # When image_refs is omitted (e.g. legacy caller / unit test),
        # the renderer must fall back to a generic label rather than
        # crash or silently emit ``image_in_message:<hash>`` verbatim.
        msg = inv._build_templated_coach_message(
            triggers=["image_in_message:DEAD0000BEEF1111"],
            pending_count=0,
        )
        # Label gracefully degrades to "image".
        assert "image" in msg.lower()
        # The hash still appears in slash commands.
        assert "/clone-image DEAD0000BEEF1111 --as=component" in msg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  _build_templated_coach_message — multi-image menu
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachImageMulti:

    def test_multi_image_emits_per_image_subheading_and_options(self):
        cmd = "[image: alpha.png] and [image: beta.png]"
        refs = ia.detect_image_attachments_in_text(cmd)
        ha, hb = refs[0].image_hash, refs[1].image_hash
        msg = inv._build_templated_coach_message(
            triggers=[
                f"image_in_message:{ha}",
                f"image_in_message:{hb}",
            ],
            pending_count=0,
            image_refs=refs,
        )
        # Sub-heading per image with the operator-facing label.
        assert "Image #1: alpha.png" in msg
        assert "Image #2: beta.png" in msg
        # Each image gets its own slash-command bullets.
        assert msg.count(f"/clone-image {ha} --as=component") == 1
        assert msg.count(f"/clone-image {hb} --as=component") == 1
        assert msg.count(f"/clone-image {ha} --as=page") == 1
        assert msg.count(f"/clone-image {hb} --as=page") == 1
        assert msg.count(f"/brand-image {ha}") == 1
        assert msg.count(f"/brand-image {hb}") == 1
        # Brand-reference option appears once per image block.
        assert msg.count("(c) 品牌參考 / Brand reference") == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  _build_templated_coach_message — toolchain leads, image trails
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachImageWithToolchain:

    def test_missing_toolchain_leads_image_appended_pep_reminder(self):
        ref = ia.detect_image_attachments_in_text("[image: hero.png]")[0]
        msg = inv._build_templated_coach_message(
            triggers=[
                "stale_pep",
                "missing_toolchain:nodejs-lts-20",
                f"image_in_message:{ref.image_hash}",
            ],
            pending_count=2,
            image_refs=[ref],
        )
        # Toolchain headline first.
        nodejs_idx = msg.find("Node.js LTS 20")
        image_intro_idx = msg.find("裝完 toolchain 之後，你貼的圖片")
        assert 0 <= nodejs_idx < image_intro_idx, (
            "Node.js display name must appear before the image appendix intro"
        )
        # Image menu appears with full slash commands.
        assert f"/clone-image {ref.image_hash} --as=component" in msg
        assert f"/clone-image {ref.image_hash} --as=page" in msg
        assert f"/brand-image {ref.image_hash}" in msg
        # PEP reminder still rendered as ONE additional line.
        reminder_lines = [
            line for line in msg.splitlines()
            if "PEP HOLD" in line and "2" in line
        ]
        assert len(reminder_lines) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  _build_templated_coach_message — URL leads, image trails
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachUrlPlusImage:

    def test_url_first_image_second_with_separator(self):
        ref = ia.detect_image_attachments_in_text(
            "[image: support.png]",
        )[0]
        msg = inv._build_templated_coach_message(
            triggers=[
                "url_in_message:https://example.com/landing",
                f"image_in_message:{ref.image_hash}",
            ],
            pending_count=0,
            image_refs=[ref],
        )
        url_idx = msg.find("/clone https://example.com/landing")
        image_intro_idx = msg.find("另外，你貼的圖片也可以走 vision-LLM")
        image_cmd_idx = msg.find(
            f"/clone-image {ref.image_hash} --as=component",
        )
        # URL menu rendered FIRST (canonical source).
        assert url_idx >= 0
        # Image menu rendered SECOND (supporting reference) with intro.
        assert image_intro_idx > url_idx
        assert image_cmd_idx > image_intro_idx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §G  _build_coach_context — LLM-driven path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildCoachContextImage:

    def test_image_block_pre_renders_three_slash_commands(self):
        ref = ia.detect_image_attachments_in_text("[image: design.png]")[0]
        block = inv._build_coach_context(
            triggers=[f"image_in_message:{ref.image_hash}"],
            pending_count=0,
            image_refs=[ref],
        )
        # The LLM must see the full slash-command syntax for each
        # capability; otherwise it has to invent the command names.
        assert f"/clone-image {ref.image_hash} --as=component" in block
        assert f"/clone-image {ref.image_hash} --as=page" in block
        assert f"/brand-image {ref.image_hash}" in block
        # Operator-facing label rendered for context.
        assert "design.png" in block
        # Hash + kind both rendered so the LLM can correlate.
        assert ref.image_hash in block
        assert ref.kind in block

    def test_image_block_without_refs_falls_back_to_generic(self):
        # No image_refs forwarded → the context still renders, just
        # with a generic ``image`` label and no ``kind`` mismatch.
        block = inv._build_coach_context(
            triggers=["image_in_message:0123456789ABCDEF"],
            pending_count=0,
        )
        assert "/clone-image 0123456789ABCDEF --as=component" in block
        assert "image" in block.lower()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §H  _resolve_image_label_for_trigger — graceful degradation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResolveImageLabelForTrigger:

    def test_match_returns_label_and_kind(self):
        ref = ia.detect_image_attachments_in_text("[image: hero.png]")[0]
        label, kind = inv._resolve_image_label_for_trigger(
            ref.image_hash, [ref],
        )
        assert label == "hero.png"
        assert kind == ia.IMAGE_REF_KIND_MARKER

    def test_no_match_falls_back_to_generic(self):
        ref = ia.detect_image_attachments_in_text("[image: hero.png]")[0]
        label, kind = inv._resolve_image_label_for_trigger(
            "NOTPRESENT0000000", [ref],
        )
        assert label == "image"
        assert kind == "image_attachment"

    def test_none_refs_falls_back_to_generic(self):
        label, kind = inv._resolve_image_label_for_trigger(
            "anyhash", None,
        )
        assert label == "image"
        assert kind == "image_attachment"

    def test_empty_refs_falls_back_to_generic(self):
        label, kind = inv._resolve_image_label_for_trigger(
            "anyhash", [],
        )
        assert label == "image"
        assert kind == "image_attachment"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Coach prompt mentions the image trigger family (drift guard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCoachSystemPromptAwareness:

    def test_system_prompt_describes_image_trigger(self):
        # Drift guard — if a future PR renames the trigger key without
        # updating the persona prompt, the LLM will receive the trigger
        # but have no instructions on how to render it.
        assert "image_in_message:<hash16>" in inv._COACH_SYSTEM_PROMPT
        assert "/clone-image" in inv._COACH_SYSTEM_PROMPT
        assert "/brand-image" in inv._COACH_SYSTEM_PROMPT

    def test_system_prompt_pins_priority_relative_to_url(self):
        # Drift guard for the W16.2 priority rule — image trails URL
        # when both fire.
        assert "image_in_message" in inv._COACH_SYSTEM_PROMPT
        # The priority paragraph must mention the URL/image co-fire
        # ordering rule.
        assert (
            "URL menu first" in inv._COACH_SYSTEM_PROMPT
            or "image menu second" in inv._COACH_SYSTEM_PROMPT
        )
