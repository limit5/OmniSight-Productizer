"""W11.7 #XXX — Contract tests for ``backend.web.clone_manifest``.

Pins:

    * Public surface (constants, dataclass shape, error hierarchy,
      package re-exports).
    * ``build_clone_manifest`` happy-path shape from valid
      :class:`TransformedSpec` + optional :class:`RiskClassification` /
      :class:`RefusalDecision`.
    * ``compute_manifest_hash`` is deterministic, excludes the
      ``manifest_hash`` field, sensitive to changes in any other field.
    * ``finalise_manifest`` is idempotent and lets
      ``verify_manifest_hash`` succeed; tampering with any field after
      finalisation breaks verification.
    * ``serialize_manifest_json`` is sorted-keys + JSON-roundtrip-safe.
    * ``write_manifest_file`` creates ``.omnisight/`` and writes a valid
      JSON file at ``.omnisight/clone-manifest.json``; ``read_manifest_file``
      round-trips it.
    * ``render_html_traceability_comment`` emits begin/end markers and
      every required key:value line; ``parse_html_traceability_comment``
      round-trips the rendered comment back to a dict.
    * ``inject_html_traceability_comment`` is idempotent (replaces an
      existing block rather than duplicating it), respects the
      ``head`` / ``body_start`` / ``prepend`` position knob, and falls
      back to ``prepend`` when no anchor tag is present.
    * ``record_clone_audit`` calls into ``backend.audit.log`` with
      ``action="web.clone"`` / ``entity_kind="web_clone"`` / ``entity_id``
      = the manifest's ``clone_id``; the ``after`` payload mirrors
      ``manifest_to_audit_payload``.
    * ``pin_clone_artefacts`` is a one-shot orchestrator: writes the
      file, injects + writes the HTML, and appends the audit row, all
      in one call. Failures in one step do not roll back another.

Every test runs without network / DB / LLM I/O. The audit log is
monkeypatched via the same ``backend.audit.log`` symbol that
``backend.web.clone_manifest.record_clone_audit`` imports from.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

import pytest

import backend.web as web_pkg
from backend.web.clone_manifest import (
    AUDIT_ACTION,
    AUDIT_ENTITY_KIND,
    CloneManifest,
    CloneManifestError,
    CloneManifestRecord,
    HTML_COMMENT_BEGIN,
    HTML_COMMENT_END,
    MANIFEST_DIR,
    MANIFEST_FILENAME,
    MANIFEST_HASH_FIELD,
    MANIFEST_RELATIVE_PATH,
    MANIFEST_VERSION,
    ManifestSchemaError,
    ManifestWriteError,
    OPEN_LOVABLE_ATTRIBUTION,
    build_clone_manifest,
    compute_manifest_hash,
    finalise_manifest,
    inject_html_traceability_comment,
    manifest_to_audit_payload,
    manifest_to_dict,
    parse_html_traceability_comment,
    pin_clone_artefacts,
    read_manifest_file,
    record_clone_audit,
    render_html_traceability_comment,
    serialize_manifest_json,
    verify_manifest_hash,
    write_manifest_file,
)
from backend.web.content_classifier import (
    RiskClassification,
    RiskScore,
)
from backend.web.output_transformer import TransformedSpec
from backend.web.refusal_signals import RefusalDecision
from backend.web.site_cloner import SiteClonerError


# ── Fixtures + test doubles ─────────────────────────────────────────────


def _make_transformed(
    *,
    title: str = "Our Take on Landing",
    nav_count: int = 3,
    section_count: int = 2,
    image_count: int = 2,
    color_count: int = 3,
    font_count: int = 2,
    transformations=("bytes_strip", "text_rewrite", "image_placeholder"),
    signals_used=("llm", "image_placeholder"),
    model: str = "claude-haiku-4.5",
    warnings=(),
    has_hero: bool = True,
    has_footer: bool = True,
) -> TransformedSpec:
    return TransformedSpec(
        source_url="https://acme.example",
        fetched_at="2026-04-29T00:00:00Z",
        backend="mock",
        title=title,
        meta={"description": "A page."},
        hero={"heading": "Welcome", "tagline": "Tagline", "cta_label": "Go"} if has_hero else None,
        nav=tuple({"label": f"Nav{i}"} for i in range(nav_count)),
        sections=tuple(
            {"heading": f"S{i}", "summary": f"Summary {i}"} for i in range(section_count)
        ),
        footer={"text": "Footer text"} if has_footer else None,
        images=tuple(
            {
                "url": f"https://placehold.co/800x600?text={i}",
                "alt": f"img-{i}",
                "kind": "placeholder",
                "source_url": f"https://acme.example/img{i}.png",
                "width": "800",
                "height": "600",
            }
            for i in range(image_count)
        ),
        colors=tuple(f"#00000{i}" for i in range(color_count)),
        fonts=tuple(f"Font{i}" for i in range(font_count)),
        spacing={"padding": ["16px"]},
        warnings=tuple(warnings),
        signals_used=tuple(signals_used),
        model=model,
        transformations=tuple(transformations),
    )


def _make_classification(
    risk_level: str = "low",
    *,
    categories=("clean",),
    model: str = "claude-haiku-4.5",
    signals_used=("heuristic", "llm"),
) -> RiskClassification:
    scores = tuple(
        RiskScore(category=cat, level=risk_level, reason="ok")
        for cat in categories
    )
    return RiskClassification(
        risk_level=risk_level,
        scores=scores,
        model=model,
        signals_used=tuple(signals_used),
        prefilter_only=False,
    )


def _make_refusal(allowed: bool = True) -> RefusalDecision:
    return RefusalDecision(
        allowed=allowed,
        signals_checked=("robots", "ai.txt"),
        reasons=() if allowed else ("robots:disallow",),
        details={},
    )


def _build_manifest(**overrides: Any) -> CloneManifest:
    """Default-args wrapper around ``build_clone_manifest`` so tests can
    override individual kwargs."""
    base = dict(
        source_url="https://acme.example",
        fetched_at="2026-04-29T00:00:00Z",
        backend="mock",
        classification=_make_classification(),
        transformed=_make_transformed(),
        tenant_id="tenant-42",
        actor="alice@example.com",
        capture_status_code=200,
        refusal_decision=_make_refusal(),
        clone_id="00000000-0000-4000-8000-000000000001",
        created_at="2026-04-29T12:00:00Z",
    )
    base.update(overrides)
    return build_clone_manifest(**base)


def _await(coro):
    return asyncio.run(coro)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_constants_pinned() -> None:
    assert MANIFEST_VERSION == "1"
    assert MANIFEST_DIR == ".omnisight"
    assert MANIFEST_FILENAME == "clone-manifest.json"
    assert MANIFEST_RELATIVE_PATH == ".omnisight/clone-manifest.json"
    assert HTML_COMMENT_BEGIN.startswith("<!--")
    assert HTML_COMMENT_END.endswith("-->")
    assert "omnisight:clone:begin" in HTML_COMMENT_BEGIN
    assert "omnisight:clone:end" in HTML_COMMENT_END
    assert AUDIT_ACTION == "web.clone"
    assert AUDIT_ENTITY_KIND == "web_clone"
    assert MANIFEST_HASH_FIELD == "manifest_hash"
    assert "open-lovable" in OPEN_LOVABLE_ATTRIBUTION
    assert "MIT" in OPEN_LOVABLE_ATTRIBUTION


def test_clone_manifest_dataclass_frozen() -> None:
    m = _build_manifest()
    with pytest.raises((AttributeError, TypeError)):
        m.tenant_id = "other"  # type: ignore[misc]


def test_clone_manifest_record_dataclass_frozen() -> None:
    m = _build_manifest()
    rec = CloneManifestRecord(manifest=m, manifest_path=Path("/tmp/x"))
    with pytest.raises((AttributeError, TypeError)):
        rec.manifest_path = Path("/tmp/y")  # type: ignore[misc]


def test_error_hierarchy_chains_to_site_cloner_error() -> None:
    assert issubclass(CloneManifestError, SiteClonerError)
    assert issubclass(ManifestSchemaError, CloneManifestError)
    assert issubclass(ManifestWriteError, CloneManifestError)


def test_package_re_exports_w11_7_symbols() -> None:
    expected = {
        "AUDIT_ACTION",
        "AUDIT_ENTITY_KIND",
        "CloneManifest",
        "CloneManifestError",
        "CloneManifestRecord",
        "HTML_COMMENT_BEGIN",
        "HTML_COMMENT_END",
        "MANIFEST_DIR",
        "MANIFEST_FILENAME",
        "MANIFEST_HASH_FIELD",
        "MANIFEST_RELATIVE_PATH",
        "MANIFEST_VERSION",
        "ManifestSchemaError",
        "ManifestWriteError",
        "OPEN_LOVABLE_ATTRIBUTION",
        "build_clone_manifest",
        "compute_manifest_hash",
        "finalise_manifest",
        "inject_html_traceability_comment",
        "manifest_to_audit_payload",
        "manifest_to_dict",
        "parse_html_traceability_comment",
        "pin_clone_artefacts",
        "read_manifest_file",
        "record_clone_audit",
        "render_html_traceability_comment",
        "serialize_manifest_json",
        "verify_manifest_hash",
        "write_manifest_file",
    }
    assert expected.issubset(set(web_pkg.__all__))
    for name in expected:
        assert hasattr(web_pkg, name), f"web package missing {name}"


def test_total_re_exports_drift_guard() -> None:
    """Drift guard: bumping this number on every W11 row makes adding
    a new symbol an explicit code-review event (matches W11.5/W11.6
    pattern). W13.2 adds 7 screenshot-breakpoint symbols → 199."""
    assert len(web_pkg.__all__) == 199, (
        "If you added/removed a public symbol, update this expectation "
        "alongside the W11/W13 row's TODO entry — drift here is a code-review "
        "trigger, not a test bug."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  build_clone_manifest
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_build_clone_manifest_happy_path_shape() -> None:
    m = _build_manifest()
    assert m.manifest_version == MANIFEST_VERSION
    assert m.clone_id == "00000000-0000-4000-8000-000000000001"
    assert m.created_at == "2026-04-29T12:00:00Z"
    assert m.tenant_id == "tenant-42"
    assert m.actor == "alice@example.com"
    assert m.attribution == OPEN_LOVABLE_ATTRIBUTION
    assert m.manifest_hash.startswith("sha256:")


def test_build_clone_manifest_source_block() -> None:
    m = _build_manifest()
    assert m.source["url"] == "https://acme.example"
    assert m.source["fetched_at"] == "2026-04-29T00:00:00Z"
    assert m.source["backend"] == "mock"
    assert m.source["status_code"] == 200


def test_build_clone_manifest_omits_status_code_when_unknown() -> None:
    m = _build_manifest(capture_status_code=None)
    assert "status_code" not in m.source


def test_build_clone_manifest_classification_block() -> None:
    cls = _make_classification("medium", categories=("brand_impersonation", "personal_data"))
    m = _build_manifest(classification=cls)
    assert m.classification["risk_level"] == "medium"
    assert m.classification["categories"] == [
        "brand_impersonation", "personal_data",
    ]
    assert m.classification["model"] == "claude-haiku-4.5"
    assert "heuristic" in m.classification["signals_used"]


def test_build_clone_manifest_classification_absent_when_none() -> None:
    m = _build_manifest(classification=None)
    assert m.classification["risk_level"] == "absent"
    assert m.classification["categories"] == []


def test_build_clone_manifest_transformation_block() -> None:
    t = _make_transformed(
        transformations=("bytes_strip", "text_rewrite_heuristic", "image_placeholder"),
        signals_used=("heuristic", "image_placeholder"),
        warnings=("rewrite_llm_unavailable: token freeze",),
        model="heuristic",
    )
    m = _build_manifest(transformed=t)
    assert m.transformation["transformations"] == [
        "bytes_strip", "text_rewrite_heuristic", "image_placeholder",
    ]
    assert m.transformation["model"] == "heuristic"
    assert m.transformation["signals_used"] == ["heuristic", "image_placeholder"]
    assert m.transformation["warnings"] == [
        "rewrite_llm_unavailable: token freeze",
    ]


def test_build_clone_manifest_warnings_capped() -> None:
    t = _make_transformed(warnings=tuple(f"w{i}" for i in range(50)))
    m = _build_manifest(transformed=t)
    assert len(m.transformation["warnings"]) <= 16


def test_build_clone_manifest_summary_counts() -> None:
    t = _make_transformed(
        nav_count=4, section_count=5, image_count=3, color_count=7, font_count=2,
    )
    m = _build_manifest(transformed=t)
    s = m.transformed_summary
    assert s["nav_count"] == 4
    assert s["section_count"] == 5
    assert s["image_count"] == 3
    assert s["color_count"] == 7
    assert s["font_count"] == 2
    assert s["has_hero"] is True
    assert s["has_footer"] is True


def test_build_clone_manifest_summary_no_hero_no_footer() -> None:
    t = _make_transformed(has_hero=False, has_footer=False)
    m = _build_manifest(transformed=t)
    assert m.transformed_summary["has_hero"] is False
    assert m.transformed_summary["has_footer"] is False


def test_build_clone_manifest_title_capped() -> None:
    long_title = "x" * 5000
    t = _make_transformed(title=long_title)
    m = _build_manifest(transformed=t)
    assert len(m.transformed_summary["title"]) <= 200


def test_build_clone_manifest_defense_layers_block() -> None:
    m = _build_manifest()
    layers = m.defense_layers
    assert layers["L1_machine_refusal"] == "passed"
    assert layers["L2_content_classifier"] == "low"
    assert layers["L3_output_transformer"] == [
        "bytes_strip", "text_rewrite", "image_placeholder",
    ]
    assert layers["L4_traceability"] == f"manifest_v{MANIFEST_VERSION}"


def test_build_clone_manifest_defense_layers_l1_refused() -> None:
    m = _build_manifest(refusal_decision=_make_refusal(allowed=False))
    assert m.defense_layers["L1_machine_refusal"] == "refused"


def test_build_clone_manifest_defense_layers_l1_absent_when_none() -> None:
    m = _build_manifest(refusal_decision=None)
    assert m.defense_layers["L1_machine_refusal"] == "absent"


def test_build_clone_manifest_defense_layers_l2_absent_when_none() -> None:
    m = _build_manifest(classification=None)
    assert m.defense_layers["L2_content_classifier"] == "absent"


def test_build_clone_manifest_auto_clone_id_when_omitted() -> None:
    m = _build_manifest(clone_id=None)
    # UUID4 is 36 chars with 4 dashes.
    assert re.match(r"^[0-9a-f-]{36}$", m.clone_id)


def test_build_clone_manifest_auto_created_at_when_omitted() -> None:
    m = _build_manifest(created_at=None)
    # ISO-8601 UTC with Z suffix.
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", m.created_at)


def test_build_clone_manifest_finalize_hash_false_leaves_empty() -> None:
    m = _build_manifest(finalize_hash=False)
    assert m.manifest_hash == ""


def test_build_clone_manifest_rejects_bad_transformed() -> None:
    with pytest.raises(ManifestSchemaError):
        build_clone_manifest(
            source_url="https://x",
            fetched_at="2026-04-29T00:00:00Z",
            backend="mock",
            classification=None,
            transformed="not-a-spec",  # type: ignore[arg-type]
            tenant_id="t",
            actor="a",
        )


def test_build_clone_manifest_rejects_bad_classification() -> None:
    with pytest.raises(ManifestSchemaError):
        build_clone_manifest(
            source_url="https://x",
            fetched_at="2026-04-29T00:00:00Z",
            backend="mock",
            classification="not-a-classification",  # type: ignore[arg-type]
            transformed=_make_transformed(),
            tenant_id="t",
            actor="a",
        )


def test_build_clone_manifest_rejects_bad_refusal_decision() -> None:
    with pytest.raises(ManifestSchemaError):
        build_clone_manifest(
            source_url="https://x",
            fetched_at="2026-04-29T00:00:00Z",
            backend="mock",
            classification=None,
            transformed=_make_transformed(),
            tenant_id="t",
            actor="a",
            refusal_decision="bad",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad", ["", " ", None])
def test_build_clone_manifest_rejects_blank_source_url(bad) -> None:
    with pytest.raises(ManifestSchemaError):
        build_clone_manifest(
            source_url=bad,  # type: ignore[arg-type]
            fetched_at="2026-04-29T00:00:00Z",
            backend="mock",
            classification=None,
            transformed=_make_transformed(),
            tenant_id="t",
            actor="a",
        )


@pytest.mark.parametrize("bad_field", ["tenant_id", "actor"])
def test_build_clone_manifest_rejects_blank_required_string(bad_field) -> None:
    args = dict(
        source_url="https://x",
        fetched_at="2026-04-29T00:00:00Z",
        backend="mock",
        classification=None,
        transformed=_make_transformed(),
        tenant_id="t",
        actor="a",
    )
    args[bad_field] = ""
    with pytest.raises(ManifestSchemaError):
        build_clone_manifest(**args)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hash + serialisation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_compute_manifest_hash_deterministic() -> None:
    m = _build_manifest()
    payload = manifest_to_dict(m)
    h1 = compute_manifest_hash(payload)
    h2 = compute_manifest_hash(payload)
    assert h1 == h2
    assert h1.startswith("sha256:")
    # 7-char prefix + 64 hex digits.
    assert len(h1) == len("sha256:") + 64


def test_compute_manifest_hash_excludes_existing_hash_field() -> None:
    m = _build_manifest()
    payload = manifest_to_dict(m)
    h_before = compute_manifest_hash(payload)
    payload[MANIFEST_HASH_FIELD] = "sha256:overwritten"
    h_after = compute_manifest_hash(payload)
    # The hash field is stripped from the canonical body, so flipping
    # its value must not change the digest.
    assert h_before == h_after


def test_compute_manifest_hash_changes_when_field_changes() -> None:
    m1 = _build_manifest(actor="alice@example.com")
    m2 = _build_manifest(actor="bob@example.com")
    assert m1.manifest_hash != m2.manifest_hash


def test_compute_manifest_hash_rejects_non_mapping() -> None:
    with pytest.raises(ManifestSchemaError):
        compute_manifest_hash("not a mapping")  # type: ignore[arg-type]


def test_finalise_manifest_idempotent() -> None:
    m = _build_manifest(finalize_hash=False)
    f1 = finalise_manifest(m)
    f2 = finalise_manifest(f1)
    assert f1.manifest_hash == f2.manifest_hash
    assert f1.manifest_hash.startswith("sha256:")


def test_finalise_manifest_rejects_non_manifest() -> None:
    with pytest.raises(ManifestSchemaError):
        finalise_manifest({"x": 1})  # type: ignore[arg-type]


def test_verify_manifest_hash_passes_finalised() -> None:
    m = _build_manifest()
    assert verify_manifest_hash(m) is True


def test_verify_manifest_hash_fails_unfinalised() -> None:
    m = _build_manifest(finalize_hash=False)
    assert verify_manifest_hash(m) is False


def test_verify_manifest_hash_detects_post_finalise_tamper() -> None:
    from dataclasses import replace as dc_replace

    m = _build_manifest()
    tampered = dc_replace(m, actor="mallory@example.com")
    # actor changed but the original hash is preserved → verification
    # must catch the mismatch.
    assert verify_manifest_hash(tampered) is False


def test_verify_manifest_hash_rejects_non_manifest() -> None:
    with pytest.raises(ManifestSchemaError):
        verify_manifest_hash({"x": 1})  # type: ignore[arg-type]


def test_manifest_to_dict_top_level_keys() -> None:
    m = _build_manifest()
    d = manifest_to_dict(m)
    assert set(d.keys()) == {
        "manifest_version", "clone_id", "created_at", "tenant_id", "actor",
        "source", "classification", "transformation",
        "transformed_summary", "defense_layers", "attribution",
        MANIFEST_HASH_FIELD,
    }


def test_manifest_to_dict_nested_blocks_are_plain_dicts() -> None:
    m = _build_manifest()
    d = manifest_to_dict(m)
    for key in ("source", "classification", "transformation",
                "transformed_summary", "defense_layers"):
        assert isinstance(d[key], dict), f"{key} should be plain dict"


def test_manifest_to_dict_rejects_non_manifest() -> None:
    with pytest.raises(ManifestSchemaError):
        manifest_to_dict({"x": 1})  # type: ignore[arg-type]


def test_serialize_manifest_json_round_trip() -> None:
    m = _build_manifest()
    raw = serialize_manifest_json(m)
    parsed = json.loads(raw)
    assert parsed["clone_id"] == m.clone_id
    assert parsed[MANIFEST_HASH_FIELD] == m.manifest_hash


def test_serialize_manifest_json_sorted_keys() -> None:
    m = _build_manifest()
    raw = serialize_manifest_json(m, indent=None)
    # First top-level key must be alphabetically smallest.
    parsed = json.loads(raw)
    assert list(parsed.keys()) == sorted(parsed.keys())


def test_serialize_manifest_json_indent_default_two() -> None:
    m = _build_manifest()
    raw = serialize_manifest_json(m)
    # With indent=2 the JSON contains newlines.
    assert "\n" in raw


def test_serialize_manifest_json_indent_none_compact() -> None:
    m = _build_manifest()
    raw = serialize_manifest_json(m, indent=None)
    # Compact mode has no leading whitespace on body lines.
    assert "\n  " not in raw


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  On-disk writer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_write_manifest_file_creates_dir_and_file(tmp_path: Path) -> None:
    m = _build_manifest()
    p = write_manifest_file(m, project_root=tmp_path)
    assert p.exists()
    assert p.parent.name == MANIFEST_DIR
    assert p.name == MANIFEST_FILENAME
    parsed = json.loads(p.read_text(encoding="utf-8"))
    assert parsed["clone_id"] == m.clone_id


def test_write_manifest_file_overwrites_existing(tmp_path: Path) -> None:
    p1 = write_manifest_file(_build_manifest(actor="a@b.c"), project_root=tmp_path)
    p2 = write_manifest_file(_build_manifest(actor="x@y.z"), project_root=tmp_path)
    assert p1 == p2
    parsed = json.loads(p2.read_text(encoding="utf-8"))
    assert parsed["actor"] == "x@y.z"


def test_write_manifest_file_finalises_unhashed_input(tmp_path: Path) -> None:
    m = _build_manifest(finalize_hash=False)
    assert m.manifest_hash == ""
    p = write_manifest_file(m, project_root=tmp_path)
    parsed = json.loads(p.read_text(encoding="utf-8"))
    assert parsed[MANIFEST_HASH_FIELD].startswith("sha256:")


def test_write_manifest_file_rejects_non_manifest(tmp_path: Path) -> None:
    with pytest.raises(ManifestSchemaError):
        write_manifest_file({"x": 1}, project_root=tmp_path)  # type: ignore[arg-type]


def test_write_manifest_file_oserror_translated(tmp_path: Path) -> None:
    # Make a file at the would-be ``.omnisight/`` path so mkdir fails.
    blocker = tmp_path / MANIFEST_DIR
    blocker.write_text("not a directory")
    m = _build_manifest()
    with pytest.raises(ManifestWriteError):
        write_manifest_file(m, project_root=tmp_path)


def test_read_manifest_file_round_trips(tmp_path: Path) -> None:
    m = _build_manifest()
    write_manifest_file(m, project_root=tmp_path)
    loaded = read_manifest_file(tmp_path)
    assert loaded.clone_id == m.clone_id
    assert loaded.tenant_id == m.tenant_id
    assert loaded.manifest_hash == m.manifest_hash
    assert verify_manifest_hash(loaded) is True


def test_read_manifest_file_missing_raises_write_error(tmp_path: Path) -> None:
    with pytest.raises(ManifestWriteError):
        read_manifest_file(tmp_path)


def test_read_manifest_file_invalid_json_raises_schema(tmp_path: Path) -> None:
    target = tmp_path / MANIFEST_DIR / MANIFEST_FILENAME
    target.parent.mkdir(parents=True)
    target.write_text("{not json")
    with pytest.raises(ManifestSchemaError):
        read_manifest_file(tmp_path)


def test_read_manifest_file_wrong_version_raises_schema(tmp_path: Path) -> None:
    target = tmp_path / MANIFEST_DIR / MANIFEST_FILENAME
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "manifest_version": "999",
        "clone_id": "x", "created_at": "x", "tenant_id": "x", "actor": "x",
    }))
    with pytest.raises(ManifestSchemaError):
        read_manifest_file(tmp_path)


def test_read_manifest_file_non_object_raises_schema(tmp_path: Path) -> None:
    target = tmp_path / MANIFEST_DIR / MANIFEST_FILENAME
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ManifestSchemaError):
        read_manifest_file(tmp_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTML traceability comment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_render_html_comment_has_begin_end_markers() -> None:
    m = _build_manifest()
    rendered = render_html_traceability_comment(m)
    assert rendered.startswith(HTML_COMMENT_BEGIN)
    assert rendered.endswith(HTML_COMMENT_END)


def test_render_html_comment_includes_required_keys() -> None:
    m = _build_manifest()
    rendered = render_html_traceability_comment(m)
    for key in (
        "manifest_version", "clone_id", "source_url", "fetched_at",
        "backend", "risk_level", "categories", "transformations", "model",
        "manifest_path", "manifest_hash", "attribution",
    ):
        assert f"{key}:" in rendered, f"missing key {key} in HTML comment"


def test_render_html_comment_includes_manifest_relative_path() -> None:
    m = _build_manifest()
    rendered = render_html_traceability_comment(m)
    assert MANIFEST_RELATIVE_PATH in rendered


def test_render_html_comment_includes_open_lovable_attribution() -> None:
    m = _build_manifest()
    rendered = render_html_traceability_comment(m)
    assert "open-lovable" in rendered
    assert "MIT" in rendered


def test_render_html_comment_escapes_premature_terminator() -> None:
    """A user-controlled value cannot terminate the HTML comment early."""
    t = _make_transformed(model="evil-->model")
    m = _build_manifest(transformed=t)
    rendered = render_html_traceability_comment(m)
    body = rendered.split(HTML_COMMENT_BEGIN, 1)[1].split(HTML_COMMENT_END, 1)[0]
    assert "-->" not in body


def test_render_html_comment_rejects_non_manifest() -> None:
    with pytest.raises(ManifestSchemaError):
        render_html_traceability_comment({"x": 1})  # type: ignore[arg-type]


def test_parse_html_comment_round_trip() -> None:
    m = _build_manifest()
    rendered = render_html_traceability_comment(m)
    parsed = parse_html_traceability_comment(rendered)
    assert parsed is not None
    assert parsed["clone_id"] == m.clone_id
    assert parsed["manifest_version"] == MANIFEST_VERSION
    assert parsed["manifest_path"] == MANIFEST_RELATIVE_PATH
    assert parsed["manifest_hash"] == m.manifest_hash


def test_parse_html_comment_returns_none_when_absent() -> None:
    assert parse_html_traceability_comment("<html>nothing here</html>") is None


def test_parse_html_comment_returns_none_for_non_string() -> None:
    assert parse_html_traceability_comment(None) is None  # type: ignore[arg-type]
    assert parse_html_traceability_comment(123) is None  # type: ignore[arg-type]


def test_parse_html_comment_extracts_from_full_html_doc() -> None:
    m = _build_manifest()
    rendered = render_html_traceability_comment(m)
    full_html = f"<!doctype html><html><head>{rendered}</head><body>x</body></html>"
    parsed = parse_html_traceability_comment(full_html)
    assert parsed is not None
    assert parsed["clone_id"] == m.clone_id


# ── inject_html_traceability_comment ──────────────────────────────────


def test_inject_into_head_inserts_after_head_tag() -> None:
    m = _build_manifest()
    html = "<html><head><title>x</title></head><body>y</body></html>"
    out = inject_html_traceability_comment(html, m, position="head")
    head_idx = out.lower().find("<head>")
    comment_idx = out.find(HTML_COMMENT_BEGIN)
    assert head_idx != -1 and comment_idx != -1
    assert head_idx < comment_idx
    # The original <title> must still come after the comment.
    title_idx = out.lower().find("<title>")
    assert comment_idx < title_idx


def test_inject_into_body_start_inserts_after_body_tag() -> None:
    m = _build_manifest()
    html = "<html><body><h1>x</h1></body></html>"
    out = inject_html_traceability_comment(html, m, position="body_start")
    body_idx = out.lower().find("<body>")
    comment_idx = out.find(HTML_COMMENT_BEGIN)
    h1_idx = out.find("<h1>")
    assert body_idx < comment_idx < h1_idx


def test_inject_prepend_puts_comment_first() -> None:
    m = _build_manifest()
    html = "<html><body>x</body></html>"
    out = inject_html_traceability_comment(html, m, position="prepend")
    assert out.startswith(HTML_COMMENT_BEGIN)


def test_inject_falls_back_to_prepend_when_no_anchor() -> None:
    m = _build_manifest()
    html = "fragment without head or body"
    out = inject_html_traceability_comment(html, m, position="head")
    assert HTML_COMMENT_BEGIN in out
    # Original content must be retained.
    assert "fragment without head or body" in out


def test_inject_idempotent_replaces_existing_block() -> None:
    m1 = _build_manifest(actor="a@b.c")
    m2 = _build_manifest(actor="x@y.z")
    html = "<html><head></head><body></body></html>"
    once = inject_html_traceability_comment(html, m1, position="head")
    twice = inject_html_traceability_comment(once, m2, position="head")
    # Exactly one BEGIN marker should be present.
    assert twice.count(HTML_COMMENT_BEGIN) == 1
    parsed = parse_html_traceability_comment(twice)
    assert parsed is not None
    assert parsed["clone_id"] == m2.clone_id


def test_inject_rejects_non_string_html() -> None:
    m = _build_manifest()
    with pytest.raises(ManifestSchemaError):
        inject_html_traceability_comment(b"<html/>", m)  # type: ignore[arg-type]


def test_inject_rejects_unknown_position() -> None:
    m = _build_manifest()
    with pytest.raises(ManifestSchemaError):
        inject_html_traceability_comment("<html/>", m, position="footer")


def test_inject_then_parse_retains_manifest_hash() -> None:
    m = _build_manifest()
    html = "<html><head></head></html>"
    out = inject_html_traceability_comment(html, m, position="head")
    parsed = parse_html_traceability_comment(out)
    assert parsed is not None
    assert parsed["manifest_hash"] == m.manifest_hash


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit log integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _patch_audit_log(monkeypatch, captured: dict, *, return_id: Optional[int] = 7):
    """Replace ``backend.audit.log`` (the symbol imported by
    ``record_clone_audit``) with a fake that records every kwarg and
    returns ``return_id``."""

    async def fake_log(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return return_id

    monkeypatch.setattr("backend.audit.log", fake_log)


def test_record_clone_audit_routes_to_backend_audit_log(monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured, return_id=42)
    m = _build_manifest()
    rid = _await(record_clone_audit(m))
    assert rid == 42
    assert captured["action"] == AUDIT_ACTION
    assert captured["entity_kind"] == AUDIT_ENTITY_KIND
    assert captured["entity_id"] == m.clone_id
    assert captured["actor"] == m.actor
    assert captured["before"] is None
    assert captured["session_id"] is None


def test_record_clone_audit_after_payload_is_full_manifest(monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured)
    m = _build_manifest()
    _await(record_clone_audit(m))
    after = captured["after"]
    assert after["clone_id"] == m.clone_id
    assert after["tenant_id"] == m.tenant_id
    assert after["manifest_version"] == MANIFEST_VERSION
    assert after[MANIFEST_HASH_FIELD] == m.manifest_hash


def test_record_clone_audit_passes_session_id_through(monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured)
    m = _build_manifest()
    _await(record_clone_audit(m, session_id="sess-abc"))
    assert captured["session_id"] == "sess-abc"


def test_record_clone_audit_passes_conn_through(monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured)
    m = _build_manifest()
    sentinel = object()
    _await(record_clone_audit(m, conn=sentinel))
    assert captured["conn"] is sentinel


def test_record_clone_audit_returns_none_when_audit_fails(monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured, return_id=None)
    m = _build_manifest()
    rid = _await(record_clone_audit(m))
    assert rid is None


def test_record_clone_audit_rejects_non_manifest(monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured)
    with pytest.raises(ManifestSchemaError):
        _await(record_clone_audit({"x": 1}))  # type: ignore[arg-type]


def test_manifest_to_audit_payload_has_all_top_level_keys() -> None:
    m = _build_manifest()
    payload = manifest_to_audit_payload(m)
    assert payload["clone_id"] == m.clone_id
    assert payload["tenant_id"] == m.tenant_id
    assert payload["actor"] == m.actor
    assert MANIFEST_HASH_FIELD in payload
    assert "source" in payload
    assert "classification" in payload
    assert "transformation" in payload
    assert "transformed_summary" in payload
    assert "defense_layers" in payload
    assert payload["attribution"] == OPEN_LOVABLE_ATTRIBUTION


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  pin_clone_artefacts (one-shot orchestrator)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_pin_clone_artefacts_writes_manifest_only(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured, return_id=11)
    m = _build_manifest()
    rec = _await(pin_clone_artefacts(
        manifest=m, project_root=tmp_path, html=None, html_path=None,
    ))
    assert rec.manifest is m
    assert rec.manifest_path.exists()
    assert rec.html_path is None
    assert rec.audit_row_id == 11


def test_pin_clone_artefacts_skips_writer_when_no_root(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured, return_id=12)
    m = _build_manifest()
    rec = _await(pin_clone_artefacts(
        manifest=m, project_root=None, html=None, html_path=None,
    ))
    # No file was written but the record still carries a resolved path
    # (anchored to CWD) so the operator can echo it.
    assert rec.manifest_path is not None
    assert rec.audit_row_id == 12


def test_pin_clone_artefacts_writes_html_when_path_given(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured, return_id=13)
    m = _build_manifest()
    html_target = tmp_path / "out" / "index.html"
    rec = _await(pin_clone_artefacts(
        manifest=m, project_root=tmp_path,
        html="<html><head></head><body>x</body></html>",
        html_path=html_target,
    ))
    assert rec.html_path == html_target
    written = html_target.read_text(encoding="utf-8")
    assert HTML_COMMENT_BEGIN in written
    parsed = parse_html_traceability_comment(written)
    assert parsed is not None
    assert parsed["clone_id"] == m.clone_id


def test_pin_clone_artefacts_skips_audit_when_disabled(tmp_path: Path, monkeypatch) -> None:
    """When ``record_audit=False`` we must not call the audit log at all."""
    called = {"n": 0}

    async def fake_log(**kwargs):
        called["n"] += 1
        return 99

    monkeypatch.setattr("backend.audit.log", fake_log)
    m = _build_manifest()
    rec = _await(pin_clone_artefacts(
        manifest=m, project_root=tmp_path, record_audit=False,
    ))
    assert called["n"] == 0
    assert rec.audit_row_id is None


def test_pin_clone_artefacts_skips_writer_when_disabled(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured, return_id=14)
    m = _build_manifest()
    rec = _await(pin_clone_artefacts(
        manifest=m, project_root=tmp_path, write_manifest=False,
    ))
    # No file should have been written.
    assert not (tmp_path / MANIFEST_DIR / MANIFEST_FILENAME).exists()
    # But the audit row still landed.
    assert rec.audit_row_id == 14


def test_pin_clone_artefacts_html_write_error_translated(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured, return_id=15)
    m = _build_manifest()
    # Make html_path point at a name where the parent already exists as
    # a regular file → mkdir(parents=True) will fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    bad_html_path = blocker / "index.html"
    with pytest.raises(ManifestWriteError):
        _await(pin_clone_artefacts(
            manifest=m, project_root=tmp_path,
            html="<html/>", html_path=bad_html_path,
        ))


def test_pin_clone_artefacts_rejects_non_manifest(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured)
    with pytest.raises(ManifestSchemaError):
        _await(pin_clone_artefacts(
            manifest={"x": 1},  # type: ignore[arg-type]
            project_root=tmp_path,
        ))


def test_pin_clone_artefacts_inject_position_passed_through(
    tmp_path: Path, monkeypatch,
) -> None:
    captured: dict = {}
    _patch_audit_log(monkeypatch, captured, return_id=16)
    m = _build_manifest()
    target = tmp_path / "out.html"
    _await(pin_clone_artefacts(
        manifest=m, project_root=tmp_path,
        html="<html><body>X</body></html>", html_path=target,
        inject_position="body_start",
    ))
    content = target.read_text(encoding="utf-8")
    body_idx = content.lower().find("<body>")
    comment_idx = content.find(HTML_COMMENT_BEGIN)
    assert body_idx < comment_idx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Whole-spec invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_audit_action_namespace_pinned() -> None:
    """The audit action MUST use the ``web.`` namespace so query
    filters (``WHERE action LIKE 'web.%'``) catch every L4 row without
    mistakenly catching auth / billing rows."""
    assert AUDIT_ACTION.startswith("web.")


def test_manifest_hash_field_name_pinned() -> None:
    """The hash field name is part of the on-disk schema. Renaming it
    is a schema-version bump (W11.7 v2)."""
    assert MANIFEST_HASH_FIELD == "manifest_hash"


def test_manifest_relative_path_invariant() -> None:
    """Downstream tooling locates the manifest by this exact path. If
    we move it, every consumer must move with it."""
    assert MANIFEST_RELATIVE_PATH == ".omnisight/clone-manifest.json"


def test_open_lovable_attribution_pinned() -> None:
    """Attribution text must travel with every artefact regardless of
    when W11.13 lands."""
    assert "open-lovable" in OPEN_LOVABLE_ATTRIBUTION
    assert "MIT" in OPEN_LOVABLE_ATTRIBUTION
    assert "LICENSES/open-lovable-mit.txt" in OPEN_LOVABLE_ATTRIBUTION


def test_html_comment_markers_machine_parseable() -> None:
    """The begin/end markers must contain stable anchors so external
    tools (DMCA scanners, takedown UIs) can grep for cloned artefacts."""
    assert "omnisight:clone:begin" in HTML_COMMENT_BEGIN
    assert "omnisight:clone:end" in HTML_COMMENT_END


def test_full_round_trip_write_read_verify(tmp_path: Path) -> None:
    """End-to-end: build → write → read → verify hash → parse HTML
    comment. This is the canonical flow the W11.11 snapshot harness
    will exercise."""
    m1 = _build_manifest()
    write_manifest_file(m1, project_root=tmp_path)
    m2 = read_manifest_file(tmp_path)
    assert verify_manifest_hash(m2) is True
    rendered = render_html_traceability_comment(m2)
    parsed = parse_html_traceability_comment(rendered)
    assert parsed is not None
    assert parsed["clone_id"] == m1.clone_id
