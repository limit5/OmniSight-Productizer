"""Phase 64-C-LOCAL S1 — T3 runner resolver.

The resolver is the single source of truth that both the DAG
validator (S3) and the T3 executor (S2) consult. Its decisions
must be deterministic across all three call sites, so the test
set locks in:
  * Canonicalisation (aarch64 ↔ arm64, amd64 ↔ x86_64, …)
  * Native-match requires BOTH arch and OS
  * Empty / malformed target degrades to BUNDLE, never silently LOCAL
  * OMNISIGHT_T3_LOCAL_ENABLED=false forces BUNDLE
  * `host_native` platform profile always resolves LOCAL
"""

from __future__ import annotations

import pytest

from backend import t3_resolver as r


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Basic matching
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_host_arch_and_os_are_strings():
    assert isinstance(r.host_arch(), str)
    assert isinstance(r.host_os(), str)


def test_exact_match_resolves_local(monkeypatch):
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_t3_runner("x86_64", "linux")
    assert res.kind == r.T3RunnerKind.LOCAL
    assert res.target_arch == "x86_64"
    assert res.host_arch == "x86_64"


def test_canonicalised_match_resolves_local(monkeypatch):
    """amd64, x64, aarch64 etc. are synonyms. A host that calls itself
    x86_64 must still match a target that calls itself amd64."""
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_t3_runner("amd64", "linux")
    assert res.kind == r.T3RunnerKind.LOCAL
    res = r.resolve_t3_runner("x64", "linux")
    assert res.kind == r.T3RunnerKind.LOCAL


def test_arch_mismatch_falls_back_to_bundle(monkeypatch):
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_t3_runner("aarch64", "linux")
    assert res.kind == r.T3RunnerKind.BUNDLE
    assert "does not match" in res.reason


def test_os_mismatch_falls_back_to_bundle(monkeypatch):
    """Same arch but different OS (e.g. x86_64 linux → x86_64 windows)
    must NOT go local."""
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_t3_runner("x86_64", "windows")
    assert res.kind == r.T3RunnerKind.BUNDLE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Defence against under-specified input
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_empty_arch_never_resolves_local(monkeypatch):
    """An under-specified DAG must not silently get LOCAL — the
    operator should see BUNDLE with "target arch not specified"
    and fix the spec."""
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_t3_runner("", "linux")
    assert res.kind == r.T3RunnerKind.BUNDLE
    assert "not specified" in res.reason


def test_native_arch_matches_rejects_empty():
    assert r.native_arch_matches("", "linux") is False
    assert r.native_arch_matches("x86_64", "") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Kill-switch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_local_disabled_forces_bundle(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_T3_LOCAL_ENABLED", "false")
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_t3_runner("x86_64", "linux")
    assert res.kind == r.T3RunnerKind.BUNDLE
    assert "OMNISIGHT_T3_LOCAL_ENABLED=false" in res.reason


def test_local_enabled_defaults_on(monkeypatch):
    """No env set → LOCAL matches stay enabled."""
    monkeypatch.delenv("OMNISIGHT_T3_LOCAL_ENABLED", raising=False)
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_t3_runner("x86_64", "linux")
    assert res.kind == r.T3RunnerKind.LOCAL


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Platform-profile convenience
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_host_native_profile_always_local(monkeypatch):
    """The host_native profile is the T1-A default — it explicitly
    means 'the host, whatever that is'. Resolver must honour that
    without caring which fields the YAML left blank."""
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_from_profile({"platform": "host_native", "kernel_arch": ""})
    assert res.kind == r.T3RunnerKind.LOCAL


def test_profile_with_arch_resolves_by_arch(monkeypatch):
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_from_profile({"platform": "aarch64", "kernel_arch": "arm64"})
    assert res.kind == r.T3RunnerKind.BUNDLE


def test_profile_none_falls_back_to_bundle(monkeypatch):
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    res = r.resolve_from_profile(None)
    assert res.kind == r.T3RunnerKind.BUNDLE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Metric bump
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_record_dispatch_swallows_errors(monkeypatch):
    """Failed metric bump must never abort task dispatch."""
    import backend.metrics as _m

    # Force a raise inside the metric path.
    class Boom:
        def labels(self, **_):
            raise RuntimeError("registry down")
    monkeypatch.setattr(_m, "t3_runner_dispatch_total", Boom(), raising=False)
    # Should return cleanly.
    r.record_dispatch(r.T3RunnerKind.LOCAL)
