"""W1 #275 — web platform profile coverage tests.

Locks in the four web profiles introduced in W1:
  * web-static
  * web-ssr-node
  * web-edge-cloudflare
  * web-vercel

Verifies they:
  1. Are enumerated by `list_profile_ids` (the schema declaration is
     skipped, but real profiles must appear).
  2. Declare `target_kind: web` so the W0 dispatcher routes them
     through `_resolve_web` rather than the embedded resolver.
  3. Carry the four W1-mandated fields: `runtime`, `build_cmd`,
     `bundle_size_budget`, `memory_limit_mb`.
  4. `validate_profile()` returns no errors for any of them — i.e.
     web profiles are NOT forced to declare embedded-only fields like
     `kernel_arch` (the central W0 invariant).
  5. Each profile's bundle / memory budget matches the platform-limit
     reasoning baked into the YAML — tests double as living docs so a
     careless edit that, say, raises the Cloudflare worker bundle to
     50 MiB trips immediately rather than failing at deploy time.
"""

from __future__ import annotations

import pytest

from backend.platform import (
    get_platform_config,
    list_profile_ids,
    load_raw_profile,
    validate_profile,
)

_WEB_PROFILES = (
    "web-static",
    "web-ssr-node",
    "web-edge-cloudflare",
    "web-vercel",
)


@pytest.mark.parametrize("profile_id", _WEB_PROFILES)
def test_web_profile_is_enumerated(profile_id):
    assert profile_id in list_profile_ids()


@pytest.mark.parametrize("profile_id", _WEB_PROFILES)
def test_web_profile_declares_target_kind_web(profile_id):
    """Every W1 profile must declare target_kind=web — that's the
    dispatch signal the W0 loader uses to pick `_resolve_web`."""
    data = load_raw_profile(profile_id)
    assert data.get("target_kind") == "web"


@pytest.mark.parametrize("profile_id", _WEB_PROFILES)
def test_web_profile_declares_required_fields(profile_id):
    """W1 spec: every profile declares runtime / bundle budget /
    memory limit / build cmd. The loader doesn't *force* these (it
    has defaults), but the profiles themselves must be explicit so
    operators can reason about them without grepping Python."""
    data = load_raw_profile(profile_id)
    for key in ("runtime", "build_cmd", "bundle_size_budget", "memory_limit_mb"):
        assert key in data, f"{profile_id} missing required W1 field: {key}"


@pytest.mark.parametrize("profile_id", _WEB_PROFILES)
def test_web_profile_validates_clean(profile_id):
    """Web profiles must not trip embedded-only validations
    (kernel_arch et al). This is the central W0 → W1 invariant."""
    data = load_raw_profile(profile_id)
    errs = validate_profile(data)
    assert errs == [], f"{profile_id}: unexpected validation errors: {errs}"


@pytest.mark.parametrize("profile_id", _WEB_PROFILES)
def test_web_profile_resolves_to_web_toolchain(profile_id):
    """The dispatched build_toolchain block must come from the web
    resolver, not embedded. Catches accidental copy-paste regressions
    that omit `target_kind`."""
    cfg = get_platform_config(profile_id)
    assert cfg["target_kind"] == "web"
    assert cfg["build_toolchain"]["kind"] == "web"
    # cross_prefix / arch must not leak into a web toolchain.
    assert "cross_prefix" not in cfg["build_toolchain"]
    assert "arch" not in cfg["build_toolchain"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-profile budget assertions (living spec)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_web_static_has_no_server_runtime():
    """Static = build artifacts only; no server process at runtime,
    so memory_limit_mb is meaningfully zero."""
    data = load_raw_profile("web-static")
    assert data["runtime"] == "static"
    assert data["memory_limit_mb"] == 0
    # 500 KiB matches the W2 spec critical-path budget.
    assert data["bundle_size_budget"] == "500KiB"


def test_web_ssr_node_pins_node20_lts():
    """W1 spec line item: SSR profile is on Node 20."""
    data = load_raw_profile("web-ssr-node")
    assert data["runtime"] == "node20"
    assert data["runtime_version"].startswith("20."), (
        "web-ssr-node must pin a 20.x runtime_version"
    )
    # 5 MiB server bundle ceiling per W2 spec.
    assert data["bundle_size_budget"] == "5MiB"


def test_web_edge_cloudflare_respects_platform_limits():
    """Cloudflare Workers cap at 1 MiB compressed bundle and 128 MiB
    memory per isolate — both are platform invariants. Any edit that
    raises these is almost certainly wrong; trip it loudly."""
    data = load_raw_profile("web-edge-cloudflare")
    assert data["runtime"] == "cloudflare-workers"
    assert data["bundle_size_budget"] == "1MiB"
    assert data["memory_limit_mb"] == 128
    assert data["deploy_provider"] == "cloudflare-pages"


def test_web_vercel_defaults_to_serverless_runtime():
    """Vercel profile defaults to Serverless (Node) — Edge Functions
    are configured per-route in vercel.json, not at profile level."""
    data = load_raw_profile("web-vercel")
    assert data["runtime"] == "vercel-serverless"
    # Hobby/Pro Serverless unzipped ceiling is 50 MB.
    assert data["bundle_size_budget"] == "50MiB"
    # Vercel Serverless default memory.
    assert data["memory_limit_mb"] == 1024
    assert data["deploy_provider"] == "vercel"
