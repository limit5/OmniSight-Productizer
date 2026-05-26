"""OP-1764 (G.A-v2 Family ⑨ · v2-⑨-Integration) — end-to-end chaos cycle.

Integration test for the ai-core *optional auxiliary service* path, wiring the
shipped pieces together with a synthetic up→down→up chaos cycle driven by a
**stub** ai-core (NOT a live container, per family ⑨ §3.6) plus a **mock
clock**:

  * probe + 3-of-5 flap window — ``backend.agents.ai_core_probe.AiCoreProbe``
    (OP-1749);
  * active fallback chain — ``backend.agents.llm.build_active_fallback_chain``
    (OP-1743, §3.5);
  * availability gauge — ``omnisight_aux_service_available`` (OP-1749, §3.4);
  * warn-level alert routing — ``backend.alerting.bridge`` AlertBridge framework
    (§4 ``OmniSightAuxServiceUnavailable24h``, ``severity=warn``).

This is **verification-only** — it imports and exercises the shipped modules
unmodified. It asserts the four contract obligations of the chaos cycle
(family ⑨ §3.6 last paragraph / §8.4 ``v2-⑨-Integration`` row):

  (1) the 3-of-5 flap window flips state correctly *and* suppresses a single
      down-blip (§3.4);
  (2) the fallback chain transitions within one probe cycle of the flip (§3.5);
  (3) the ``omnisight_aux_service_available`` gauge flips with the flag (§3.4);
  (4) a ``warn``-level alert fires after the 24h-equivalent of continuous
      unavailability (mock clock) and is **never** a page (§4.1 / §4.2).

The §4 alert rule itself is a Prometheus/AlertBridge rule that lives in the
deploy stack (out of area for this ticket); the in-process trigger it keys on
(``avg_over_time(omnisight_aux_service_available[24h]) == 0``) is the probe's
``aux_service.long_outage`` signal, and the in-process router is the shipped
AlertBridge. The test bridges those two real components.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest

from backend import metrics
from backend.agents import ai_core_probe as probe_mod
from backend.agents import llm
from backend.agents.ai_core_probe import AiCoreProbe
from backend.alerting.bridge import (
    AlertBridge,
    SmtpConfig,
    StdoutEmailAdapter,
    canonical_envelope,
)
from backend.config import settings

pytestmark = pytest.mark.skipif(
    not metrics.is_available(), reason="prometheus_client not installed"
)

# Declared operator chain with ollama present (the ai-core-backed provider). The
# *active* chain drops ollama whenever the probe reports ai-core unavailable.
_DECLARED_CHAIN = "anthropic,openai,google,groq,deepseek,openrouter,ollama"


def setup_function() -> None:
    metrics.reset_for_tests()
    probe_mod.AI_CORE_AVAILABLE = False
    probe_mod.AUX_SERVICE_AVAILABLE[probe_mod.SERVICE_LABEL] = False


@pytest.fixture(autouse=True)
def _declared_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a known declared chain (with ollama) for the chain-builder reads."""
    monkeypatch.setattr(settings, "llm_fallback_chain", _DECLARED_CHAIN, raising=False)


class MockClock:
    """Manually-advanced clock so the 24h outage window costs no wall time."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StubAiCore:
    """Stub ai-core health endpoint (§3.6 — a stub, not a live container).

    Models a sister container: ``running`` → HTTP 200; stopped → the probe's
    ``httpx`` GET raises ``ConnectError`` exactly as a ``docker stop``'d
    container's closed port would. Flipping ``running`` is the chaos lever.
    """

    def __init__(self, *, running: bool) -> None:
        self.running = running
        self.calls = 0

    def __call__(self, _url: str, _timeout_s: float) -> int:
        self.calls += 1
        if not self.running:
            raise httpx.ConnectError("connection refused (stub ai-core stopped)")
        return 200


def _gauge_value(service: str = probe_mod.SERVICE_LABEL) -> float | None:
    """Return the current ``omnisight_aux_service_available`` gauge sample."""
    from prometheus_client import generate_latest

    needle = f'omnisight_aux_service_available{{service="{service}"}} '
    for line in generate_latest(metrics.REGISTRY).decode().splitlines():
        if line.startswith(needle):
            return float(line[len(needle):])
    return None


def _ollama_in_active_chain() -> bool:
    return "ollama" in llm.build_active_fallback_chain()


# ── (1)+(2)+(3) full up→down→up chaos cycle ─────────────────────────────────


def test_chaos_up_down_up_flips_flap_chain_and_gauge() -> None:
    """§3.6 / §8.4: a synthetic up→down→up cycle flips the 3-of-5 flap window,
    the active chain, and the gauge in lock-step, with the chain reflecting each
    flip within the same probe cycle (no lag)."""
    stub = StubAiCore(running=True)
    clock = MockClock()
    probe = AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        initial_available=False,  # fail-closed bootstrap (§3.4)
        http_get=stub,
        clock=clock,
    )

    # Bootstrap: fail-closed — unavailable, ollama excluded, gauge 0.
    assert probe_mod.AI_CORE_AVAILABLE is False
    assert _ollama_in_active_chain() is False
    assert _gauge_value() == 0.0

    # ── UP phase: 3 contiguous up-probes flip available on the 3rd ──────────
    assert probe.probe_once() is False  # 1/3
    assert probe.probe_once() is False  # 2/3
    assert _ollama_in_active_chain() is False  # not flipped yet → still excluded
    assert probe.probe_once() is True  # 3/3 → flip up
    # (2) chain transitions within the *same* probe cycle as the flip.
    assert _ollama_in_active_chain() is True
    assert llm.build_active_fallback_chain()[-1] == "ollama"
    # (3) gauge flipped with the flag.
    assert _gauge_value() == 1.0

    # ── DOWN phase: stop the stub container; 3 down-probes flip unavailable ──
    stub.running = False
    assert probe.probe_once() is True  # 1/3 — single blip suppressed (§3.4)
    assert _ollama_in_active_chain() is True
    assert probe.probe_once() is True  # 2/3 still suppressed
    assert _gauge_value() == 1.0
    assert probe.probe_once() is False  # 3/3 → flip down
    # (2) chain drops ollama within the same probe cycle.
    assert _ollama_in_active_chain() is False
    # (3) gauge flipped down.
    assert _gauge_value() == 0.0

    # ── UP phase again: restart the stub; 3 up-probes flip back up ──────────
    stub.running = True
    assert probe.probe_once() is False  # 1/3
    assert probe.probe_once() is False  # 2/3
    assert probe.probe_once() is True  # 3/3 → flip up
    assert _ollama_in_active_chain() is True
    assert _gauge_value() == 1.0

    # The operator's *declared* chain is never mutated across the cycle (§3.5).
    assert settings.llm_fallback_chain == _DECLARED_CHAIN


def test_single_down_blip_is_suppressed_chain_and_gauge_hold() -> None:
    """(1) §3.4: while available, a lone down-observation surrounded by ups never
    reaches the 3-of-5 threshold — the flag, the active chain (ollama kept) and
    the gauge all hold steady."""
    stub = StubAiCore(running=True)
    probe = AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        initial_available=True,  # already up
        http_get=stub,
    )

    # up, up, DOWN-blip, up, up → only 1 down in the window of 5.
    states = [True, True, False, True, True]
    for up in states:
        stub.running = up
        assert probe.probe_once() is True  # never flips
        assert _ollama_in_active_chain() is True
        assert _gauge_value() == 1.0

    assert probe_mod.AI_CORE_AVAILABLE is True


def test_chain_transitions_within_one_probe_cycle_of_flip() -> None:
    """(2) §3.5: the active chain reflects a state flip on the very probe cycle
    that flips it — zero additional probe cycles of lag."""
    stub = StubAiCore(running=True)
    probe = AiCoreProbe(
        flap_window=5, flap_threshold=3, initial_available=False, http_get=stub
    )

    # Two ups: not flipped, ollama still excluded.
    probe.probe_once()
    probe.probe_once()
    assert probe_mod.AI_CORE_AVAILABLE is False
    assert _ollama_in_active_chain() is False

    # The flip cycle: chain already includes ollama the instant the flag flips.
    flipped = probe.probe_once()
    assert flipped is True
    assert _ollama_in_active_chain() is True


# ── (4) warn-level 24h alert, never a page ──────────────────────────────────


def _fire_aux_unavailable_24h_alert(bridge: AlertBridge):
    """Fire the §4.1 ``OmniSightAuxServiceUnavailable24h`` envelope through the
    shipped AlertBridge. Shape mirrors family ⑨ §4.1 exactly."""
    return bridge.fire(
        alertname="OmniSightAuxServiceUnavailable24h",
        severity="warn",
        area="integration",
        family="9",
        defense_dimension="D2",
        labels={"service": probe_mod.SERVICE_LABEL},
        annotations={
            "summary": "Optional auxiliary service ai_core unavailable for 24h.",
            "description": (
                "The optional auxiliary service ai_core has not answered its "
                "health probe for the last 24 hours; the productizer is running "
                "without it (available-then-use, unavailable-then-skip)."
            ),
            "runbook_url": (
                "https://docs.sora.services/runbooks/aux-service-24h-unavailable"
            ),
            "remediation_hint": (
                "Check the auxiliary service's own logs; restart it per its own "
                "runbook, or silence this rule if it was taken down intentionally."
            ),
        },
        critical_labels=["service"],
    )


def test_warn_alert_fires_after_24h_and_never_pages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(4) §4.1/§4.2: after the 24h-equivalent (mock clock) of continuous
    unavailability the probe raises its long-outage signal, which routes a
    ``warn`` alert through the shipped AlertBridge — never a page."""
    stub = StubAiCore(running=False)  # stopped container
    clock = MockClock()
    probe = AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        initial_available=True,  # was up, now goes down
        http_get=stub,
        clock=clock,
    )

    # Flip down (3 contiguous down-probes), then sit unavailable for < 24h:
    # the long-outage trigger must NOT have fired yet.
    with caplog.at_level("ERROR", logger=probe_mod.__name__):
        for _ in range(3):
            probe.probe_once()
        assert probe_mod.AI_CORE_AVAILABLE is False
        assert _gauge_value() == 0.0

        clock.advance(probe_mod.LONG_OUTAGE_S - 1)
        probe.probe_once()
        assert not any(
            "aux_service.long_outage" in r.message for r in caplog.records
        )

        # Cross the 24h threshold: now the long-outage signal fires.
        clock.advance(2)
        probe.probe_once()
        assert any(
            "aux_service.long_outage" in r.message for r in caplog.records
        )

    # The long-outage signal routes a warn alert through the shipped AlertBridge.
    # warn → SEVERITY_CHANNEL_MAP["warn"] == (email, stdout); inject a fake SMTP
    # sink so we exercise both channels without real mail (§4.3 v0 routing).
    stream = io.StringIO()
    sent: list = []
    adapter = StdoutEmailAdapter(
        smtp=SmtpConfig("smtp.test", 587, "alerts@example.test", ("oncall@example.test",)),
        stream=stream,
        send_fn=lambda _smtp, msg: sent.append(msg),
    )
    bridge = AlertBridge(adapter)
    envelope = _fire_aux_unavailable_24h_alert(bridge)

    # warn — and never a page (§4.2).
    assert envelope.severity == "warn"
    delivered = json.loads(stream.getvalue().strip())
    assert delivered["severity"] == "warn"
    assert delivered["severity"] != "page"
    assert delivered["alertname"] == "OmniSightAuxServiceUnavailable24h"
    assert delivered["labels"]["service"] == "ai_core"
    # Routed to both warn channels (stdout above + email here), subject is warn.
    assert len(sent) == 1
    assert sent[0]["Subject"].startswith("[warn]")
    assert "[page]" not in sent[0]["Subject"]
    # The canonical envelope is what an AM-webhook swap would have to match.
    assert canonical_envelope(envelope)["severity"] == "warn"


def test_recovery_clears_long_outage_tracking_no_stale_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§3.4: once ai-core recovers the long-outage tracking resets, so a later
    outage cannot inherit a stale 24h timer and fire a spurious alert early."""
    stub = StubAiCore(running=False)
    clock = MockClock()
    probe = AiCoreProbe(
        flap_window=5,
        flap_threshold=3,
        initial_available=True,
        http_get=stub,
        clock=clock,
    )

    # Go down and breach 24h once (timer running).
    for _ in range(3):
        probe.probe_once()
    clock.advance(probe_mod.LONG_OUTAGE_S + 1)
    probe.probe_once()

    # Recover (3 contiguous ups) — this must reset the outage timer.
    stub.running = True
    for _ in range(3):
        probe.probe_once()
    assert probe_mod.AI_CORE_AVAILABLE is True

    # Go down again; a *fresh* short outage must not re-trigger the signal.
    stub.running = False
    caplog.clear()
    with caplog.at_level("ERROR", logger=probe_mod.__name__):
        for _ in range(3):
            probe.probe_once()
        clock.advance(60)  # well under 24h
        probe.probe_once()
        assert not any(
            "aux_service.long_outage" in r.message for r in caplog.records
        )
