"""Phase 4-6 soak — multi-tab aggregator load vs ``per_user = 300/60s``.

Validates the central claim behind Phase 4-5 (reverse rate-limit back
to a real defensive cap):

    After the Phase-4 polling consolidation (4-1 aggregator + 4-2
    frontend demux + 4-3 10s interval), a realistic 3-tab free-tier
    operator drives **≈ 36 req/min/tab** (per
    ``docs/dashboard-polling-inventory.md`` §4 "Phase 4-3 current"
    column). 3 × 36 = 108 req/min on the per-user bucket. The
    bucket's capacity is 300 and refill is 5 tok/s = 300/min,
    meaning **sustained load < refill** — zero 429s at steady state.

This file exercises that claim against the real
``InMemoryLimiter`` + real ``PLAN_QUOTAS["free"]`` constants via a
virtual wall-clock, so we can compress a 30-minute operator session
into < 1 s of test time and still assert byte-exact allow/deny
accounting. The matching browser-side validation ("operator live
verify") sits in TODO 4-6 as the operator-gated half of the row.

Why the unit-level simulation is sufficient here:
  * The per-user middleware path itself is already covered by
    ``test_rate_limit_middleware`` — we're not re-testing wiring.
  * The question 4-6 answers is "at the Phase-4 polling rate, does
    the token-bucket mathematically stay above zero?" That is a
    pure function of (capacity, window, request timeline) — the
    HTTP transport is irrelevant.
  * Simulated time lets us run the full 30-min window in CI
    without a slow soak; the invariant it asserts holds identically
    for any duration at this sustained rate.

Polling schedule is a 1:1 transcription of the §4 table (post-4-3,
pre-absorption — the conservative case; once the 4-5 follow-up
absorbs ``opsSummary`` + ``platformStatus`` into the aggregator,
headroom only improves).
"""

from __future__ import annotations


from backend.quota import PLAN_QUOTAS
from backend.rate_limit import InMemoryLimiter


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Polling schedule (mirrors inventory §4 "Phase 4-3 current")
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Each entry: (label, interval_seconds). 60 / interval = req/min.
# Sum = 36 req/min per tab. Labels match the endpoint families
# documented in docs/dashboard-polling-inventory.md.
TAB_POLL_SCHEDULE: tuple[tuple[str, int], ...] = (
    ("dashboard.summary",        10),  # 6/min — useEngine top-level
    ("ops.summary",              10),  # 6/min — ops-summary-panel
    ("orchestration.snapshot",   10),  # 6/min — orchestration-panel poll
    ("pipeline.timeline",        10),  # 6/min — pipeline-timeline poll
    ("audit.entries",            15),  # 4/min — audit-panel
    ("project-runs",             15),  # 4/min — run-history-panel
    ("runtime.platform-status",  15),  # 4/min — arch-indicator
)

EXPECTED_REQ_PER_MIN_PER_TAB = sum(60 // iv for _, iv in TAB_POLL_SCHEDULE)
assert EXPECTED_REQ_PER_MIN_PER_TAB == 36, (
    "Schedule drifted from inventory §4 Phase-4-3 total of 36 req/min — "
    "update docs/dashboard-polling-inventory.md or this constant"
)

TAB_COUNT = 3
# 2.5 s stagger between tab opens so all tabs don't fire t=0 bursts
# together — mirrors a real operator opening tabs over ~5 s.
TAB_STAGGER_S = 2.5
USER_KEY = "api:user:soak-operator"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Virtual-clock plumbing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _VirtualClock:
    """Drop-in replacement for ``time.time`` that advances on demand.

    The limiter computes ``elapsed = now - bucket.last_refill`` to
    decide refill volume, so as long as reads of ``now`` are
    monotonically increasing, the bucket behaves identically to a
    real clock at the same timestamps."""
    __slots__ = ("t",)

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def _build_event_timeline(duration_s: float) -> list[float]:
    """Expand the 3-tab × 7-endpoint schedule into a sorted list of
    absolute fire timestamps for ``duration_s`` of simulated time.

    Each tab's endpoints start at ``tab_id * TAB_STAGGER_S`` and
    repeat at their interval. Same-timestamp events are retained
    (multiple endpoints legitimately fire at t = tab_offset when
    their intervals align) — the limiter sees them sequentially."""
    events: list[float] = []
    for tab_id in range(TAB_COUNT):
        offset = tab_id * TAB_STAGGER_S
        for _label, interval in TAB_POLL_SCHEDULE:
            t = offset
            while t < duration_s:
                events.append(t)
                t += interval
    events.sort()
    return events


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core soak runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _run_soak(duration_s: float, monkeypatch) -> tuple[int, int, list[tuple[float, float]]]:
    """Drive a fresh ``InMemoryLimiter`` through the 3-tab timeline.

    Returns ``(allowed, denied, denies)`` where ``denies`` is a list
    of ``(timestamp, wait_hint)`` tuples for any 429 that fired.
    """
    limiter = InMemoryLimiter()
    quota = PLAN_QUOTAS["free"]
    user_budget = quota.per_user

    # Monkey-patch the ``time.time`` call the limiter reads. We patch
    # inside the rate_limit module via its ``time`` import so the
    # real ``time`` module is untouched for other test paths.
    import backend.rate_limit as rl
    vclock = _VirtualClock(start=0.0)
    monkeypatch.setattr(rl.time, "time", vclock)

    events = _build_event_timeline(duration_s)
    allowed = 0
    denied = 0
    denies: list[tuple[float, float]] = []

    for ts in events:
        vclock.t = ts
        ok, wait = limiter.allow(
            USER_KEY,
            user_budget.capacity,
            user_budget.window_seconds,
        )
        if ok:
            allowed += 1
        else:
            denied += 1
            denies.append((ts, wait))

    return allowed, denied, denies


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Contract tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_soak_3tab_30min_produces_zero_429(monkeypatch):
    """The headline contract: 3 tabs × 36 req/min = 108 req/min
    against a ``per_user = 300 / 60s`` bucket refills at 5 tok/s
    (300/min) > 108/min, so the bucket never depletes and every
    request is allowed. Asserts zero 429s across the full 30-min
    simulated window.

    If this test flips to red, it means either (a) the inventory
    schedule is out of date vs actual dashboard behaviour and is
    undercounting request rate — revisit
    ``docs/dashboard-polling-inventory.md`` §4, or (b) ``per_user``
    got tightened below the sustained rate — revisit
    ``backend/quota.py::PLAN_QUOTAS['free']``. Either way the
    operator's 3-tab live verify would also be producing 429s."""
    duration_s = 30 * 60  # 30 minutes
    allowed, denied, denies = _run_soak(duration_s, monkeypatch)

    # Sanity: the simulated 30 min actually fired the expected volume.
    # Expected = 3 tabs × 36 req/min × 30 min = 3240. We allow ±1 per
    # tab per endpoint for boundary-step rounding (an endpoint that
    # fires at t=0 fires int(duration/interval)+1 times if it aligns,
    # or one fewer if it doesn't — see _build_event_timeline).
    expected = TAB_COUNT * EXPECTED_REQ_PER_MIN_PER_TAB * (duration_s // 60)
    slack = TAB_COUNT * len(TAB_POLL_SCHEDULE)  # ≤ 1 per tab per endpoint
    total = allowed + denied
    assert expected - slack <= total <= expected + slack, (
        f"simulated request count {total} out of bounds "
        f"[{expected - slack}, {expected + slack}] — schedule drift?"
    )

    # Core invariant: **zero** 429s.
    assert denied == 0, (
        f"per_user bucket fired {denied} 429(s) under Phase-4 polling "
        f"pattern (expected 0). First 5: {denies[:5]}. Bucket "
        f"capacity={PLAN_QUOTAS['free'].per_user.capacity}, refill "
        f"{PLAN_QUOTAS['free'].per_user.capacity}/60s = "
        f"{PLAN_QUOTAS['free'].per_user.capacity / 60:.2f} tok/s; "
        f"total requests {total}, mean rate "
        f"{total / duration_s * 60:.1f} req/min."
    )


def test_soak_peak_second_never_exceeds_bucket(monkeypatch):
    """Second-by-second peak-rate guard: even the bursty t=0 alignment
    (where tab 0's 4 x 10s-interval endpoints + 3 x 15s endpoints
    all fire at the same virtual instant) must stay within the
    instantaneous bucket capacity of 300. Guards against a future
    schedule change that would clump endpoints into a co-firing
    spike that could individually bust the cap even if the mean
    rate is healthy."""
    # Worst alignment is t=0 on a cold bucket (300 tokens available).
    # At t=0: tab 0 fires every endpoint whose offset is exactly 0 →
    # all 7. Tabs 1+ are staggered by TAB_STAGGER_S so they don't
    # pile onto t=0. Peak same-instant request count = 7.
    timeline = _build_event_timeline(60.0)  # first minute only
    from collections import Counter
    bucketed = Counter(round(ts, 3) for ts in timeline)
    peak_same_instant = max(bucketed.values())
    assert peak_same_instant <= PLAN_QUOTAS["free"].per_user.capacity, (
        f"{peak_same_instant} simultaneous requests exceeds bucket "
        f"capacity {PLAN_QUOTAS['free'].per_user.capacity} — 429 "
        f"would fire deterministically at peak"
    )

    # Rolling 60-second window check (bucket is 300 / 60s): no 60s
    # window in the first simulated minute exceeds 300. Easy case —
    # total first-minute requests are ~110 ≪ 300, but this guard
    # catches future schedule drift.
    assert len(timeline) <= PLAN_QUOTAS["free"].per_user.capacity, (
        f"{len(timeline)} requests in first minute exceeds 60s bucket "
        f"capacity {PLAN_QUOTAS['free'].per_user.capacity}"
    )


def test_soak_3tab_1hour_also_clean(monkeypatch):
    """Duration extension: the invariant "mean rate < refill ⇒ 0
    429s" is duration-independent. Verify by extending to 1 h. If
    this fails while the 30-min variant passes, the bucket is
    leaking state across refills — a regression in
    ``InMemoryLimiter._refill``."""
    allowed, denied, _ = _run_soak(60 * 60, monkeypatch)
    assert denied == 0, (
        f"1-hour soak saw {denied} 429s — bucket state leaking "
        f"across refills? allowed={allowed}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Defensive-cap sanity: pathological client still gets capped
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_per_user_still_defends_against_pathological_client(monkeypatch):
    """Counterpoint to the soak: if a runaway client / compromised
    credential pushes > 300 req/min sustained (refill 5 tok/s), the
    per-user bucket MUST 429 — that is the "defensive cap" meaning
    Phase 4-5 restored. This guards against a future rate-limit
    refactor that accidentally neutralises the cap for authenticated
    users."""
    limiter = InMemoryLimiter()
    quota = PLAN_QUOTAS["free"]
    budget = quota.per_user

    import backend.rate_limit as rl
    vclock = _VirtualClock(start=0.0)
    monkeypatch.setattr(rl.time, "time", vclock)

    # 400 req/min sustained = 6.67 req/sec; refill is 5/sec → bucket
    # drains at 1.67 tok/sec → empties in 300/1.67 ≈ 180 sec.
    # Run for 10 min = 600 sec so we're well past the empty point.
    denied = 0
    fired = 0
    for i in range(4000):  # 10 min at 400/min = 4000 requests
        vclock.t = i * (60.0 / 400.0)  # 0.15 sec spacing
        ok, _ = limiter.allow(
            "api:user:abuse-client",
            budget.capacity, budget.window_seconds,
        )
        fired += 1
        if not ok:
            denied += 1
    # After the bucket empties, every extra request over the refill
    # rate must be denied. Expect plenty of 429s.
    assert denied > 0, (
        f"per_user bucket did not 429 under 400 req/min sustained "
        f"(fired {fired}, denied {denied}) — defensive cap broken"
    )
    # The exact proportion isn't load-bearing — bucket math gives
    # ~1600 denies out of 4000 at 400/min vs 300/min refill, but
    # the floor guard is "at least some denies happened". The
    # precise arithmetic is covered by unit tests in
    # ``test_rate_limit_middleware.py::test_ip_rate_limit_triggers``.
