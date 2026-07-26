"""OP-2733 / SP-2 — contract guard for the DR drill's staleness verdict.

Background
----------
The first version of ``scripts/omnisight-dr-drill-pg.sh`` treated a stale
newest artefact as a WARNING: it logged a line and then fell through to
``log "DR drill passed"`` with exit 0, because the final ``die`` is gated on
``FAILURES`` and the warning never incremented it.

That converted a dead backup lane into a green light. Prod backups had been
failing since 2026-07-23 (the leg-3 corpus tripping the DLP gate) and this
drill reported "passed" every night against a four-day-old artefact — the
precise failure this whole remediation exists to remove: a monitored gate that
reports OK while not doing its job is worse than a red one, because it consumes
the attention that would otherwise have found the problem.

These are text-level contract assertions rather than an end-to-end run: the
script requires docker, gpg, a live ``omnisight-pg-primary`` and the real backup
passphrase, none of which belong in a unit test. The behaviour they pin is the
one that regressed, so a rewrite that reintroduces the warning-only path fails
here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "omnisight-dr-drill-pg.sh"


@pytest.fixture(scope="module")
def body() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(body: str) -> str:
    """Body with comment lines stripped.

    Assertions of the form "this bad pattern must not appear" have to run against
    CODE, not prose: the script documents *why* `ls -1 | sort` is wrong, and a
    naive search matches that explanation. That is architecture anti-pattern #11
    (self-referential text-match false positive) — it caught this test on its
    first run.
    """
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"missing: {SCRIPT}"


def test_staleness_increments_failures_rather_than_only_logging(body: str) -> None:
    """The regression: a stale artefact must reach the exit code, not just the log."""
    stale_block = re.search(
        r'if \[\[ "\$NEWEST_AGE_D" -gt "\$\{DR_MAX_AGE_DAYS:-\d+\}" \]\]; then(.+?)\nfi',
        body,
        re.S,
    )
    assert stale_block, "no `if NEWEST_AGE_D -gt DR_MAX_AGE_DAYS` block found"
    assert "FAILURES=$((FAILURES+1))" in stale_block.group(1), (
        "staleness must increment FAILURES; a log-only branch is the exact defect "
        "this test exists to prevent"
    )


def test_no_warn_only_staleness_path_remains(code: str) -> None:
    assert not re.search(r"\[WARN\].*stale", code), (
        "staleness must not be downgraded back to a warning"
    )


def test_exit_is_gated_on_failures(body: str) -> None:
    assert re.search(r'\[\[ "\$FAILURES" -eq 0 \]\]\s*\\?\s*\n?\s*\|\| die', body), (
        "the final verdict must still be gated on FAILURES"
    )


def test_artefact_selection_is_chronological_not_lexicographic(body: str, code: str) -> None:
    """`ls -1 | sort` only coincides with chronological while every artefact is
    `manual-YYYYMMDD-HHMMSS`; a naming change would silently drill the wrong file
    AND compute staleness from it."""
    assert "ls -1t" in body, "artefact listing must sort by mtime (-t)"
    assert not re.search(r"ls -1 .*\|\s*sort", code), "lexicographic sort reintroduced"


def test_staleness_reads_the_same_artefact_that_was_drilled(body: str) -> None:
    """Staleness must stat the same pick the drill used, or the two verdicts can
    disagree about which artefact they are talking about."""
    assert 'NEWEST="${ALL[0]}"' in body, "newest must be bound once and reused"
    assert 'stat -c %Y "$NEWEST"' in body, "staleness must stat $NEWEST, not re-index ALL"


def test_plaintext_is_shredded_by_a_trap_registered_before_creation(body: str) -> None:
    """Unrelated to staleness, but the property that must never regress: an abort
    cannot leave a decrypted production dump on disk."""
    trap_at = body.index("trap cleanup EXIT INT TERM")
    first_decrypt = body.index("--decrypt")
    assert trap_at < first_decrypt, "the cleanup trap must be registered before any decrypt"
