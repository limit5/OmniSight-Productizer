"""W11.11 #XXX — 5 reference URL × snapshot diff regression tests.

Pins the end-to-end behaviour of the W11 clone pipeline against a fixed
set of five reference HTML fixtures (each representing a different
real-world clone shape: SaaS landing page, deeply-nested docs page,
single-article blog post, image-heavy portfolio, sparse "coming soon"
stub). For every fixture the test runs the full L1→L4 pipeline plus
W11.9 multi-framework render plus W11.10 agent-prompt context block,
then diffs the result against a golden JSON snapshot pinned in
``backend/tests/golden/clone_reference/<fixture>.json``.

Why a snapshot diff is the right shape for this row
---------------------------------------------------
W11.1–W11.10 already ship per-row contract tests covering every helper
in isolation. What those tests do **not** catch is *cross-row drift*
— e.g. a future tweak to ``build_clone_spec_from_capture`` that
inadvertently changes the section count, propagates into the manifest's
``transformed_summary``, changes the manifest hash, and silently breaks
DMCA tooling that grep's for the old hash. The W11.11 row exists
precisely to lock the cross-row contract: drift in *any* layer surfaces
as a snapshot diff, and the operator either updates the golden (after
review) or fixes the underlying regression.

What the snapshot pins
----------------------
For each fixture, the JSON snapshot pins:

* ``spec`` — the populated :class:`CloneSpec` summary (title / hero
  shape / nav count / section count / image count / colour count /
  font count / spacing keys / warnings).
* ``transformed`` — the L3 :class:`TransformedSpec` summary
  (rewritten title / hero shape / nav labels / section headings /
  image placeholder URLs / colour + font tokens / signals_used /
  transformations / warnings). The deterministic heuristic rewrite
  path is exercised (no LLM I/O) so the snapshot is reproducible
  bit-for-bit across CI runs.
* ``manifest`` — the full :class:`CloneManifest` dict (with pinned
  ``clone_id`` + ``created_at`` + ``manifest_hash``). Any change to
  the manifest schema or to upstream layer outputs flips the hash
  and therefore the snapshot.
* ``rendered`` — the W11.9 render output's audit-payload projection
  for each of the three supported frameworks (Next / Nuxt / Astro):
  framework name + adapter name + emitted file paths + traceability
  metadata. File contents are not embedded (the per-row test for
  W11.9 already pins them) — we just pin the shape so a regression
  that drops a file or renames it surfaces here too.
* ``context`` — the W11.10 agent-prompt context block, line-broken
  for diff readability.

Determinism contract
--------------------
The harness pins ``clone_id``, ``created_at``, and the LLM rewrite
path so the snapshot is reproducible without network I/O:

* Capture timestamp + clone_id + manifest created_at are hard-coded.
* The L3 transformer is given a stub :class:`TextRewriteLLM` that
  always raises :class:`RewriteUnavailableError`, forcing the
  deterministic heuristic rewrite fallback. This pins behaviour
  regardless of which provider key happens to be configured in CI.
* The L2 classifier is bypassed via a hand-rolled
  :class:`RiskClassification` with ``model="heuristic"`` so no LLM
  call is required.
* The L1 :class:`RefusalDecision` is hand-rolled with
  ``allowed=True`` so the manifest's ``defense_layers.L1_machine_refusal``
  reads ``"passed"`` (not ``"absent"``).

Regenerating the snapshots
--------------------------
When an intentional change to any W11 layer requires a snapshot
update, regenerate by setting ``OMNISIGHT_W11_REGENERATE_SNAPSHOTS=1``
and re-running pytest. The harness rewrites the goldens in place. The
operator must then commit the resulting diff alongside the layer
change so reviewers see the cross-row impact in a single PR.

Module-global state audit (SOP §1)
----------------------------------
N/A. This is a pure pytest module — every fixture is loaded fresh per
test, the harness builds a new pipeline state per call, and the only
"shared" state is the on-disk goldens (which are read-only at test
time unless the regenerate flag is set). Cross-worker consistency:
trivially answer #1 (every worker reads the same fixture + golden
files from disk).

Inspired by firecrawl/open-lovable (MIT). The full attribution +
license text lands in the W11.13 row.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Mapping

import pytest

from backend.web.clone_manifest import (
    CloneManifest,
    build_clone_manifest,
    manifest_to_dict,
    parse_html_traceability_comment,
)
from backend.web.clone_spec_context import build_clone_spec_context
from backend.web.content_classifier import RiskClassification, RiskScore
from backend.web.framework_adapter import (
    SUPPORTED_FRAMEWORKS,
    project_to_audit_payload,
    render_clone_project,
)
from backend.web.output_transformer import (
    RewriteUnavailableError,
    TransformedSpec,
    transform_clone_spec,
)
from backend.web.refusal_signals import RefusalDecision
from backend.web.site_cloner import (
    CloneSpec,
    RawCapture,
    build_clone_spec_from_capture,
)


# ── Fixture catalogue ─────────────────────────────────────────────────


#: The five reference fixtures pinned by W11.11. Each row is a tuple of
#: ``(slug, source_url, capture_backend, capture_status_code,
#: tenant_id, actor, clone_id, created_at)``. The slug doubles as both
#: the on-disk fixture filename stem and the golden snapshot filename.
#:
#: Five distinct shapes deliberately exercise different parts of the
#: pipeline:
#:
#: 1. ``01_landing_page`` — full-fat SaaS marketing page; happy path.
#: 2. ``02_docs_page`` — documentation page with eight nav items + seven
#:    sections; exercises section/nav cap + heuristic rewrite at scale.
#: 3. ``03_blog_post`` — single-article retrospective; minimal nav,
#:    rich body copy, custom typography.
#: 4. ``04_portfolio_page`` — image-heavy gallery; exercises image
#:    placeholder substitution at MAX_RENDERED_IMAGES quantity.
#: 5. ``05_minimal_page`` — sparse "coming soon" stub; exercises empty-
#:    spec degradation paths (no nav / no sections / no images).
REFERENCE_FIXTURES: tuple[dict[str, Any], ...] = (
    {
        "slug": "01_landing_page",
        "source_url": "https://acme.example/landing",
        "capture_backend": "mock",
        "capture_status_code": 200,
        "tenant_id": "tenant-fixture-001",
        "actor": "snapshot-harness@omnisight.test",
        "clone_id": "00000000-0000-4000-8000-000000000001",
        "created_at": "2026-04-29T00:00:00Z",
        "fetched_at": "2026-04-29T00:00:00Z",
    },
    {
        "slug": "02_docs_page",
        "source_url": "https://docs.globex.example/sdk/getting-started",
        "capture_backend": "mock",
        "capture_status_code": 200,
        "tenant_id": "tenant-fixture-002",
        "actor": "snapshot-harness@omnisight.test",
        "clone_id": "00000000-0000-4000-8000-000000000002",
        "created_at": "2026-04-29T00:00:00Z",
        "fetched_at": "2026-04-29T00:00:00Z",
    },
    {
        "slug": "03_blog_post",
        "source_url": "https://eng.initech.example/blog/build-system",
        "capture_backend": "mock",
        "capture_status_code": 200,
        "tenant_id": "tenant-fixture-003",
        "actor": "snapshot-harness@omnisight.test",
        "clone_id": "00000000-0000-4000-8000-000000000003",
        "created_at": "2026-04-29T00:00:00Z",
        "fetched_at": "2026-04-29T00:00:00Z",
    },
    {
        "slug": "04_portfolio_page",
        "source_url": "https://umbrella.example/work",
        "capture_backend": "mock",
        "capture_status_code": 200,
        "tenant_id": "tenant-fixture-004",
        "actor": "snapshot-harness@omnisight.test",
        "clone_id": "00000000-0000-4000-8000-000000000004",
        "created_at": "2026-04-29T00:00:00Z",
        "fetched_at": "2026-04-29T00:00:00Z",
    },
    {
        "slug": "05_minimal_page",
        "source_url": "https://stark.example/coming-soon",
        "capture_backend": "mock",
        "capture_status_code": 200,
        "tenant_id": "tenant-fixture-005",
        "actor": "snapshot-harness@omnisight.test",
        "clone_id": "00000000-0000-4000-8000-000000000005",
        "created_at": "2026-04-29T00:00:00Z",
        "fetched_at": "2026-04-29T00:00:00Z",
    },
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "clone_reference"
GOLDEN_DIR = Path(__file__).parent / "golden" / "clone_reference"
REGENERATE_ENV_VAR = "OMNISIGHT_W11_REGENERATE_SNAPSHOTS"


# ── Test doubles ──────────────────────────────────────────────────────


class _UnavailableRewriteLLM:
    """:class:`TextRewriteLLM` stub that always raises
    :class:`RewriteUnavailableError`.

    Forces :func:`transform_clone_spec` down the deterministic
    heuristic-rewrite fallback so the snapshot is reproducible without
    any LLM provider key. The L3 transformer's documented contract
    (W11.6 row docstring §"Failure modes") is that this fallback is
    bit-stable — same input → same heuristic envelope.
    """

    name = "snapshot-harness-stub"

    async def rewrite_text(self, prompt: str, *, system: str) -> str:
        raise RewriteUnavailableError(
            "snapshot harness — forces heuristic fallback for determinism"
        )


def _stub_classification() -> RiskClassification:
    """Hand-roll a clean L2 verdict so the manifest carries a real
    ``classification`` block (not the ``"absent"`` placeholder used when
    L2 is bypassed entirely)."""
    return RiskClassification(
        risk_level="low",
        scores=(
            RiskScore(
                category="clean",
                level="low",
                reason="snapshot harness stub — fixture pre-vetted",
            ),
        ),
        model="snapshot-harness-classifier",
        signals_used=("heuristic",),
    )


def _stub_refusal_decision(source_url: str) -> RefusalDecision:
    """Hand-roll a passing L1 verdict so the manifest's
    ``defense_layers.L1_machine_refusal`` reads ``"passed"`` (not
    ``"absent"``)."""
    return RefusalDecision(
        allowed=True,
        signals_checked=("robots", "ai.txt", "meta_noai"),
        reasons=(),
        details={"url": source_url},
    )


# ── Snapshot rendering helpers ────────────────────────────────────────


def _spec_summary(spec: CloneSpec) -> Mapping[str, Any]:
    """Project the W11.3 :class:`CloneSpec` onto a snapshot-friendly
    dict. Counts + first-N values rather than full enumeration so a
    one-character text edit in the fixture doesn't churn the snapshot
    while the structural counts stay pinned."""
    return {
        "title": spec.title,
        "meta_keys": sorted((spec.meta or {}).keys()),
        "hero": spec.hero,
        "nav_count": len(spec.nav or []),
        "nav_labels": [n.get("label") for n in (spec.nav or [])],
        "section_count": len(spec.sections or []),
        "section_headings": [s.get("heading") for s in (spec.sections or [])],
        "image_count": len(spec.images or []),
        "image_urls": [i.get("url") for i in (spec.images or [])],
        "color_count": len(spec.colors or []),
        "colors": list(spec.colors or []),
        "font_count": len(spec.fonts or []),
        "fonts": list(spec.fonts or []),
        "spacing_keys": sorted((spec.spacing or {}).keys()),
        "warnings": list(spec.warnings or []),
    }


def _transformed_summary(transformed: TransformedSpec) -> Mapping[str, Any]:
    """Project the L3 :class:`TransformedSpec` onto a snapshot-friendly
    dict. Captures every text surface the rewrite path produced so a
    drift in ``_heuristic_rewrite_text`` surfaces as a snapshot diff."""
    return {
        "title": transformed.title,
        "meta": dict(transformed.meta or {}),
        "hero": dict(transformed.hero) if transformed.hero else None,
        "nav": [dict(n) for n in (transformed.nav or ())],
        "sections": [dict(s) for s in (transformed.sections or ())],
        "footer": dict(transformed.footer) if transformed.footer else None,
        "images": [dict(i) for i in (transformed.images or ())],
        "colors": list(transformed.colors or ()),
        "fonts": list(transformed.fonts or ()),
        "spacing": dict(transformed.spacing or {}),
        "warnings": list(transformed.warnings or ()),
        "signals_used": list(transformed.signals_used or ()),
        "model": transformed.model,
        "transformations": list(transformed.transformations or ()),
    }


def _render_summary_for_framework(
    transformed: TransformedSpec,
    *,
    framework: str,
    manifest: CloneManifest,
) -> Mapping[str, Any]:
    """Render the project for ``framework`` and project to the
    snapshot-friendly W11.12 audit-payload shape (file paths only — file
    contents are pinned by the per-framework W11.9 contract test)."""
    project = render_clone_project(transformed, framework, manifest=manifest)
    payload = project_to_audit_payload(project)
    # Sort the file-path tuple so a stable-but-reordered render does
    # not falsely flag as drift; W11.9's test pins the *unsorted* order
    # already.
    return {
        "framework": payload["framework"],
        "adapter": payload["adapter"],
        "files": sorted(payload["files"]),
        "traceability_html_path": payload["traceability_html_path"],
        "manifest_clone_id": payload["manifest_clone_id"],
        "manifest_hash": payload["manifest_hash"],
    }


def _build_snapshot(fixture: Mapping[str, Any]) -> Mapping[str, Any]:
    """Run the full W11 pipeline against ``fixture`` and project every
    layer's output onto a snapshot-friendly dict."""
    fixture_path = FIXTURE_DIR / f"{fixture['slug']}.html"
    html = fixture_path.read_text(encoding="utf-8")

    capture = RawCapture(
        url=fixture["source_url"],
        html=html,
        status_code=fixture["capture_status_code"],
        fetched_at=fixture["fetched_at"],
        backend=fixture["capture_backend"],
        asset_urls=(),
        headers={},
    )
    spec = build_clone_spec_from_capture(
        capture, source_url=fixture["source_url"],
    )
    classification = _stub_classification()
    transformed = asyncio.run(
        transform_clone_spec(
            spec,
            classification=classification,
            llm=_UnavailableRewriteLLM(),
        )
    )
    refusal = _stub_refusal_decision(fixture["source_url"])
    manifest = build_clone_manifest(
        source_url=fixture["source_url"],
        fetched_at=fixture["fetched_at"],
        backend=fixture["capture_backend"],
        classification=classification,
        transformed=transformed,
        tenant_id=fixture["tenant_id"],
        actor=fixture["actor"],
        capture_status_code=fixture["capture_status_code"],
        refusal_decision=refusal,
        clone_id=fixture["clone_id"],
        created_at=fixture["created_at"],
    )
    rendered = {
        framework: _render_summary_for_framework(
            transformed, framework=framework, manifest=manifest,
        )
        for framework in sorted(SUPPORTED_FRAMEWORKS)
    }
    context_block = build_clone_spec_context(transformed, manifest=manifest)

    return {
        "fixture": {
            "slug": fixture["slug"],
            "source_url": fixture["source_url"],
            "tenant_id": fixture["tenant_id"],
            "clone_id": fixture["clone_id"],
        },
        "spec": _spec_summary(spec),
        "transformed": _transformed_summary(transformed),
        "manifest": manifest_to_dict(manifest),
        "rendered": rendered,
        "context_block_lines": context_block.splitlines(),
    }


def _golden_path(slug: str) -> Path:
    return GOLDEN_DIR / f"{slug}.json"


def _read_golden(slug: str) -> Mapping[str, Any]:
    path = _golden_path(slug)
    if not path.exists():
        raise FileNotFoundError(
            f"missing golden snapshot for fixture {slug!r}: {path}. "
            f"Set {REGENERATE_ENV_VAR}=1 and rerun pytest to "
            f"generate it (then commit alongside the W11 layer "
            f"change that motivated the new snapshot)."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _write_golden(slug: str, snapshot: Mapping[str, Any]) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = _golden_path(slug)
    path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ── Tests — 5 reference URL × snapshot diff ───────────────────────────


@pytest.mark.parametrize(
    "fixture",
    REFERENCE_FIXTURES,
    ids=[f["slug"] for f in REFERENCE_FIXTURES],
)
def test_clone_pipeline_snapshot_matches_golden(
    fixture: Mapping[str, Any],
) -> None:
    """End-to-end pipeline snapshot diff for one of the five reference
    URLs.

    Failure semantics:

    * If the golden does not exist (first run / new fixture), the test
      raises a :class:`FileNotFoundError` instructing the operator to
      run with :envvar:`OMNISIGHT_W11_REGENERATE_SNAPSHOTS`. This path
      is never silent.
    * If the golden exists and the live pipeline produces a different
      output, ``assert live == golden`` fails; pytest's diff renderer
      shows exactly which field drifted.
    * If :envvar:`OMNISIGHT_W11_REGENERATE_SNAPSHOTS` is set, the test
      writes the live output to disk and asserts equality against the
      freshly written file (passes by construction). This is the only
      mode in which the test mutates the workspace.
    """
    snapshot = _build_snapshot(fixture)

    if os.environ.get(REGENERATE_ENV_VAR):
        _write_golden(fixture["slug"], snapshot)

    golden = _read_golden(fixture["slug"])
    assert snapshot == golden, (
        f"W11 pipeline snapshot drifted for fixture {fixture['slug']!r}. "
        f"If the change is intentional, regenerate via "
        f"{REGENERATE_ENV_VAR}=1 pytest and commit the diff."
    )


def test_reference_fixture_count_pinned() -> None:
    """W11.11 row spec literally requires 5 reference URLs. Pin the
    catalogue size so a future PR cannot silently drop a fixture."""
    assert len(REFERENCE_FIXTURES) == 5


def test_reference_slugs_unique() -> None:
    """Every fixture slug must be unique — both for golden filenames
    and for parametrize IDs."""
    slugs = [f["slug"] for f in REFERENCE_FIXTURES]
    assert len(slugs) == len(set(slugs)), f"duplicate slugs: {slugs}"


def test_reference_clone_ids_unique() -> None:
    """Every fixture's pinned clone_id must be unique — the W11.7
    audit chain uses ``clone_id`` as ``entity_id``, so collision would
    cross-link two unrelated audit rows."""
    clone_ids = [f["clone_id"] for f in REFERENCE_FIXTURES]
    assert len(clone_ids) == len(set(clone_ids))


def test_reference_fixtures_all_exist() -> None:
    """Every catalogue entry has a matching HTML file on disk."""
    for fixture in REFERENCE_FIXTURES:
        path = FIXTURE_DIR / f"{fixture['slug']}.html"
        assert path.exists(), f"missing fixture file: {path}"
        assert path.read_text(encoding="utf-8").strip(), (
            f"fixture {path} is empty"
        )


def test_reference_goldens_all_exist() -> None:
    """Every catalogue entry has a matching golden JSON on disk.

    Drift guard: a future PR that adds a fixture but forgets to
    regenerate goldens fails here with a clear pointer to the
    regenerate flow."""
    for fixture in REFERENCE_FIXTURES:
        path = _golden_path(fixture["slug"])
        assert path.exists(), (
            f"missing golden snapshot: {path}. Run with "
            f"{REGENERATE_ENV_VAR}=1 to generate it."
        )


@pytest.mark.parametrize(
    "fixture",
    REFERENCE_FIXTURES,
    ids=[f["slug"] for f in REFERENCE_FIXTURES],
)
def test_snapshot_pipeline_is_deterministic(
    fixture: Mapping[str, Any],
) -> None:
    """Running the harness twice on the same fixture produces an
    identical snapshot. Pins the determinism contract documented in
    the module docstring (heuristic-only LLM, hand-rolled L1/L2,
    pinned clone_id + created_at)."""
    first = _build_snapshot(fixture)
    second = _build_snapshot(fixture)
    assert first == second


@pytest.mark.parametrize(
    "fixture",
    REFERENCE_FIXTURES,
    ids=[f["slug"] for f in REFERENCE_FIXTURES],
)
def test_snapshot_manifest_hash_matches_recompute(
    fixture: Mapping[str, Any],
) -> None:
    """The manifest hash inside the snapshot must verify against the
    rest of the manifest. Catches the case where a future schema change
    silently invalidates :func:`verify_manifest_hash` while the
    snapshot still loads."""
    from backend.web.clone_manifest import compute_manifest_hash

    snapshot = _build_snapshot(fixture)
    manifest = snapshot["manifest"]
    expected = compute_manifest_hash(manifest)
    assert manifest["manifest_hash"] == expected


@pytest.mark.parametrize(
    "fixture",
    REFERENCE_FIXTURES,
    ids=[f["slug"] for f in REFERENCE_FIXTURES],
)
def test_snapshot_carries_no_data_uri(
    fixture: Mapping[str, Any],
) -> None:
    """Belt-and-braces W11.6 invariant: the snapshot pipeline output
    never carries ``data:`` URIs / ``base64,`` payloads in any string
    surface. The unit test for ``assert_no_copied_bytes`` already
    covers this for fresh CloneSpec / TransformedSpec inputs; this
    test pins the same invariant on the *cross-row* snapshot output so
    a regression in any layer surfaces here."""
    snapshot = _build_snapshot(fixture)
    blob = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    assert "data:image/" not in blob.lower()
    assert "base64," not in blob.lower()


@pytest.mark.parametrize(
    "fixture",
    REFERENCE_FIXTURES,
    ids=[f["slug"] for f in REFERENCE_FIXTURES],
)
def test_snapshot_attribution_present(
    fixture: Mapping[str, Any],
) -> None:
    """The W11.13 attribution string must travel with every cloned
    artefact. Pins the manifest's ``attribution`` field carries both
    ``open-lovable`` and ``MIT`` tokens so a future refactor cannot
    silently drop the credit."""
    snapshot = _build_snapshot(fixture)
    attribution = snapshot["manifest"]["attribution"]
    assert "open-lovable" in attribution
    assert "MIT" in attribution


@pytest.mark.parametrize(
    "fixture",
    REFERENCE_FIXTURES,
    ids=[f["slug"] for f in REFERENCE_FIXTURES],
)
def test_snapshot_renders_all_three_frameworks(
    fixture: Mapping[str, Any],
) -> None:
    """Every fixture renders successfully into all three supported
    frameworks. Pins the W11.9 multi-framework contract: the spec is a
    *content* contract, not a React contract, so every framework
    receives the same input and produces a non-empty file list."""
    snapshot = _build_snapshot(fixture)
    rendered = snapshot["rendered"]
    assert set(rendered.keys()) == set(SUPPORTED_FRAMEWORKS)
    for framework, payload in rendered.items():
        assert payload["files"], (
            f"fixture {fixture['slug']} produced empty file list for "
            f"framework {framework!r}"
        )
        assert payload["traceability_html_path"]
        assert payload["manifest_clone_id"] == fixture["clone_id"]


@pytest.mark.parametrize(
    "fixture",
    REFERENCE_FIXTURES,
    ids=[f["slug"] for f in REFERENCE_FIXTURES],
)
def test_snapshot_context_block_carries_manifest_fingerprint(
    fixture: Mapping[str, Any],
) -> None:
    """The W11.10 agent-prompt context block must carry the
    ``clone_id`` and ``manifest_hash`` so the frontend agent can echo
    them into any artefact for W11.12 audit replay."""
    snapshot = _build_snapshot(fixture)
    block = "\n".join(snapshot["context_block_lines"])
    assert fixture["clone_id"] in block
    assert snapshot["manifest"]["manifest_hash"] in block


# ── Cross-fixture invariants ──────────────────────────────────────────


def test_all_fixtures_have_distinct_titles() -> None:
    """Five distinct titles → five distinct rewritten titles → five
    distinct manifest hashes. Pins that the fixtures actually exercise
    different shapes."""
    titles: list[str] = []
    for fixture in REFERENCE_FIXTURES:
        snapshot = _build_snapshot(fixture)
        titles.append(snapshot["spec"]["title"])
    assert len(set(titles)) == 5, f"non-distinct titles: {titles}"


def test_all_fixtures_have_distinct_manifest_hashes() -> None:
    """Five distinct fixtures must produce five distinct manifest
    hashes. Catches the regression where a layer accidentally collapses
    multiple inputs onto the same output (e.g. an over-aggressive
    truncation cap)."""
    hashes: list[str] = []
    for fixture in REFERENCE_FIXTURES:
        snapshot = _build_snapshot(fixture)
        hashes.append(snapshot["manifest"]["manifest_hash"])
    assert len(set(hashes)) == 5, f"colliding manifest hashes: {hashes}"


def test_all_fixtures_carry_w11_traceability_in_render() -> None:
    """Every rendered project for every framework carries a parseable
    W11.7 traceability comment in the static
    ``public/clone-traceability.html`` scaffold. Pins the cross-row
    contract that W11.7 enforces from disk + W11.9 enforces from
    render output."""
    for fixture in REFERENCE_FIXTURES:
        fixture_path = FIXTURE_DIR / f"{fixture['slug']}.html"
        html = fixture_path.read_text(encoding="utf-8")
        capture = RawCapture(
            url=fixture["source_url"],
            html=html,
            status_code=fixture["capture_status_code"],
            fetched_at=fixture["fetched_at"],
            backend=fixture["capture_backend"],
            asset_urls=(),
            headers={},
        )
        spec = build_clone_spec_from_capture(
            capture, source_url=fixture["source_url"],
        )
        classification = _stub_classification()
        transformed = asyncio.run(
            transform_clone_spec(
                spec,
                classification=classification,
                llm=_UnavailableRewriteLLM(),
            )
        )
        manifest = build_clone_manifest(
            source_url=fixture["source_url"],
            fetched_at=fixture["fetched_at"],
            backend=fixture["capture_backend"],
            classification=classification,
            transformed=transformed,
            tenant_id=fixture["tenant_id"],
            actor=fixture["actor"],
            refusal_decision=_stub_refusal_decision(fixture["source_url"]),
            clone_id=fixture["clone_id"],
            created_at=fixture["created_at"],
        )
        for framework in sorted(SUPPORTED_FRAMEWORKS):
            project = render_clone_project(
                transformed, framework, manifest=manifest,
            )
            traceability_files = [
                f for f in project.files
                if f.relative_path == project.traceability_html_relative_path
            ]
            assert len(traceability_files) == 1, (
                f"{fixture['slug']}/{framework}: missing or duplicate "
                f"traceability HTML"
            )
            parsed = parse_html_traceability_comment(
                traceability_files[0].content
            )
            assert parsed is not None, (
                f"{fixture['slug']}/{framework}: traceability HTML "
                f"lacks a parseable W11.7 comment"
            )
            assert parsed.get("clone_id") == fixture["clone_id"]
            assert parsed.get("manifest_hash") == manifest.manifest_hash
