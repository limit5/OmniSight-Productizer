"""W16.8 — Reference attachment persistence + agent prompt context tests.

Locks the public surface of ``backend.web.reference_attachment`` so the
on-disk index shape (``<project_root>/.omnisight/references/index.json``)
+ the agent prompt-context renderer + the ``reference.attached`` SSE
event stay binding for the W16.9 e2e tests.

Coverage axes
─────────────

  §A  Drift guards — frozen wire-shape constants + bound caps + kind
      tuple + dataclass invariants + index version + event name.
  §B  Resolvers — ``resolve_reference_dir`` /
      ``resolve_reference_index_path`` /
      ``resolve_reference_payload_path`` build absolute paths under
      ``.omnisight/references``.
  §C  ID minting — ``mint_reference_id`` produces the ``ref_<hex16>``
      shape; same (kind, seed) → same id; different (kind, seed) →
      different id; bad inputs raise.
  §D  Index round-trip — empty index when missing; load/write JSON
      preserves all rows + frozen index_version; malformed file raises.
  §E  Standalone payload I/O — ``write_reference_payload`` /
      ``read_reference_payload`` round-trip; over-cap raises.
  §F  ``register_reference_attachment`` — clone path (existing
      payload_path) + image path (payload_dict) + screenshot path;
      idempotent re-register replaces the existing row; FIFO eviction
      kicks in past ``MAX_REFERENCE_ATTACHMENTS_PER_PROJECT``;
      validation failure paths.
  §G  Factory helpers — ``register_reference_from_clone_manifest`` /
      ``register_reference_from_layout_spec`` /
      ``register_reference_from_screenshot_manifest`` produce the
      expected summary + ref_id + payload_path.
  §H  ``render_reference_attachment_context`` — empty when no rows;
      newest-first by created_at; ``max_attachments`` cap; pinned
      heading literal; bullet shape; payload + source thread through.
  §I  ``build_chat_message_for_reference_attached`` — projects to the
      WorkspaceChat shape with ``referenceAttachment`` field.
  §J  ``emit_reference_attached`` — publishes one event with the
      validated attachment + default broadcast scope.
  §K  Integration — orchestrator agent prompt context loader picks up
      persisted references and prepends them to ``handoff_ctx``.
  §L  Re-export sweep — every public symbol surfaces from the
      ``backend.web`` package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend import web as web_pkg
from backend.web import reference_attachment as ra
from backend.web.reference_attachment import (
    DEFAULT_REFERENCE_ATTACHMENT_RENDER_LIMIT,
    MAX_REFERENCE_ATTACHMENTS_PER_PROJECT,
    MAX_REFERENCE_PAYLOAD_BYTES,
    MAX_REFERENCE_PAYLOAD_PATH_BYTES,
    MAX_REFERENCE_REF_ID_BYTES,
    MAX_REFERENCE_SOURCE_URL_BYTES,
    MAX_REFERENCE_SUMMARY_BYTES,
    REFERENCE_ATTACHED_DEFAULT_BROADCAST_SCOPE,
    REFERENCE_ATTACHED_DEFAULT_LABEL,
    REFERENCE_ATTACHED_EVENT_NAME,
    REFERENCE_ATTACHMENT_DIR_NAME,
    REFERENCE_ATTACHMENT_INDEX_FILENAME,
    REFERENCE_ATTACHMENT_INDEX_VERSION,
    REFERENCE_ATTACHMENT_PIPELINE_PHASE,
    REFERENCE_ATTACHMENT_SUBDIR,
    REFERENCE_ID_PREFIX,
    REFERENCE_KINDS,
    REFERENCE_KIND_CLONE,
    REFERENCE_KIND_IMAGE,
    REFERENCE_KIND_SCREENSHOT,
    ReferenceAttachment,
    ReferenceAttachmentError,
    ReferenceIndex,
    build_chat_message_for_reference_attached,
    emit_reference_attached,
    list_reference_attachments,
    load_reference_index,
    mint_reference_id,
    read_reference_payload,
    register_reference_attachment,
    register_reference_from_clone_manifest,
    register_reference_from_layout_spec,
    register_reference_from_screenshot_manifest,
    render_reference_attachment_context,
    resolve_reference_dir,
    resolve_reference_index_path,
    resolve_reference_payload_path,
    write_reference_index,
    write_reference_payload,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  Drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDriftGuards:

    def test_index_version_pinned_at_1(self) -> None:
        # Bump in lock-step with the on-disk schema.  W16.9 e2e tests
        # pin this literal too.
        assert REFERENCE_ATTACHMENT_INDEX_VERSION == "1"

    def test_dir_name_pinned(self) -> None:
        # Already used by W11.7 (clone-manifest.json) and W13.3
        # (refs/manifest.json) — drift here would break both.
        assert REFERENCE_ATTACHMENT_DIR_NAME == ".omnisight"

    def test_subdir_pinned(self) -> None:
        # Distinct from W13.3's ``refs/`` so the screenshot writer's
        # atomic-rename pattern does not collide with the index write.
        assert REFERENCE_ATTACHMENT_SUBDIR == "references"

    def test_index_filename_pinned(self) -> None:
        assert REFERENCE_ATTACHMENT_INDEX_FILENAME == "index.json"

    def test_kinds_tuple_order_binding(self) -> None:
        # Renderer iterates in tuple order — clone first (canonical),
        # image second (paste), screenshot third (derived).
        assert REFERENCE_KINDS == (
            REFERENCE_KIND_CLONE,
            REFERENCE_KIND_IMAGE,
            REFERENCE_KIND_SCREENSHOT,
        )

    def test_kind_clone_value(self) -> None:
        assert REFERENCE_KIND_CLONE == "clone"

    def test_kind_image_value(self) -> None:
        assert REFERENCE_KIND_IMAGE == "image"

    def test_kind_screenshot_value(self) -> None:
        assert REFERENCE_KIND_SCREENSHOT == "screenshot"

    def test_ref_id_prefix_pinned(self) -> None:
        assert REFERENCE_ID_PREFIX == "ref_"

    def test_event_name_pinned(self) -> None:
        # FE SSE consumer + W16.9 e2e tests pin this literal.
        assert REFERENCE_ATTACHED_EVENT_NAME == "reference.attached"

    def test_pipeline_phase_pinned(self) -> None:
        assert REFERENCE_ATTACHMENT_PIPELINE_PHASE == "reference_attachment"

    def test_default_broadcast_scope_session(self) -> None:
        # Mirrors W16.4–W16.7 — preview surface is per-operator-session.
        assert REFERENCE_ATTACHED_DEFAULT_BROADCAST_SCOPE == "session"

    def test_default_label_human_readable(self) -> None:
        assert isinstance(REFERENCE_ATTACHED_DEFAULT_LABEL, str)
        assert REFERENCE_ATTACHED_DEFAULT_LABEL.strip()

    def test_byte_caps_positive(self) -> None:
        assert MAX_REFERENCE_REF_ID_BYTES > 0
        assert MAX_REFERENCE_SOURCE_URL_BYTES > 0
        assert MAX_REFERENCE_SUMMARY_BYTES > 0
        assert MAX_REFERENCE_PAYLOAD_PATH_BYTES > 0
        assert MAX_REFERENCE_PAYLOAD_BYTES > 0

    def test_attachments_per_project_cap_positive(self) -> None:
        assert MAX_REFERENCE_ATTACHMENTS_PER_PROJECT > 0

    def test_render_limit_within_cap(self) -> None:
        # Renderer can never ask for more than the index can hold.
        assert (
            DEFAULT_REFERENCE_ATTACHMENT_RENDER_LIMIT
            <= MAX_REFERENCE_ATTACHMENTS_PER_PROJECT
        )

    def test_attachment_dataclass_frozen(self) -> None:
        a = ReferenceAttachment(
            ref_id="ref_x", kind=REFERENCE_KIND_CLONE,
            created_at="2026-05-03T00:00:00Z",
            summary="x", payload_path="clone-manifest.json",
        )
        with pytest.raises(Exception):
            a.summary = "mutated"  # type: ignore[misc]

    def test_index_dataclass_frozen(self) -> None:
        idx = ReferenceIndex(
            index_version="1",
            created_at="2026-05-03T00:00:00Z",
            updated_at="2026-05-03T00:00:00Z",
            attachments=(),
        )
        with pytest.raises(Exception):
            idx.updated_at = "mutated"  # type: ignore[misc]

    def test_error_subclasses_value_error(self) -> None:
        # Callers that already except on bad URL inputs keep working.
        assert issubclass(ReferenceAttachmentError, ValueError)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  Resolvers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResolvers:

    def test_resolve_reference_dir(self, tmp_path: Path) -> None:
        out = resolve_reference_dir(tmp_path)
        assert out == tmp_path / ".omnisight" / "references"
        assert out.is_absolute()

    def test_resolve_reference_index_path(self, tmp_path: Path) -> None:
        out = resolve_reference_index_path(tmp_path)
        assert out == tmp_path / ".omnisight" / "references" / "index.json"

    def test_resolve_reference_payload_path(self, tmp_path: Path) -> None:
        out = resolve_reference_payload_path(tmp_path, "ref_abc")
        assert out == tmp_path / ".omnisight" / "references" / "ref_abc.json"

    def test_resolver_rejects_non_path(self) -> None:
        with pytest.raises(ReferenceAttachmentError, match="project_root"):
            resolve_reference_dir(123)  # type: ignore[arg-type]

    def test_resolver_with_string_project_root(self, tmp_path: Path) -> None:
        out = resolve_reference_dir(str(tmp_path))
        assert out == tmp_path / ".omnisight" / "references"

    def test_resolver_relative_path_is_resolved_to_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        out = resolve_reference_dir(Path("project"))
        assert out.is_absolute()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  ID minting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMintReferenceId:

    def test_format_pinned(self) -> None:
        rid = mint_reference_id(kind=REFERENCE_KIND_CLONE, seed="https://acme.dev")
        assert rid.startswith(REFERENCE_ID_PREFIX)
        suffix = rid[len(REFERENCE_ID_PREFIX):]
        assert len(suffix) == 16
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_deterministic_same_inputs(self) -> None:
        rid1 = mint_reference_id(kind=REFERENCE_KIND_CLONE, seed="https://x")
        rid2 = mint_reference_id(kind=REFERENCE_KIND_CLONE, seed="https://x")
        assert rid1 == rid2

    def test_different_kind_different_id(self) -> None:
        rid_clone = mint_reference_id(kind=REFERENCE_KIND_CLONE, seed="https://x")
        rid_image = mint_reference_id(kind=REFERENCE_KIND_IMAGE, seed="https://x")
        assert rid_clone != rid_image

    def test_different_seed_different_id(self) -> None:
        rid1 = mint_reference_id(kind=REFERENCE_KIND_CLONE, seed="a")
        rid2 = mint_reference_id(kind=REFERENCE_KIND_CLONE, seed="b")
        assert rid1 != rid2

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ReferenceAttachmentError, match="kind"):
            mint_reference_id(kind="bogus", seed="x")

    def test_empty_seed_raises(self) -> None:
        with pytest.raises(ReferenceAttachmentError, match="seed"):
            mint_reference_id(kind=REFERENCE_KIND_CLONE, seed="")

    def test_non_string_seed_raises(self) -> None:
        with pytest.raises(ReferenceAttachmentError, match="seed"):
            mint_reference_id(kind=REFERENCE_KIND_CLONE, seed=123)  # type: ignore[arg-type]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  Index round-trip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIndexRoundTrip:

    def test_load_missing_returns_empty_index(self, tmp_path: Path) -> None:
        idx = load_reference_index(tmp_path)
        assert isinstance(idx, ReferenceIndex)
        assert idx.index_version == REFERENCE_ATTACHMENT_INDEX_VERSION
        assert idx.attachments == ()

    def test_write_and_load_round_trip(self, tmp_path: Path) -> None:
        a = ReferenceAttachment(
            ref_id="ref_aaaaaaaaaaaaaaaa",
            kind=REFERENCE_KIND_CLONE,
            created_at="2026-05-03T00:00:00Z",
            summary="round trip",
            payload_path="clone-manifest.json",
            source_url="https://acme.dev",
        )
        idx = ReferenceIndex(
            index_version="1",
            created_at="2026-05-03T00:00:00Z",
            updated_at="2026-05-03T00:00:01Z",
            attachments=(a,),
        )
        written = write_reference_index(idx, project_root=tmp_path)
        assert written.exists()
        loaded = load_reference_index(tmp_path)
        assert loaded.attachments == (a,)
        assert loaded.index_version == "1"
        assert loaded.updated_at == "2026-05-03T00:00:01Z"

    def test_load_unsupported_version_raises(self, tmp_path: Path) -> None:
        target = resolve_reference_index_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "index_version": "999",
            "created_at": "2026-05-03T00:00:00Z",
            "updated_at": "2026-05-03T00:00:00Z",
            "attachments": [],
        }))
        with pytest.raises(ReferenceAttachmentError, match="index_version"):
            load_reference_index(tmp_path)

    def test_load_malformed_json_raises(self, tmp_path: Path) -> None:
        target = resolve_reference_index_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json")
        with pytest.raises(ReferenceAttachmentError, match="not valid JSON"):
            load_reference_index(tmp_path)

    def test_load_non_object_raises(self, tmp_path: Path) -> None:
        target = resolve_reference_index_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps([]))
        with pytest.raises(ReferenceAttachmentError, match="not a JSON object"):
            load_reference_index(tmp_path)

    def test_load_attachments_must_be_list(self, tmp_path: Path) -> None:
        target = resolve_reference_index_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "index_version": "1",
            "created_at": "2026-05-03T00:00:00Z",
            "updated_at": "2026-05-03T00:00:00Z",
            "attachments": "not-a-list",
        }))
        with pytest.raises(ReferenceAttachmentError, match="must be a list"):
            load_reference_index(tmp_path)

    def test_write_rejects_non_index(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="ReferenceIndex"):
            write_reference_index(
                {"index_version": "1"},  # type: ignore[arg-type]
                project_root=tmp_path,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  Standalone payload I/O
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPayloadIO:

    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        payload = {"a": 1, "b": [2, 3], "c": {"d": "e"}}
        target = write_reference_payload(
            project_root=tmp_path,
            ref_id="ref_payload01234",
            payload=payload,
        )
        assert target.exists()
        loaded = read_reference_payload(
            project_root=tmp_path, ref_id="ref_payload01234",
        )
        assert loaded == payload

    def test_write_rejects_non_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="payload"):
            write_reference_payload(
                project_root=tmp_path,
                ref_id="ref_x",
                payload=[1, 2, 3],  # type: ignore[arg-type]
            )

    def test_write_rejects_oversize(self, tmp_path: Path) -> None:
        oversized = {"data": "x" * (MAX_REFERENCE_PAYLOAD_BYTES + 1)}
        with pytest.raises(ReferenceAttachmentError, match="byte cap"):
            write_reference_payload(
                project_root=tmp_path,
                ref_id="ref_x",
                payload=oversized,
            )

    def test_read_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="failed to read"):
            read_reference_payload(
                project_root=tmp_path, ref_id="ref_missing",
            )

    def test_read_malformed_raises(self, tmp_path: Path) -> None:
        target = resolve_reference_payload_path(tmp_path, "ref_x")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json")
        with pytest.raises(ReferenceAttachmentError, match="not valid JSON"):
            read_reference_payload(project_root=tmp_path, ref_id="ref_x")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  register_reference_attachment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRegisterReferenceAttachment:

    def test_register_clone_with_payload_path(self, tmp_path: Path) -> None:
        a = register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="a",
            source_url="https://acme.dev",
            payload_path="clone-manifest.json",
        )
        assert a.kind == REFERENCE_KIND_CLONE
        assert a.payload_path == "clone-manifest.json"
        assert a.source_url == "https://acme.dev"
        assert a.ref_id.startswith(REFERENCE_ID_PREFIX)
        # Index file written.
        assert resolve_reference_index_path(tmp_path).exists()

    def test_register_image_with_payload_dict(self, tmp_path: Path) -> None:
        a = register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_IMAGE,
            summary="img",
            payload_dict={"image_hash": "abc", "summary": "x"},
        )
        assert a.kind == REFERENCE_KIND_IMAGE
        assert a.payload_path == f"references/{a.ref_id}.json"
        # Standalone payload blob written.
        assert resolve_reference_payload_path(
            tmp_path, a.ref_id,
        ).exists()

    def test_register_screenshot_with_payload_path(self, tmp_path: Path) -> None:
        a = register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_SCREENSHOT,
            summary="shot",
            source_url="https://acme.dev",
            payload_path="refs/manifest.json",
        )
        assert a.kind == REFERENCE_KIND_SCREENSHOT
        assert a.payload_path == "refs/manifest.json"

    def test_register_idempotent_replaces_existing_row(self, tmp_path: Path) -> None:
        first = register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="first",
            source_url="https://acme.dev",
            payload_path="clone-manifest.json",
        )
        second = register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="second",  # different summary, same source_url
            source_url="https://acme.dev",
            payload_path="clone-manifest.json",
        )
        assert first.ref_id == second.ref_id
        attachments = list_reference_attachments(tmp_path)
        assert len(attachments) == 1
        assert attachments[0].summary == "second"

    def test_register_two_distinct_specs_appends(self, tmp_path: Path) -> None:
        a1 = register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="first",
            source_url="https://a.example",
            payload_path="clone-manifest.json",
        )
        a2 = register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="second",
            source_url="https://b.example",
            payload_path="clone-manifest.json",
        )
        assert a1.ref_id != a2.ref_id
        attachments = list_reference_attachments(tmp_path)
        assert len(attachments) == 2

    def test_register_rejects_both_payload_path_and_dict(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="exactly one"):
            register_reference_attachment(
                project_root=tmp_path,
                kind=REFERENCE_KIND_CLONE,
                summary="x",
                payload_path="clone-manifest.json",
                payload_dict={"a": 1},
            )

    def test_register_rejects_neither_payload_path_nor_dict(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="exactly one"):
            register_reference_attachment(
                project_root=tmp_path,
                kind=REFERENCE_KIND_CLONE,
                summary="x",
            )

    def test_register_rejects_unknown_kind(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="kind"):
            register_reference_attachment(
                project_root=tmp_path,
                kind="bogus",
                summary="x",
                payload_path="clone-manifest.json",
            )

    def test_register_rejects_oversize_summary(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="summary"):
            register_reference_attachment(
                project_root=tmp_path,
                kind=REFERENCE_KIND_CLONE,
                summary="x" * (MAX_REFERENCE_SUMMARY_BYTES + 1),
                payload_path="clone-manifest.json",
            )

    def test_register_explicit_ref_id_must_have_prefix(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="must start with"):
            register_reference_attachment(
                project_root=tmp_path,
                kind=REFERENCE_KIND_CLONE,
                summary="x",
                payload_path="clone-manifest.json",
                ref_id="not-a-ref-id",
            )

    def test_fifo_eviction_past_cap(self, tmp_path: Path) -> None:
        cap = MAX_REFERENCE_ATTACHMENTS_PER_PROJECT
        for i in range(cap + 5):
            register_reference_attachment(
                project_root=tmp_path,
                kind=REFERENCE_KIND_CLONE,
                summary=f"clone {i}",
                source_url=f"https://acme.dev/p{i}",
                payload_path="clone-manifest.json",
                created_at=f"2026-05-03T00:00:{i:02d}Z",
            )
        attachments = list_reference_attachments(tmp_path)
        assert len(attachments) == cap
        # The oldest 5 should have been evicted; first surviving row's
        # created_at should match index 5.
        assert attachments[0].created_at == "2026-05-03T00:00:05Z"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §G  Factory helpers (W11/W12/W13 → ReferenceAttachment)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_W11_MANIFEST_FIXTURE: dict[str, Any] = {
    "manifest_version": "1",
    "clone_id": "c1",
    "created_at": "2026-05-03T00:00:00Z",
    "tenant_id": "t1",
    "actor": "rt3628@gmail.com",
    "source": {
        "url": "https://acme.example/landing",
        "fetched_at": "2026-05-03T00:00:00Z",
        "backend": "playwright",
    },
    "transformed_summary": {
        "title": "Acme Landing",
        "nav_count": 4,
        "section_count": 3,
        "image_count": 5,
        "color_count": 7,
        "font_count": 2,
        "has_hero": True,
        "has_footer": True,
    },
    "classification": {},
    "transformation": {},
    "defense_layers": {},
    "attribution": "",
    "manifest_hash": "sha256:deadbeef",
}


_W12_LAYOUT_SPEC_FIXTURE: dict[str, Any] = {
    "image_hash": "abc1234567890def",
    "summary": "A login form with email/password fields",
    "components": ["email-input", "password-input", "submit-button"],
    "colors": ["#3b82f6", "#ffffff"],
    "fonts": ["Inter"],
    "raw_text": "...",
    "degraded": False,
}


_W13_SCREENSHOT_MANIFEST_FIXTURE: dict[str, Any] = {
    "manifest_version": "1",
    "source_url": "https://acme.example/landing",
    "captured_at": "2026-05-03T00:00:00Z",
    "breakpoints": [
        {"name": "mobile_375"},
        {"name": "tablet_768"},
        {"name": "desktop_1280"},
    ],
}


class TestRegisterFromCloneManifest:

    def test_summary_includes_title(self, tmp_path: Path) -> None:
        a = register_reference_from_clone_manifest(
            project_root=tmp_path, manifest=_W11_MANIFEST_FIXTURE,
        )
        assert "Acme Landing" in a.summary
        assert "nav=4" in a.summary

    def test_default_payload_path_points_to_w11_artefact(self, tmp_path: Path) -> None:
        a = register_reference_from_clone_manifest(
            project_root=tmp_path, manifest=_W11_MANIFEST_FIXTURE,
        )
        assert a.payload_path == "clone-manifest.json"

    def test_source_url_threaded(self, tmp_path: Path) -> None:
        a = register_reference_from_clone_manifest(
            project_root=tmp_path, manifest=_W11_MANIFEST_FIXTURE,
        )
        assert a.source_url == "https://acme.example/landing"

    def test_kind_is_clone(self, tmp_path: Path) -> None:
        a = register_reference_from_clone_manifest(
            project_root=tmp_path, manifest=_W11_MANIFEST_FIXTURE,
        )
        assert a.kind == REFERENCE_KIND_CLONE

    def test_rejects_non_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="manifest"):
            register_reference_from_clone_manifest(
                project_root=tmp_path, manifest=[1, 2, 3],  # type: ignore[arg-type]
            )

    def test_rejects_non_mapping_source(self, tmp_path: Path) -> None:
        bad = dict(_W11_MANIFEST_FIXTURE, source="not-a-mapping")
        with pytest.raises(ReferenceAttachmentError, match="source"):
            register_reference_from_clone_manifest(
                project_root=tmp_path, manifest=bad,
            )


class TestRegisterFromLayoutSpec:

    def test_summary_includes_headline(self, tmp_path: Path) -> None:
        a = register_reference_from_layout_spec(
            project_root=tmp_path, layout_spec=_W12_LAYOUT_SPEC_FIXTURE,
        )
        assert "login form" in a.summary
        assert "components=3" in a.summary

    def test_payload_path_points_to_w16_8_owned_blob(self, tmp_path: Path) -> None:
        a = register_reference_from_layout_spec(
            project_root=tmp_path, layout_spec=_W12_LAYOUT_SPEC_FIXTURE,
        )
        assert a.payload_path == f"references/{a.ref_id}.json"

    def test_payload_blob_round_trips(self, tmp_path: Path) -> None:
        a = register_reference_from_layout_spec(
            project_root=tmp_path, layout_spec=_W12_LAYOUT_SPEC_FIXTURE,
        )
        loaded = read_reference_payload(
            project_root=tmp_path, ref_id=a.ref_id,
        )
        assert loaded["image_hash"] == "abc1234567890def"
        assert loaded["summary"] == _W12_LAYOUT_SPEC_FIXTURE["summary"]

    def test_kind_is_image(self, tmp_path: Path) -> None:
        a = register_reference_from_layout_spec(
            project_root=tmp_path, layout_spec=_W12_LAYOUT_SPEC_FIXTURE,
        )
        assert a.kind == REFERENCE_KIND_IMAGE

    def test_ref_id_derived_from_image_hash(self, tmp_path: Path) -> None:
        a1 = register_reference_from_layout_spec(
            project_root=tmp_path, layout_spec=_W12_LAYOUT_SPEC_FIXTURE,
        )
        a2 = register_reference_from_layout_spec(
            project_root=tmp_path, layout_spec=_W12_LAYOUT_SPEC_FIXTURE,
        )
        # Same image_hash → same ref_id (idempotent re-register).
        assert a1.ref_id == a2.ref_id

    def test_rejects_missing_image_hash(self, tmp_path: Path) -> None:
        bad = dict(_W12_LAYOUT_SPEC_FIXTURE)
        bad["image_hash"] = ""
        with pytest.raises(ReferenceAttachmentError, match="image_hash"):
            register_reference_from_layout_spec(
                project_root=tmp_path, layout_spec=bad,
            )


class TestRegisterFromScreenshotManifest:

    def test_summary_includes_breakpoint_count(self, tmp_path: Path) -> None:
        a = register_reference_from_screenshot_manifest(
            project_root=tmp_path,
            screenshot_manifest=_W13_SCREENSHOT_MANIFEST_FIXTURE,
        )
        assert "breakpoints=3" in a.summary

    def test_default_payload_path_points_to_w13_artefact(self, tmp_path: Path) -> None:
        a = register_reference_from_screenshot_manifest(
            project_root=tmp_path,
            screenshot_manifest=_W13_SCREENSHOT_MANIFEST_FIXTURE,
        )
        assert a.payload_path == "refs/manifest.json"

    def test_kind_is_screenshot(self, tmp_path: Path) -> None:
        a = register_reference_from_screenshot_manifest(
            project_root=tmp_path,
            screenshot_manifest=_W13_SCREENSHOT_MANIFEST_FIXTURE,
        )
        assert a.kind == REFERENCE_KIND_SCREENSHOT

    def test_summary_when_source_url_missing(self, tmp_path: Path) -> None:
        bare = {"breakpoints": [{"name": "mobile_375"}]}
        a = register_reference_from_screenshot_manifest(
            project_root=tmp_path, screenshot_manifest=bare,
        )
        assert a.source_url is None
        assert "breakpoints=1" in a.summary


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §H  render_reference_attachment_context
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRenderContext:

    def test_empty_workspace_returns_empty_string(self, tmp_path: Path) -> None:
        assert render_reference_attachment_context(project_root=tmp_path) == ""

    def test_renders_pinned_heading(self, tmp_path: Path) -> None:
        register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="x",
            source_url="https://x",
            payload_path="clone-manifest.json",
        )
        ctx = render_reference_attachment_context(project_root=tmp_path)
        assert ctx.startswith("## Reference Attachments\n")

    def test_bullet_shape(self, tmp_path: Path) -> None:
        register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="cloned acme",
            source_url="https://acme.dev",
            payload_path="clone-manifest.json",
        )
        ctx = render_reference_attachment_context(project_root=tmp_path)
        assert "- [clone] cloned acme " in ctx
        assert "ref_id=ref_" in ctx
        assert "source=https://acme.dev" in ctx
        assert "payload=clone-manifest.json" in ctx

    def test_newest_first_sort(self, tmp_path: Path) -> None:
        register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="OLDER",
            source_url="https://a",
            payload_path="clone-manifest.json",
            created_at="2026-05-03T00:00:00Z",
        )
        register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="NEWER",
            source_url="https://b",
            payload_path="clone-manifest.json",
            created_at="2026-05-03T00:00:01Z",
        )
        ctx = render_reference_attachment_context(project_root=tmp_path)
        idx_newer = ctx.index("NEWER")
        idx_older = ctx.index("OLDER")
        assert idx_newer < idx_older

    def test_max_attachments_caps_render_count(self, tmp_path: Path) -> None:
        for i in range(5):
            register_reference_attachment(
                project_root=tmp_path,
                kind=REFERENCE_KIND_CLONE,
                summary=f"row {i}",
                source_url=f"https://x/{i}",
                payload_path="clone-manifest.json",
                created_at=f"2026-05-03T00:00:0{i}Z",
            )
        ctx = render_reference_attachment_context(
            project_root=tmp_path, max_attachments=2,
        )
        bullet_count = sum(1 for line in ctx.splitlines() if line.startswith("- "))
        assert bullet_count == 2

    def test_max_attachments_must_be_positive(self, tmp_path: Path) -> None:
        with pytest.raises(ReferenceAttachmentError, match="positive"):
            render_reference_attachment_context(
                project_root=tmp_path, max_attachments=0,
            )

    def test_corrupt_index_returns_empty_string(self, tmp_path: Path) -> None:
        # A malformed index must not break agent invocation.
        target = resolve_reference_index_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json")
        assert render_reference_attachment_context(project_root=tmp_path) == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §I  build_chat_message_for_reference_attached
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildChatMessage:

    def _attachment(self) -> ReferenceAttachment:
        return ReferenceAttachment(
            ref_id="ref_aaaaaaaaaaaaaaaa",
            kind=REFERENCE_KIND_CLONE,
            created_at="2026-05-03T00:00:00Z",
            summary="cloned acme",
            payload_path="clone-manifest.json",
            source_url="https://acme.dev",
        )

    def test_role_is_system(self) -> None:
        msg = build_chat_message_for_reference_attached(self._attachment())
        assert msg["role"] == "system"

    def test_text_includes_default_label_and_summary(self) -> None:
        msg = build_chat_message_for_reference_attached(self._attachment())
        assert REFERENCE_ATTACHED_DEFAULT_LABEL in msg["text"]
        assert "cloned acme" in msg["text"]

    def test_camelcase_field_shape(self) -> None:
        msg = build_chat_message_for_reference_attached(self._attachment())
        ra_field = msg["referenceAttachment"]
        assert ra_field["refId"] == "ref_aaaaaaaaaaaaaaaa"
        assert ra_field["kind"] == REFERENCE_KIND_CLONE
        assert ra_field["createdAt"] == "2026-05-03T00:00:00Z"
        assert ra_field["summary"] == "cloned acme"
        assert ra_field["payloadPath"] == "clone-manifest.json"
        assert ra_field["sourceUrl"] == "https://acme.dev"

    def test_source_url_omitted_when_none(self) -> None:
        a = ReferenceAttachment(
            ref_id="ref_bbbbbbbbbbbbbbbb",
            kind=REFERENCE_KIND_IMAGE,
            created_at="2026-05-03T00:00:00Z",
            summary="img",
            payload_path="references/ref_x.json",
        )
        msg = build_chat_message_for_reference_attached(a)
        assert "sourceUrl" not in msg["referenceAttachment"]

    def test_message_id_threaded(self) -> None:
        msg = build_chat_message_for_reference_attached(
            self._attachment(), message_id="msg-42",
        )
        assert msg["id"] == "msg-42"

    def test_custom_label_threaded(self) -> None:
        msg = build_chat_message_for_reference_attached(
            self._attachment(), label="Captured",
        )
        assert msg["text"].startswith("Captured:")

    def test_rejects_non_attachment(self) -> None:
        with pytest.raises(ReferenceAttachmentError, match="ReferenceAttachment"):
            build_chat_message_for_reference_attached({"ref_id": "x"})  # type: ignore[arg-type]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §J  emit_reference_attached
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmitReferenceAttached:

    def _attachment(self) -> ReferenceAttachment:
        return ReferenceAttachment(
            ref_id="ref_aaaaaaaaaaaaaaaa",
            kind=REFERENCE_KIND_CLONE,
            created_at="2026-05-03T00:00:00Z",
            summary="x",
            payload_path="clone-manifest.json",
            source_url="https://x",
        )

    def test_publishes_event(self) -> None:
        from backend import events as events_mod
        captured: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        def fake_publish(event_type: str, data: dict[str, Any], **kwargs: Any) -> None:
            captured.append((event_type, dict(data), dict(kwargs)))

        # Direct instance-attr swap with try/finally — robust against
        # both class-level and instance-level pollution from prior
        # tests in the same pytest run.
        had_attr = "publish" in events_mod.bus.__dict__
        prior = events_mod.bus.__dict__.get("publish")
        events_mod.bus.publish = fake_publish  # type: ignore[assignment]
        try:
            emit_reference_attached(self._attachment(), workspace_id="ws-42")
        finally:
            if had_attr:
                events_mod.bus.publish = prior  # type: ignore[assignment]
            else:
                try:
                    del events_mod.bus.publish  # type: ignore[misc]
                except AttributeError:
                    pass
        assert len(captured) == 1
        event, data, _kwargs = captured[0]
        assert event == REFERENCE_ATTACHED_EVENT_NAME
        assert data["ref_id"] == "ref_aaaaaaaaaaaaaaaa"
        assert data["workspace_id"] == "ws-42"
        assert data["source_url"] == "https://x"

    def test_default_broadcast_scope_session(self) -> None:
        from backend import events as events_mod
        captured: list[dict[str, Any]] = []

        def fake_publish(event_type: str, data: dict[str, Any], **kwargs: Any) -> None:
            captured.append(kwargs)

        had_attr = "publish" in events_mod.bus.__dict__
        prior = events_mod.bus.__dict__.get("publish")
        events_mod.bus.publish = fake_publish  # type: ignore[assignment]
        try:
            emit_reference_attached(self._attachment())
        finally:
            if had_attr:
                events_mod.bus.publish = prior  # type: ignore[assignment]
            else:
                try:
                    del events_mod.bus.publish  # type: ignore[misc]
                except AttributeError:
                    pass
        assert captured[0]["broadcast_scope"] == "session"

    def test_returns_attachment(self) -> None:
        from backend import events as events_mod

        def fake_publish(event_type: str, data: dict[str, Any], **kwargs: Any) -> None:
            return None

        had_attr = "publish" in events_mod.bus.__dict__
        prior = events_mod.bus.__dict__.get("publish")
        events_mod.bus.publish = fake_publish  # type: ignore[assignment]
        try:
            attachment = self._attachment()
            out = emit_reference_attached(attachment)
            assert out is attachment
        finally:
            if had_attr:
                events_mod.bus.publish = prior  # type: ignore[assignment]
            else:
                try:
                    del events_mod.bus.publish  # type: ignore[misc]
                except AttributeError:
                    pass

    def test_rejects_non_attachment(self) -> None:
        with pytest.raises(ReferenceAttachmentError, match="ReferenceAttachment"):
            emit_reference_attached({"ref_id": "x"})  # type: ignore[arg-type]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §K  Orchestrator integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestOrchestratorIntegration:
    """Verify the W16.8 helper threaded into ``backend.routers.invoke``
    picks up persisted attachments and renders a non-empty block."""

    def test_loader_returns_empty_for_no_workspace_path(self) -> None:
        from backend.routers.invoke import _load_reference_attachment_context
        assert _load_reference_attachment_context(None) == ""
        assert _load_reference_attachment_context("") == ""

    def test_loader_returns_empty_for_unknown_dir(self, tmp_path: Path) -> None:
        from backend.routers.invoke import _load_reference_attachment_context
        ghost = tmp_path / "does-not-exist"
        assert _load_reference_attachment_context(str(ghost)) == ""

    def test_loader_renders_block_when_index_present(self, tmp_path: Path) -> None:
        register_reference_attachment(
            project_root=tmp_path,
            kind=REFERENCE_KIND_CLONE,
            summary="acme landing",
            source_url="https://acme.dev",
            payload_path="clone-manifest.json",
        )
        from backend.routers.invoke import _load_reference_attachment_context
        block = _load_reference_attachment_context(str(tmp_path))
        assert "## Reference Attachments" in block
        assert "acme landing" in block

    def test_loader_swallows_corrupt_index(self, tmp_path: Path) -> None:
        from backend.routers.invoke import _load_reference_attachment_context
        target = resolve_reference_index_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json")
        # Must not raise; agent invocation must keep working.
        assert _load_reference_attachment_context(str(tmp_path)) == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §L  Re-export sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_W16_8_SYMBOLS = (
    "DEFAULT_REFERENCE_ATTACHMENT_RENDER_LIMIT",
    "MAX_REFERENCE_ATTACHMENTS_PER_PROJECT",
    "MAX_REFERENCE_PAYLOAD_BYTES",
    "MAX_REFERENCE_PAYLOAD_PATH_BYTES",
    "MAX_REFERENCE_REF_ID_BYTES",
    "MAX_REFERENCE_SOURCE_URL_BYTES",
    "MAX_REFERENCE_SUMMARY_BYTES",
    "REFERENCE_ATTACHED_DEFAULT_BROADCAST_SCOPE",
    "REFERENCE_ATTACHED_DEFAULT_LABEL",
    "REFERENCE_ATTACHED_EVENT_NAME",
    "REFERENCE_ATTACHMENT_DIR_NAME",
    "REFERENCE_ATTACHMENT_INDEX_FILENAME",
    "REFERENCE_ATTACHMENT_INDEX_VERSION",
    "REFERENCE_ATTACHMENT_PIPELINE_PHASE",
    "REFERENCE_ATTACHMENT_SUBDIR",
    "REFERENCE_ID_PREFIX",
    "REFERENCE_KINDS",
    "REFERENCE_KIND_CLONE",
    "REFERENCE_KIND_IMAGE",
    "REFERENCE_KIND_SCREENSHOT",
    "ReferenceAttachment",
    "ReferenceAttachmentError",
    "ReferenceIndex",
    "build_chat_message_for_reference_attached",
    "emit_reference_attached",
    "list_reference_attachments",
    "load_reference_attachment",
    "load_reference_index",
    "mint_reference_id",
    "read_reference_payload",
    "register_reference_attachment",
    "register_reference_from_clone_manifest",
    "register_reference_from_layout_spec",
    "register_reference_from_screenshot_manifest",
    "render_reference_attachment_context",
    "resolve_reference_dir",
    "resolve_reference_index_path",
    "resolve_reference_payload_path",
    "write_reference_index",
    "write_reference_payload",
)


@pytest.mark.parametrize("symbol", _W16_8_SYMBOLS)
def test_w16_8_symbol_re_exported(symbol: str) -> None:
    assert symbol in web_pkg.__all__, (
        f"{symbol} missing from backend.web.__all__"
    )
    assert getattr(web_pkg, symbol) is getattr(ra, symbol)


def test_w16_8_symbol_count_pinned_at_40() -> None:
    # Drift guard — bump in lock-step with __all__ if W16.8 grows or
    # shrinks the public surface.
    assert len(_W16_8_SYMBOLS) == 40


def test_total_re_export_count_matches_w16_8_baseline() -> None:
    # Bumped from 426 (W16.7 baseline) → 466 (W16.8 +40 W16.8 symbols).
    assert len(web_pkg.__all__) == 466
