"""Contract tests for docs-site hosting (ADR-0022 — Cloudflare Tunnel).

These pin the cross-file invariants of the LIVE hosting decision in ADR-0022
so a future edit cannot silently break the public URL contract:

- The MkDocs `site_url` matches the canonical hostname (so generated links
  and sitemaps point at the right place).
- ADR-0022 exists, supersedes ADR-0013's host decision, and documents the
  Cloudflare Tunnel + Caddy serving path.
- The Caddy reverse proxy has the docs `:8081` file_server block, and the
  compose file mounts docs-site-dist read-only into it.
- The CF Tunnel hosting runbook documents the required operator surfaces.

The GitHub Pages path (ADR-0013 + .github/workflows/docs-site-publish.yml +
test_docs_site_publish_pipeline.py) is retained as a dormant fallback and is
NOT asserted here — see ADR-0022 "Retained dormant fallback".
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_HOSTNAME = "docs.sora-dev.app"

MKDOCS_YAML = REPO_ROOT / "docs-site" / "mkdocs.yml"
CADDYFILE = REPO_ROOT / "deploy" / "reverse-proxy" / "Caddyfile"
COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
ADR = REPO_ROOT / "docs" / "adr" / "ADR-0022-docs-site-hosting-cf-tunnel.md"
RUNBOOK = REPO_ROOT / "docs" / "operations" / "docs-site-hosting-cf-tunnel.md"


def test_mkdocs_site_url_matches_canonical_hostname() -> None:
    config = yaml.safe_load(MKDOCS_YAML.read_text(encoding="utf-8"))
    site_url = config.get("site_url", "")

    assert CANONICAL_HOSTNAME in site_url, (
        f"mkdocs site_url ({site_url!r}) must reference {CANONICAL_HOSTNAME!r}"
    )
    assert site_url.startswith("https://"), "site_url must be HTTPS"


def test_adr_0022_present_and_documents_cf_tunnel_path() -> None:
    assert ADR.exists(), f"ADR-0022 missing: {ADR}"
    body = ADR.read_text(encoding="utf-8")

    assert CANONICAL_HOSTNAME in body
    assert "Cloudflare Tunnel" in body
    assert "caddy:8081" in body
    assert "ADR-0013" in body, "ADR-0022 must reference the ADR it supersedes"
    assert "docs-site-hosting-cf-tunnel.md" in body, (
        "ADR-0022 must link to its operations runbook"
    )


def test_caddy_serves_docs_on_dedicated_port() -> None:
    body = CADDYFILE.read_text(encoding="utf-8")

    assert ":8081" in body, "Caddyfile must declare the docs :8081 site block"
    assert "/srv/docs" in body, "the :8081 block must serve root /srv/docs"
    assert "file_server" in body, "the :8081 block must use file_server"


def test_compose_mounts_docs_dist_into_caddy() -> None:
    body = COMPOSE.read_text(encoding="utf-8")

    assert "./docs-site-dist:/srv/docs:ro" in body, (
        "caddy service must mount docs-site-dist read-only at /srv/docs"
    )


def test_cf_tunnel_runbook_present_and_documents_required_surfaces() -> None:
    assert RUNBOOK.exists(), f"CF tunnel hosting runbook missing: {RUNBOOK}"
    body = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        "DNS configuration",
        "TLS",
        "Access control",
        "Rotation",
        "One-time operator setup",
        "Disaster recovery",
        "Verification",
        CANONICAL_HOSTNAME,
        "ADR-0022",
    ):
        assert required in body, (
            f"CF tunnel hosting runbook must cover {required!r}; missing from {RUNBOOK}"
        )


def test_cname_artifact_removed() -> None:
    """The GitHub-Pages CNAME marker is deleted under the CF Tunnel path.

    Serving a public /CNAME would leak the hostname for no functional gain
    (Cloudflare Tunnel does not use a CNAME file). ADR-0022 deletes it.
    """
    cname = REPO_ROOT / "docs-site" / "docs" / "CNAME"
    assert not cname.exists(), (
        f"CNAME should be deleted under ADR-0022 (CF Tunnel), still present: {cname}"
    )


def test_no_legacy_wiki_redirects_required() -> None:
    """Audit pin: no stale third-party doc-host references in the corpus.

    Re-confirmed during the ADR-0022 migration (2026-05-20). A future commit
    cannot reintroduce a legacy doc host silently.
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
        "found new legacy-host references that need redirect planning;"
        " either remove them or document the redirect in ADR-0022:\n  "
        + "\n  ".join(hits)
    )
