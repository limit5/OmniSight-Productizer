"""[OP-1488] G4 dual-publish recipe in the release-cut runbook.

The OP-1488 ticket adds §8–§11 to `docs/operations/release-cut-runbook.md`:

* §8 — Dual-publish (ghcr.io + GitLab CR) with a digest-parity verify
  loop over backend/frontend/bridge.
* §9 — Staging smoke against GitLab CR (`OMNISIGHT_REGISTRY=
  sora.services:49154/...`) gated on `/readyz` and the
  `/api/v1/agents/cards` integration surface.
* §10 — Rollback recipe that flips `OMNISIGHT_REGISTRY` back to the
  compose default (ghcr.io) and rolling-restarts staging.
* §11 — 2-week observation window: ≥ 3 release cuts must run through
  dual-publish without divergence or rollback before G5 may strip the
  ghcr.io fallback.

These structural tests pin the contract so the regression can't silently
come back (e.g. someone reverts the digest-parity loop or strips the
`/readyz` gate from §9).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = REPO_ROOT / "docs" / "operations" / "release-cut-runbook.md"


def _read() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_exists() -> None:
    assert RUNBOOK.is_file(), f"missing runbook: {RUNBOOK}"


def test_dual_publish_section_present() -> None:
    text = _read()
    assert re.search(
        r"^## 8\. Dual-publish window \(ghcr\.io \+ GitLab CR\)",
        text,
        re.MULTILINE,
    ), "§8 dual-publish window section not found"
    assert "OP-1488" in text, "OP-1488 ticket reference missing"
    assert "META OP-1478" in text, "parent META OP-1478 reference missing"


def test_dual_publish_lists_both_pipelines() -> None:
    text = _read()
    assert ".github/workflows/build-images.yml" in text, (
        "§8 must name the GHCR pipeline (build-images.yml) explicitly"
    )
    assert ".gitlab-ci.yml" in text, (
        "§8 must name the GitLab CR pipeline (.gitlab-ci.yml) explicitly"
    )
    assert "ghcr.io/" in text and "sora.services:49154" in text, (
        "§8 must name both registry hosts explicitly per L-OP-1474"
    )


def test_digest_parity_verify_recipe_present() -> None:
    text = _read()
    assert "docker buildx imagetools inspect" in text, (
        "§8 must use `docker buildx imagetools inspect` to read manifest digests"
    )
    assert "{{.Manifest.Digest}}" in text, (
        "§8 must extract Manifest.Digest from imagetools inspect"
    )
    assert "DIGEST DRIFT" in text, (
        "§8 must fail loud on digest drift between registries"
    )
    for image in ("omnisight-backend", "omnisight-frontend", "omnisight-bridge"):
        assert image in text, f"§8 parity loop must cover {image}"


def test_staging_smoke_section_present() -> None:
    text = _read()
    assert re.search(
        r"^## 9\. Staging smoke — pull from GitLab CR",
        text,
        re.MULTILINE,
    ), "§9 staging smoke section not found"
    # AC #2 — explicit OMNISIGHT_REGISTRY override on staging.
    assert (
        "OMNISIGHT_REGISTRY=sora.services:49154/omnisight/OmniSight-Productizer"
        in text
    ), "§9 must set OMNISIGHT_REGISTRY to the GitLab CR prefix"
    # AC #2 — /readyz gate per prod-deploy-runbook Rule 5.
    assert "/readyz" in text, "§9 must gate on /readyz"
    # The grep pattern in §9 follows the gitlab-cr-cutover-checklist style
    # (backslash-escaped double quotes inside a double-quoted `bash -c`).
    assert re.search(r'\\?"ready\\?":\s*true', text), (
        "§9 must wait for the canonical /readyz ready:true payload"
    )
    # AC #3 — integration smoke endpoints.
    assert "/api/v1/agents/cards" in text, (
        "§9 must smoke /api/v1/agents/cards per AC #3"
    )
    assert "/api/v1/version" in text, (
        "§9 must smoke /api/v1/version per AC #3"
    )


def test_staging_smoke_uses_g3_compose_abstraction() -> None:
    text = _read()
    # G3 (OP-1487) shipped the compose abstraction that makes the
    # OMNISIGHT_REGISTRY override a drop-in. §9 must lean on it.
    assert "docker-compose.staging.yml" in text, (
        "§9 must drive docker-compose.staging.yml (the G3 abstraction file)"
    )
    assert "L-OP-1487" in text or "OP-1487" in text, (
        "§9 should cross-link the G3 compose abstraction lesson/ticket"
    )


def test_rollback_section_present() -> None:
    text = _read()
    assert re.search(
        r"^## 10\. Rollback — GitLab CR pull failure on staging",
        text,
        re.MULTILINE,
    ), "§10 rollback section not found"
    # The G3 default is ghcr.io, so `unset` is the canonical roll-back
    # gesture for a shell-local override.
    assert "unset OMNISIGHT_REGISTRY" in text, (
        "§10 must show the shell-local unset path back to ghcr.io"
    )
    # Rolling restart is mandatory — no big-bang restarts.
    assert "rolling" in text.lower(), (
        "§10 must restore the stack via a rolling restart, not all-at-once"
    )
    # §10 must defer the prod rollback to the existing cutover checklist
    # rather than duplicating it (single-source-of-truth rule).
    assert "gitlab-cr-cutover-checklist.md" in text, (
        "§10 must point operators at the prod cutover checklist for the prod path"
    )


def test_observation_window_section_present() -> None:
    text = _read()
    assert re.search(
        r"^## 11\. Observation window \(Phase G4\)",
        text,
        re.MULTILINE,
    ), "§11 observation window section not found"
    # AC #4 contract: ≥ 3 cuts, ~2-week window, zero divergence.
    assert re.search(r"≥\s*3", text) or "at least 3" in text.lower(), (
        "§11 must state the ≥ 3 successful cuts requirement"
    )
    assert "2-week" in text or "two-week" in text.lower(), (
        "§11 must state the ~2-week observation window"
    )
    # Per-cut record template — anchors the AC #4 audit trail.
    assert "OP-1488 G4 dual-publish record" in text, (
        "§11 must include the per-cut JIRA-comment record template"
    )
    # Reset rule must be explicit so an incident actually pauses promotion.
    assert "Incident reset rule" in text or "incident reset" in text.lower(), (
        "§11 must define an incident-reset rule that restarts the count"
    )


def test_section_ordering_preserved() -> None:
    """§7 (R5 audit) precedes §8–§11; cross-refs in other runbooks rely on it."""
    text = _read()
    r5 = text.find("## 7. R5 — Staging tip-match audit")
    dual = text.find("## 8. Dual-publish window")
    smoke = text.find("## 9. Staging smoke")
    rollback = text.find("## 10. Rollback")
    window = text.find("## 11. Observation window")
    assert -1 < r5 < dual < smoke < rollback < window, (
        "section order must be §7 (R5) → §8 → §9 → §10 → §11"
    )
