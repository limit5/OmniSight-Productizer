"""BS.7.2 — install_intercept coaching card unit tests.

Scope: every BS.7.2 ``pep_gateway`` change in isolation:

* ``_format_size_bytes`` — human-readable size formatter (None / 0 / KB / MB
  / GB / negative / non-numeric).
* ``_build_install_coaching`` — install-specific 4-line card built from
  the ``arguments`` dict that ``backend.routers.installer.create_job``
  hands the gateway. Missing fields fall back to neutral copy without
  raising ``KeyError``.
* ``_build_coaching`` — dispatches to the install builder when
  ``tool == "install_entry"``, leaves every other tool on the existing
  R20-A path (rule_why_overrides + tool template).
* ``_propose_hold`` — ``source.category == "install_intercept"`` for
  install_entry HOLDs, ``"pep_tool_intercept"`` for everything else.
* ``evaluate`` end-to-end — install_entry HOLD propagates the install
  coaching card + category through the propose_fn injection point,
  matching what R20-A's ToastCenter SSE pipeline reads downstream.

Tests are isolated from PG / decision_engine — every HOLD path is
exercised through the ``propose_fn`` / ``wait_for_decision`` injection
hooks documented on ``pep_gateway.evaluate`` (same pattern as
``test_pep_gateway.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from backend import pep_gateway as pep


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fakes (mirror test_pep_gateway.py shape)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class _FakeProposal:
    id: str
    kind: str
    severity: Any
    title: str
    detail: str
    options: list[dict]
    default_option_id: str | None
    source: dict[str, Any] = field(default_factory=dict)


def _make_propose_fn(outcomes: dict[str, str]):
    counter = {"n": 0}
    calls: list[_FakeProposal] = []

    def _fn(*, kind, title, detail="", options=None, default_option_id=None,
            severity=None, timeout_s=None, source=None):
        counter["n"] += 1
        pid = f"fake-dec-{counter['n']}"
        prop = _FakeProposal(
            id=pid, kind=kind, severity=severity, title=title, detail=detail,
            options=options or [], default_option_id=default_option_id,
            source=dict(source or {}),
        )
        calls.append(prop)
        outcomes.setdefault(pid, "approved")
        return prop

    return _fn, calls


def _waiter(outcomes: dict[str, str]):
    async def _wait(decision_id: str, timeout_s: float) -> str:
        return outcomes.get(decision_id, "rejected")
    return _wait


@pytest.fixture(autouse=True)
def _reset_pep():
    pep._reset_for_tests()
    yield
    pep._reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _format_size_bytes — human-readable sizes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFormatSizeBytes:

    def test_none_returns_unknown(self):
        assert pep._format_size_bytes(None) == "size unknown"

    def test_zero_bytes_renders_as_bytes(self):
        assert pep._format_size_bytes(0) == "0 B"

    def test_under_1024_renders_as_bytes(self):
        assert pep._format_size_bytes(512) == "512 B"

    def test_1_kib_renders_as_kb_with_one_decimal(self):
        # 1024 bytes = 1.0 KB; format keeps 1 decimal under 100.
        assert pep._format_size_bytes(1024) == "~1.0 KB"

    def test_1_mib_renders_as_mb(self):
        assert pep._format_size_bytes(1024 * 1024) == "~1.0 MB"

    def test_1_gib_renders_as_gb(self):
        assert pep._format_size_bytes(1024 ** 3) == "~1.0 GB"

    def test_under_100_keeps_one_decimal(self):
        # 1.5 GiB → "~1.5 GB"
        assert pep._format_size_bytes(int(1.5 * 1024 ** 3)) == "~1.5 GB"

    def test_over_100_drops_decimals(self):
        # 250 GiB → "~250 GB" (no decimal — large numbers read clearer
        # without ".0").
        assert pep._format_size_bytes(250 * 1024 ** 3) == "~250 GB"

    def test_huge_size_falls_back_to_tb(self):
        # 5 TiB still under PB cap — should land in TB unit.
        out = pep._format_size_bytes(5 * 1024 ** 4)
        assert out.endswith("TB")
        assert "5" in out

    def test_negative_returns_unknown(self):
        assert pep._format_size_bytes(-1) == "size unknown"

    def test_non_numeric_string_returns_unknown(self):
        assert pep._format_size_bytes("not a number") == "size unknown"

    def test_numeric_string_is_coerced(self):
        # PG bigint sometimes round-trips as str — be permissive.
        assert pep._format_size_bytes("1024") == "~1.0 KB"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _build_install_coaching — install-specific 4-line card
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildInstallCoaching:

    def _full_args(self) -> dict[str, Any]:
        return {
            "entry_id": "nxp-mcuxpresso-imxrt1170",
            "display_name": "NXP MCUXpresso for i.MX RT1170",
            "version": "12.1.0",
            "install_method": "docker_pull",
            "size_bytes": int(2.5 * 1024 ** 3),  # 2.5 GiB
            "vendor": "NXP",
            "family": "embedded",
        }

    def test_full_args_renders_all_four_keys(self):
        out = pep._build_install_coaching(self._full_args())
        assert set(out.keys()) == {"what", "why", "if_approve", "if_reject"}

    def test_what_includes_display_name_and_version(self):
        out = pep._build_install_coaching(self._full_args())
        assert "NXP MCUXpresso for i.MX RT1170" in out["what"]
        assert "12.1.0" in out["what"]

    def test_why_includes_destructive_and_method_human_and_size(self):
        out = pep._build_install_coaching(self._full_args())
        assert "destructive" in out["why"]
        assert "Docker image" in out["why"]  # docker_pull human label
        assert "GB" in out["why"]  # 2.5 GiB → "~2.5 GB"

    def test_if_approve_mentions_sidecar_and_progress_eta(self):
        out = pep._build_install_coaching(self._full_args())
        approve = out["if_approve"]
        assert "sidecar" in approve.lower()
        assert "progress" in approve.lower() or "eta" in approve.lower()

    def test_if_reject_mentions_nothing_happens_and_queue_clears(self):
        out = pep._build_install_coaching(self._full_args())
        reject = out["if_reject"]
        # "Nothing happens on the host." + "queue clears immediately"
        assert "nothing happens" in reject.lower()
        assert "queue clears" in reject.lower()

    @pytest.mark.parametrize("method,human_marker", [
        ("docker_pull", "Docker image"),
        ("shell_script", "shell script"),
        ("vendor_installer", "native installer"),
        ("noop", "no-op"),
    ])
    def test_install_method_humanised(self, method, human_marker):
        args = self._full_args()
        args["install_method"] = method
        out = pep._build_install_coaching(args)
        assert human_marker in out["why"]

    def test_unknown_install_method_falls_back_to_repr(self):
        args = self._full_args()
        args["install_method"] = "future_method_v9"
        out = pep._build_install_coaching(args)
        assert "future_method_v9" in out["why"]

    def test_missing_display_name_falls_back_to_entry_id(self):
        args = self._full_args()
        args.pop("display_name")
        out = pep._build_install_coaching(args)
        assert "nxp-mcuxpresso-imxrt1170" in out["what"]

    def test_missing_display_name_and_entry_id_uses_neutral_label(self):
        out = pep._build_install_coaching({
            "version": "1.0.0",
            "install_method": "docker_pull",
        })
        # Must not crash — fall back to a neutral label.
        assert "this catalog entry" in out["what"]

    def test_missing_version_uses_unknown_marker(self):
        args = self._full_args()
        args.pop("version")
        out = pep._build_install_coaching(args)
        assert "version unknown" in out["what"]

    def test_missing_size_bytes_yields_size_unknown(self):
        args = self._full_args()
        args.pop("size_bytes")
        out = pep._build_install_coaching(args)
        assert "size unknown" in out["why"]

    def test_none_arguments_does_not_raise(self):
        # Safety net: gateway should never crash on a malformed caller.
        out = pep._build_install_coaching(None)
        assert set(out.keys()) == {"what", "why", "if_approve", "if_reject"}
        assert "this catalog entry" in out["what"]

    def test_empty_arguments_does_not_raise(self):
        out = pep._build_install_coaching({})
        assert set(out.keys()) == {"what", "why", "if_approve", "if_reject"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _build_coaching dispatch — install_entry vs other tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildCoachingDispatch:

    def test_install_entry_routes_to_install_builder(self):
        out = pep._build_coaching(
            tool="install_entry",
            rule="tier_unlisted",
            impact_scope="local",
            command="install_entry entry_id=foo install_method=docker_pull",
            arguments={
                "display_name": "Foo Toolchain",
                "version": "9.9.9",
                "install_method": "docker_pull",
                "size_bytes": 100 * 1024 * 1024,
            },
        )
        assert "Foo Toolchain" in out["what"]
        assert "9.9.9" in out["what"]
        assert "Docker image" in out["why"]
        assert "MB" in out["why"]

    def test_non_install_entry_tool_uses_existing_template(self):
        # run_bash + tier_unlisted is the canonical R20-A path; must not
        # have shifted under us.
        out = pep._build_coaching(
            tool="run_bash",
            rule="tier_unlisted",
            impact_scope="local",
            command="ls -la",
        )
        assert set(out.keys()) == {"what", "why", "if_approve", "if_reject"}
        assert "shell" in out["what"].lower()

    def test_non_install_entry_rule_override_still_applies(self):
        # When the prod-scope rule fires the rule-specific "why" replaces
        # the generic tool default.
        out = pep._build_coaching(
            tool="run_bash",
            rule="terraform_apply",
            impact_scope="prod",
            command="terraform apply",
        )
        assert "infrastructure" in out["why"].lower() or "terraform" in out["why"].lower()

    def test_install_entry_ignores_rule_override(self):
        # install_entry coaching is content-driven (entry name / size /
        # method) — the rule-name override table doesn't apply here.
        # Pass a rule that *would* override on run_bash and verify the
        # install coaching wins.
        out = pep._build_coaching(
            tool="install_entry",
            rule="terraform_apply",  # would override on run_bash
            impact_scope="prod",
            command="install_entry entry_id=foo",
            arguments={
                "display_name": "Foo Toolchain",
                "version": "1.0.0",
                "install_method": "docker_pull",
                "size_bytes": 1024,
            },
        )
        assert "Foo Toolchain" in out["what"]
        # The terraform-specific override copy must NOT appear here.
        assert "terraform" not in out["why"].lower()

    def test_install_entry_with_no_arguments_still_renders(self):
        out = pep._build_coaching(
            tool="install_entry",
            rule="tier_unlisted",
            impact_scope="local",
            command="install_entry",
            arguments=None,
        )
        assert set(out.keys()) == {"what", "why", "if_approve", "if_reject"}
        assert "this catalog entry" in out["what"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _propose_hold — source.category dispatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestProposeHoldCategory:

    @pytest.mark.asyncio
    async def test_install_entry_sets_install_intercept_category(self):
        outcomes: dict[str, str] = {}
        propose_fn, calls = _make_propose_fn(outcomes)
        outcomes["fake-dec-1"] = "rejected"  # short-circuit waiter
        await pep.evaluate(
            tool="install_entry",
            arguments={
                "entry_id": "nodejs-lts-20",
                "display_name": "Node.js LTS",
                "version": "20.11.1",
                "install_method": "docker_pull",
                "size_bytes": 130 * 1024 * 1024,
            },
            agent_id="operator:alice@example.com",
            tier="t1",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        assert len(calls) == 1
        assert calls[0].source["category"] == pep.INSTALL_INTERCEPT_CATEGORY

    @pytest.mark.asyncio
    async def test_non_install_entry_keeps_pep_tool_intercept_category(self):
        outcomes: dict[str, str] = {"fake-dec-1": "rejected"}
        propose_fn, calls = _make_propose_fn(outcomes)
        await pep.evaluate(
            tool="run_bash",
            arguments={"command": "kubectl --context production apply -f x.yaml"},
            agent_id="a1", tier="t3",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        assert len(calls) == 1
        assert calls[0].source["category"] == pep.DEFAULT_PEP_INTERCEPT_CATEGORY

    @pytest.mark.asyncio
    async def test_install_intercept_constants_are_distinct(self):
        # Future-proofing: the two category strings must never collide
        # — the ToastCenter switches rendering on equality.
        assert pep.INSTALL_INTERCEPT_CATEGORY != pep.DEFAULT_PEP_INTERCEPT_CATEGORY


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  evaluate end-to-end — install_intercept coaching propagation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEvaluateInstallIntercept:

    @pytest.mark.asyncio
    async def test_install_entry_hold_carries_install_coaching_through_propose_fn(self):
        outcomes: dict[str, str] = {"fake-dec-1": "approved"}
        propose_fn, calls = _make_propose_fn(outcomes)
        out = await pep.evaluate(
            tool="install_entry",
            arguments={
                "entry_id": "zephyr-rtos-3-7",
                "display_name": "Zephyr RTOS",
                "version": "3.7.0",
                "install_method": "shell_script",
                "size_bytes": 850 * 1024 * 1024,
            },
            agent_id="operator:bob@example.com",
            tier="t1",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        assert out.action is pep.PepAction.auto_allow  # operator approved
        assert out.rule == "tier_unlisted"
        assert len(calls) == 1
        coaching = calls[0].source["coaching"]
        # 4 keys, content-aware
        assert set(coaching.keys()) == {"what", "why", "if_approve", "if_reject"}
        assert "Zephyr RTOS" in coaching["what"]
        assert "3.7.0" in coaching["what"]
        assert "shell script" in coaching["why"]
        assert "MB" in coaching["why"]
        assert "sidecar" in coaching["if_approve"].lower()
        assert "nothing happens" in coaching["if_reject"].lower()
        # Category is install_intercept (BS.7.2 contract).
        assert calls[0].source["category"] == pep.INSTALL_INTERCEPT_CATEGORY

    @pytest.mark.asyncio
    async def test_install_entry_reject_flips_to_deny(self):
        outcomes: dict[str, str] = {"fake-dec-1": "rejected"}
        propose_fn, _calls = _make_propose_fn(outcomes)
        out = await pep.evaluate(
            tool="install_entry",
            arguments={
                "entry_id": "nxp-foo",
                "display_name": "NXP Foo",
                "version": "1.0.0",
                "install_method": "docker_pull",
                "size_bytes": 50 * 1024 * 1024,
            },
            agent_id="operator:carol@example.com", tier="t1",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        assert out.action is pep.PepAction.deny
        assert "operator rejected" in out.reason

    @pytest.mark.asyncio
    async def test_install_entry_propose_fn_receives_arguments_indirectly(self):
        # The arguments dict isn't surfaced raw to propose_fn — but the
        # coaching card derived from it is. Verify the round-trip is
        # lossless for the four fields the UI cares about.
        outcomes: dict[str, str] = {"fake-dec-1": "approved"}
        propose_fn, calls = _make_propose_fn(outcomes)
        await pep.evaluate(
            tool="install_entry",
            arguments={
                "entry_id": "tooling-suite",
                "display_name": "Tooling Suite",
                "version": "2.0.0-rc.4",
                "install_method": "vendor_installer",
                "size_bytes": 5 * 1024 ** 3,
            },
            agent_id="o1", tier="t1",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        coaching = calls[0].source["coaching"]
        assert "Tooling Suite" in coaching["what"]
        assert "2.0.0-rc.4" in coaching["what"]
        assert "native installer" in coaching["why"]
        # 5 GiB rounds to "~5.0 GB"
        assert "5.0 GB" in coaching["why"]

    @pytest.mark.asyncio
    async def test_install_entry_hold_uses_risky_severity_not_destructive(self):
        # impact_scope is "local" for tier_unlisted (not "prod"), so
        # severity stays "risky" — destructive is reserved for prod
        # rules. Operator doesn't see install holds escalate to red.
        from backend import decision_engine as de
        outcomes: dict[str, str] = {"fake-dec-1": "approved"}
        propose_fn, calls = _make_propose_fn(outcomes)
        await pep.evaluate(
            tool="install_entry",
            arguments={
                "entry_id": "x",
                "display_name": "X",
                "version": "1.0",
                "install_method": "docker_pull",
                "size_bytes": 1024,
            },
            agent_id="o1", tier="t1",
            propose_fn=propose_fn,
            wait_for_decision=_waiter(outcomes),
            hold_timeout_s=1.0,
        )
        assert calls[0].severity == de.DecisionSeverity.risky
