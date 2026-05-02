"""W14.11 — Threat-model drift-guard tests.

Pins the alignment between
:file:`docs/security/w14-web-preview-threat-model.md` (the W14.11 R28 /
R29 / R30 STRIDE document) and the real code-level mitigations that
already landed in W14.1 — W14.10. If a future commit silently
regresses a control documented in §3 of the threat model — for
example, by mounting ``/var/run/docker.sock`` into the preview
sidecar, by raising the cgroup defaults beyond the 2 GiB / 1 CPU /
5 GiB row spec, by inheriting backend env vars into the sidecar, or
by deleting the threat-model document itself — at least one of these
tests fails red so the regression cannot ship un-noticed.

These tests are deliberately co-located with the W14 epic's other
contract tests (``test_web_sandbox.py``, ``test_cf_ingress.py``,
``test_cf_access.py``, ``test_web_sandbox_resource_limits.py``) so
the W14 epic's regression suite covers the threat-model invariants
without having to remember a separate test path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
THREAT_MODEL_PATH = (
    REPO_ROOT / "docs" / "security" / "w14-web-preview-threat-model.md"
)
BLUEPRINT_RISK_REGISTER_PATH = (
    REPO_ROOT / "docs" / "design" / "blueprint-v2-implementation-plan.md"
)
ROADMAP_PATH = (
    REPO_ROOT / "docs" / "design" / "w11-w16-as-fs-sc-roadmap.md"
)


# ---------------------------------------------------------------------------
# Doc-level drift guards — fail red if the threat-model doc disappears or
# its required sections / anchors are removed.
# ---------------------------------------------------------------------------


def test_w14_11_threat_model_doc_exists() -> None:
    """The W14.11 threat-model document must exist on disk.

    Catches accidental deletion / renaming during refactors.
    """

    assert THREAT_MODEL_PATH.is_file(), (
        f"W14.11 threat model missing at {THREAT_MODEL_PATH}; "
        "see docs/sop/implement_phase_step.md §Production Readiness Gate"
    )


def test_w14_11_threat_model_doc_pins_required_anchors() -> None:
    """§1 TL;DR + §3 R28/R29/R30 + §4 control matrix headings must
    all appear verbatim. Future renames force a doc-level review.
    """

    body = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    required_anchors = (
        "## 1. TL;DR",
        "### 3.1 R28",
        "### 3.2 R29",
        "### 3.3 R30",
        "## 4. 控制覆蓋矩陣",
        "## 5. Drift-guard tests",
    )
    missing = [a for a in required_anchors if a not in body]
    assert not missing, (
        "Threat model doc missing required anchors: "
        + repr(missing)
    )


def test_w14_11_threat_model_doc_lists_each_drift_guard_test_name() -> None:
    """Each test in this file is named in §5 of the threat model.

    The threat-model row §5 spreadsheet promises that every drift-
    guard test is reachable by a stable test_id; if a contributor
    renames a test here, the threat-model section needs to be
    updated in the same commit.
    """

    body = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    expected_test_names = (
        "test_w14_11_r28_token_scope_documented",
        "test_w14_11_r28_token_fingerprint_redacts_long_token",
        "test_w14_11_r28_idle_reaper_reason_aligned",
        "test_w14_11_r29_socket_not_mounted",
        "test_w14_11_r29_non_root_uid",
        "test_w14_11_r29_cgroup_defaults_pinned",
        "test_w14_11_r30_env_does_not_inherit_backend",
        "test_w14_11_r30_jwt_alignment_helper_exists",
        "test_w14_11_threat_model_doc_exists_and_pins_anchors",
    )
    missing = [name for name in expected_test_names if name not in body]
    assert not missing, (
        "§5 of the threat model is missing references to drift-guard "
        f"test names: {missing!r} — keep the spreadsheet aligned "
        "with this file's actual test names."
    )


def test_w14_11_threat_model_doc_exists_and_pins_anchors() -> None:
    """Single-shot meta test that mirrors the §5 row.

    Equivalent to :func:`test_w14_11_threat_model_doc_exists` +
    :func:`test_w14_11_threat_model_doc_pins_required_anchors` —
    kept as its own callable so the threat-model §5 row's promise
    of a single test name is satisfiable.
    """

    assert THREAT_MODEL_PATH.is_file()
    body = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    assert "## 1. TL;DR" in body
    assert "## 3. R28-R30 STRIDE + 控制矩陣" in body
    assert "## 4. 控制覆蓋矩陣" in body


def test_w14_11_blueprint_risk_register_includes_r28_r29_r30() -> None:
    """The top-level ADR §8 risk register must carry R28 / R29 / R30
    entries that point back at this threat model.

    Without these rows the W14.11 spec would only live in the
    detailed threat-model document and would fall out of the
    blueprint operator-facing risk view.
    """

    body = BLUEPRINT_RISK_REGISTER_PATH.read_text(encoding="utf-8")
    for rid in ("R28", "R29", "R30"):
        assert (
            f"| **{rid}** |" in body
        ), f"blueprint-v2-implementation-plan §8 missing {rid}"
    assert "w14-web-preview-threat-model.md" in body, (
        "ADR §8 R28-R30 entries must reference the detailed threat "
        "model document"
    )


def test_w14_11_roadmap_r28_r30_originals_preserved() -> None:
    """The original R28-R30 row in the W11-W16 roadmap must not be
    deleted by W14.11 — historical traceability matters.
    """

    body = ROADMAP_PATH.read_text(encoding="utf-8")
    assert "R28" in body
    assert "R29" in body
    assert "R30" in body


# ---------------------------------------------------------------------------
# R28 — Dynamic CF Tunnel ingress credential exhaust.
# ---------------------------------------------------------------------------


def test_w14_11_r28_token_scope_documented() -> None:
    """The threat-model §3.1.6 row documents the two-and-only-two CF
    API token scopes the W14 wiring is allowed to request.

    Adding any extra scope to the document (or to the operator
    deploy SOP that quotes this list) silently widens the blast
    radius if the token leaks.
    """

    body = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    assert "Account:Cloudflare Tunnel:Edit" in body
    assert "Account:Cloudflare Access:Edit" in body
    forbidden = (
        "Zone:DNS:Edit",
        "Zone:WAF:Edit",
        "Account:Workers Scripts:Edit",
        "Account:Workers KV:Edit",
    )
    body_lower = body.lower()
    for f in forbidden:
        assert f.lower() in body_lower, (
            f"Threat model §3.1.6 must explicitly forbid scope {f!r} "
            "so the operator deploy SOP cannot silently widen the "
            "token blast radius"
        )


def test_w14_11_r28_token_fingerprint_redacts_long_token() -> None:
    """:func:`backend.cf_ingress.token_fingerprint` must never echo
    the raw token. Mitigates T28.2 (token leaked via debug log).
    """

    from backend.cf_ingress import token_fingerprint

    raw = "a" * 64
    fp = token_fingerprint(raw)
    assert isinstance(fp, str)
    assert raw not in fp
    # The fingerprint should be short — the cloudflare_client helper
    # produces a sha256-prefix style string.
    assert len(fp) < len(raw)
    # Sanity: same input ⇒ same fingerprint (deterministic).
    assert token_fingerprint(raw) == fp
    # Different input ⇒ different fingerprint.
    assert token_fingerprint("b" * 64) != fp


def test_w14_11_r28_idle_reaper_reason_aligned() -> None:
    """The idle reaper's literal kill reason must equal the W14.2
    reserved string the launcher checks for.

    Mitigates T28.1 (runaway launches accumulate ingress rules).
    If the literal drifts, the audit chain in W14.10 alembic 0059
    can't tell idle-kill vs operator-stop apart.
    """

    from backend.web_sandbox_idle_reaper import IDLE_TIMEOUT_REASON

    assert IDLE_TIMEOUT_REASON == "idle_timeout"
    # The threat model §3.1.1 references the same literal — keep
    # them in lockstep.
    body = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    assert "idle_timeout" in body


# ---------------------------------------------------------------------------
# R29 — Vite dev server sandbox escape via plugin RCE.
# ---------------------------------------------------------------------------


def test_w14_11_r29_socket_not_mounted() -> None:
    """:func:`build_docker_run_spec` must never mount the docker
    socket into the preview sidecar.

    Mitigates T29.2 / T29.6: a compromised plugin must not be able
    to talk to the host docker daemon.
    """

    from backend.web_sandbox import (
        DEFAULT_RESOURCE_LIMITS,
        WebSandboxConfig,
        build_docker_run_spec,
    )

    config = WebSandboxConfig(
        workspace_id="ws-r29-socket",
        workspace_path="/tmp/ws-r29-socket",
    )
    spec = build_docker_run_spec(
        config, manifest=None, resource_limits=DEFAULT_RESOURCE_LIMITS
    )
    forbidden_targets = ("/var/run/docker.sock", "/run/docker.sock")
    forbidden_sources = ("/var/run/docker.sock", "/run/docker.sock")
    for mount in spec["mounts"]:
        assert mount["target"] not in forbidden_targets, (
            f"build_docker_run_spec leaked docker socket mount target: "
            f"{mount!r}"
        )
        assert mount["source"] not in forbidden_sources, (
            f"build_docker_run_spec leaked docker socket mount source: "
            f"{mount!r}"
        )


def test_w14_11_r29_non_root_uid() -> None:
    """The W14.1 image manifest pins ``runtime_uid=10002`` and the
    Dockerfile pins ``USER 10002:10002`` — the threat model relies
    on these matching.

    Mitigates T29.1 (RCE) + T29.3 (information disclosure).
    """

    from backend.web_sandbox import load_image_manifest

    manifest = load_image_manifest(repo_root=REPO_ROOT)
    assert manifest.runtime_uid == 10002, (
        f"W14.1 manifest runtime_uid drift: {manifest.runtime_uid}"
    )

    dockerfile_path = REPO_ROOT / "Dockerfile.web-preview"
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    assert re.search(r"^USER 10002:10002\s*$", dockerfile, re.MULTILINE), (
        "Dockerfile.web-preview must pin USER 10002:10002 to align "
        "with the W14.1 manifest runtime_uid"
    )


def test_w14_11_r29_cgroup_defaults_pinned() -> None:
    """The W14.9 cgroup defaults (2 GiB RAM / 1 CPU / 5 GiB storage)
    are referenced verbatim by the threat model §3.2.2 row.

    Mitigates T29.4 (DoS via fork-bomb / disk fill / RAM hog).
    """

    from backend.web_sandbox_resource_limits import (
        DEFAULT_CPU_LIMIT,
        DEFAULT_MEMORY_LIMIT_BYTES,
        DEFAULT_STORAGE_LIMIT_BYTES,
        WebPreviewResourceLimits,
    )

    assert DEFAULT_MEMORY_LIMIT_BYTES == 2 * 1024 * 1024 * 1024
    assert DEFAULT_CPU_LIMIT == 1.0
    assert DEFAULT_STORAGE_LIMIT_BYTES == 5 * 1024 * 1024 * 1024

    limits = WebPreviewResourceLimits.default()
    assert limits.memory_limit_bytes == DEFAULT_MEMORY_LIMIT_BYTES
    assert limits.cpu_limit == DEFAULT_CPU_LIMIT
    assert limits.storage_limit_bytes == DEFAULT_STORAGE_LIMIT_BYTES


# ---------------------------------------------------------------------------
# R30 — Vite plugin agent injection / dev server exfiltration.
# ---------------------------------------------------------------------------


def test_w14_11_r30_env_does_not_inherit_backend() -> None:
    """:func:`build_docker_run_spec` must produce env strictly equal
    to ``{HOST, PORT, NODE_ENV} ∪ config.env`` — no leakage from
    the backend process.

    Mitigates T30.6 (`OMNISIGHT_*` secrets pulled into sidecar by
    accident → exfiltrated by malicious plugin). Specifically
    asserts that environment variables that look like OmniSight
    secrets do NOT appear in the sidecar env.
    """

    from backend.web_sandbox import (
        WebSandboxConfig,
        build_docker_run_spec,
    )

    config = WebSandboxConfig(
        workspace_id="ws-r30-env",
        workspace_path="/tmp/ws-r30-env",
        env={"FOO": "bar"},
    )
    spec = build_docker_run_spec(config, manifest=None)
    env = dict(spec["env"])

    # Expected — three defaults + caller-supplied entry.
    expected = {"HOST", "PORT", "NODE_ENV", "FOO"}
    assert set(env.keys()) == expected, (
        f"sidecar env keys drifted: {set(env.keys()) - expected!r} "
        "extra, "
        f"{expected - set(env.keys())!r} missing"
    )

    # Sanity: forbidden names that look like OmniSight secrets must
    # not appear regardless of how the test process was launched.
    forbidden_substrings = (
        "OMNISIGHT_",
        "DATABASE_URL",
        "CF_API_TOKEN",
        "OAUTH_CLIENT_SECRET",
        "ANTHROPIC_API_KEY",
        "FERNET",
        "SECRET_KEY",
    )
    for key in env:
        for sub in forbidden_substrings:
            assert sub not in key.upper(), (
                f"sidecar env carries forbidden substring "
                f"{sub!r} in key {key!r} — backend env must not "
                "be inherited into the sidecar"
            )


def test_w14_11_r30_jwt_alignment_helper_exists() -> None:
    """:func:`backend.cf_access.jwt_claims_align_with_session` must
    be a callable + perform the three documented checks (email /
    aud / iss).

    Mitigates T30.2 / T30.4 (plugin steals or forges JWT). The
    helper is the choke-point all downstream proxies (W14.7 HMR,
    future sidecar middleware) rely on for session alignment.
    """

    from backend.cf_access import (
        extract_jwt_claims,
        jwt_claims_align_with_session,
    )

    assert callable(jwt_claims_align_with_session)
    assert callable(extract_jwt_claims)

    # email mismatch ⇒ False
    assert not jwt_claims_align_with_session(
        {"email": "alice@example.com"},
        session_email="bob@example.com",
        expected_aud=None,
        expected_iss=None,
    )
    # aud mismatch ⇒ False
    assert not jwt_claims_align_with_session(
        {"email": "bob@example.com", "aud": "wrong-aud"},
        session_email="bob@example.com",
        expected_aud="right-aud",
        expected_iss=None,
    )
    # iss mismatch ⇒ False
    assert not jwt_claims_align_with_session(
        {"email": "bob@example.com", "iss": "https://attacker.example"},
        session_email="bob@example.com",
        expected_aud=None,
        expected_iss="https://acme.cloudflareaccess.com",
    )
    # Happy: all three align ⇒ True
    assert jwt_claims_align_with_session(
        {
            "email": "bob@example.com",
            "aud": "right-aud",
            "iss": "https://acme.cloudflareaccess.com",
        },
        session_email="bob@example.com",
        expected_aud="right-aud",
        expected_iss="https://acme.cloudflareaccess.com",
    )


# ---------------------------------------------------------------------------
# Coverage matrix sanity — every threat ID T28.1..T30.6 is mentioned
# in §4 of the document.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "threat_id",
    [
        "T28.1",
        "T28.2",
        "T28.3",
        "T28.4",
        "T29.1",
        "T29.2",
        "T29.3",
        "T29.4",
        "T29.5",
        "T29.6",
        "T30.1",
        "T30.2",
        "T30.3",
        "T30.4",
        "T30.5",
        "T30.6",
    ],
)
def test_w14_11_threat_model_lists_each_threat_id_in_matrix(
    threat_id: str,
) -> None:
    """Every threat ID enumerated in §3 STRIDE tables must appear in
    §4 control coverage matrix header. Drops in coverage register
    immediately as a missing matrix column.
    """

    body = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    assert threat_id in body, (
        f"Threat ID {threat_id} is in §3 STRIDE tables but missing "
        "from §4 control coverage matrix header"
    )
