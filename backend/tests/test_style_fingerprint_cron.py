"""RPG.W3.3 -- contract tests for ``backend/agents/style_fingerprint_cron.py``.

The W3.3 daily cron is a thin orchestrator around the W3.2 pure
fingerprint helper plus the W1 Character Card store. These tests
cover the contract advertised in the module docstring:

1. ``style_drift_ratio`` returns ``0.0`` for an empty or fully
   uniform window, approaches ``1.0`` as the window splits across
   distinct canonical tuples.
2. ``recompute_style_fingerprints`` updates the card when the
   fingerprint changes and leaves it alone when the fingerprint
   matches.
3. Drift above the threshold lifts the per-card log line to
   ``WARNING``; drift below remains ``INFO``.
4. ``last_n`` clamps the window so older tasks do not influence
   the recomputed fingerprint.
5. ``drift_threshold`` is validated; out-of-range values raise
   before any IO.
6. ``summarize_drift`` aggregates per-card reports for dashboards.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from backend.agents.character_card import (
    CharacterCard,
    CharacterCardUpdate,
)
from backend.agents.style_fingerprint import (
    TaskStyleSignals,
    compute_style_fingerprint,
)
from backend.agents.style_fingerprint_cron import (
    DEFAULT_STYLE_DRIFT_THRESHOLD,
    StyleDriftReport,
    recompute_style_fingerprints,
    style_drift_ratio,
    summarize_drift,
)


T0 = datetime(2026, 5, 16, tzinfo=timezone.utc)


def _signals(commit: str, tests: str = "focused", refactor: str = "surgical") -> TaskStyleSignals:
    return TaskStyleSignals(
        commit_style=commit,
        test_pattern=tests,
        refactor_tendency=refactor,
    )


def _card(agent_id: str, fingerprint: str = "") -> CharacterCard:
    return CharacterCard(
        agent_id=agent_id,
        agent_class="api-anthropic",
        instance_suffix="alpha",
        guild="backend",
        level=1,
        xp=0,
        specialization_label="",
        style_fingerprint=fingerprint,
        created_at=T0,
    )


class _RecordingCardStore:
    """Minimal in-memory store satisfying ``CardListAndUpdateStore``."""

    def __init__(self, cards: Sequence[CharacterCard]) -> None:
        self._cards = {c.agent_id: c for c in cards}
        self.updates: list[tuple[str, CharacterCardUpdate]] = []

    async def list_cards(self) -> Sequence[CharacterCard]:
        return tuple(self._cards.values())

    async def update_card(
        self,
        agent_id: str,
        patch: CharacterCardUpdate,
    ) -> CharacterCard:
        self.updates.append((agent_id, patch))
        existing = self._cards[agent_id]
        if patch.style_fingerprint is not None:
            self._cards[agent_id] = replace(
                existing,
                style_fingerprint=patch.style_fingerprint.strip(),
            )
        return self._cards[agent_id]


def _provider_from(
    history_by_agent: dict[str, Sequence[TaskStyleSignals]],
) -> Any:
    async def provider(agent_id: str) -> Sequence[TaskStyleSignals]:
        return history_by_agent.get(agent_id, ())

    return provider


# ── style_drift_ratio ───────────────────────────────────────────────


def test_style_drift_ratio_empty_window_is_zero() -> None:
    assert style_drift_ratio(()) == 0.0


def test_style_drift_ratio_uniform_window_is_zero() -> None:
    samples = tuple(_signals("body trailers") for _ in range(5))
    assert style_drift_ratio(samples) == 0.0


def test_style_drift_ratio_one_outlier_in_five() -> None:
    samples = (
        _signals("body trailers"),
        _signals("body trailers"),
        _signals("body trailers"),
        _signals("body trailers"),
        _signals("inline subject"),
    )
    assert style_drift_ratio(samples) == pytest.approx(0.2)


def test_style_drift_ratio_half_split_hits_threshold() -> None:
    samples = (
        _signals("body trailers"),
        _signals("body trailers"),
        _signals("inline subject"),
        _signals("inline subject"),
    )
    # Two-way tie -> mode_count == 2 of 4 -> 1 - 0.5 = 0.5
    ratio = style_drift_ratio(samples)
    assert ratio == pytest.approx(0.5)
    assert ratio >= DEFAULT_STYLE_DRIFT_THRESHOLD


def test_style_drift_ratio_uses_canonical_normalization() -> None:
    # Identical canonical tuples after case + whitespace normalization.
    samples = (
        _signals("Body Trailers"),
        _signals("body   trailers"),
        _signals("body\ttrailers"),
    )
    assert style_drift_ratio(samples) == 0.0


# ── recompute_style_fingerprints ────────────────────────────────────


@pytest.mark.asyncio
async def test_recompute_updates_card_when_fingerprint_changes() -> None:
    card = _card("codex-alpha", fingerprint="stale-fingerprint")
    store = _RecordingCardStore([card])
    history = (
        _signals("body trailers"),
        _signals("body trailers"),
        _signals("body trailers"),
    )
    expected_fp = compute_style_fingerprint(history)
    provider = _provider_from({"codex-alpha": history})

    reports = await recompute_style_fingerprints(store, provider)

    assert len(reports) == 1
    report = reports[0]
    assert report.previous_fingerprint == "stale-fingerprint"
    assert report.current_fingerprint == expected_fp
    assert report.fingerprint_changed is True
    assert report.drift_ratio == 0.0
    assert report.above_threshold is False
    assert store.updates == [
        ("codex-alpha", CharacterCardUpdate(style_fingerprint=expected_fp)),
    ]


@pytest.mark.asyncio
async def test_recompute_skips_update_when_fingerprint_matches() -> None:
    history = (_signals("body trailers"),)
    expected_fp = compute_style_fingerprint(history)
    card = _card("codex-alpha", fingerprint=expected_fp)
    store = _RecordingCardStore([card])
    provider = _provider_from({"codex-alpha": history})

    reports = await recompute_style_fingerprints(store, provider)

    assert store.updates == []
    assert reports[0].fingerprint_changed is False
    assert reports[0].above_threshold is False


@pytest.mark.asyncio
async def test_recompute_clamps_window_to_last_n() -> None:
    older_tasks = tuple(_signals(f"old-{i}") for i in range(5))
    recent_tasks = (
        _signals("body trailers"),
        _signals("body trailers"),
    )
    history = older_tasks + recent_tasks
    # Caller asks for last_n=2 — only the two newest tasks should
    # contribute to the fingerprint.
    expected_fp = compute_style_fingerprint(recent_tasks, last_n=2)
    card = _card("codex-alpha")
    store = _RecordingCardStore([card])
    provider = _provider_from({"codex-alpha": history})

    reports = await recompute_style_fingerprints(
        store,
        provider,
        last_n=2,
    )

    assert reports[0].current_fingerprint == expected_fp
    assert reports[0].samples_considered == 2
    assert reports[0].window_size == 2


@pytest.mark.asyncio
async def test_drift_above_threshold_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    history = (
        _signals("body trailers"),
        _signals("body trailers"),
        _signals("inline subject"),
        _signals("inline subject"),
    )  # drift_ratio == 0.5
    card = _card("codex-alpha", fingerprint="stale")
    store = _RecordingCardStore([card])
    provider = _provider_from({"codex-alpha": history})

    caplog.set_level(logging.INFO, logger="rpg.style_fingerprint_cron")
    reports = await recompute_style_fingerprints(
        store,
        provider,
        drift_threshold=0.5,
    )

    assert reports[0].above_threshold is True
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "rpg.style_fingerprint_cron"
    ]
    assert len(warnings) == 1
    assert warnings[0].message == "style_fingerprint_drift_above_threshold"
    assert getattr(warnings[0], "agent_id", None) == "codex-alpha"


@pytest.mark.asyncio
async def test_drift_below_threshold_emits_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    history = (
        _signals("body trailers"),
        _signals("body trailers"),
        _signals("body trailers"),
        _signals("inline subject"),
    )  # drift_ratio == 0.25
    card = _card("codex-alpha", fingerprint="stale")
    store = _RecordingCardStore([card])
    provider = _provider_from({"codex-alpha": history})

    caplog.set_level(logging.INFO, logger="rpg.style_fingerprint_cron")
    reports = await recompute_style_fingerprints(
        store,
        provider,
        drift_threshold=0.5,
    )

    assert reports[0].fingerprint_changed is True
    assert reports[0].above_threshold is False
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "rpg.style_fingerprint_cron"
    ]
    assert warnings == []
    info_records = [
        r
        for r in caplog.records
        if r.message == "style_fingerprint_drift"
        and r.name == "rpg.style_fingerprint_cron"
    ]
    assert len(info_records) == 1


@pytest.mark.asyncio
async def test_unchanged_emits_info_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    history = (_signals("body trailers"),)
    expected_fp = compute_style_fingerprint(history)
    card = _card("codex-alpha", fingerprint=expected_fp)
    store = _RecordingCardStore([card])
    provider = _provider_from({"codex-alpha": history})

    caplog.set_level(logging.INFO, logger="rpg.style_fingerprint_cron")
    await recompute_style_fingerprints(store, provider)

    unchanged_records = [
        r
        for r in caplog.records
        if r.message == "style_fingerprint_unchanged"
        and r.name == "rpg.style_fingerprint_cron"
    ]
    assert len(unchanged_records) == 1


@pytest.mark.asyncio
async def test_recompute_handles_empty_history_as_no_fingerprint() -> None:
    card = _card("codex-alpha", fingerprint="")
    store = _RecordingCardStore([card])
    provider = _provider_from({"codex-alpha": ()})

    reports = await recompute_style_fingerprints(store, provider)

    # Empty history -> compute_style_fingerprint returns ""; stored
    # fingerprint already "" -> no update, no drift.
    assert reports[0].current_fingerprint == ""
    assert reports[0].fingerprint_changed is False
    assert reports[0].drift_ratio == 0.0
    assert store.updates == []


@pytest.mark.asyncio
async def test_recompute_walks_every_card() -> None:
    alpha_history = (_signals("body trailers"),)
    beta_history = (_signals("inline subject"),)
    cards = [_card("codex-alpha"), _card("codex-beta")]
    store = _RecordingCardStore(cards)
    provider = _provider_from(
        {
            "codex-alpha": alpha_history,
            "codex-beta": beta_history,
        }
    )

    reports = await recompute_style_fingerprints(store, provider)
    agent_ids = {r.agent_id for r in reports}
    assert agent_ids == {"codex-alpha", "codex-beta"}
    assert len(store.updates) == 2


# ── threshold validation ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_threshold", [-0.1, 1.5, 2])
async def test_recompute_rejects_out_of_range_threshold(bad_threshold: float) -> None:
    store = _RecordingCardStore([_card("codex-alpha")])
    provider = _provider_from({})
    with pytest.raises(ValueError):
        await recompute_style_fingerprints(
            store,
            provider,
            drift_threshold=bad_threshold,
        )


@pytest.mark.asyncio
async def test_recompute_rejects_non_numeric_threshold() -> None:
    store = _RecordingCardStore([_card("codex-alpha")])
    provider = _provider_from({})
    with pytest.raises(TypeError):
        await recompute_style_fingerprints(
            store,
            provider,
            drift_threshold="0.5",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_recompute_rejects_non_positive_window() -> None:
    store = _RecordingCardStore([_card("codex-alpha")])
    provider = _provider_from({})
    with pytest.raises(ValueError):
        await recompute_style_fingerprints(store, provider, last_n=0)


# ── summarize_drift ─────────────────────────────────────────────────


def test_summarize_drift_counts_buckets() -> None:
    reports = (
        StyleDriftReport(
            agent_id="a",
            previous_fingerprint="x",
            current_fingerprint="x",
            drift_ratio=0.0,
            samples_considered=5,
            window_size=20,
            fingerprint_changed=False,
            above_threshold=False,
        ),
        StyleDriftReport(
            agent_id="b",
            previous_fingerprint="x",
            current_fingerprint="y",
            drift_ratio=0.25,
            samples_considered=4,
            window_size=20,
            fingerprint_changed=True,
            above_threshold=False,
        ),
        StyleDriftReport(
            agent_id="c",
            previous_fingerprint="x",
            current_fingerprint="z",
            drift_ratio=0.75,
            samples_considered=4,
            window_size=20,
            fingerprint_changed=True,
            above_threshold=True,
        ),
    )
    assert summarize_drift(reports) == {
        "visited": 3,
        "unchanged": 1,
        "drifted": 2,
        "above_threshold": 1,
    }


def test_summarize_drift_empty_sweep() -> None:
    assert summarize_drift(()) == {
        "visited": 0,
        "unchanged": 0,
        "drifted": 0,
        "above_threshold": 0,
    }
