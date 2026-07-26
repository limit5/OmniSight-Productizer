"""OP-2731 P4: the freshness probe's stage must be a validated enum.

The first draft of this probe fixed a dead gate by making its cohort
requirements configurable — and configurability immediately reintroduced the
way to make the gate vacuous, because a digest-only member set would let an
incomplete backup pass as a complete cohort. These tests pin the fix: the stage
selects a hardcoded minimum member set that configuration cannot shrink, and
anything unrecognised FAILS rather than defaulting to something permissive.

Every test here asserts a REFUSAL. A freshness probe that cannot be made to
refuse is not a gate (the OP-2728 induced-failure lesson, applied to config).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = PROJECT_ROOT / "scripts" / "omnisight-backup-freshness.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


probe = _load("omnisight_backup_freshness", PROBE_PATH)


def test_stage_names_are_a_closed_set() -> None:
    assert set(probe.STAGES) == {"s1_payload_digest", "s2_encrypted", "s3_attested"}


@pytest.mark.parametrize("stage", sorted(probe.STAGES))
def test_every_stage_requires_at_least_payload_and_digest(stage: str) -> None:
    """The irreducible minimum. A payload with no digest is not restorable and
    a digest with no payload is not a backup."""
    assert {"payload", "digest"} <= probe.STAGES[stage]["members"]


def test_attestation_is_required_only_at_the_stage_that_produces_it() -> None:
    """Staging exists so the probe never demands an artefact nothing yet emits;
    requiring attestation before P1 ships would make the probe permanently red,
    which is how a check gets written to swallow its own condition."""
    assert "attestation" not in probe.STAGES["s1_payload_digest"]["members"]
    assert "attestation" not in probe.STAGES["s2_encrypted"]["members"]
    assert "attestation" in probe.STAGES["s3_attested"]["members"]


def test_unset_stage_refuses(monkeypatch) -> None:
    monkeypatch.delenv(probe.STAGE_ENV, raising=False)
    with pytest.raises(SystemExit) as exc:
        probe.resolve_stage()
    assert exc.value.code == 1


@pytest.mark.parametrize("bad", ["", "  ", "s1", "S1_PAYLOAD_DIGEST", "unknown", "s4"])
def test_unknown_stage_refuses(monkeypatch, bad: str) -> None:
    monkeypatch.setenv(probe.STAGE_ENV, bad)
    with pytest.raises(SystemExit) as exc:
        probe.resolve_stage()
    assert exc.value.code == 1


def test_a_shrunken_member_set_is_refused_at_runtime(monkeypatch) -> None:
    """Belt and braces: even if a future edit empties a stage's member set in
    source, resolve_stage() must refuse rather than pass everything."""
    monkeypatch.setitem(
        probe.STAGES, "s1_payload_digest",
        {"members": frozenset({"digest"}), "payload": probe.STAGES["s1_payload_digest"]["payload"]},
    )
    monkeypatch.setenv(probe.STAGE_ENV, "s1_payload_digest")
    with pytest.raises(SystemExit) as exc:
        probe.resolve_stage()
    assert exc.value.code == 1


def test_valid_stage_resolves(monkeypatch) -> None:
    monkeypatch.setenv(probe.STAGE_ENV, "s1_payload_digest")
    assert probe.resolve_stage()["members"] == frozenset({"payload", "digest"})


def test_payload_pattern_accepts_both_names_during_the_f1_cutover() -> None:
    """s2 must match encrypted AND unencrypted so the probe does not go red on
    the changeover night; s1 must NOT match the encrypted name, or the probe
    would silently accept F1 output before F1's stage tightening lands."""
    assert probe.STAGES["s2_encrypted"]["payload"].match("20260726T162119Z.dump.gz")
    assert probe.STAGES["s2_encrypted"]["payload"].match("20260726T162119Z.dump.gz.gpg")
    assert probe.STAGES["s1_payload_digest"]["payload"].match("20260726T162119Z.dump.gz")
    assert not probe.STAGES["s1_payload_digest"]["payload"].match("20260726T162119Z.dump.gz.gpg")
