"""OP-793 contract tests for docs-site hosting + DNS + TLS.

These tests pin the cross-file invariants of the hosting decision in
ADR-0013 so a future edit cannot silently break the public URL contract:

- The CNAME source-of-truth file (the GitHub Pages custom-domain marker)
  exists with exactly the canonical hostname.
- The publish workflow stages that CNAME into `docs-site-dist/` before
  upload-pages-artifact.
- The MkDocs `site_url` matches the canonical hostname (so generated
  links and sitemaps point at the right place).
- ADR-0013 and the operations runbook reference each other (so a reader
  finding either one can navigate to the other).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_HOSTNAME = "docs.sora.services"

CNAME_SOURCE = REPO_ROOT / "docs-site" / "docs" / "CNAME"
MKDOCS_YAML = REPO_ROOT / "docs-site" / "mkdocs.yml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-site-publish.yml"
ADR = REPO_ROOT / "docs" / "adr" / "ADR-0013-docs-site-hosting-dns-tls.md"
RUNBOOK = REPO_ROOT / "docs" / "operations" / "docs-site-hosting.md"
PIPELINE_RUNBOOK = REPO_ROOT / "docs" / "operations" / "docs-site-pipeline.md"


def test_cname_source_of_truth_holds_canonical_hostname() -> None:
    assert CNAME_SOURCE.exists(), f"CNAME source missing: {CNAME_SOURCE}"
    body = CNAME_SOURCE.read_text(encoding="utf-8").strip()

    assert body == CANONICAL_HOSTNAME, (
        f"CNAME must hold exactly {CANONICAL_HOSTNAME!r}, got {body!r}"
    )


def test_workflow_stages_cname_before_upload() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]

    step_names = [step.get("name", "") for step in steps if isinstance(step, dict)]
    cname_idx = next(
        (i for i, n in enumerate(step_names) if "CNAME" in n),
        None,
    )
    upload_idx = next(
        (
            i
            for i, step in enumerate(steps)
            if isinstance(step, dict)
            and step.get("uses", "").startswith("actions/upload-pages-artifact")
        ),
        None,
    )

    assert cname_idx is not None, "workflow must have a step that stages CNAME"
    assert upload_idx is not None, "workflow must upload the pages artifact"
    assert cname_idx < upload_idx, (
        "CNAME-stage step must run before upload-pages-artifact, otherwise the"
        " custom-domain marker is missing from the deployed site"
    )

    cname_step = steps[cname_idx]
    run_block = cname_step.get("run", "")
    assert "docs-site/docs/CNAME" in run_block
    assert "docs-site-dist/CNAME" in run_block


def test_mkdocs_site_url_matches_canonical_hostname() -> None:
    config = yaml.safe_load(MKDOCS_YAML.read_text(encoding="utf-8"))
    site_url = config.get("site_url", "")

    assert CANONICAL_HOSTNAME in site_url, (
        f"mkdocs site_url ({site_url!r}) must reference {CANONICAL_HOSTNAME!r}"
    )
    assert site_url.startswith("https://"), "site_url must be HTTPS"


def test_adr_0013_present_and_references_runbook() -> None:
    assert ADR.exists(), f"ADR-0013 missing: {ADR}"
    body = ADR.read_text(encoding="utf-8")

    assert "docs.sora.services" in body
    assert "GitHub Pages" in body
    assert "Let's Encrypt" in body
    assert "docs-site-hosting.md" in body, "ADR must link to its operations runbook"


def test_hosting_runbook_present_and_documents_required_surfaces() -> None:
    assert RUNBOOK.exists(), f"hosting runbook missing: {RUNBOOK}"
    body = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        "DNS configuration",
        "TLS",
        "Access control",
        "Rotation",
        "One-time operator setup",
        "Migration redirects",
        "Disaster recovery",
        CANONICAL_HOSTNAME,
        "ADR-0013",
    ):
        assert required in body, (
            f"hosting runbook must cover {required!r}; missing from {RUNBOOK}"
        )


def test_pipeline_runbook_links_hosting_runbook() -> None:
    body = PIPELINE_RUNBOOK.read_text(encoding="utf-8")

    assert "docs-site-hosting.md" in body, (
        "OP-792 pipeline runbook must cross-link OP-793 hosting runbook so"
        " a reader following the build flow finds the public-URL surface"
    )


def test_no_legacy_wiki_redirects_required() -> None:
    """OP-793 spec asks: redirect old wiki / readme links to new host (if any).

    This audit ran 2026-05-08: zero legacy hosts found in our docs corpus.
    The test pins the audit so a future commit cannot reintroduce a stale
    legacy URL silently.
    """
    docs_dir = REPO_ROOT / "docs"
    legacy_hosts = ("readthedocs.io", "gitbook.io", "readme.io")

    hits: list[str] = []
    for path in docs_dir.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for host in legacy_hosts:
            if host in text:
                hits.append(f"{path.relative_to(REPO_ROOT)}: {host}")

    assert not hits, (
        "OP-793 audit found new legacy-host references that need redirect"
        " planning; either remove them or update ADR-0013 §Migration"
        f" redirects:\n  " + "\n  ".join(hits)
    )
