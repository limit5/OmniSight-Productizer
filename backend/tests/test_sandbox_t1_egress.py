"""Phase 64-A S2 — Tier 1 egress whitelist + double-gate."""

from __future__ import annotations

import logging

import pytest

from backend import sandbox_net as sn


@pytest.fixture(autouse=True)
def _reset():
    sn._reset_dns_cache_for_tests()
    yield
    sn._reset_dns_cache_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Host list parsing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_parse_empty_returns_empty_list():
    assert sn._parse_host_list("") == []
    assert sn._parse_host_list("   ") == []


def test_parse_simple_csv():
    assert sn._parse_host_list("a.com,b.com") == [("a.com", None), ("b.com", None)]


def test_parse_with_ports():
    assert sn._parse_host_list("github.com,gerrit.internal:29418") == [
        ("github.com", None), ("gerrit.internal", 29418),
    ]


def test_parse_drops_invalid_port_with_warning(caplog):
    caplog.set_level(logging.WARNING, logger="backend.sandbox_net")
    out = sn._parse_host_list("good.com,bad.com:not-a-port")
    assert out == [("good.com", None)]
    assert any("invalid port" in rec.message for rec in caplog.records)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Double-gate: both required to open egress
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_default_air_gapped_when_both_gates_off(monkeypatch):
    monkeypatch.setattr("backend.config.settings.t1_allow_egress", False, raising=False)
    monkeypatch.setattr("backend.config.settings.t1_egress_allow_hosts", "", raising=False)
    arg = await sn.resolve_network_arg()
    assert arg == "--network none"


@pytest.mark.asyncio
async def test_air_gapped_when_only_hosts_set(monkeypatch, caplog):
    monkeypatch.setattr("backend.config.settings.t1_allow_egress", False, raising=False)
    monkeypatch.setattr("backend.config.settings.t1_egress_allow_hosts", "github.com", raising=False)
    caplog.set_level(logging.WARNING, logger="backend.sandbox_net")
    arg = await sn.resolve_network_arg()
    assert arg == "--network none"
    assert any("ALLOW_EGRESS is false" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_air_gapped_when_only_flag_set(monkeypatch, caplog):
    monkeypatch.setattr("backend.config.settings.t1_allow_egress", True, raising=False)
    monkeypatch.setattr("backend.config.settings.t1_egress_allow_hosts", "", raising=False)
    caplog.set_level(logging.WARNING, logger="backend.sandbox_net")
    arg = await sn.resolve_network_arg()
    assert arg == "--network none"
    assert any("allow-hosts is empty" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_egress_opens_when_both_gates_satisfied(monkeypatch):
    monkeypatch.setattr("backend.config.settings.t1_allow_egress", True, raising=False)
    monkeypatch.setattr("backend.config.settings.t1_egress_allow_hosts", "127.0.0.1", raising=False)
    # Stub docker network ls to claim the bridge already exists.
    async def fake_runner(cmd, timeout=10):
        if "network ls" in cmd:
            return (0, sn.T1_NETWORK_NAME, "")
        return (0, "", "")
    arg = await sn.resolve_network_arg(runner=fake_runner)
    assert arg == f"--network {sn.T1_NETWORK_NAME}"


@pytest.mark.asyncio
async def test_egress_falls_back_when_bridge_creation_fails(monkeypatch, caplog):
    monkeypatch.setattr("backend.config.settings.t1_allow_egress", True, raising=False)
    monkeypatch.setattr("backend.config.settings.t1_egress_allow_hosts", "127.0.0.1", raising=False)
    async def fake_runner(cmd, timeout=10):
        if "network ls" in cmd:
            return (0, "", "")  # bridge missing
        if "network create" in cmd:
            return (1, "", "permission denied")
        return (0, "", "")
    caplog.set_level(logging.ERROR, logger="backend.sandbox_net")
    arg = await sn.resolve_network_arg(runner=fake_runner)
    assert arg == "--network none"
    assert any("falling back to --network none" in rec.message for rec in caplog.records)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ensure_egress_network — idempotent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_ensure_egress_network_creates_when_missing():
    calls: list[str] = []
    async def fake_runner(cmd, timeout=10):
        calls.append(cmd)
        if "network ls" in cmd:
            return (0, "", "")
        if "network create" in cmd:
            return (0, "abc123", "")
        return (0, "", "")
    name = await sn.ensure_egress_network(runner=fake_runner)
    assert name == sn.T1_NETWORK_NAME
    assert any("network create" in c for c in calls)


@pytest.mark.asyncio
async def test_ensure_egress_network_skips_when_present():
    calls: list[str] = []
    async def fake_runner(cmd, timeout=10):
        calls.append(cmd)
        if "network ls" in cmd:
            return (0, sn.T1_NETWORK_NAME, "")
        return (0, "", "")
    await sn.ensure_egress_network(runner=fake_runner)
    assert not any("network create" in c for c in calls)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DNS cache
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_resolve_allow_ips_caches_within_ttl(monkeypatch):
    calls = {"n": 0}
    _real_resolver = None

    async def fake_getaddrinfo(host, port, type=None):
        calls["n"] += 1
        return [(0, 0, 0, "", ("127.0.0.1", port or 0))]

    import asyncio
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo, raising=False)

    hosts = [("example.com", None)]
    out1 = await sn.resolve_allow_ips(hosts, now=1000.0)
    out2 = await sn.resolve_allow_ips(hosts, now=1100.0)  # within TTL
    assert out1 == out2 == {"example.com": ["127.0.0.1"]}
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_resolve_allow_ips_reresolves_after_ttl(monkeypatch):
    calls = {"n": 0}

    async def fake_getaddrinfo(host, port, type=None):
        calls["n"] += 1
        return [(0, 0, 0, "", ("127.0.0.1", port or 0))]

    import asyncio
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo, raising=False)

    hosts = [("example.com", None)]
    await sn.resolve_allow_ips(hosts, now=1000.0)
    await sn.resolve_allow_ips(hosts, now=1000.0 + sn._DNS_CACHE_TTL_S + 1)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_resolve_allow_ips_failed_dns_returns_empty(monkeypatch, caplog):
    async def fake_getaddrinfo(host, port, type=None):
        raise OSError("nodename nor servname provided")

    import asyncio
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo, raising=False)

    caplog.set_level(logging.WARNING, logger="backend.sandbox_net")
    out = await sn.resolve_allow_ips([("nope.invalid", None)])
    assert out == {"nope.invalid": []}
    assert any("DNS resolve" in rec.message for rec in caplog.records)
