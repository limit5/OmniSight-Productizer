"""Phase 64-D — killswitch unification: output truncation + healthz."""

from __future__ import annotations

import pytest

from backend import container as ct


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  D3 — exec_in_container output cap
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_exec_output_under_cap_passes_through(monkeypatch):
    monkeypatch.setattr(
        "backend.config.settings.sandbox_max_output_bytes", 1000, raising=False,
    )
    async def fake_run(cmd, timeout=60):
        return (0, "small output", "")
    monkeypatch.setattr(ct, "_run", fake_run)

    rc, out = await ct.exec_in_container("c1", "ls")
    assert rc == 0
    assert out == "small output"
    assert "TRUNCATED" not in out


@pytest.mark.asyncio
async def test_exec_output_above_cap_is_truncated(monkeypatch):
    monkeypatch.setattr(
        "backend.config.settings.sandbox_max_output_bytes", 50, raising=False,
    )
    big = "x" * 500
    async def fake_run(cmd, timeout=60):
        return (0, big, "")
    monkeypatch.setattr(ct, "_run", fake_run)

    rc, out = await ct.exec_in_container("c1", "spam")
    assert rc == 0
    # Truncated body + marker.
    assert "[TRUNCATED" in out
    assert "500 bytes total" in out
    assert "cap=50" in out
    # Body before marker fits within the cap.
    head, _, _ = out.partition("\n[TRUNCATED")
    assert len(head.encode("utf-8")) <= 50


@pytest.mark.asyncio
async def test_exec_output_cap_zero_disables_check(monkeypatch):
    monkeypatch.setattr(
        "backend.config.settings.sandbox_max_output_bytes", 0, raising=False,
    )
    big = "y" * 100_000
    async def fake_run(cmd, timeout=60):
        return (0, big, "")
    monkeypatch.setattr(ct, "_run", fake_run)

    rc, out = await ct.exec_in_container("c1", "huge")
    assert "TRUNCATED" not in out
    assert len(out) == 100_000


@pytest.mark.asyncio
async def test_exec_truncation_increments_metric(monkeypatch):
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()
    monkeypatch.setattr(
        "backend.config.settings.sandbox_max_output_bytes", 10, raising=False,
    )
    async def fake_run(cmd, timeout=60):
        return (0, "z" * 200, "")
    monkeypatch.setattr(ct, "_run", fake_run)

    await ct.exec_in_container("c1", "x", tier="t1")
    samples = list(m.sandbox_output_truncated_total.collect()[0].samples)
    t1 = [s for s in samples
          if s.labels.get("tier") == "t1" and s.name.endswith("_total")]
    assert t1 and t1[0].value >= 1


@pytest.mark.asyncio
async def test_exec_stderr_appended_then_truncated(monkeypatch):
    monkeypatch.setattr(
        "backend.config.settings.sandbox_max_output_bytes", 30, raising=False,
    )
    async def fake_run(cmd, timeout=60):
        return (1, "out", "error spam " * 50)
    monkeypatch.setattr(ct, "_run", fake_run)

    rc, out = await ct.exec_in_container("c1", "fail")
    assert rc == 1
    assert "[STDERR]" in out  # combination happened first
    assert "[TRUNCATED" in out  # then cap kicked in


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  D4 — /healthz sandbox section
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_healthz_exposes_sandbox_block(client):
    r = await client.get("/api/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert "sandbox" in body
    sb = body["sandbox"]
    for k in ("launched", "errors", "lifetime_killed",
              "image_rejected", "output_truncated"):
        assert k in sb, f"missing key: {k}"
        assert isinstance(sb[k], int)


@pytest.mark.asyncio
async def test_healthz_sandbox_counters_track_truncations(client, monkeypatch):
    """When prom is installed and we synthetically bump the counter,
    /healthz must reflect the new value."""
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()
    m.sandbox_output_truncated_total.labels(tier="t1").inc(3)

    r = await client.get("/api/v1/healthz")
    assert r.json()["sandbox"]["output_truncated"] == 3
