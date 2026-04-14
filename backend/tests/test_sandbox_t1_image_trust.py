"""Phase 64-A S3 — agent image digest allow-list."""

from __future__ import annotations

import logging

import pytest

from backend import container as ct


GOOD_DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Allow-list parsing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_parse_empty_returns_empty_set():
    assert ct._parse_allowed_digests("") == set()
    assert ct._parse_allowed_digests("   ") == set()


def test_parse_normal_csv():
    out = ct._parse_allowed_digests(f"{GOOD_DIGEST},{OTHER_DIGEST}")
    assert out == {GOOD_DIGEST, OTHER_DIGEST}


def test_parse_lowercases_and_trims():
    out = ct._parse_allowed_digests(f"  {GOOD_DIGEST.upper()}  ")
    assert out == {GOOD_DIGEST}  # canonical lowercase form


def test_parse_drops_malformed_with_warning(caplog):
    caplog.set_level(logging.WARNING, logger="backend.container")
    out = ct._parse_allowed_digests(f"{GOOD_DIGEST},not-a-digest,sha256:short")
    assert out == {GOOD_DIGEST}
    msgs = [r.message for r in caplog.records]
    assert any("not-a-digest" in m for m in msgs)
    assert any("sha256:short" in m for m in msgs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  assert_image_trusted — open mode (empty allow-list)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_open_mode_skips_check_entirely(monkeypatch):
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "", raising=False,
    )
    called = {"n": 0}
    async def fake_run(cmd, timeout=10):
        called["n"] += 1
        return (0, GOOD_DIGEST, "")
    monkeypatch.setattr(ct, "_run", fake_run)
    # Must not call docker inspect when allow-list is empty.
    await ct.assert_image_trusted("omnisight-agent:any")
    assert called["n"] == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  assert_image_trusted — strict mode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_strict_mode_passes_when_digest_in_list(monkeypatch):
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests",
        f"{GOOD_DIGEST},{OTHER_DIGEST}", raising=False,
    )
    async def fake_run(cmd, timeout=10):
        if "image inspect" in cmd:
            return (0, GOOD_DIGEST, "")
        return (0, "", "")
    monkeypatch.setattr(ct, "_run", fake_run)
    await ct.assert_image_trusted("omnisight-agent:trusted")  # must not raise


@pytest.mark.asyncio
async def test_strict_mode_rejects_when_digest_not_in_list(monkeypatch, caplog):
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests",
        GOOD_DIGEST, raising=False,
    )
    async def fake_run(cmd, timeout=10):
        if "image inspect" in cmd:
            return (0, OTHER_DIGEST, "")
        return (0, "", "")
    monkeypatch.setattr(ct, "_run", fake_run)
    caplog.set_level(logging.ERROR, logger="backend.container")
    with pytest.raises(ct.ImageNotTrusted):
        await ct.assert_image_trusted("omnisight-agent:tampered")
    assert any("NOT in" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_strict_mode_rejects_when_digest_unresolvable(monkeypatch, caplog):
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests",
        GOOD_DIGEST, raising=False,
    )
    async def fake_run(cmd, timeout=10):
        if "image inspect" in cmd:
            return (1, "", "no such image")
        return (0, "", "")
    monkeypatch.setattr(ct, "_run", fake_run)
    caplog.set_level(logging.WARNING, logger="backend.container")
    with pytest.raises(ct.ImageNotTrusted):
        await ct.assert_image_trusted("omnisight-agent:missing")
    assert any("cannot resolve digest" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_strict_mode_lowercases_inspect_output(monkeypatch):
    """The image inspect call may return uppercase hex on some daemons.
    Allow-list parser already lowercases; verify the inspect path
    matches by lowercasing too."""
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests",
        GOOD_DIGEST, raising=False,
    )
    async def fake_run(cmd, timeout=10):
        if "image inspect" in cmd:
            return (0, GOOD_DIGEST.upper(), "")
        return (0, "", "")
    monkeypatch.setattr(ct, "_run", fake_run)
    await ct.assert_image_trusted("omnisight-agent:case-test")  # must not raise
