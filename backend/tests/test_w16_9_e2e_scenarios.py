"""W16.9 — End-to-end scenario coverage for the W16 orchestrator-chat
auto-integration epic.

Where this row slots in
-----------------------

W16.1–W16.8 each shipped a *unit-test slab* pinning a single module's
public surface plus a *near-neighbour test* threading the planner /
emission helper into one consumer.  W16.9 is the integration layer:
five synthetic operator journeys exercise the modules together so a
future regression in *any one* W16.x module surfaces as a red e2e
case rather than as a silent consumer-side break that only a manual
smoke would catch.

Five row-spec scenarios (from the TODO.md W16.9 row):

  1. **純 URL 克隆** — operator pastes ``http(s)://...`` →
     ``_detect_coaching_triggers`` fires ``url_in_message:<url>`` →
     coach surfaces clone / brand / screenshot / skip menu →
     downstream W11.7 produces a ``CloneManifest`` →
     :func:`register_reference_from_clone_manifest` writes
     ``.omnisight/references/index.json`` →
     :func:`render_reference_attachment_context` renders the
     ``## Reference Attachments`` block → the next agent invocation
     prepends the block to ``handoff_ctx`` via
     :func:`backend.routers.invoke._load_reference_attachment_context`.

  2. **純 image 設計** — operator pastes ``[image: hero.png]`` (or an
     inline ``data:image/...`` URL) →
     ``_detect_coaching_triggers`` fires ``image_in_message:<hash16>``
     → ``_plan_actions`` stashes the :class:`ImageAttachmentRef` on
     the coach action → :func:`generate_layout_spec_for_image` runs
     the vision LLM (test fake) and returns a :class:`LayoutSpec` →
     :func:`register_reference_from_layout_spec` writes the
     standalone payload + index row →
     :func:`render_reference_attachment_context` renders the layout
     spec headline.

  3. **build intent** — operator types "蓋一個 landing page" →
     ``_detect_coaching_triggers`` fires ``build_intent:<hash16>``
     with ``scaffold_kind="landing"`` → after scaffolding completes
     :func:`emit_preview_ready` publishes ``preview.ready`` (mounts
     the chat iframe via W16.4) → :func:`emit_preview_next_steps`
     publishes ``preview.next_steps`` (W16.7 four-option coach card
     pre-recommending the Vercel deploy slash command).

  4. **edit live** — preview iframe already mounted → operator types
     "header 大一點" → ``_detect_coaching_triggers`` fires
     ``edit_while_preview:<hash16>`` → coach renders the
     ``/edit-preview`` slash command → after the agent edits the
     source file, vite HMR fires →
     :func:`emit_preview_hmr_reload` publishes ``preview.hmr_reload``
     → the chat-message renderer for ``previewHmrReload`` carries
     the matching ``editHash`` so the FE re-mounts only the matching
     iframe.

  5. **error 自修** — preview running → vite plugin reports a build
     error → :func:`preview_vite_error_payload_from_history_entry`
     projects the W15.2 history entry to a payload →
     :func:`emit_preview_vite_error` publishes
     ``preview.vite_error`` (status=detected, amber chat trace card)
     → simulated W15.6 self-fix loop runs → the same helper projects
     a ``status=resolved`` payload → :func:`emit_preview_vite_error`
     publishes ``preview.vite_error_resolved`` (emerald flip on the
     same card via ``error_signature`` correlation).

Coverage philosophy
-------------------

These tests are the integration glue between the W16.x unit tests.
They are deliberately PG-free, docker-free, and SSE-bus-mocked: the
goal is to assert "the modules speak the same wire shape end-to-end",
not to spin up a real preview sandbox.  Live smoke remains the
operator's job per the W16.x dossiers' "deployed-inactive" gate.

Module-global / cross-worker state audit (per
docs/sop/implement_phase_step.md Step 1): every helper exercised here
is a pure projection or a frozen-dataclass constructor — no mutable
module-level state, no DB pool, no asyncio.gather race surface.  The
SSE emission tests monkeypatch :meth:`backend.events.EventBus.publish`
to avoid coupling to the bus's cross-worker delivery path.  Answer #1
applies (every uvicorn worker reads the same constants from the same
git checkout).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.routers import invoke as inv
from backend.web import (
    BUILD_INTENT_AUTO_PREVIEW_FLAG,
    BUILD_INTENT_KIND_LANDING,
    BUILD_INTENT_SCAFFOLD_COMMAND,
    BUILD_INTENT_TRIGGER_PREFIX,
    EDIT_INTENT_SLASH_COMMAND,
    EDIT_INTENT_TRIGGER_PREFIX,
    IMAGE_COACH_TRIGGER_PREFIX,
    IMAGE_REF_KIND_MARKER,
    LayoutSpec,
    PREVIEW_HMR_RELOAD_DEFAULT_LABEL,
    PREVIEW_HMR_RELOAD_EVENT_NAME,
    PREVIEW_NEXT_STEPS_EVENT_NAME,
    PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND,
    PREVIEW_NEXT_STEP_KINDS,
    PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY,
    PREVIEW_READY_EVENT_NAME,
    PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL,
    PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME,
    PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME,
    PREVIEW_VITE_ERROR_STATUS_DETECTED,
    PREVIEW_VITE_ERROR_STATUS_RESOLVED,
    REFERENCE_ATTACHMENT_DIR_NAME,
    REFERENCE_ATTACHMENT_INDEX_FILENAME,
    REFERENCE_ATTACHMENT_SUBDIR,
    REFERENCE_KIND_CLONE,
    REFERENCE_KIND_IMAGE,
    REFERENCE_KIND_SCREENSHOT,
    build_chat_message_for_preview_hmr_reload,
    build_chat_message_for_preview_next_steps,
    build_chat_message_for_preview_ready,
    build_chat_message_for_preview_vite_error,
    build_default_next_step_options,
    build_preview_hmr_reload_payload,
    build_preview_next_steps_payload,
    build_preview_ready_payload,
    detect_build_intents_in_text,
    detect_edit_intents_in_text,
    detect_image_attachments_in_text,
    emit_preview_hmr_reload,
    emit_preview_next_steps,
    emit_preview_ready,
    emit_preview_vite_error,
    generate_layout_spec_for_image,
    list_reference_attachments,
    preview_vite_error_payload_from_history_entry,
    register_reference_from_clone_manifest,
    register_reference_from_layout_spec,
    resolve_reference_index_path,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — fixtures, fakes, capture utilities
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _empty_state(installed: frozenset[str] | None = None) -> dict[str, Any]:
    """Build a minimal ``state`` dict the way ``_plan_actions`` /
    ``_detect_coaching_triggers`` consume it.

    Mirrors the helper used by the W16.1 / W16.2 / W16.3 / W16.5
    invoke-coach unit tests so the e2e cases assemble the planner
    inputs the same way without reaching into the real INVOKE handler
    (which would pull a PG round-trip for ``installed_entries``).
    """
    state: dict[str, Any] = {
        "agents": [],
        "tasks": [],
        "running_agents": [],
        "idle_agents": [],
    }
    if installed is not None:
        state["installed_entries"] = installed
    return state


class _PublishCapture:
    """Records every captured ``EventBus.publish`` call in order.  The
    underlying bus would otherwise broadcast via Redis Pub/Sub when
    configured — that path is unrelated to the e2e contracts under
    test, so the tests stub it out via ``monkeypatch.setattr``.

    Stored as a plain holder (no ``__call__``) because the actual bus-
    method replacement is a normal ``def`` that Python's descriptor
    protocol binds as a method when accessed via the instance; an
    object-with-``__call__`` would *not* receive the implicit ``self``
    so the bound-method shape would silently break the call site.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def event_names(self) -> list[str]:
        return [evt for evt, _data, _kw in self.events]

    def first(self, name: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        for entry in self.events:
            if entry[0] == name:
                return entry
        raise AssertionError(
            f"event {name!r} not captured (got {self.event_names()})"
        )


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _PublishCapture:
    """Patch :class:`EventBus.publish` for one test, return the capture
    so the test can grep recorded events by name."""
    from backend import events as events_mod

    cap = _PublishCapture()

    def _fake_publish(
        bus_self: Any,
        event: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        # Copy so subsequent ``setdefault`` mutations inside
        # ``EventBus.publish`` don't bleed into the capture.
        cap.events.append((event, dict(data), dict(kwargs)))

    monkeypatch.setattr(
        events_mod.EventBus, "publish", _fake_publish, raising=True,
    )
    return cap


def _clone_manifest_fixture(*, source_url: str, title: str) -> dict[str, Any]:
    """Hand-build the W11.7 :func:`manifest_to_dict` shape that
    :func:`register_reference_from_clone_manifest` consumes.  The shape
    is pinned by ``backend.web.clone_manifest.manifest_to_dict`` so this
    fixture stays in lock-step with the W11.7 module.
    """
    return {
        "manifest_version": "1",
        "clone_id": "clone-w16-9-e2e",
        "created_at": "2026-05-03T00:00:00Z",
        "tenant_id": "tenant-e2e",
        "actor": "operator-e2e",
        "source": {"url": source_url, "captured_at": "2026-05-03T00:00:00Z"},
        "classification": {"risk_level": "low"},
        "transformation": {"strategy": "default"},
        "transformed_summary": {
            "title": title,
            "nav_count": 4,
            "section_count": 3,
            "image_count": 2,
        },
        "defense_layers": {},
        "attribution": "operator-e2e",
        "manifest_hash": "0" * 16,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scenario 1 — 純 URL 克隆 (pure URL clone)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScenarioPureUrlClone:
    """End-to-end URL → clone → reference → agent prompt cycle.

    The scenario asserts that the W16.1 detector, the W16.8 reference
    helper, and the orchestrator's reference-loader speak the same
    wire shape: pasting a URL into INVOKE produces a coach trigger
    *and* the downstream clone produces an index row that the next
    agent invocation can read.
    """

    URL = "https://acme.example/landing"

    def test_url_paste_emits_coach_trigger_and_clone_menu(self) -> None:
        # 1) Operator pastes a URL into the chat.
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=f"please clone {self.URL}",
        )
        # 2) W16.1 fires the per-URL trigger key — full URL preserved
        #    so the downstream slash command does not 4xx the W11
        #    router.
        assert f"url_in_message:{self.URL}" in triggers

        # 3) The coach surfaces the four-option bilingual menu with the
        #    full slash commands.  This is the operator-facing rendering
        #    that happens inside _plan_actions's priority-4 coach
        #    branch.
        msg = inv._build_templated_coach_message(
            triggers=triggers, pending_count=0,
        )
        assert "(a) 克隆網站 / Clone" in msg
        assert "(b) 抽取品牌風格 / Extract brand" in msg
        assert "(c) 多斷點截圖 / Screenshot" in msg
        assert "(d) 不用 / Skip" in msg
        # The /clone slash carries the full URL — operators copy/paste
        # the bullet into the chat and the W11 router does the rest.
        assert f"/clone {self.URL}" in msg

    def test_clone_completion_persists_reference_for_next_agent(
        self, tmp_path: Path,
    ) -> None:
        # 1) Simulate the downstream W11.7 clone landing on disk.  The
        #    W11.7 module would write ``.omnisight/clone-manifest.json``
        #    + return a CloneManifest dict; here we hand-build the dict
        #    so the test does not need a live cloner.
        manifest = _clone_manifest_fixture(
            source_url=self.URL, title="Acme Landing",
        )
        attachment = register_reference_from_clone_manifest(
            project_root=tmp_path, manifest=manifest,
        )
        assert attachment.kind == REFERENCE_KIND_CLONE
        assert attachment.source_url == self.URL
        # Default payload_path mirrors W11.7's pinned filename so the
        # index points at the existing artefact (no payload duplication).
        assert attachment.payload_path == "clone-manifest.json"

        # 2) The on-disk index lives at the W16.8 frozen relative path.
        index_path = resolve_reference_index_path(tmp_path)
        assert index_path == (
            tmp_path
            / REFERENCE_ATTACHMENT_DIR_NAME
            / REFERENCE_ATTACHMENT_SUBDIR
            / REFERENCE_ATTACHMENT_INDEX_FILENAME
        )
        assert index_path.exists()

        # 3) The next agent invocation calls into
        #    _load_reference_attachment_context(workspace_path) which
        #    in turn calls render_reference_attachment_context.  The
        #    rendered block carries the pinned heading + the clone
        #    summary so the agent prompts know what reference is
        #    attached.
        block = inv._load_reference_attachment_context(str(tmp_path))
        assert "## Reference Attachments" in block
        assert "[clone] Cloned page 'Acme Landing'" in block
        # Source URL is threaded into the bullet so the agent can
        # dereference if it wants the live URL again.
        assert self.URL in block

    def test_idempotent_re_register_keeps_one_row(
        self, tmp_path: Path,
    ) -> None:
        # Two back-to-back clones of the same URL should land in a
        # single row (re-clones replace, not append) — the operator
        # otherwise sees a growing index full of duplicates.  Keys on
        # the deterministic ref_id derived from kind+source_url.
        manifest = _clone_manifest_fixture(
            source_url=self.URL, title="Acme Landing",
        )
        first = register_reference_from_clone_manifest(
            project_root=tmp_path, manifest=manifest,
        )
        second = register_reference_from_clone_manifest(
            project_root=tmp_path, manifest=manifest,
        )
        assert first.ref_id == second.ref_id
        rows = list_reference_attachments(tmp_path)
        assert len(rows) == 1
        assert rows[0].ref_id == first.ref_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scenario 2 — 純 image 設計 (pure image design)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _FakeVisionLLM:
    """Minimal vision-LLM stub conforming to the
    ``generate_layout_spec(ref) -> LayoutSpec`` test-fake protocol that
    :func:`generate_layout_spec_for_image` accepts.

    Returns a deterministic spec keyed off the ref's image_hash so the
    e2e assertions can pin the round-trip from detection → spec →
    reference index.
    """

    def __init__(self, *, summary: str) -> None:
        self.summary = summary
        self.invocations: list[str] = []

    def generate_layout_spec(self, ref: Any) -> LayoutSpec:
        self.invocations.append(ref.image_hash)
        return LayoutSpec(
            image_hash=ref.image_hash,
            summary=self.summary,
            components=("Header", "Hero", "Pricing", "Footer"),
            colors=("#0a0a0a", "#ffffff"),
            fonts=("Inter",),
            raw_text="",
            degraded=False,
        )


class TestScenarioPureImageDesign:
    """End-to-end image paste → vision LLM → reference → agent prompt
    cycle.
    """

    COMMAND = "design please [image: hero.png]"

    def test_image_paste_emits_image_trigger_and_layout_spec(self) -> None:
        # 1) Operator pastes an image marker.  Detection finds one ref
        #    with kind=marker and a stable 16-hex image_hash so the
        #    suppress key is deterministic.
        refs = detect_image_attachments_in_text(self.COMMAND)
        assert len(refs) == 1
        assert refs[0].kind == IMAGE_REF_KIND_MARKER
        # The image_hash drives the trigger key; W16.2 pins a 16-hex
        # prefix so the FE's sessionStorage suppress key stays bounded.
        assert len(refs[0].image_hash) == 16

        # 2) The same detection runs inside the planner; the trigger
        #    keys carry the image hash so the operator can dismiss a
        #    specific paste without silencing the next one.
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=self.COMMAND,
        )
        assert any(
            t.startswith(IMAGE_COACH_TRIGGER_PREFIX) for t in triggers
        )

        # 3) The vision LLM (test fake here) produces the LayoutSpec
        #    the coach card surfaces and the reference module persists.
        fake = _FakeVisionLLM(summary="A login form with header & footer")
        spec = generate_layout_spec_for_image(refs[0], vision_llm=fake)
        assert spec.degraded is False
        assert spec.summary == "A login form with header & footer"
        assert fake.invocations == [refs[0].image_hash]

    def test_layout_spec_persists_as_reference_for_next_agent(
        self, tmp_path: Path,
    ) -> None:
        # 1) Run the W16.2 detection + vision-LLM pipeline.
        refs = detect_image_attachments_in_text(self.COMMAND)
        spec = generate_layout_spec_for_image(
            refs[0],
            vision_llm=_FakeVisionLLM(
                summary="A login form with header & footer",
            ),
        )

        # 2) Persist the layout spec via the W16.8 factory helper.  The
        #    helper writes a standalone payload blob under
        #    ``references/<ref_id>.json`` because the W12 spec has no
        #    sibling on-disk artefact (unlike W11.7 / W13.3 which the
        #    other two factory helpers point at).
        layout_dict: dict[str, Any] = {
            "image_hash": spec.image_hash,
            "summary": spec.summary,
            "components": list(spec.components),
            "colors": list(spec.colors),
            "fonts": list(spec.fonts),
        }
        attachment = register_reference_from_layout_spec(
            project_root=tmp_path,
            layout_spec=layout_dict,
            source_url=None,
        )
        assert attachment.kind == REFERENCE_KIND_IMAGE

        # 3) The standalone payload was written under the W16.8 owned
        #    blob path.  The W16.9 row spec calls out
        #    ``references/<ref_id>.json`` so the assertion pins both
        #    the directory and the filename derived from ref_id.
        payload_file = (
            tmp_path
            / REFERENCE_ATTACHMENT_DIR_NAME
            / REFERENCE_ATTACHMENT_SUBDIR
            / f"{attachment.ref_id}.json"
        )
        assert payload_file.exists()
        decoded = json.loads(payload_file.read_text(encoding="utf-8"))
        assert decoded["image_hash"] == spec.image_hash
        assert decoded["summary"] == spec.summary

        # 4) The agent prompt context carries the headline so the
        #    agent edits with the operator's pasted image in scope.
        block = inv._load_reference_attachment_context(str(tmp_path))
        assert "## Reference Attachments" in block
        assert "[image] Pasted image: A login form" in block

    def test_re_paste_same_image_does_not_duplicate(
        self, tmp_path: Path,
    ) -> None:
        # The W16.8 ref_id is derived from the W16.2 image_hash so a
        # re-paste of the same screenshot lands in a single index row.
        refs = detect_image_attachments_in_text(self.COMMAND)
        spec = generate_layout_spec_for_image(
            refs[0],
            vision_llm=_FakeVisionLLM(summary="Headline"),
        )
        layout_dict: dict[str, Any] = {
            "image_hash": spec.image_hash,
            "summary": spec.summary,
            "components": list(spec.components),
            "colors": list(spec.colors),
            "fonts": list(spec.fonts),
        }
        first = register_reference_from_layout_spec(
            project_root=tmp_path, layout_spec=layout_dict,
        )
        second = register_reference_from_layout_spec(
            project_root=tmp_path, layout_spec=layout_dict,
        )
        assert first.ref_id == second.ref_id
        assert len(list_reference_attachments(tmp_path)) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scenario 3 — build intent → scaffold → preview-ready / next-steps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScenarioBuildIntent:
    """End-to-end "蓋一個 landing page" → scaffold → preview iframe →
    next-step coach card cycle.
    """

    COMMAND = "幫我蓋一個 landing page"
    WORKSPACE_ID = "ws-w16-9-build"
    PREVIEW_URL = "https://preview-w16-9-build.example.com"

    def test_build_intent_classifies_to_landing_kind(self) -> None:
        # 1) The W16.3 detector pairs the CJK action verb (蓋) with
        #    the Latin subject phrase ("landing page") and routes the
        #    intent to the canonical "landing" scaffold kind.
        intents = detect_build_intents_in_text(self.COMMAND)
        assert len(intents) == 1
        assert intents[0].scaffold_kind == BUILD_INTENT_KIND_LANDING

        # 2) The trigger key carries the 16-hex SHA-256 prefix the FE
        #    suppress system uses.
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=self.COMMAND,
        )
        intent_keys = [
            t for t in triggers if t.startswith(BUILD_INTENT_TRIGGER_PREFIX)
        ]
        assert len(intent_keys) == 1
        assert intent_keys[0] == intents[0].trigger_key()

        # 3) The intent's slash command embeds the scaffold kind + the
        #    --auto-preview flag so the downstream router auto-launches
        #    W14 live preview after scaffolding.  The literals are
        #    pinned by W16.3 drift guards.
        slash = intents[0].scaffold_command()
        assert slash == (
            f"{BUILD_INTENT_SCAFFOLD_COMMAND} {BUILD_INTENT_KIND_LANDING} "
            f"{BUILD_INTENT_AUTO_PREVIEW_FLAG}"
        )

    def test_preview_ready_then_next_steps_emit_in_order(
        self, capture: _PublishCapture,
    ) -> None:
        # 1) After scaffolding, the W14.1 sidecar finishes booting and
        #    the operator-tier ``POST /web-sandbox/preview/{ws}/ready``
        #    handler emits ``preview.ready`` (W16.4) + ``preview.
        #    next_steps`` (W16.7) back-to-back.  We exercise the two
        #    emission helpers directly so the test does not need the
        #    docker-bound router.
        emit_preview_ready(
            workspace_id=self.WORKSPACE_ID,
            preview_url=self.PREVIEW_URL,
            broadcast_scope="session",
        )
        emit_preview_next_steps(
            workspace_id=self.WORKSPACE_ID,
            preview_url=self.PREVIEW_URL,
            broadcast_scope="session",
        )

        # 2) The events surface in row-spec order: ready first (mounts
        #    iframe), next_steps second (coaches what to do).
        names = capture.event_names()
        assert names == [
            PREVIEW_READY_EVENT_NAME,
            PREVIEW_NEXT_STEPS_EVENT_NAME,
        ]

        # 3) The ready payload carries the workspace + URL so the FE
        #    iframe mounts to the right chat thread.
        _, ready_data, _ = capture.first(PREVIEW_READY_EVENT_NAME)
        assert ready_data["workspace_id"] == self.WORKSPACE_ID
        assert ready_data["preview_url"] == self.PREVIEW_URL

        # 4) The next_steps payload pre-fills four bilingual options +
        #    pre-recommends Vercel deploy (the row-spec default).
        _, next_data, _ = capture.first(PREVIEW_NEXT_STEPS_EVENT_NAME)
        assert next_data["workspace_id"] == self.WORKSPACE_ID
        opt_kinds = [opt["kind"] for opt in next_data["options"]]
        assert tuple(opt_kinds) == PREVIEW_NEXT_STEP_KINDS
        recommended = [
            opt for opt in next_data["options"]
            if opt.get("recommended") is True
        ]
        assert len(recommended) == 1
        assert recommended[0]["kind"] == PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND
        assert recommended[0]["kind"] == PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY

    def test_chat_messages_carry_iframe_and_next_steps(self) -> None:
        # Round-trip via the chat-message builders so the SSE consumer
        # path matches: ``previewEmbed`` (mount iframe, W16.4) +
        # ``previewNextSteps`` (coach card, W16.7).
        ready_payload = build_preview_ready_payload(
            workspace_id=self.WORKSPACE_ID,
            preview_url=self.PREVIEW_URL,
        )
        ready_msg = build_chat_message_for_preview_ready(
            ready_payload, message_id="m-ready",
        )
        assert ready_msg["previewEmbed"]["url"] == self.PREVIEW_URL
        assert ready_msg["previewEmbed"]["workspaceId"] == self.WORKSPACE_ID

        steps_payload = build_preview_next_steps_payload(
            workspace_id=self.WORKSPACE_ID,
            options=build_default_next_step_options(
                workspace_id=self.WORKSPACE_ID,
            ),
            preview_url=self.PREVIEW_URL,
        )
        steps_msg = build_chat_message_for_preview_next_steps(
            steps_payload, message_id="m-next",
        )
        next_steps_field = steps_msg["previewNextSteps"]
        assert next_steps_field["workspaceId"] == self.WORKSPACE_ID
        # The slash command on each option carries the workspace_id
        # threaded in so the operator's composer pre-fills with a
        # ready-to-fire command.
        for opt in next_steps_field["options"]:
            assert self.WORKSPACE_ID in opt["slashCommand"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scenario 4 — edit live (header 大一點 → /edit-preview → HMR reload)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScenarioEditLive:

    COMMAND = "header 大一點"
    WORKSPACE_ID = "ws-w16-9-edit"
    SOURCE_PATH = "components/Header.tsx"

    def test_edit_intent_emits_trigger_and_slash_command(self) -> None:
        # 1) The W16.5 detector pairs the Latin target ("header") with
        #    the CJK modifier ("大一點") and produces a stable hash.
        intents = detect_edit_intents_in_text(self.COMMAND)
        assert len(intents) == 1
        intent = intents[0]
        assert intent.target == "header"
        assert intent.trigger == "大一點"
        assert len(intent.edit_hash) == 16

        # 2) The planner forwards the trigger key into the coach
        #    branch.
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command=self.COMMAND,
        )
        edit_keys = [
            t for t in triggers if t.startswith(EDIT_INTENT_TRIGGER_PREFIX)
        ]
        assert edit_keys == [intent.trigger_key()]

        # 3) The slash command is copy-paste-ready: includes the
        #    workspace_id and quotes the operator's verbatim instruction.
        slash = intent.slash_command(self.WORKSPACE_ID)
        assert slash.startswith(EDIT_INTENT_SLASH_COMMAND)
        assert self.WORKSPACE_ID in slash
        assert '"header 大一點"' in slash

    def test_hmr_reload_event_carries_edit_hash_for_correlation(
        self, capture: _PublishCapture,
    ) -> None:
        # 1) Run the W16.5 detector to get the edit_hash (the FE
        #    correlates the chat coach card with the eventual
        #    "Preview updated" message via this hash).
        intent = detect_edit_intents_in_text(self.COMMAND)[0]

        # 2) After the agent edit pipeline writes the file, vite HMR
        #    fires; the consumer-side hook would post
        #    ``preview.hmr_reload`` with the matching edit_hash.
        emit_preview_hmr_reload(
            workspace_id=self.WORKSPACE_ID,
            label="Preview updated: header bigger",
            change_kind="update",
            source_path=self.SOURCE_PATH,
            edit_hash=intent.edit_hash,
            broadcast_scope="session",
        )

        # 3) The captured event speaks the W16.5 wire shape.
        assert capture.event_names() == [PREVIEW_HMR_RELOAD_EVENT_NAME]
        _, data, _ = capture.first(PREVIEW_HMR_RELOAD_EVENT_NAME)
        assert data["workspace_id"] == self.WORKSPACE_ID
        assert data["change_kind"] == "update"
        assert data["source_path"] == self.SOURCE_PATH
        assert data["edit_hash"] == intent.edit_hash
        # Optional fields stay absent when omitted (drift guard for
        # the wire shape's tightness).
        assert "ingress_url" not in data

    def test_chat_message_for_hmr_reload_carries_camelCase_field(
        self,
    ) -> None:
        intent = detect_edit_intents_in_text(self.COMMAND)[0]
        payload = build_preview_hmr_reload_payload(
            workspace_id=self.WORKSPACE_ID,
            label=PREVIEW_HMR_RELOAD_DEFAULT_LABEL,
            source_path=self.SOURCE_PATH,
            edit_hash=intent.edit_hash,
        )
        msg = build_chat_message_for_preview_hmr_reload(
            payload, message_id="m-hmr",
        )
        # Sibling field to W16.4's ``previewEmbed``: the FE
        # ChatPreviewEmbed component picks up the matching workspaceId
        # and bumps its iframe-reload counter rather than mounting a
        # fresh iframe.
        embed = msg["previewHmrReload"]
        assert embed["workspaceId"] == self.WORKSPACE_ID
        assert embed["editHash"] == intent.edit_hash
        assert embed["sourcePath"] == self.SOURCE_PATH


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scenario 5 — error 自修 (vite error → 正在修 → 已修 ✓)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScenarioErrorSelfFix:

    WORKSPACE_ID = "ws-w16-9-err"
    HISTORY_ENTRY = (
        "vite[transform] src/Header.tsx:42: compile: "
        "Failed to parse module"
    )

    def test_history_entry_projects_to_detected_payload_and_event(
        self, capture: _PublishCapture,
    ) -> None:
        # 1) The W15.1 ingest endpoint receives a vite-plugin error
        #    line; the W15.2 history formatter normalises it into the
        #    ``vite[<phase>] <file>:<line>: <kind>: <message>`` shape
        #    that W16.6's projection helper accepts.
        payload = preview_vite_error_payload_from_history_entry(
            self.HISTORY_ENTRY,
            workspace_id=self.WORKSPACE_ID,
        )
        assert payload is not None
        assert payload.status == PREVIEW_VITE_ERROR_STATUS_DETECTED
        assert payload.target == "src/Header.tsx"
        assert payload.source_path == "src/Header.tsx"
        assert payload.source_line == 42
        # The bilingual narrative label embeds the target + the
        # classifier-routed error_class, with the row-spec literal
        # "我看到 X 有 Y，正在修…".
        assert "我看到 src/Header.tsx" in payload.label
        assert payload.label.endswith("，正在修…")

        # 2) Emission publishes the W16.6 detected event.  Operator
        #    sees the amber chat trace card in their orchestrator chat.
        emit_preview_vite_error(
            workspace_id=payload.workspace_id,
            status=payload.status,
            label=payload.label,
            error_class=payload.error_class,
            target=payload.target,
            error_signature=payload.error_signature,
            source_path=payload.source_path,
            source_line=payload.source_line,
            broadcast_scope="session",
        )
        assert capture.event_names() == [
            PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME,
        ]
        _, data, _ = capture.first(PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME)
        assert data["status"] == PREVIEW_VITE_ERROR_STATUS_DETECTED
        assert data["target"] == "src/Header.tsx"
        # error_signature carries the W15.4 head-only signature so the
        # subsequent resolved event can correlate against this card.
        assert data["error_signature"]
        assert data["error_signature"].startswith("vite[transform]")

    def test_self_fix_resolves_via_matching_signature(
        self, capture: _PublishCapture,
    ) -> None:
        # 1) Detection — same as the prior test but capture the signature
        #    so the resolved event can be routed back to the same chat
        #    card.
        detected = preview_vite_error_payload_from_history_entry(
            self.HISTORY_ENTRY,
            workspace_id=self.WORKSPACE_ID,
        )
        assert detected is not None
        emit_preview_vite_error(
            workspace_id=detected.workspace_id,
            status=detected.status,
            label=detected.label,
            error_class=detected.error_class,
            target=detected.target,
            error_signature=detected.error_signature,
            broadcast_scope="session",
        )

        # 2) The W15.6 self-fix loop runs; once the next build is
        #    clean the consumer-side hook posts the resolved event.
        #    The W16.6 helper produces a payload with the row-spec
        #    "已修 ✓" label and ``status="resolved"`` so the FE flips
        #    the matching card to emerald.
        resolved = preview_vite_error_payload_from_history_entry(
            self.HISTORY_ENTRY,
            workspace_id=self.WORKSPACE_ID,
            status=PREVIEW_VITE_ERROR_STATUS_RESOLVED,
        )
        assert resolved is not None
        assert resolved.status == PREVIEW_VITE_ERROR_STATUS_RESOLVED
        assert resolved.label == PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL
        emit_preview_vite_error(
            workspace_id=resolved.workspace_id,
            status=resolved.status,
            label=resolved.label,
            error_signature=resolved.error_signature,
            broadcast_scope="session",
        )

        # 3) The two events fire in order, with the second one carrying
        #    the matching error_signature so the FE flip is unambiguous.
        names = capture.event_names()
        assert names == [
            PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME,
            PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME,
        ]
        _, det_data, _ = capture.first(PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME)
        _, res_data, _ = capture.first(PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME)
        assert det_data["error_signature"] == res_data["error_signature"]
        assert res_data["label"] == PREVIEW_VITE_ERROR_DEFAULT_RESOLVED_LABEL

    def test_chat_messages_for_detected_then_resolved_carry_status(
        self,
    ) -> None:
        # The chat-message renderer projects each payload into a
        # ``previewViteError`` field whose ``status`` discriminator is
        # what the FE switches the colour theme on (amber for
        # detected, emerald for resolved).
        detected = preview_vite_error_payload_from_history_entry(
            self.HISTORY_ENTRY, workspace_id=self.WORKSPACE_ID,
        )
        resolved = preview_vite_error_payload_from_history_entry(
            self.HISTORY_ENTRY,
            workspace_id=self.WORKSPACE_ID,
            status=PREVIEW_VITE_ERROR_STATUS_RESOLVED,
        )
        assert detected is not None
        assert resolved is not None

        det_msg = build_chat_message_for_preview_vite_error(
            detected, message_id="m-vite-det",
        )
        res_msg = build_chat_message_for_preview_vite_error(
            resolved, message_id="m-vite-res",
        )
        assert det_msg["previewViteError"]["status"] == (
            PREVIEW_VITE_ERROR_STATUS_DETECTED
        )
        assert res_msg["previewViteError"]["status"] == (
            PREVIEW_VITE_ERROR_STATUS_RESOLVED
        )
        # Same error_signature on both → the FE flip routes the
        # resolution to the matching detection card.
        assert det_msg["previewViteError"]["errorSignature"] == (
            res_msg["previewViteError"]["errorSignature"]
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §Z — Row-spec drift guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRowSpecDriftGuards:
    """Pin the W16.9 row-spec literals so the five-scenario contract
    cannot drift silently.  The TODO row reads:

        W16.9 Tests: 5 個 e2e scenarios
        （純 URL 克隆 / 純 image 設計 / build intent / edit live / error 自修）

    Each scenario maps to one or more frozen literals on the W16.x
    public surface; if any of those literals drift, the matching
    scenario test breaks loudly here.
    """

    def test_scenario_1_clone_pins_clone_kind(self) -> None:
        assert REFERENCE_KIND_CLONE == "clone"

    def test_scenario_2_image_pins_image_kind_and_trigger_prefix(self) -> None:
        assert REFERENCE_KIND_IMAGE == "image"
        assert IMAGE_COACH_TRIGGER_PREFIX.endswith(":")

    def test_scenario_3_build_intent_pins_kind_event_chain(self) -> None:
        assert BUILD_INTENT_KIND_LANDING == "landing"
        assert PREVIEW_READY_EVENT_NAME == "preview.ready"
        assert PREVIEW_NEXT_STEPS_EVENT_NAME == "preview.next_steps"
        assert PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY == "vercel_deploy"
        assert PREVIEW_NEXT_STEP_DEFAULT_RECOMMENDED_KIND == (
            PREVIEW_NEXT_STEP_KIND_VERCEL_DEPLOY
        )

    def test_scenario_4_edit_live_pins_trigger_and_event(self) -> None:
        assert EDIT_INTENT_TRIGGER_PREFIX.endswith(":")
        assert EDIT_INTENT_SLASH_COMMAND == "/edit-preview"
        assert PREVIEW_HMR_RELOAD_EVENT_NAME == "preview.hmr_reload"

    def test_scenario_5_error_self_fix_pins_event_pair(self) -> None:
        assert PREVIEW_VITE_ERROR_DETECTED_EVENT_NAME == "preview.vite_error"
        assert PREVIEW_VITE_ERROR_RESOLVED_EVENT_NAME == (
            "preview.vite_error_resolved"
        )
        assert PREVIEW_VITE_ERROR_STATUS_DETECTED == "detected"
        assert PREVIEW_VITE_ERROR_STATUS_RESOLVED == "resolved"

    def test_screenshot_kind_is_a_valid_third_reference_kind(self) -> None:
        # Even though the W16.9 row spec calls out only "純 URL 克隆"
        # and "純 image 設計" for the reference-attachment scenarios,
        # the W16.8 module also recognises a screenshot kind.  Pinning
        # it here so the e2e drift guard catches any future renaming
        # that would silently break the W11.7 / W12 / W13 → W16.8
        # round-trip.
        assert REFERENCE_KIND_SCREENSHOT == "screenshot"
