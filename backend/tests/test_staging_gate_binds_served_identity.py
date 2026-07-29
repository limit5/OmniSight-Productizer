"""OP-2738: the canary gate must bind the DEPLOYED identity, not the branch tip.

A canary record stamps the Gerrit revision it was run for, but probes whatever
image staging is actually pinned to. The record carries both. Matching on
``revision`` alone admits two failures:

  false-RED    the develop tip is a merge commit while the stamp is the merged
               patchset, so nothing matches and the gate is unsatisfiable for a
               reason nobody can read off the output.
  false-GREEN  on a fast-forward the stamped patchset EQUALS the tip, so a
               commit never deployed to staging satisfies the gate.

The false-GREEN is the dangerous one. ``promote_image_bundle`` is digest-bound
and would catch it, but ``auto_promote_develop_to_main.sh`` is a second consumer
that does not go through that check.

The false-GREEN test below is the one that matters: it constructs exactly the
state that used to pass and asserts it now does not.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHECKER = PROJECT_ROOT / "scripts" / "release_milestone_checker.py"


def _load():
    spec = importlib.util.spec_from_file_location("release_milestone_checker", CHECKER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["release_milestone_checker"] = mod
    spec.loader.exec_module(mod)
    return mod


rmc = _load()

TIP = "34672ccc9f1e4b7a8d2c5e6f0a1b2c3d4e5f6a7b"
SERVED_MATCHING = f"develop+20260722.sha{TIP[:8]}"
SERVED_OTHER = "develop+20260722.sha61fb362"


def _record(**over):
    base = {
        "status": "green",
        "revision": TIP,
        "bundle_id": SERVED_MATCHING,
        "backend_digest": "sha256:aaaa",
        "run_id": "r1",
    }
    base.update(over)
    return base


def test_false_green_is_refused() -> None:
    """THE decisive case. Stamped revision equals the develop tip -- which is what
    a fast-forward merge produces -- while staging actually serves a different,
    pinned image. This used to pass the gate."""
    result = rmc.check_latest_status(
        _record(bundle_id=SERVED_OTHER), gate="ci_canary", revision=TIP
    )
    assert not result.ok
    assert result.evidence["code"] == "staging_pinned_elsewhere"


def test_a_genuinely_corresponding_record_still_passes() -> None:
    """The fix must not make the gate unsatisfiable in the case it exists for."""
    result = rmc.check_latest_status(_record(), gate="ci_canary", revision=TIP)
    assert result.ok, result.evidence


def test_refusal_explains_itself_rather_than_reporting_a_bare_red() -> None:
    """An unexplained red on a gate that can never go green teaches people to
    override the gate, which is worse than the gate not existing."""
    result = rmc.check_latest_status(
        _record(bundle_id=SERVED_OTHER), gate="ci_canary", revision=TIP
    )
    ev = result.evidence
    assert ev["served_bundle_id"] == SERVED_OTHER
    assert ev["develop_tip"] == TIP
    assert "re-run the canary" in ev["detail"]
    assert "NOT a silent pass" in ev["detail"]


def test_a_non_green_record_is_still_refused_on_status() -> None:
    """The new check must not shadow the original one."""
    result = rmc.check_latest_status(
        _record(status="red"), gate="ci_canary", revision=TIP
    )
    assert not result.ok
    assert result.evidence["code"] == "status_not_green"


def test_missing_record_still_refused() -> None:
    result = rmc.check_latest_status(None, gate="ci_canary", revision=TIP)
    assert not result.ok
    assert result.evidence["code"] == "missing_status"


@pytest.mark.parametrize("served", ["", None])
def test_record_without_a_served_bundle_falls_through_to_status_checks(served) -> None:
    """Older records predate the bundle_id field. They must not be silently
    accepted by the new branch, but they also must not crash it -- they fall
    through to the status check, which is the pre-existing behaviour."""
    result = rmc.check_latest_status(
        _record(bundle_id=served, status="red"), gate="ci_canary", revision=TIP
    )
    assert not result.ok
    assert result.evidence["code"] == "status_not_green"
