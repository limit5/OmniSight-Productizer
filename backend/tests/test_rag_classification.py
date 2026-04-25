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
    Doc,
    VALID_AUDIENCES,
    _classify_default,  # type: ignore[attr-defined]
    _parse_doc,  # type: ignore[attr-defined]
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
