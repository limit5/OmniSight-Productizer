r"""L11 #338 #5 — README Deploy-button badge contract.

Sibling `test_deploy_parity_across_platforms.py` verifies that each of the
three platform deploy buttons exists in the root README.md by checking for
the bare deploy URL (`cloud.digitalocean.com/apps/new`, etc). That catches
complete removal but cannot catch subtler regressions that still break the
one-click UX:

- a raw link without the image badge (no visual affordance for users)
- a badge pointing to a dead/unofficial SVG host (image 404s)
- deploy URL pointing at a different repo or branch than the cloud spec
  files (click goes to the wrong project)
- missing alt text (fails accessibility + shows raw URL on image-404)
- badges scattered across the README instead of co-located in the
  "One-click cloud deploy" section (operator hunts for them)
- companion runbook links (post-deploy steps) that drift to files that
  no longer exist after a refactor

Those are exactly the regressions that make a "just click Deploy" story
rot silently — no CI signal until a user complains that the badge image is
broken or the Deploy button opens the wrong repo.

This file pins the **button surface itself**: markdown structure, badge
SVG host, deploy-URL repo target, alt text, section proximity, and
runbook-link integrity. It intentionally does NOT re-verify spec contents
— those are the per-platform suites' job.

Cost: <10ms. stdlib only (no yaml/json needed — README is pure markdown).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# The canonical repo slug + branch must be the SAME in all three deploy
# button URLs. Per-platform specs (app.yaml / railway.json / render.yaml)
# independently reference github.com/limit5/OmniSight-Productizer on
# branch master — the buttons must route users to the same place or
# clicking lands them in a repo that doesn't match the spec.
CANONICAL_REPO_SLUG = "limit5/OmniSight-Productizer"
CANONICAL_BRANCH = "master"


@pytest.fixture(scope="module")
def readme_text() -> str:
    assert README.is_file(), "README.md is missing from repo root"
    return README.read_text(encoding="utf-8")


# ── Section 1 — dedicated deploy section exists ──────────────────────


@pytest.fixture(scope="module")
def deploy_section(readme_text: str) -> str:
    """Extract the block that holds the 3 deploy buttons. Pinning a
    dedicated section (rather than hunting anywhere in README) prevents
    the buttons from drifting to random spots across edits.
    """
    match = re.search(
        r"###\s+One-click cloud deploy.*?(?=\n##\s|\n###\s|\Z)",
        readme_text,
        re.DOTALL,
    )
    assert match, (
        "README must contain a `### One-click cloud deploy` section "
        "holding all three Deploy buttons together"
    )
    return match.group(0)


def test_deploy_section_is_near_top_of_readme(readme_text: str):
    """Users shouldn't have to scroll 200 lines to find the one-click
    deploy buttons. Pinning <200 lines prevents the section drifting
    into the appendix over time."""
    lines_before = readme_text.split("### One-click cloud deploy", 1)[0]
    line_index = lines_before.count("\n")
    assert line_index < 200, (
        f"One-click deploy section moved to line {line_index}; keep it "
        f"within the top 200 lines so first-time visitors see it. "
        f"Move it back up toward the Quick Start block."
    )


# ── Section 2 — each platform has a properly-formed badge ───────────


# [![alt](badge.svg)](deploy URL)
BADGE_MARKDOWN_RE = re.compile(
    r"\[!\[(?P<alt>[^\]]+)\]\((?P<badge>https?://[^\s)]+)\)\]\((?P<href>https?://[^\s)]+)\)"
)


@pytest.fixture(scope="module")
def badges(deploy_section: str) -> dict[str, dict[str, str]]:
    """Parse all badge-link pairs from the deploy section and key them
    by platform based on the deploy URL host."""
    found: dict[str, dict[str, str]] = {}
    for m in BADGE_MARKDOWN_RE.finditer(deploy_section):
        href = m.group("href")
        if "digitalocean.com" in href:
            platform = "digitalocean"
        elif "railway.com" in href:
            platform = "railway"
        elif "render.com" in href:
            platform = "render"
        else:
            continue
        found[platform] = {
            "alt": m.group("alt"),
            "badge": m.group("badge"),
            "href": href,
        }
    return found


def test_all_three_platforms_have_image_badges(
    badges: dict[str, dict[str, str]],
):
    """Not just a text link — each platform must be a clickable image
    badge. `[![alt](badge.svg)](url)` is the markdown shape that renders
    as the familiar blue/purple/gray Deploy button."""
    missing = {"digitalocean", "railway", "render"} - badges.keys()
    assert not missing, (
        f"Deploy section missing image-badge markdown for: {sorted(missing)}. "
        f"Use the `[![alt](badge.svg)](deploy-url)` shape, not a bare "
        f"`[text](url)` link — users expect a visual button."
    )


def test_each_badge_has_non_empty_alt_text(
    badges: dict[str, dict[str, str]],
):
    """Alt text is shown when the badge SVG 404s (e.g. provider rotates
    badge URL) and is required for screen readers. Catches copy-paste
    bugs like `![](badge.svg)` that silently ship broken accessibility."""
    for platform, parts in badges.items():
        alt = parts["alt"].strip()
        assert alt, f"{platform}: badge is missing alt text"
        assert len(alt) >= 6, (
            f"{platform}: alt text '{alt}' is too short — use something "
            f"descriptive like 'Deploy to DigitalOcean'"
        )


# ── Section 3 — badge SVGs come from the OFFICIAL provider host ─────


EXPECTED_BADGE_HOSTS = {
    "digitalocean": "www.deploytodo.com",
    "railway": "railway.com",
    "render": "render.com",
}


def test_badges_served_from_official_provider_hosts(
    badges: dict[str, dict[str, str]],
):
    """Each provider ships a stable canonical badge SVG URL:
      DO     — www.deploytodo.com/do-btn-blue.svg
      Railway — railway.com/button.svg
      Render — render.com/images/deploy-to-render-button.svg
    Using the provider's own URL means the badge updates if the provider
    rebrands (Render did this in 2024; Railway in 2025). Anything else
    (a mirrored copy, img.shields.io approximation, third-party host)
    is a maintenance liability — pin it now."""
    for platform, expected_host in EXPECTED_BADGE_HOSTS.items():
        actual = badges[platform]["badge"]
        assert expected_host in actual, (
            f"{platform}: badge URL '{actual}' must be served from the "
            f"official provider host '{expected_host}'. Third-party "
            f"mirrors rot when the provider rotates the file."
        )
        assert actual.endswith(".svg"), (
            f"{platform}: badge URL '{actual}' must be an SVG. PNG/JPG "
            f"badges degrade on high-DPI displays and don't match the "
            f"rest of the provider ecosystem."
        )


# ── Section 4 — Deploy URL points at the canonical repo + branch ────


def test_digitalocean_deploy_url_points_at_canonical_repo(
    badges: dict[str, dict[str, str]],
):
    """DO's one-click URL shape is
    `cloud.digitalocean.com/apps/new?repo=<URL>/tree/<branch>`.
    The `?repo=...` must point at limit5/OmniSight-Productizer on master
    so it matches `deploy/digitalocean/app.yaml`'s `github.repo`."""
    href = badges["digitalocean"]["href"]
    assert href.startswith("https://cloud.digitalocean.com/apps/new"), (
        f"DO Deploy URL must start with https://cloud.digitalocean.com/apps/new; got {href}"
    )
    assert CANONICAL_REPO_SLUG in href, (
        f"DO Deploy URL must reference '{CANONICAL_REPO_SLUG}'; got {href}"
    )
    assert f"tree/{CANONICAL_BRANCH}" in href, (
        f"DO Deploy URL must include 'tree/{CANONICAL_BRANCH}'; got {href}"
    )


def test_railway_deploy_url_points_at_canonical_repo(
    badges: dict[str, dict[str, str]],
):
    """Railway's one-click URL shape is
    `railway.com/new/template?template=<URL-encoded repo URL>`. The
    template parameter is URL-encoded so checking for the raw slug with
    encoded path separators."""
    href = badges["railway"]["href"]
    assert href.startswith("https://railway.com/new/template"), (
        f"Railway Deploy URL must start with https://railway.com/new/template; got {href}"
    )
    assert "template=" in href, (
        f"Railway Deploy URL must carry a ?template= query param; got {href}"
    )
    # URL-encoded or raw — accept either form, but the slug must appear
    assert (
        CANONICAL_REPO_SLUG in href
        or CANONICAL_REPO_SLUG.replace("/", "%2F") in href
    ), (
        f"Railway Deploy URL must reference '{CANONICAL_REPO_SLUG}' "
        f"(raw or URL-encoded); got {href}"
    )


def test_render_deploy_url_points_at_canonical_repo(
    badges: dict[str, dict[str, str]],
):
    """Render's one-click Blueprint URL shape is
    `render.com/deploy?repo=<URL>`. Render accepts a raw github.com URL
    (no /tree/branch segment — Render reads the default branch from the
    Blueprint file itself). The repo slug must match."""
    href = badges["render"]["href"]
    assert href.startswith("https://render.com/deploy"), (
        f"Render Deploy URL must start with https://render.com/deploy; got {href}"
    )
    assert "repo=" in href, (
        f"Render Deploy URL must carry a ?repo= query param; got {href}"
    )
    assert CANONICAL_REPO_SLUG in href, (
        f"Render Deploy URL must reference '{CANONICAL_REPO_SLUG}'; got {href}"
    )


# ── Section 5 — companion runbook links point to real files ─────────


COMPANION_RUNBOOKS = {
    "digitalocean": Path("deploy/digitalocean/README.md"),
    "railway": Path("deploy/railway/README.md"),
    "render": Path("deploy/render/README.md"),
}

COMPANION_SPECS = {
    "digitalocean": Path("deploy/digitalocean/app.yaml"),
    "railway": Path("deploy/railway/railway.json"),
    "render": Path("deploy/render/render.yaml"),
}


def test_deploy_section_links_to_every_companion_runbook(deploy_section: str):
    """Below the badges the section lists each platform's Spec + Runbook
    links so operators can peek at the config before clicking Deploy.
    Those links must resolve to real files — broken links here mean a
    404 the moment a user tries to review the spec."""
    for platform, runbook in COMPANION_RUNBOOKS.items():
        assert str(runbook) in deploy_section, (
            f"{platform}: deploy section must link to {runbook} "
            f"(operator-facing runbook for post-deploy steps)"
        )
        assert (REPO_ROOT / runbook).is_file(), (
            f"{platform}: referenced runbook {runbook} does not exist — "
            f"remove the link or restore the file"
        )


def test_deploy_section_links_to_every_platform_spec(deploy_section: str):
    """Operators reviewing what's about to be deployed click the spec
    link. If it's stale, they read the wrong config."""
    for platform, spec in COMPANION_SPECS.items():
        assert str(spec) in deploy_section, (
            f"{platform}: deploy section must link to the spec file {spec}"
        )
        assert (REPO_ROOT / spec).is_file(), (
            f"{platform}: referenced spec {spec} does not exist"
        )


# ── Section 6 — ordering + single section ───────────────────────────


def test_only_one_deploy_section(readme_text: str):
    """If a future PR adds a second 'One-click cloud deploy' section
    (e.g., duplicated in an appendix), the badges split and half the
    audience misses the newer set. Keep exactly one."""
    count = readme_text.count("### One-click cloud deploy")
    assert count == 1, (
        f"README has {count} 'One-click cloud deploy' sections; expected "
        f"exactly 1 — consolidate them"
    )


def test_badges_appear_before_runbook_links_in_section(deploy_section: str):
    """Visual hierarchy: badge row first (the primary CTA), then the
    spec/runbook bullet list (reference material). Reversing the order
    hides the one-click button below a wall of text."""
    first_badge = deploy_section.find("[![")
    first_bullet = deploy_section.find("\n- ")
    assert first_badge != -1, "deploy section has no badge markdown"
    assert first_bullet != -1, "deploy section has no bullet list"
    assert first_badge < first_bullet, (
        "Badges must appear BEFORE the Spec/Runbook bullet list — the "
        "one-click button is the primary CTA, not a footnote"
    )
