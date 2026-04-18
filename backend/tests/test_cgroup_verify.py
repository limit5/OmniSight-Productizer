"""M1 — cgroup_verify helper tests.

The helper reads `cpu.weight` (cgroup v2) or `cpu.shares` (cgroup v1)
out of `/sys/fs/cgroup/...`. We can't actually create a cgroup in CI
(needs root + cgroupfs mount), so the tests fake the daemon path
discovery and the file reads via a tmp-path layout that mirrors what
systemd-managed docker writes.
"""

from __future__ import annotations


import pytest

from backend import cgroup_verify as cv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  read_cpu_weight
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_reads_cgroup_v2_cpu_weight(tmp_path, monkeypatch):
    cid = "f" * 64
    scope = tmp_path / f"docker-{cid}.scope"
    scope.mkdir()
    (scope / "cpu.weight").write_text("400\n")

    async def fake_id(_n):
        return cid
    monkeypatch.setattr(cv, "_container_id", fake_id)
    monkeypatch.setattr(
        cv, "_candidate_cgroup_paths", lambda _c: [scope],
    )

    weight, source = await cv.read_cpu_weight("any")
    assert weight == 400
    assert source == "cpu.weight"


@pytest.mark.asyncio
async def test_falls_back_to_cgroup_v1_cpu_shares(tmp_path, monkeypatch):
    cid = "e" * 64
    scope = tmp_path / f"docker-{cid}.scope"
    scope.mkdir()
    (scope / "cpu.shares").write_text("4096\n")

    async def fake_id(_n):
        return cid
    monkeypatch.setattr(cv, "_container_id", fake_id)
    monkeypatch.setattr(
        cv, "_candidate_cgroup_paths", lambda _c: [scope],
    )

    weight, source = await cv.read_cpu_weight("any")
    assert weight == 4096
    assert source == "cpu.shares"


@pytest.mark.asyncio
async def test_returns_none_when_container_unknown(monkeypatch):
    async def fake_id(_n):
        return None
    monkeypatch.setattr(cv, "_container_id", fake_id)

    weight, source = await cv.read_cpu_weight("ghost")
    assert weight is None
    assert source == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  verify_weight_ratio — A:B = 4:1 (the M1 acceptance scenario)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_four_to_one_ratio_within_tolerance(monkeypatch):
    async def fake_read(name):
        return ({"a": (4096, "cpu.weight"),
                 "b": (1024, "cpu.weight")}[name])
    monkeypatch.setattr(cv, "read_cpu_weight", fake_read)

    ok, actual, details = await cv.verify_weight_ratio("a", "b", 4.0)
    assert ok is True
    assert actual == 4.0
    assert details["weight_a"] == 4096
    assert details["weight_b"] == 1024


@pytest.mark.asyncio
async def test_off_ratio_fails(monkeypatch):
    async def fake_read(name):
        return ({"a": (1024, "cpu.weight"),
                 "b": (1024, "cpu.weight")}[name])
    monkeypatch.setattr(cv, "read_cpu_weight", fake_read)

    ok, actual, details = await cv.verify_weight_ratio("a", "b", 4.0)
    assert ok is False
    assert actual == 1.0


@pytest.mark.asyncio
async def test_tolerance_window(monkeypatch):
    """4.0 expected with default 20% tolerance accepts [3.2, 4.8]."""
    async def fake_read(name):
        return ({"a": (3700, "cpu.weight"),  # 3700/1000 = 3.7 → in window
                 "b": (1000, "cpu.weight")}[name])
    monkeypatch.setattr(cv, "read_cpu_weight", fake_read)

    ok, actual, _ = await cv.verify_weight_ratio("a", "b", 4.0)
    assert ok is True
    assert 3.2 <= actual <= 4.8


@pytest.mark.asyncio
async def test_missing_weights_treated_as_failure(monkeypatch):
    async def fake_read(name):
        return (None, "")
    monkeypatch.setattr(cv, "read_cpu_weight", fake_read)

    ok, actual, details = await cv.verify_weight_ratio("a", "b", 4.0)
    assert ok is False
    assert actual == 0.0
    assert details["weight_a"] is None
