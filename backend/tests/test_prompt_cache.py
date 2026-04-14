"""Phase 67-A — prompt cache marker layer."""

from __future__ import annotations

import logging

import pytest

from backend import prompt_cache as pc


@pytest.fixture(autouse=True)
def _reset():
    pc.reset_warnings_for_tests()
    yield
    pc.reset_warnings_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Order contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_message_order_is_enforced_regardless_of_add_order():
    b = pc.CachedPromptBuilder()
    b.add_volatile_log("latest log").add_static_kb("manual").add_system("you are…")
    msgs = b.build_for("openai")
    # First message must be system, last must be the volatile log.
    assert msgs[0]["content"].startswith("you are")
    assert msgs[-1]["content"] == "latest log"


def test_blank_segments_are_dropped():
    b = pc.CachedPromptBuilder().add_system("").add_static_kb("   ").add_volatile_log("real")
    msgs = b.build_for("openai")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "real"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Anthropic — explicit cache_control markers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_anthropic_marks_system_blocks_cacheable():
    b = pc.CachedPromptBuilder().add_system("rules").add_tools("tool list")
    msgs = b.build_for("anthropic")
    sys_blocks = msgs[0]["_anthropic_system_blocks"]
    assert len(sys_blocks) == 2
    for blk in sys_blocks:
        assert blk["cache_control"] == {"type": "ephemeral"}


def test_anthropic_marks_static_kb_cacheable_but_not_conversation_or_volatile():
    b = (pc.CachedPromptBuilder()
         .add_system("sys")
         .add_static_kb("manual")
         .add_conversation("user said hi")
         .add_volatile_log("compile output 5 MB"))
    msgs = b.build_for("anthropic")
    # Skip the system blocks wrapper.
    body = [m for m in msgs if "role" in m]
    static_msg = body[0]
    convo_msg = body[1]
    log_msg = body[2]
    assert static_msg["content"][0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in convo_msg["content"][0]
    assert "cache_control" not in log_msg["content"][0]


def test_anthropic_handles_only_volatile_no_system_block():
    msgs = pc.CachedPromptBuilder().add_volatile_log("just log").build_for("anthropic")
    # No system wrapper because nothing cacheable.
    assert all("_anthropic_system_blocks" not in m for m in msgs)
    assert msgs[0]["content"][0]["text"] == "just log"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OpenAI — auto-cache, no markers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_openai_emits_plain_messages():
    msgs = (pc.CachedPromptBuilder()
            .add_system("sys")
            .add_static_kb("kb")
            .add_volatile_log("log")
            .build_for("openai"))
    for m in msgs:
        assert "cache_control" not in str(m), "OpenAI must NOT carry cache_control"
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "kb"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ollama — no-op + one-shot warning
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_ollama_warns_once_then_silent(caplog):
    caplog.set_level(logging.WARNING, logger="backend.prompt_cache")
    pc.CachedPromptBuilder().add_system("sys").build_for("ollama")
    pc.CachedPromptBuilder().add_system("sys2").build_for("ollama")
    pc.CachedPromptBuilder().add_system("sys3").build_for("ollama")
    warns = [r for r in caplog.records if "Ollama" in r.getMessage()]
    assert len(warns) == 1, f"expected exactly one Ollama warning, got {len(warns)}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unknown provider — graceful fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_unknown_provider_warns_once_and_falls_back(caplog):
    caplog.set_level(logging.WARNING, logger="backend.prompt_cache")
    msgs = pc.CachedPromptBuilder().add_system("sys").build_for("alien-llm")
    assert msgs[0]["content"] == "sys"
    assert any("alien-llm" in r.getMessage() for r in caplog.records)


def test_empty_provider_string_falls_back():
    """Defensive: callers may pass settings.llm_provider unset."""
    msgs = pc.CachedPromptBuilder().add_system("sys").build_for("")
    assert msgs[0]["content"] == "sys"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Master switch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_PROMPT_CACHE_ENABLED", raising=False)
    assert pc.is_enabled() is True


@pytest.mark.parametrize("val,expected", [
    ("true", True), ("True", True), ("yes", True), ("1", True), ("on", True),
    ("false", False), ("False", False), ("no", False), ("0", False), ("off", False),
])
def test_is_enabled_env_overrides(monkeypatch, val, expected):
    monkeypatch.setenv("OMNISIGHT_PROMPT_CACHE_ENABLED", val)
    assert pc.is_enabled() is expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  record_cache_outcome — metric round-trip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_record_cache_outcome_increments_hit_metric():
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()
    pc.record_cache_outcome("anthropic", hit_tokens=512, miss_tokens=0)
    pc.record_cache_outcome("anthropic", hit_tokens=128, miss_tokens=0)
    samples = list(m.prompt_cache_hit_total.collect()[0].samples)
    hit = [s for s in samples if s.labels.get("provider") == "anthropic"
           and s.name.endswith("_total")]
    assert hit and hit[0].value == 640


def test_record_cache_outcome_increments_miss_when_no_hit():
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()
    pc.record_cache_outcome("openai", hit_tokens=0, miss_tokens=300)
    samples = list(m.prompt_cache_miss_total.collect()[0].samples)
    miss = [s for s in samples if s.labels.get("provider") == "openai"
            and s.name.endswith("_total")]
    assert miss and miss[0].value == 300


def test_record_cache_outcome_silent_when_metrics_unavailable(monkeypatch):
    """Caller side-effects must not hard-depend on prometheus."""
    pc.record_cache_outcome("any", hit_tokens=1, miss_tokens=0)  # must not raise
