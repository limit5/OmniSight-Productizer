"""R2 (#308) — Semantic Entropy Monitor.

Catches agents that are "stuck talking in circles": every iteration
reads differently but means the same thing. The classic loop detector
(``backend.stuck_detector``) already handles ``same error N×`` and
wall-clock timeouts; this module fills the blind spot in between —
rephrased-but-identical output, the kind of cognitive deadlock that
burns tokens without moving the work forward.

Pipeline per agent:
    1. Every N rounds (default 3), ``record_output()`` is invoked with
       the agent's latest textual output.
    2. A rolling window of the last WINDOW_SIZE outputs (default 5) is
       embedded — backend is pluggable (sentence-transformers, Anthropic
       embedding API, or a zero-dep lexical fallback that works offline
       and without heavy ML deps).
    3. Pairwise cosine similarity is computed across the window; the mean
       similarity is published as the ``entropy_score``.
    4. Verdict buckets:
           < 0.50 → ``ok``
           0.50–0.70 → ``warning``
           > 0.70 → ``deadlock``
    5. On ``deadlock``:
           - ``emit_debug_finding`` writes a ``cognitive_deadlock``
             finding to the Debug Blackboard.
           - The ``cognitive_deadlock_total`` Prometheus counter ticks.
       On every measurement:
           - ``agent.entropy`` SSE event is published.
           - ``semantic_entropy_score{agent_id}`` gauge is set.

The monitor deliberately runs BEFORE the stuck_detector so that a
semantically stuck agent can be rescued a full iteration earlier than
the retry-count / wall-clock rules would fire.

Cost profile: local MiniLM inference is ~5 ms / round. We do NOT use
an LLM to judge LLM output (that would double the spend and introduce
circularity). The lexical fallback is O(W²·|V|) with tiny constants —
well under a millisecond for the 5-round window.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_WINDOW_SIZE = 5          # rolling window of last-N outputs
DEFAULT_CHECK_EVERY_N = 3        # check every N-th round
DEFAULT_WARNING_THRESHOLD = 0.50
DEFAULT_DEADLOCK_THRESHOLD = 0.70
SPARKLINE_HISTORY = 20           # how many scores to keep for the UI


Verdict = str  # "ok" | "warning" | "deadlock"


def classify(score: float,
             warn: float = DEFAULT_WARNING_THRESHOLD,
             dead: float = DEFAULT_DEADLOCK_THRESHOLD) -> Verdict:
    if score >= dead:
        return "deadlock"
    if score >= warn:
        return "warning"
    return "ok"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Embedding backends (pluggable)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# An embedding backend is any callable ``(list[str]) -> list[list[float]]``.
EmbedFn = Callable[[Iterable[str]], list[list[float]]]


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "")]


def lexical_embed(texts: Iterable[str]) -> list[list[float]]:
    """Zero-dep fallback: term-frequency cosine in a shared vocabulary.

    This is NOT a real semantic embedding — it only catches
    lexical-overlap rephrasing. It's intentionally good enough to flag
    outputs like ``"I will fix the bug"`` vs ``"Let me fix this bug"``
    which are the canonical cognitive-deadlock cases. We use it by
    default so the module works without installing heavy ML deps; a
    caller can swap in a real transformer via ``set_embedder()``.
    """
    docs = [_tokenize(t) for t in texts]
    vocab: dict[str, int] = {}
    for doc in docs:
        for w in doc:
            if w not in vocab:
                vocab[w] = len(vocab)
    dim = max(1, len(vocab))
    vectors: list[list[float]] = []
    for doc in docs:
        v = [0.0] * dim
        for w in doc:
            v[vocab[w]] += 1.0
        vectors.append(v)
    return vectors


def _try_sentence_transformer_embed() -> EmbedFn | None:
    """Return a MiniLM embed callable, or None if the dep isn't available."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        return None

    try:
        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    except Exception as exc:  # pragma: no cover — network / disk
        logger.info("MiniLM unavailable (%s); falling back to lexical embedder", exc)
        return None

    def _fn(texts: Iterable[str]) -> list[list[float]]:
        vecs = model.encode(list(texts), convert_to_numpy=False, show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]

    return _fn


_embed_lock = threading.Lock()
_active_embedder: EmbedFn = lexical_embed


def set_embedder(fn: EmbedFn | None) -> None:
    """Swap in an embedding backend. Pass None to reset to lexical fallback.

    Tests use this to inject a deterministic vector, and production
    bootstrap can call it with a MiniLM/Anthropic-backed function.
    """
    global _active_embedder
    with _embed_lock:
        _active_embedder = fn or lexical_embed


def get_embedder() -> EmbedFn:
    with _embed_lock:
        return _active_embedder


def autodetect_embedder() -> str:
    """Bootstrap-time helper: pick the best available embedder and
    return a short label for logs / health.
    """
    fn = _try_sentence_transformer_embed()
    if fn is not None:
        set_embedder(fn)
        return "sentence-transformers/MiniLM-L6-v2"
    set_embedder(lexical_embed)
    return "lexical-fallback"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Math
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for i in range(n):
        ai = a[i]
        bi = b[i]
        dot += ai * bi
        norm_a += ai * ai
        norm_b += bi * bi
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (math.sqrt(norm_a) * math.sqrt(norm_b))))


def pairwise_similarity_mean(vectors: list[list[float]]) -> float:
    """Mean of cosine similarity across all unordered pairs."""
    n = len(vectors)
    if n < 2:
        return 0.0
    total = 0.0
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += _cosine(vectors[i], vectors[j])
            count += 1
    return total / count if count else 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-agent rolling state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class AgentEntropyState:
    agent_id: str
    outputs: deque = field(default_factory=lambda: deque(maxlen=DEFAULT_WINDOW_SIZE))
    history: deque = field(default_factory=lambda: deque(maxlen=SPARKLINE_HISTORY))
    round_counter: int = 0
    last_score: float = 0.0
    last_verdict: Verdict = "ok"
    last_updated: float = 0.0
    loop_count: int = 0
    loop_max: int = 10  # ReAct loop max; surfaced in the UI
    deadlock_events: int = 0

    def snapshot(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "entropy_score": round(self.last_score, 4),
            "verdict": self.last_verdict,
            "sparkline": [round(x, 4) for x in self.history],
            "recent_outputs": [o[:280] for o in self.outputs],
            "round_counter": self.round_counter,
            "loop_count": self.loop_count,
            "loop_max": self.loop_max,
            "last_updated": self.last_updated,
            "deadlock_events": self.deadlock_events,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Monitor (thread-safe singleton)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SemanticEntropyMonitor:
    def __init__(
        self,
        *,
        window_size: int = DEFAULT_WINDOW_SIZE,
        check_every_n: int = DEFAULT_CHECK_EVERY_N,
        warn_threshold: float = DEFAULT_WARNING_THRESHOLD,
        dead_threshold: float = DEFAULT_DEADLOCK_THRESHOLD,
    ) -> None:
        self.window_size = window_size
        self.check_every_n = max(1, check_every_n)
        self.warn_threshold = warn_threshold
        self.dead_threshold = dead_threshold
        self._states: dict[str, AgentEntropyState] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._states.clear()

    def _get_state(self, agent_id: str) -> AgentEntropyState:
        st = self._states.get(agent_id)
        if st is None:
            st = AgentEntropyState(
                agent_id=agent_id,
                outputs=deque(maxlen=self.window_size),
                history=deque(maxlen=SPARKLINE_HISTORY),
            )
            self._states[agent_id] = st
        return st

    def ingest(
        self,
        agent_id: str,
        output: str,
        *,
        task_id: str | None = None,
        force_check: bool = False,
    ) -> dict | None:
        """Record an output and, if this is an Nth round, compute entropy.

        Returns the measurement dict when a check ran this call,
        otherwise None. The dict is what gets broadcast — callers that
        want to build their own SSE / DB write can reuse it instead of
        re-running the math.
        """
        if not output:
            return None
        with self._lock:
            st = self._get_state(agent_id)
            st.outputs.append(output)
            st.round_counter += 1
            st.loop_count = min(st.loop_count + 1, st.loop_max)
            should_check = force_check or (st.round_counter % self.check_every_n == 0)
            if not should_check or len(st.outputs) < 2:
                return None
            # Snapshot inputs under the lock, compute outside.
            texts = list(st.outputs)
            round_idx = st.round_counter

        # Embedding + math is the expensive part; run outside the lock.
        try:
            vectors = get_embedder()(texts)
        except Exception as exc:
            logger.warning("semantic_entropy embed failed (%s); using lexical fallback", exc)
            vectors = lexical_embed(texts)
        score = pairwise_similarity_mean(vectors)
        verdict = classify(score, self.warn_threshold, self.dead_threshold)

        # Re-acquire lock to commit.
        with self._lock:
            st = self._get_state(agent_id)
            st.last_score = score
            st.last_verdict = verdict
            st.history.append(score)
            st.last_updated = time.time()
            if verdict == "deadlock":
                st.deadlock_events += 1

        payload = {
            "agent_id": agent_id,
            "task_id": task_id,
            "entropy_score": round(score, 4),
            "threshold_warn": self.warn_threshold,
            "threshold_deadlock": self.dead_threshold,
            "verdict": verdict,
            "window_size": len(texts),
            "round": round_idx,
        }
        _broadcast(payload, recent_outputs=texts)
        return payload

    def snapshot_agent(self, agent_id: str) -> dict | None:
        with self._lock:
            st = self._states.get(agent_id)
            return st.snapshot() if st else None

    def snapshot_all(self) -> list[dict]:
        with self._lock:
            return [st.snapshot() for st in self._states.values()]

    def highest_entropy(self) -> dict | None:
        """Return the agent with the highest recent entropy_score, or None."""
        with self._lock:
            snaps = [st.snapshot() for st in self._states.values() if st.history]
        if not snaps:
            return None
        top = max(snaps, key=lambda s: s["entropy_score"])
        return top


# Module-level singleton. Multiple monitors per process would be
# confusing — everyone should write through the same rolling windows.
_MONITOR = SemanticEntropyMonitor()


def get_monitor() -> SemanticEntropyMonitor:
    return _MONITOR


def reset_for_tests() -> None:
    _MONITOR.reset()
    set_embedder(None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Broadcast — keeps event emission + metrics in one place
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _broadcast(payload: dict, *, recent_outputs: list[str]) -> None:
    """Emit SSE event, bump metrics, fire deadlock finding if needed."""
    agent_id = payload["agent_id"]
    score = float(payload["entropy_score"])
    verdict = payload["verdict"]

    # Prometheus gauge — best-effort, never raise.
    try:
        from backend import metrics as _m
        _m.semantic_entropy_score.labels(agent_id=agent_id).set(score)
        if verdict == "deadlock":
            _m.cognitive_deadlock_total.labels(agent_id=agent_id).inc()
    except Exception:
        pass

    # SSE event — best-effort.
    try:
        from backend.events import emit_agent_entropy
        emit_agent_entropy(
            agent_id=agent_id,
            entropy_score=score,
            verdict=verdict,
            threshold_warn=payload["threshold_warn"],
            threshold_deadlock=payload["threshold_deadlock"],
            window_size=payload["window_size"],
            round_idx=payload.get("round", 0),
            task_id=payload.get("task_id"),
        )
    except Exception as exc:
        logger.debug("emit_agent_entropy failed: %s", exc)

    # Debug blackboard finding when we cross the deadlock threshold.
    if verdict == "deadlock":
        try:
            from backend.events import emit_debug_finding
            from backend.finding_types import FindingType

            task_id = payload.get("task_id") or "-"
            preview = " | ".join(o[:80].replace("\n", " ") for o in recent_outputs[-3:])
            emit_debug_finding(
                task_id=task_id,
                agent_id=agent_id,
                finding_type=FindingType.cognitive_deadlock.value,
                severity="warn",
                message=(
                    f"Semantic entropy {score:.2f} ≥ {payload['threshold_deadlock']:.2f} "
                    f"over last {payload['window_size']} outputs"
                ),
                context={
                    "entropy_score": score,
                    "threshold": payload["threshold_deadlock"],
                    "window_size": payload["window_size"],
                    "round": payload.get("round"),
                    "preview": preview,
                },
            )
        except Exception as exc:
            logger.debug("emit_debug_finding (cognitive_deadlock) failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Convenience: thin wrappers for callers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def record_output(
    agent_id: str,
    output: str,
    *,
    task_id: str | None = None,
    force_check: bool = False,
) -> dict | None:
    """Module-level shortcut used by agents / orchestrator hooks."""
    return _MONITOR.ingest(agent_id, output, task_id=task_id, force_check=force_check)


def snapshot_all() -> list[dict]:
    return _MONITOR.snapshot_all()


def snapshot_agent(agent_id: str) -> dict | None:
    return _MONITOR.snapshot_agent(agent_id)


def highest_entropy_agent() -> dict | None:
    return _MONITOR.highest_entropy()
