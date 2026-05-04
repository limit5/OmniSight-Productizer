"""R20 Phase 0 — RAG classification-gate enforcement tests.

THE critical security property: ``internal``-tagged docs MUST never
appear in any retrieval result, regardless of role. Operator role
sees public+operator. Admin role sees public+operator+admin.
Anonymous role sees public only.

Tests build a synthetic corpus in a temp directory so they don't depend
on the production docs/ tree (which may shift over time).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.rag.corpus import (
    HD_RAG_RELATIVE_GLOBS,
    VALID_AUDIENCES,
    _classify_default,  # type: ignore[attr-defined]
    _parse_doc,  # type: ignore[attr-defined]
    load_corpus,
    visible_audiences_for,
)
from backend.rag import retrieval as _retrieval


@pytest.fixture
def fake_docs(tmp_path: Path) -> Path:
    """Build a tiny multi-audience corpus rooted at tmp_path/docs/."""
    docs = tmp_path / "docs"

    # Public — README at repo root would be classified public by
    # _DIR_DEFAULTS, but we can't easily test that without putting the
    # file at tmp_path/README.md (outside docs/), so we use explicit
    # frontmatter for the test.
    (docs / "operator").mkdir(parents=True)
    (docs / "design").mkdir(parents=True)
    (docs / "spec").mkdir(parents=True)
    (docs / "phase-x").mkdir(parents=True)
    (docs / "operator" / "git-setup.md").write_text(
        "# Git Setup\n"
        "How to add a git repo: open Settings, click Source Control, "
        "paste the URL, choose auth method.\n",
    )
    (docs / "design" / "security-architecture.md").write_text(
        "---\naudience: internal\n---\n"
        "# Security Architecture\n"
        "PEP rules in pep_gateway.py. Admin tokens at /etc/secrets/admin.\n",
    )
    (docs / "spec" / "tenant-model.md").write_text(
        "---\naudience: admin\n---\n"
        "# Tenant Model\n"
        "Multi-tenancy uses X-Tenant-Id header for routing.\n",
    )
    (docs / "phase-x" / "design.md").write_text(
        "# Phase X internal design\n"
        "We pivot to git mid-flight. Don't tell users yet.\n",
    )
    return docs


def test_default_classification_by_directory(fake_docs):
    op = _parse_doc(fake_docs / "operator" / "git-setup.md", fake_docs)
    assert op.audience == "operator"
    # explicit frontmatter wins
    sec = _parse_doc(fake_docs / "design" / "security-architecture.md", fake_docs)
    assert sec.audience == "internal"
    spec = _parse_doc(fake_docs / "spec" / "tenant-model.md", fake_docs)
    assert spec.audience == "admin"
    # phase-x with no frontmatter falls back to internal via _DIR_DEFAULTS
    phx = _parse_doc(fake_docs / "phase-x" / "design.md", fake_docs)
    assert phx.audience == "internal"


def test_visible_audiences_never_includes_internal():
    for role in ("anonymous", "operator", "admin", "", None, "unknown_role"):
        visible = visible_audiences_for(role or "")
        assert "internal" not in visible, (
            f"FAIL: role={role} can see internal docs"
        )


def test_visible_audiences_role_cumulative():
    assert visible_audiences_for("admin") == frozenset(
        {"public", "operator", "admin"}
    )
    assert visible_audiences_for("operator") == frozenset(
        {"public", "operator"}
    )
    assert visible_audiences_for("anonymous") == frozenset({"public"})


def test_retrieve_filters_internal_for_admin(fake_docs, monkeypatch):
    # Point retrieval at the fake corpus.
    from backend.rag import corpus as _c

    monkeypatch.setattr(_c, "DOC_ROOT", fake_docs)
    _retrieval.reset_corpus_cache()

    # An admin asking about "security architecture" should get NOTHING
    # back — the only matching doc is internal-tagged.
    hits = _retrieval.retrieve("security architecture", role="admin")
    paths = [h.doc_path for h in hits]
    assert all("security-architecture" not in p for p in paths), (
        f"FAIL: admin can retrieve internal doc: {paths}"
    )


def test_retrieve_filters_admin_docs_for_operator(fake_docs, monkeypatch):
    from backend.rag import corpus as _c

    monkeypatch.setattr(_c, "DOC_ROOT", fake_docs)
    _retrieval.reset_corpus_cache()

    # Operator asks about tenant-model — only admin doc matches → no
    # hits because operator can't see admin audience.
    hits = _retrieval.retrieve("tenant model multi-tenancy", role="operator")
    paths = [h.doc_path for h in hits]
    assert all("tenant-model" not in p for p in paths)


def test_retrieve_returns_operator_doc_for_operator(fake_docs, monkeypatch):
    from backend.rag import corpus as _c

    monkeypatch.setattr(_c, "DOC_ROOT", fake_docs)
    _retrieval.reset_corpus_cache()

    hits = _retrieval.retrieve("git setup repo", role="operator")
    assert len(hits) >= 1
    assert any("git-setup" in h.doc_path for h in hits)


def test_retrieve_anonymous_role_sees_nothing_in_this_corpus(fake_docs, monkeypatch):
    from backend.rag import corpus as _c

    monkeypatch.setattr(_c, "DOC_ROOT", fake_docs)
    _retrieval.reset_corpus_cache()

    # All synthetic docs are operator/admin/internal — anonymous gets
    # zero hits.
    hits = _retrieval.retrieve("git setup", role="anonymous")
    assert hits == []


def test_classify_default_unknown_dir_falls_to_internal():
    # Fail-closed: a doc placed in a directory we haven't catalogued
    # gets `internal` so it doesn't leak via chat.
    assert _classify_default("docs/unknown/foo.md") == "internal"
    assert _classify_default("misc/random.md") == "internal"


def test_valid_audiences_constant_matches_classification():
    # Sanity: every audience the corpus parser may emit must appear in
    # at least one role's visible set or be "internal" (chat-invisible).
    visible_union = (
        visible_audiences_for("admin")
        | visible_audiences_for("operator")
        | visible_audiences_for("anonymous")
    )
    for aud in VALID_AUDIENCES:
        if aud == "internal":
            assert aud not in visible_union
        else:
            assert aud in visible_union, (
                f"audience {aud!r} declared but not reachable by any role"
            )


def test_load_corpus_auto_ingests_hd_rag_markdown_with_parent_walker(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "org" / "repo"
    app = repo / "apps" / "firmware"
    docs = app / "docs"
    parent_hd = repo / "hd" / "datasheets"
    current_specs = app / "sensor_specs"
    current_errata = app / "errata"
    docs.mkdir(parents=True)
    parent_hd.mkdir(parents=True)
    current_specs.mkdir(parents=True)
    current_errata.mkdir(parents=True)

    (docs / "operator-guide.md").write_text(
        "---\naudience: operator\n---\n# Operator Guide\nnormal docs\n"
    )
    (parent_hd / "imx415.md").write_text(
        "# IMX415 Datasheet\nMIPI lane timing and register map\n"
    )
    (current_specs / "os08a20.md").write_text(
        "# OS08A20 Sensor Spec\nHDR mode and pixel array\n"
    )
    (current_errata / "imx415-rev-b.md").write_text(
        "# IMX415 Rev B Errata\n60Hz flicker rejection caveat\n"
    )
    (tmp_path / "hd" / "datasheets").mkdir(parents=True)
    (tmp_path / "hd" / "datasheets" / "too-far.md").write_text(
        "# Too Far\noutside three-parent walk\n"
    )

    out = load_corpus(docs)
    paths = [doc.path for doc in out]

    assert "docs/operator-guide.md" in paths
    assert "hd/datasheets/imx415.md" in paths
    assert "sensor_specs/os08a20.md" in paths
    assert "errata/imx415-rev-b.md" in paths
    assert "hd/datasheets/too-far.md" not in paths
    assert all(
        doc.audience == "operator"
        for doc in out
        if doc.path
        in {
            "hd/datasheets/imx415.md",
            "sensor_specs/os08a20.md",
            "errata/imx415-rev-b.md",
        }
    )


def test_retrieve_returns_auto_ingested_hd_rag_doc(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from backend.rag import corpus as _c

    docs = tmp_path / "repo" / "docs"
    hd = tmp_path / "repo" / "hd" / "errata"
    docs.mkdir(parents=True)
    hd.mkdir(parents=True)
    (hd / "imx415.md").write_text(
        "# IMX415 Errata\nblack level calibration fails after hot reset\n"
    )

    monkeypatch.setattr(_c, "DOC_ROOT", docs)
    _retrieval.reset_corpus_cache()

    hits = _retrieval.retrieve("black level hot reset", role="operator")

    assert any(hit.doc_path == "hd/errata/imx415.md" for hit in hits)


def test_hd_rag_globs_cover_expected_document_families() -> None:
    assert "hd/datasheets/**/*.md" in HD_RAG_RELATIVE_GLOBS
    assert "hd/sensor_specs/**/*.md" in HD_RAG_RELATIVE_GLOBS
    assert "hd/errata/**/*.md" in HD_RAG_RELATIVE_GLOBS
