"""Phase 65 S3 — fine-tune backend abstraction."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend import finetune_backend as fb


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  select_backend factory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_default_is_noop(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_FINETUNE_BACKEND", raising=False)
    backend = fb.select_backend()
    assert backend.name == "noop"
    assert isinstance(backend, fb.NoopBackend)


@pytest.mark.parametrize("env,cls", [
    ("noop", fb.NoopBackend),
    ("openai", fb.OpenAIBackend),
    ("unsloth", fb.UnslothBackend),
    ("NoOp", fb.NoopBackend),       # case-insensitive
    ("OPENAI", fb.OpenAIBackend),
])
def test_select_by_env(monkeypatch, env, cls):
    monkeypatch.setenv("OMNISIGHT_FINETUNE_BACKEND", env)
    assert isinstance(fb.select_backend(), cls)


def test_select_unknown_falls_back_to_noop(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("OMNISIGHT_FINETUNE_BACKEND", "imaginary-backend")
    caplog.set_level(logging.WARNING, logger="backend.finetune_backend")
    b = fb.select_backend()
    assert b.name == "noop"
    assert any("imaginary-backend" in r.getMessage() for r in caplog.records)


def test_select_explicit_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_FINETUNE_BACKEND", "openai")
    assert fb.select_backend("noop").name == "noop"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NoopBackend
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_noop_submit_returns_synthetic_handle(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("{}\n")
    h = await fb.NoopBackend().submit(p, base_model="gpt-4o-mini",
                                       suffix="exp1")
    assert h.backend == "noop"
    assert h.external_id.startswith("noop-")
    assert h.metadata["suffix"] == "exp1"


@pytest.mark.asyncio
async def test_noop_poll_returns_succeeded_immediately(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("{}\n")
    backend = fb.NoopBackend()
    h = await backend.submit(p, base_model="b", suffix="s")
    s = await backend.poll(h)
    assert s.state == "succeeded"
    assert s.fine_tuned_model is not None
    assert s.fine_tuned_model.startswith("ft:b:s-")


@pytest.mark.asyncio
async def test_noop_poll_works_with_empty_suffix(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("{}\n")
    backend = fb.NoopBackend()
    h = await backend.submit(p, base_model="b")
    s = await backend.poll(h)
    assert s.state == "succeeded"
    assert "noop" in s.fine_tuned_model


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OpenAIBackend availability gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_openai_raises_unavailable_when_no_key(monkeypatch):
    """SDK might be installed; but missing key → BackendUnavailable."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    backend = fb.OpenAIBackend()
    # _client() is the gate.
    try:
        import openai  # noqa: F401
    except ImportError:
        pytest.skip("openai SDK not installed; gate path covered separately")
    with pytest.raises(fb.BackendUnavailable, match="OPENAI_API_KEY"):
        backend._client()


@pytest.mark.asyncio
async def test_openai_raises_unavailable_when_sdk_missing(monkeypatch):
    """Force the import path to fail to exercise the SDK-missing branch."""
    import sys
    real = sys.modules.pop("openai", None)
    sys.modules["openai"] = None  # type: ignore
    try:
        backend = fb.OpenAIBackend()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-anything")
        with pytest.raises(fb.BackendUnavailable, match="openai SDK"):
            backend._client()
    finally:
        if real is not None:
            sys.modules["openai"] = real
        else:
            sys.modules.pop("openai", None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UnslothBackend with injected runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _scripted_runner(replies: list[tuple[int, str, str]]):
    """Yield consecutive replies for each call. Records the cmds."""
    calls: list[list[str]] = []
    it = iter(replies)

    async def runner(cmd):
        calls.append(cmd)
        try:
            return next(it)
        except StopIteration:
            return (0, "", "")

    return runner, calls


@pytest.mark.asyncio
async def test_unsloth_submit_happy(tmp_path):
    runner, calls = _scripted_runner([(0, "OK\n", "")])
    backend = fb.UnslothBackend(runner=runner)
    h = await backend.submit(tmp_path / "data.jsonl",
                              base_model="meta/llama-3", suffix="omn")
    assert h.backend == "unsloth"
    assert h.external_id.startswith("unsloth-")
    assert h.metadata["base_model"] == "meta/llama-3"
    # Submit cmd carries data path + base + suffix + job-id.
    cmd = calls[0]
    assert "submit" in cmd
    assert any("data.jsonl" in c for c in cmd)
    assert "--base" in cmd and "meta/llama-3" in cmd
    assert "--suffix" in cmd and "omn" in cmd


@pytest.mark.asyncio
async def test_unsloth_submit_failure_raises(tmp_path):
    runner, _ = _scripted_runner([(2, "", "GPU OOM")])
    with pytest.raises(fb.BackendUnavailable, match="rc=2"):
        await fb.UnslothBackend(runner=runner).submit(
            tmp_path / "x.jsonl", base_model="b",
        )


@pytest.mark.asyncio
async def test_unsloth_poll_parses_status_and_model():
    runner, _ = _scripted_runner([
        (0, "OK\n", ""),  # submit
        (0, "STATUS: succeeded\nMODEL: meta/llama3-omn-abc123\n", ""),
    ])
    backend = fb.UnslothBackend(runner=runner)
    h = await backend.submit(Path("/tmp/x"), base_model="b")
    s = await backend.poll(h)
    assert s.state == "succeeded"
    assert s.fine_tuned_model == "meta/llama3-omn-abc123"


@pytest.mark.asyncio
async def test_unsloth_poll_running_no_model_yet():
    runner, _ = _scripted_runner([
        (0, "OK\n", ""),
        (0, "STATUS: running\n", ""),
    ])
    backend = fb.UnslothBackend(runner=runner)
    h = await backend.submit(Path("/tmp/x"), base_model="b")
    s = await backend.poll(h)
    assert s.state == "running"
    assert s.fine_tuned_model is None


@pytest.mark.asyncio
async def test_unsloth_poll_failure_returns_failed_state():
    runner, _ = _scripted_runner([
        (0, "OK\n", ""),
        (3, "", "process died"),
    ])
    backend = fb.UnslothBackend(runner=runner)
    h = await backend.submit(Path("/tmp/x"), base_model="b")
    s = await backend.poll(h)
    assert s.state == "failed"
    assert "process died" in (s.error or "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Protocol conformance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_all_backends_implement_protocol():
    for cls in (fb.NoopBackend, fb.OpenAIBackend, fb.UnslothBackend):
        assert isinstance(cls(), fb.FinetuneBackend)
