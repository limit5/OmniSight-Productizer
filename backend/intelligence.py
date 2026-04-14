"""Phase 63-A — Intelligence Immune System: signal layer.

Per-agent sliding window collecting four orthogonal indicators of
"is this model still thinking clearly?". Read-only — exposes a score
+ alert list. **No mitigation here**. Phase 63-B converts alerts into
Decision Engine proposals.

Indicators (design source: docs/design/intelligence-immune-system.md):

  1. code_pass_rate     — (window of build/simulation outcomes) pass / total
  2. compliance_rate    — (window of workflow finishes) HANDOFF.md updated /
                          total. NEVER LLM-judged — git diff only.
  3. logic_consistency  — token-overlap between the agent's proposed
                          solution and the historical L3 solution for
                          the same error_signature. v1 = simple Jaccard
                          on tokenised words; ≥ 0.3 = consistent. Real
                          embeddings come in 63-B/C.
  4. token_entropy_z    — per-response token-count z-score against the
                          window's own mean+stdev. |z| > 2 → flag (too
                          short = lazy / too long = repetition).

Each `IntelligenceWindow.alerts()` call may yield (level, dim, reason)
tuples where level ∈ {info, warning, critical}. The mapping to
Decision Engine `kind=intelligence/{calibrate,route,contain}` lives in
Phase 63-B — this module is intentionally policy-free.
"""

from __future__ import annotations

import logging
import math
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Literal

logger = logging.getLogger(__name__)

# Default sliding-window size matches the design spec.
DEFAULT_WINDOW = 10

# Threshold knobs (tunable; pinned by tests).
PASS_RATE_WARN = 0.6   # < 60% → warning
PASS_RATE_CRIT = 0.3   # < 30% → critical
COMPLIANCE_WARN = 0.7  # < 70% → warning
CONSISTENCY_MIN = 0.3  # Jaccard < 0.3 vs history → flag for that record
ENTROPY_Z_WARN = 2.0   # |z| > 2 sigma → warning

AlertLevel = Literal["info", "warning", "critical"]
DimName = Literal["code_pass", "compliance", "consistency", "entropy"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tokeniser for Jaccard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _tokenise(text: str) -> set[str]:
    """Cheap word-level tokeniser for the v1 Jaccard score. Lowercases
    and drops noise (numbers, punctuation, single chars). NOT a
    semantic embedding — by design (see module docstring)."""
    return {tok.lower() for tok in _TOKEN_RE.findall(text or "")}


def jaccard(a: str, b: str) -> float:
    """Jaccard similarity between word-token sets of `a` and `b`.
    Returns 1.0 when both are empty so an empty-vs-empty doesn't
    spuriously look like a regression."""
    sa, sb = _tokenise(a), _tokenise(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Window
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _Outcome:
    """A single observed event. The fields are sparse on purpose:
    every callsite only fills the relevant dim."""
    code_pass: bool | None = None
    compliance: bool | None = None
    consistency: float | None = None  # Jaccard score [0,1]
    response_tokens: int | None = None


@dataclass
class IntelligenceWindow:
    """Per-agent rolling window. Thread-safety: the only mutator is
    `record()`, which is sync; callers wishing to share a window
    across threads should wrap externally. The web tier uses one
    window per agent_id (see `get_window()`)."""
    agent_id: str
    size: int = DEFAULT_WINDOW
    _entries: deque[_Outcome] = field(default_factory=lambda: deque(maxlen=DEFAULT_WINDOW))

    def __post_init__(self):
        # deque maxlen has to be set at construction; rebuild if size differs.
        if self._entries.maxlen != self.size:
            self._entries = deque(maxlen=self.size)

    # ── Recording ──

    def record(self, *, code_pass: bool | None = None,
               compliance: bool | None = None,
               consistency: float | None = None,
               response_tokens: int | None = None) -> None:
        """Append one observation. All fields optional; callsites
        record only the dim(s) they have ground truth for."""
        if all(v is None for v in (code_pass, compliance, consistency, response_tokens)):
            return  # no-op: nothing to learn
        self._entries.append(_Outcome(
            code_pass=code_pass, compliance=compliance,
            consistency=consistency, response_tokens=response_tokens,
        ))

    # ── Scoring ──

    def _values(self, attr: str) -> list[float]:
        return [
            float(getattr(e, attr))
            for e in self._entries
            if getattr(e, attr) is not None
        ]

    def code_pass_rate(self) -> float | None:
        v = self._values("code_pass")
        return None if not v else sum(v) / len(v)

    def compliance_rate(self) -> float | None:
        v = self._values("compliance")
        return None if not v else sum(v) / len(v)

    def logic_consistency(self) -> float | None:
        v = self._values("consistency")
        return None if not v else sum(v) / len(v)

    def token_entropy_z(self) -> float | None:
        """z-score of the *most recent* response token count vs the
        prior window. None when window doesn't have ≥2 prior samples
        (need stdev). Returns 0.0 when the prior is constant."""
        ints = [int(e.response_tokens) for e in self._entries
                if e.response_tokens is not None]
        if len(ints) < 3:
            return None
        prior = ints[:-1]
        latest = ints[-1]
        mean = sum(prior) / len(prior)
        var = sum((x - mean) ** 2 for x in prior) / len(prior)
        if var == 0:
            return 0.0
        return (latest - mean) / math.sqrt(var)

    def score(self) -> dict[DimName, float | None]:
        return {
            "code_pass": self.code_pass_rate(),
            "compliance": self.compliance_rate(),
            "consistency": self.logic_consistency(),
            "entropy": self.token_entropy_z(),
        }

    # ── Alerts (signal only — no mitigation) ──

    def alerts(self) -> list[tuple[AlertLevel, DimName, str]]:
        out: list[tuple[AlertLevel, DimName, str]] = []
        cpr = self.code_pass_rate()
        if cpr is not None:
            if cpr < PASS_RATE_CRIT:
                out.append(("critical", "code_pass",
                            f"pass rate {cpr:.0%} < {PASS_RATE_CRIT:.0%}"))
            elif cpr < PASS_RATE_WARN:
                out.append(("warning", "code_pass",
                            f"pass rate {cpr:.0%} < {PASS_RATE_WARN:.0%}"))

        comp = self.compliance_rate()
        if comp is not None and comp < COMPLIANCE_WARN:
            out.append(("warning", "compliance",
                        f"compliance {comp:.0%} < {COMPLIANCE_WARN:.0%}"))

        cons = self.logic_consistency()
        if cons is not None and cons < CONSISTENCY_MIN:
            out.append(("warning", "consistency",
                        f"avg Jaccard {cons:.2f} < {CONSISTENCY_MIN:.2f}"))

        z = self.token_entropy_z()
        if z is not None and abs(z) > ENTROPY_Z_WARN:
            direction = "too short" if z < 0 else "too long"
            out.append(("warning", "entropy",
                        f"latest response z={z:+.2f} ({direction})"))
        return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-agent registry + metric publishers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_WINDOWS: dict[str, IntelligenceWindow] = {}


def get_window(agent_id: str) -> IntelligenceWindow:
    """Get-or-create the window for an agent."""
    w = _WINDOWS.get(agent_id)
    if w is None:
        w = IntelligenceWindow(agent_id=agent_id)
        _WINDOWS[agent_id] = w
    return w


def reset_for_tests() -> None:
    """Drop all per-agent state. Test hook."""
    _WINDOWS.clear()


def _publish_score(w: IntelligenceWindow) -> None:
    """Push the four dims to the Gauge — best-effort."""
    try:
        from backend import metrics as _m
        for dim, val in w.score().items():
            if val is not None:
                _m.intelligence_score.labels(
                    agent_id=w.agent_id, dim=dim,
                ).set(float(val))
    except Exception:
        pass


def _publish_alerts(w: IntelligenceWindow,
                    alerts: Iterable[tuple[AlertLevel, DimName, str]]) -> None:
    try:
        from backend import metrics as _m
        for level, dim, _reason in alerts:
            _m.intelligence_alert_total.labels(
                agent_id=w.agent_id, dim=dim, level=level,
            ).inc()
    except Exception as exc:
        logger.debug("intelligence_alert metric bump failed: %s", exc)


def record_and_publish(agent_id: str, **kwargs) -> tuple[
    dict[DimName, float | None],
    list[tuple[AlertLevel, DimName, str]],
]:
    """High-level convenience: append observation, recompute, push to
    Prometheus. Returns (score, alerts) for the caller to optionally
    log or hand to 63-B. **Does not** trigger any mitigation here."""
    w = get_window(agent_id)
    w.record(**kwargs)
    score = w.score()
    alerts = w.alerts()
    _publish_score(w)
    _publish_alerts(w, alerts)
    if alerts:
        logger.info(
            "[IIS] agent=%s score=%s alerts=%s",
            agent_id, score,
            [(lvl, d, r) for lvl, d, r in alerts],
        )
    return score, alerts
