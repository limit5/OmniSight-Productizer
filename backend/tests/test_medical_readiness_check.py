"""OP-2414 medical readiness gate tests."""
from __future__ import annotations

from types import SimpleNamespace

from scripts import medical_readiness_check as mrc


def test_non_medical_ticket_does_not_require_gate() -> None:
    result = mrc.check_medical_readiness(
        labels=("area:backend",),
        summary="regular backend ticket",
    )

    assert result.passed is True
    assert result.required is False


def test_medical_signal_requires_regulated_medical_and_clearance_labels() -> None:
    result = mrc.check_medical_readiness(
        labels=(mrc.REGULATED_LANE_LABEL,),
        run_negative_leak=False,
    )

    assert result.passed is False
    assert result.required is True
    assert result.reasons == (
        "missing required JIRA label 'regulated:medical'",
        "missing human clearance label 'regulatory-cleared'",
    )


def test_medical_ticket_runs_negative_leak_command() -> None:
    calls: list[tuple] = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = mrc.check_medical_readiness(
        labels=(mrc.REGULATED_MEDICAL_LABEL, mrc.REGULATORY_CLEARED_LABEL),
        command=("pytest", "negative-leak"),
        runner=fake_run,
    )

    assert result.passed is True
    assert result.required is True
    assert result.negative_leak_command == ("pytest", "negative-leak")
    assert calls[0][0][0] == ("pytest", "negative-leak")
    assert calls[0][1]["timeout"] == 300


def test_negative_leak_failure_blocks_medical_ticket() -> None:
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="leak failed")

    result = mrc.check_medical_readiness(
        labels=(mrc.REGULATED_MEDICAL_LABEL, mrc.REGULATORY_CLEARED_LABEL),
        command=("pytest", "negative-leak"),
        runner=fake_run,
    )

    assert result.passed is False
    assert result.reasons == ("negative-leak test failed rc=1: leak failed",)
