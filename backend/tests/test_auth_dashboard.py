"""AS.5.2 — Unit tests for ``backend.security.auth_dashboard``.

Coverage shape (test families):

  * Family 1  — Six rule constants + ALL_DASHBOARD_RULES tuple.
  * Family 2  — Three severity literals + SEVERITIES set.
  * Family 3  — DEFAULT_THRESHOLDS shape + immutability + per-rule
                values + DEFAULT_RULE_SEVERITIES mapping.
  * Family 4  — empty_summary shape, knob-off banner.
  * Family 5  — summarise reducer: counters per event, per-vocabulary
                breakdowns, ratios, rate-is-None when denominator zero,
                ignore unknown actions, ignore typo'd vocab values.
  * Family 6  — Detector: login_fail_burst (per fp grouping, sliding
                window threshold).
  * Family 7  — Detector: bot_challenge_fail_spike (per form_path).
  * Family 8  — Detector: token_refresh_storm (per entity_id).
  * Family 9  — Detector: honeypot_triggered (bright-line, count >= 1).
  * Family 10 — Detector: oauth_revoke_relink_loop (alternating
                revoke + connect cycles).
  * Family 11 — Detector: distributed_login_fail (sliding distinct-IPs).
  * Family 12 — detect_suspicious_patterns dispatch + threshold override
                + enabled_rules subset + stable sort.
  * Family 13 — Async compute_dashboard: knob-off banner returns
                empty result + knob-on routes through fake fetch +
                truncated flag.
  * Family 14 — Module-global state: no IO at import, no mutable
                module-level container, MappingProxyType pinning.
  * Family 15 — __all__ export shape (every public symbol present,
                no stowaways).

Module-global state audit (per implement_phase_step.md SOP §1)
* All test data is local to test fns; no module-global mutable state.
* The async compute_dashboard test injects a fake conn (a tiny class
  with a ``fetch(sql, *params)`` method) — no real DB pool.
* The knob-off branch is deterministic by construction (monkeypatches
  ``is_enabled`` to return False) — no race possible.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pathlib
import re
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

import pytest

from backend.security import auth_dashboard as d
from backend.security import auth_event as ae


def _run(coro):
    """Sync wrapper for one-off async helpers."""
    return asyncio.run(coro)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Six rule constants + ALL_DASHBOARD_RULES tuple
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_rule_strings_exact() -> None:
    """The 6 rule strings are pinned by the AS.5.2 row.  Renaming any
    of these is a breaking change for the AS.7.x notification template
    and AS.6.4 dashboard widget."""
    assert d.RULE_LOGIN_FAIL_BURST == "login_fail_burst"
    assert d.RULE_BOT_CHALLENGE_FAIL_SPIKE == "bot_challenge_fail_spike"
    assert d.RULE_TOKEN_REFRESH_STORM == "token_refresh_storm"
    assert d.RULE_HONEYPOT_TRIGGERED == "honeypot_triggered"
    assert d.RULE_OAUTH_REVOKE_RELINK_LOOP == "oauth_revoke_relink_loop"
    assert d.RULE_DISTRIBUTED_LOGIN_FAIL == "distributed_login_fail"


def test_all_dashboard_rules_tuple_complete() -> None:
    assert d.ALL_DASHBOARD_RULES == (
        d.RULE_LOGIN_FAIL_BURST,
        d.RULE_BOT_CHALLENGE_FAIL_SPIKE,
        d.RULE_TOKEN_REFRESH_STORM,
        d.RULE_HONEYPOT_TRIGGERED,
        d.RULE_OAUTH_REVOKE_RELINK_LOOP,
        d.RULE_DISTRIBUTED_LOGIN_FAIL,
    )
    assert len(d.ALL_DASHBOARD_RULES) == 6
    assert len(set(d.ALL_DASHBOARD_RULES)) == 6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Three severity literals + SEVERITIES set
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_severity_strings_exact() -> None:
    assert d.SEVERITY_INFO == "info"
    assert d.SEVERITY_WARN == "warn"
    assert d.SEVERITY_CRITICAL == "critical"


def test_severities_set_complete_and_frozen() -> None:
    assert d.SEVERITIES == frozenset({"info", "warn", "critical"})
    assert isinstance(d.SEVERITIES, frozenset)


def test_default_rule_severities_total() -> None:
    """Every rule in ALL_DASHBOARD_RULES has a default severity entry —
    drift guard's own drift guard."""
    assert set(d.DEFAULT_RULE_SEVERITIES.keys()) == set(d.ALL_DASHBOARD_RULES)
    for rule, sev in d.DEFAULT_RULE_SEVERITIES.items():
        assert sev in d.SEVERITIES, f"rule {rule} severity {sev!r} not in SEVERITIES"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — DEFAULT_THRESHOLDS shape + immutability
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_default_thresholds_keys_match_rules() -> None:
    assert set(d.DEFAULT_THRESHOLDS.keys()) == set(d.ALL_DASHBOARD_RULES)


def test_default_thresholds_per_rule_shape() -> None:
    for rule, threshold in d.DEFAULT_THRESHOLDS.items():
        assert "count" in threshold, f"{rule} missing count"
        assert "window_s" in threshold, f"{rule} missing window_s"
        assert isinstance(threshold["count"], int)
        assert isinstance(threshold["window_s"], int)
        assert threshold["count"] > 0
        assert threshold["window_s"] > 0


def test_default_thresholds_immutability() -> None:
    """Both the outer mapping and each per-rule mapping are
    MappingProxyType — caller cannot mutate the shared default."""
    assert isinstance(d.DEFAULT_THRESHOLDS, MappingProxyType)
    with pytest.raises(TypeError):
        d.DEFAULT_THRESHOLDS["new_rule"] = MappingProxyType({})  # type: ignore[index]
    for rule in d.ALL_DASHBOARD_RULES:
        assert isinstance(d.DEFAULT_THRESHOLDS[rule], MappingProxyType)


def test_default_thresholds_specific_values() -> None:
    """Default values pinned by AS.5.2 spec — overrides come via
    thresholds= kwarg, not by mutating the shared default."""
    assert d.DEFAULT_THRESHOLDS[d.RULE_LOGIN_FAIL_BURST] == {"count": 10, "window_s": 60}
    assert d.DEFAULT_THRESHOLDS[d.RULE_BOT_CHALLENGE_FAIL_SPIKE] == {"count": 20, "window_s": 60}
    assert d.DEFAULT_THRESHOLDS[d.RULE_TOKEN_REFRESH_STORM] == {"count": 10, "window_s": 60}
    assert d.DEFAULT_THRESHOLDS[d.RULE_HONEYPOT_TRIGGERED] == {"count": 1, "window_s": 60}
    assert d.DEFAULT_THRESHOLDS[d.RULE_OAUTH_REVOKE_RELINK_LOOP] == {"count": 3, "window_s": 600}
    assert d.DEFAULT_THRESHOLDS[d.RULE_DISTRIBUTED_LOGIN_FAIL] == {"count": 5, "window_s": 300}


def test_limit_rows_default_pinned() -> None:
    assert d.LIMIT_ROWS_DEFAULT == 50_000


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — empty_summary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_empty_summary_shape() -> None:
    s = d.empty_summary("t-test", since=100.0, until=200.0)
    assert s.tenant_id == "t-test"
    assert s.since == 100.0
    assert s.until == 200.0
    assert s.total_events == 0
    assert s.login_success_count == 0
    assert s.login_fail_count == 0
    assert s.login_success_rate is None  # NOT 0.0 — rate-is-None contract
    assert s.bot_challenge_pass_rate is None
    assert dict(s.login_fail_reasons) == {}
    assert dict(s.auth_method_distribution) == {}


def test_empty_summary_distributions_immutable() -> None:
    """Even the empty distribution dicts are frozen so a caller cannot
    mutate the singleton."""
    s = d.empty_summary("t-test")
    assert isinstance(s.login_fail_reasons, MappingProxyType)
    with pytest.raises(TypeError):
        s.login_fail_reasons["x"] = 1  # type: ignore[index]


def test_dashboard_summary_is_frozen() -> None:
    """Frozen dataclass — accidental mutation raises FrozenInstanceError."""
    s = d.empty_summary("t-test")
    with pytest.raises(Exception):  # FrozenInstanceError
        s.total_events = 5  # type: ignore[misc]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — summarise reducer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _row(action: str, ts: float = 100.0, entity_id: str = "", **after: Any) -> dict[str, Any]:
    return {"action": action, "ts": ts, "entity_id": entity_id, "after": dict(after)}


def test_summarise_login_success_distribution() -> None:
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_SUCCESS, 1, "u-1", auth_method="password"),
        _row(ae.EVENT_AUTH_LOGIN_SUCCESS, 2, "u-2", auth_method="oauth"),
        _row(ae.EVENT_AUTH_LOGIN_SUCCESS, 3, "u-3", auth_method="passkey"),
    ]
    s = d.summarise(rows, tenant_id="t-test")
    assert s.login_success_count == 3
    assert s.login_fail_count == 0
    assert s.login_success_rate == 1.0
    assert dict(s.auth_method_distribution) == {
        "password": 1, "oauth": 1, "passkey": 1,
    }


def test_summarise_login_success_rate_calculation() -> None:
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_SUCCESS, 1, "u-1", auth_method="password"),
        _row(ae.EVENT_AUTH_LOGIN_FAIL, 2, "fp-1",
             attempted_user_fp="fp-1", fail_reason="bad_password",
             auth_method="password"),
        _row(ae.EVENT_AUTH_LOGIN_FAIL, 3, "fp-1",
             attempted_user_fp="fp-1", fail_reason="bad_password",
             auth_method="password"),
    ]
    s = d.summarise(rows, tenant_id="t-test")
    assert s.login_success_count == 1
    assert s.login_fail_count == 2
    # 1 / (1+2) = 0.333...
    assert abs(s.login_success_rate - (1.0 / 3.0)) < 1e-9
    assert dict(s.login_fail_reasons) == {"bad_password": 2}


def test_summarise_bot_challenge_pass_fail_rate() -> None:
    rows = [
        _row(ae.EVENT_AUTH_BOT_CHALLENGE_PASS, 1, "/login", kind="verified", form_path="/login"),
        _row(ae.EVENT_AUTH_BOT_CHALLENGE_PASS, 2, "/login", kind="bypass_apikey", form_path="/login"),
        _row(ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, 3, "/signup", reason="lowscore", form_path="/signup"),
        _row(ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, 4, "/signup", reason="honeypot", form_path="/signup"),
    ]
    s = d.summarise(rows, tenant_id="t-test")
    assert s.bot_challenge_pass_count == 2
    assert s.bot_challenge_fail_count == 2
    assert s.bot_challenge_pass_rate == 0.5
    assert dict(s.bot_challenge_pass_kinds) == {"verified": 1, "bypass_apikey": 1}
    assert dict(s.bot_challenge_fail_reasons) == {"lowscore": 1, "honeypot": 1}


def test_summarise_oauth_revoke_initiators() -> None:
    rows = [
        _row(ae.EVENT_AUTH_OAUTH_CONNECT, 1, "github:u-1"),
        _row(ae.EVENT_AUTH_OAUTH_REVOKE, 2, "github:u-1", initiator="user"),
        _row(ae.EVENT_AUTH_OAUTH_REVOKE, 3, "github:u-2", initiator="admin"),
        _row(ae.EVENT_AUTH_OAUTH_REVOKE, 4, "github:u-3", initiator="dsar"),
    ]
    s = d.summarise(rows, tenant_id="t-test")
    assert s.oauth_connect_count == 1
    assert s.oauth_revoke_count == 3
    assert dict(s.oauth_revoke_initiators) == {"user": 1, "admin": 1, "dsar": 1}


def test_summarise_token_refresh_outcomes() -> None:
    rows = [
        _row(ae.EVENT_AUTH_TOKEN_REFRESH, 1, "github:u-1", outcome="success"),
        _row(ae.EVENT_AUTH_TOKEN_REFRESH, 2, "github:u-1", outcome="success"),
        _row(ae.EVENT_AUTH_TOKEN_REFRESH, 3, "github:u-2", outcome="provider_error"),
        _row(ae.EVENT_AUTH_TOKEN_ROTATED, 4, "github:u-1", triggered_by="auto_refresh"),
    ]
    s = d.summarise(rows, tenant_id="t-test")
    assert s.token_refresh_count == 3
    assert s.token_rotated_count == 1
    assert dict(s.token_refresh_outcomes) == {"success": 2, "provider_error": 1}


def test_summarise_zero_denominator_returns_none_rate() -> None:
    """No login_success + no login_fail ⇒ rate is None (not 0.0).
    UX contract: None = "no data", 0.0 = "0 % of N".
    """
    rows = [_row(ae.EVENT_AUTH_OAUTH_CONNECT, 1, "github:u-1")]
    s = d.summarise(rows, tenant_id="t-test")
    assert s.login_success_rate is None
    assert s.bot_challenge_pass_rate is None


def test_summarise_skips_unknown_actions() -> None:
    """Other event families (oauth.*, bot_challenge.*, honeypot.*) MUST
    NOT pollute the rollup counts even if a sloppy caller passes them in.
    """
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_SUCCESS, 1, "u-1", auth_method="password"),
        _row("oauth.login_init", 2, "github"),         # forensic family
        _row("bot_challenge.pass", 3, "/x"),            # vendor family
        _row("honeypot.triggered", 4, "/y"),            # honeypot family
        _row("unknown.action", 5, "z"),                 # garbage
    ]
    s = d.summarise(rows, tenant_id="t-test")
    assert s.total_events == 1
    assert s.login_success_count == 1


def test_summarise_silently_drops_typo_vocab() -> None:
    """Unknown vocab values (someone typo'd 'passwordd') get dropped
    from the breakdown but the row still counts in the total."""
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_SUCCESS, 1, "u-1", auth_method="passwordd"),
        _row(ae.EVENT_AUTH_LOGIN_FAIL, 2, "fp-1", fail_reason="not_a_real_reason",
             auth_method="password", attempted_user_fp="fp-1"),
    ]
    s = d.summarise(rows, tenant_id="t-test")
    assert s.login_success_count == 1
    assert s.login_fail_count == 1
    assert dict(s.auth_method_distribution) == {}  # typo dropped
    assert dict(s.login_fail_reasons) == {}        # typo dropped


def test_summarise_propagates_since_until() -> None:
    s = d.summarise([], tenant_id="t-test", since=100.0, until=500.0)
    assert s.since == 100.0
    assert s.until == 500.0
    assert s.total_events == 0


def test_summarise_handles_malformed_rows_gracefully() -> None:
    """A row with no action / no after / no ts must not crash."""
    rows = [
        {},
        {"action": None},
        {"action": ae.EVENT_AUTH_LOGIN_SUCCESS},  # no after
        {"action": ae.EVENT_AUTH_LOGIN_SUCCESS, "after": "not a dict"},
        {"action": ae.EVENT_AUTH_LOGIN_SUCCESS, "after": {"auth_method": "password"}},
    ]
    s = d.summarise(rows, tenant_id="t-test")
    # Three of the five have valid `auth.login_success` action → 3 counted.
    assert s.login_success_count == 3
    # Only the last one had a valid auth_method.
    assert dict(s.auth_method_distribution) == {"password": 1}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — Detector: login_fail_burst
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_detect_login_fail_burst_fires_at_threshold() -> None:
    """10 fails on the same fp within 60 s default window → fire."""
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_FAIL, ts=float(i), entity_id="fp-x",
             attempted_user_fp="fp-x", fail_reason="bad_password",
             auth_method="password", ip_fp="ip-1")
        for i in range(10)
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    burst = [a for a in alerts if a.rule == d.RULE_LOGIN_FAIL_BURST]
    assert len(burst) == 1
    assert burst[0].severity == d.SEVERITY_WARN
    assert burst[0].evidence["attempted_user_fp"] == "fp-x"
    assert burst[0].evidence["fail_count"] == 10


def test_detect_login_fail_burst_below_threshold_silent() -> None:
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_FAIL, ts=float(i), entity_id="fp-x",
             attempted_user_fp="fp-x", fail_reason="bad_password",
             auth_method="password", ip_fp="ip-1")
        for i in range(9)  # one below the default
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    assert not [a for a in alerts if a.rule == d.RULE_LOGIN_FAIL_BURST]


def test_detect_login_fail_burst_window_respected() -> None:
    """10 fails spread across 5 minutes (300 s) — outside the default
    60-s window — must NOT fire."""
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_FAIL, ts=float(i * 30), entity_id="fp-x",
             attempted_user_fp="fp-x", fail_reason="bad_password",
             auth_method="password", ip_fp="ip-1")
        for i in range(10)  # 10 events, 30 s apart → spans 270 s
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    assert not [a for a in alerts if a.rule == d.RULE_LOGIN_FAIL_BURST]


def test_detect_login_fail_burst_per_fp() -> None:
    """Two distinct fps each at threshold → two alerts."""
    rows = []
    for fp in ("fp-A", "fp-B"):
        for i in range(10):
            rows.append(_row(
                ae.EVENT_AUTH_LOGIN_FAIL, ts=float(i), entity_id=fp,
                attempted_user_fp=fp, fail_reason="bad_password",
                auth_method="password", ip_fp="ip-1",
            ))
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    burst = [a for a in alerts if a.rule == d.RULE_LOGIN_FAIL_BURST]
    assert len(burst) == 2
    fps = sorted(a.evidence["attempted_user_fp"] for a in burst)
    assert fps == ["fp-A", "fp-B"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — Detector: bot_challenge_fail_spike
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_detect_bot_challenge_fail_spike_fires() -> None:
    rows = [
        _row(ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, ts=float(i), entity_id="/signup",
             reason="lowscore", form_path="/signup")
        for i in range(20)  # default threshold = 20
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    spikes = [a for a in alerts if a.rule == d.RULE_BOT_CHALLENGE_FAIL_SPIKE]
    assert len(spikes) == 1
    assert spikes[0].evidence["form_path"] == "/signup"
    assert spikes[0].evidence["fail_count"] == 20


def test_detect_bot_challenge_fail_spike_per_form() -> None:
    rows = []
    for form_path in ("/signup", "/contact"):
        for i in range(20):
            rows.append(_row(
                ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, ts=float(i), entity_id=form_path,
                reason="lowscore", form_path=form_path,
            ))
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    spikes = [a for a in alerts if a.rule == d.RULE_BOT_CHALLENGE_FAIL_SPIKE]
    assert len(spikes) == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — Detector: token_refresh_storm
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_detect_token_refresh_storm_fires() -> None:
    rows = [
        _row(ae.EVENT_AUTH_TOKEN_REFRESH, ts=float(i), entity_id="github:u-1",
             outcome="success", provider="github")
        for i in range(10)  # default threshold = 10
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    storms = [a for a in alerts if a.rule == d.RULE_TOKEN_REFRESH_STORM]
    assert len(storms) == 1
    assert storms[0].evidence["entity_id"] == "github:u-1"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 — Detector: honeypot_triggered (bright-line)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_detect_honeypot_triggered_fires_on_one() -> None:
    """Default threshold is 1 — one honeypot trip is enough."""
    rows = [
        _row(ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, ts=100, entity_id="/signup",
             reason=ae.BOT_CHALLENGE_FAIL_HONEYPOT, form_path="/signup"),
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    hps = [a for a in alerts if a.rule == d.RULE_HONEYPOT_TRIGGERED]
    assert len(hps) == 1
    assert hps[0].severity == d.SEVERITY_CRITICAL
    assert hps[0].evidence["form_path"] == "/signup"
    assert hps[0].evidence["trigger_count"] == 1


def test_detect_honeypot_does_not_fire_on_other_reasons() -> None:
    """A bot_challenge_fail with reason=lowscore must NOT fire honeypot."""
    rows = [
        _row(ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, ts=100, entity_id="/signup",
             reason="lowscore", form_path="/signup"),
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    assert not [a for a in alerts if a.rule == d.RULE_HONEYPOT_TRIGGERED]


def test_detect_honeypot_aggregates_per_form() -> None:
    """Three trips on /signup + two on /contact → two alerts (one per form),
    each with the row count in evidence."""
    rows = []
    for i in range(3):
        rows.append(_row(
            ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, ts=float(i), entity_id="/signup",
            reason=ae.BOT_CHALLENGE_FAIL_HONEYPOT, form_path="/signup",
        ))
    for i in range(2):
        rows.append(_row(
            ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, ts=float(i + 100), entity_id="/contact",
            reason=ae.BOT_CHALLENGE_FAIL_HONEYPOT, form_path="/contact",
        ))
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    hps = [a for a in alerts if a.rule == d.RULE_HONEYPOT_TRIGGERED]
    assert len(hps) == 2
    counts = {a.evidence["form_path"]: a.evidence["trigger_count"] for a in hps}
    assert counts == {"/signup": 3, "/contact": 2}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 10 — Detector: oauth_revoke_relink_loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_detect_revoke_relink_loop_fires_at_three_cycles() -> None:
    """3 revoke→connect cycles within 600 s → fire."""
    rows = []
    ts = 0.0
    for _ in range(3):
        rows.append(_row(ae.EVENT_AUTH_OAUTH_REVOKE, ts=ts, entity_id="github:u-1",
                         initiator="user"))
        ts += 10
        rows.append(_row(ae.EVENT_AUTH_OAUTH_CONNECT, ts=ts, entity_id="github:u-1"))
        ts += 10
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    loops = [a for a in alerts if a.rule == d.RULE_OAUTH_REVOKE_RELINK_LOOP]
    assert len(loops) == 1
    assert loops[0].evidence["entity_id"] == "github:u-1"
    assert loops[0].evidence["cycle_count"] == 3


def test_detect_revoke_relink_loop_silent_below_threshold() -> None:
    rows = [
        _row(ae.EVENT_AUTH_OAUTH_REVOKE, ts=0, entity_id="github:u-1", initiator="user"),
        _row(ae.EVENT_AUTH_OAUTH_CONNECT, ts=10, entity_id="github:u-1"),
        _row(ae.EVENT_AUTH_OAUTH_REVOKE, ts=20, entity_id="github:u-1", initiator="user"),
        _row(ae.EVENT_AUTH_OAUTH_CONNECT, ts=30, entity_id="github:u-1"),
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    assert not [a for a in alerts if a.rule == d.RULE_OAUTH_REVOKE_RELINK_LOOP]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 11 — Detector: distributed_login_fail
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_detect_distributed_login_fail_fires() -> None:
    """5 distinct IPs targeting one fp within 300 s → fire."""
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_FAIL, ts=float(i * 10), entity_id="fp-target",
             attempted_user_fp="fp-target", fail_reason="bad_password",
             auth_method="password", ip_fp=f"ip-{i}")
        for i in range(5)
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    dist = [a for a in alerts if a.rule == d.RULE_DISTRIBUTED_LOGIN_FAIL]
    assert len(dist) == 1
    assert dist[0].severity == d.SEVERITY_CRITICAL
    assert dist[0].evidence["distinct_ip_count"] == 5
    assert len(dist[0].evidence["ip_fps"]) == 5


def test_detect_distributed_login_fail_silent_when_same_ip() -> None:
    """Same fp, same IP, 10 attempts → distributed rule must NOT fire
    (login_fail_burst will fire but that's a different rule)."""
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_FAIL, ts=float(i), entity_id="fp-x",
             attempted_user_fp="fp-x", fail_reason="bad_password",
             auth_method="password", ip_fp="ip-same")
        for i in range(10)
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    assert not [a for a in alerts if a.rule == d.RULE_DISTRIBUTED_LOGIN_FAIL]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 12 — Detector dispatch: thresholds, enabled_rules, sort
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_detect_threshold_override_per_key_falls_back() -> None:
    """Override only `count` — `window_s` should fall back to default."""
    rows = [
        _row(ae.EVENT_AUTH_LOGIN_FAIL, ts=float(i), entity_id="fp-x",
             attempted_user_fp="fp-x", fail_reason="bad_password",
             auth_method="password", ip_fp="ip-1")
        for i in range(5)
    ]
    alerts = d.detect_suspicious_patterns(
        rows, tenant_id="t-test",
        thresholds={d.RULE_LOGIN_FAIL_BURST: {"count": 5}},  # only count, not window_s
    )
    burst = [a for a in alerts if a.rule == d.RULE_LOGIN_FAIL_BURST]
    assert len(burst) == 1
    assert burst[0].evidence["window_s"] == 60  # default preserved


def test_detect_enabled_rules_subset_skips_others() -> None:
    """Restrict to only login_fail_burst — even with honeypot rows,
    no honeypot alert."""
    rows = [
        _row(ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, ts=100, entity_id="/x",
             reason=ae.BOT_CHALLENGE_FAIL_HONEYPOT, form_path="/x"),
    ]
    alerts = d.detect_suspicious_patterns(
        rows, tenant_id="t-test",
        enabled_rules=[d.RULE_LOGIN_FAIL_BURST],
    )
    assert alerts == ()


def test_detect_unknown_rule_raises() -> None:
    with pytest.raises(ValueError, match="unknown dashboard rule"):
        d.detect_suspicious_patterns(
            [], tenant_id="t-test", enabled_rules=["nonexistent_rule"],
        )


def test_detect_alerts_stable_sort() -> None:
    """Two evaluations over byte-equal input produce byte-equal output —
    helps the AS.7.x notification de-dup logic."""
    rows = []
    for fp in ("fp-zzzz", "fp-aaaa"):
        for i in range(10):
            rows.append(_row(
                ae.EVENT_AUTH_LOGIN_FAIL, ts=float(i), entity_id=fp,
                attempted_user_fp=fp, fail_reason="bad_password",
                auth_method="password", ip_fp="ip-1",
            ))
    a1 = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    a2 = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    assert a1 == a2
    # Sort should be by (rule, subject) — fp-aaaa before fp-zzzz.
    burst = [a for a in a1 if a.rule == d.RULE_LOGIN_FAIL_BURST]
    assert burst[0].evidence["attempted_user_fp"] == "fp-aaaa"
    assert burst[1].evidence["attempted_user_fp"] == "fp-zzzz"


def test_alert_is_frozen() -> None:
    rows = [
        _row(ae.EVENT_AUTH_BOT_CHALLENGE_FAIL, ts=100, entity_id="/x",
             reason=ae.BOT_CHALLENGE_FAIL_HONEYPOT, form_path="/x"),
    ]
    alerts = d.detect_suspicious_patterns(rows, tenant_id="t-test")
    assert len(alerts) == 1
    with pytest.raises(Exception):  # FrozenInstanceError
        alerts[0].rule = "x"  # type: ignore[misc]
    assert isinstance(alerts[0].evidence, MappingProxyType)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 13 — Async compute_dashboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _FakeRecord(dict):
    """Mock asyncpg.Record — supports both .__getitem__ and dict access."""


class _FakeConn:
    """Tiny stand-in for an asyncpg connection.  Records the SQL +
    params it was called with and returns the canned `rows`.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *params: Any) -> list[_FakeRecord]:
        import json
        self.calls.append((sql, params))
        out: list[_FakeRecord] = []
        for r in self.rows:
            rec = _FakeRecord({
                "id": r.get("id", 0),
                "ts": r.get("ts", 0.0),
                "actor": r.get("actor", "system"),
                "action": r.get("action", ""),
                "entity_kind": r.get("entity_kind", ""),
                "entity_id": r.get("entity_id", ""),
                "before_json": json.dumps(r.get("before") or {}),
                "after_json": json.dumps(r.get("after") or {}),
            })
            out.append(rec)
        return out


def test_compute_dashboard_knob_off_returns_empty_no_db() -> None:
    """Knob-off ⇒ banner shape, no DB read.  AS.0.8 §6 contract."""
    fake = _FakeConn([])
    with patch.object(d, "is_enabled", return_value=False):
        result = _run(d.compute_dashboard("t-test", conn=fake))
    assert result.knob_off is True
    assert result.summary.total_events == 0
    assert result.alerts == ()
    assert result.row_count_observed == 0
    assert fake.calls == []  # NO DB READ


def test_compute_dashboard_knob_on_routes_through_fetch() -> None:
    """Knob-on ⇒ run the fetch → summarise + detect."""
    rows = [
        {"id": 1, "ts": 1.0, "actor": "u-1",
         "action": ae.EVENT_AUTH_LOGIN_SUCCESS, "entity_kind": "auth_session",
         "entity_id": "u-1", "after": {"auth_method": "password"}},
        {"id": 2, "ts": 2.0, "actor": "u-2",
         "action": ae.EVENT_AUTH_LOGIN_FAIL, "entity_kind": "auth_session",
         "entity_id": "fp-x",
         "after": {"attempted_user_fp": "fp-x", "fail_reason": "bad_password",
                   "auth_method": "password", "ip_fp": "ip-1"}},
    ]
    fake = _FakeConn(rows)
    with patch.object(d, "is_enabled", return_value=True):
        result = _run(d.compute_dashboard(
            "t-test", since=0.0, until=10.0, conn=fake,
        ))
    assert result.knob_off is False
    assert result.summary.login_success_count == 1
    assert result.summary.login_fail_count == 1
    assert result.row_count_observed == 2
    assert result.row_count_truncated is False
    # SQL hits the right table + filters action LIKE 'auth.%'
    assert len(fake.calls) == 1
    sql, params = fake.calls[0]
    assert "audit_log" in sql
    assert "action LIKE 'auth.%'" in sql
    assert params[0] == "t-test"
    assert 0.0 in params  # since
    assert 10.0 in params  # until


def test_compute_dashboard_truncated_flag() -> None:
    """row_count_observed == limit ⇒ truncated=True signal for the UI."""
    rows = [
        {"id": i, "ts": float(i), "actor": "u",
         "action": ae.EVENT_AUTH_LOGIN_SUCCESS, "entity_kind": "auth_session",
         "entity_id": f"u-{i}", "after": {"auth_method": "password"}}
        for i in range(5)
    ]
    fake = _FakeConn(rows)
    with patch.object(d, "is_enabled", return_value=True):
        result = _run(d.compute_dashboard("t-test", limit=5, conn=fake))
    assert result.row_count_observed == 5
    assert result.row_count_truncated is True


def test_compute_dashboard_enabled_rules_propagates() -> None:
    """compute_dashboard forwards the enabled_rules subset to the
    detector — only requested rules can fire."""
    rows = [
        {"id": i, "ts": float(i), "actor": "x",
         "action": ae.EVENT_AUTH_BOT_CHALLENGE_FAIL,
         "entity_kind": "auth_session", "entity_id": "/signup",
         "after": {"reason": ae.BOT_CHALLENGE_FAIL_HONEYPOT,
                   "form_path": "/signup"}}
        for i in range(3)
    ]
    fake = _FakeConn(rows)
    with patch.object(d, "is_enabled", return_value=True):
        result = _run(d.compute_dashboard(
            "t-test", conn=fake,
            enabled_rules=[d.RULE_LOGIN_FAIL_BURST],  # exclude honeypot
        ))
    assert result.alerts == ()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 14 — Module-global state audit (per SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_module_no_runtime_side_effects_on_import() -> None:
    """Re-importing the module must produce identical constants — no
    randomness, no env reads, no DB connections at import time."""
    m1 = importlib.reload(d)
    assert m1.ALL_DASHBOARD_RULES == d.ALL_DASHBOARD_RULES
    assert m1.DEFAULT_THRESHOLDS == d.DEFAULT_THRESHOLDS
    assert m1.LIMIT_ROWS_DEFAULT == d.LIMIT_ROWS_DEFAULT


def test_module_constants_are_immutable() -> None:
    """Frozen frozenset / MappingProxyType / tuple — no module-level
    mutable container two workers could disagree on."""
    assert isinstance(d.SEVERITIES, frozenset)
    assert isinstance(d.DEFAULT_THRESHOLDS, MappingProxyType)
    assert isinstance(d.DEFAULT_RULE_SEVERITIES, MappingProxyType)
    assert isinstance(d.ALL_DASHBOARD_RULES, tuple)


def test_no_module_level_mutable_dict_or_list() -> None:
    """Source-level grep — no top-level `= {}` / `= []` / `= set()`
    bare assignment that could be mutated by a worker.  Allow
    MappingProxyType, tuple, frozenset, dataclass, function, type,
    constant str/int/None.
    """
    src_path = pathlib.Path(d.__file__)
    src = src_path.read_text(encoding="utf-8")
    # Forbid bare top-level mutable containers (top-level lines starting
    # with NAME = { / NAME = [ / NAME = set(...).  Allow them inside
    # function bodies (4-space indent).
    bad = re.findall(r"^[A-Z_][A-Z_0-9]*\s*=\s*(\{|\[|set\()", src, re.MULTILINE)
    assert not bad, f"module-level mutable containers found: {bad}"


def test_no_random_imports() -> None:
    """AS.5.2 must not import :mod:`secrets` / :mod:`random` —
    deterministic by construction (sorting / counting / windowing only).
    """
    src = pathlib.Path(d.__file__).read_text(encoding="utf-8")
    assert "import secrets" not in src
    assert re.search(r"^\s*import\s+random\b", src, re.MULTILINE) is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 15 — __all__ export shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_all_exports_resolve() -> None:
    for name in d.__all__:
        assert hasattr(d, name), f"__all__ lists {name} but module has no such attr"


def test_no_stowaways_in_all() -> None:
    """No public-looking name (CapsCase / snake_case starting with a
    letter) is missing from __all__."""
    public_attrs = {
        n for n in dir(d)
        if not n.startswith("_") and n not in {"annotations", "logging"}
        and not inspect.ismodule(getattr(d, n))
    }
    # Some allowed sentinels we don't want to export externally — ALL
    # public symbols MUST be in __all__ otherwise they are accidental.
    missing = public_attrs - set(d.__all__)
    # Tolerate the typing imports that happen to leak through.
    tolerated = {"Counter", "MappingProxyType", "Iterable", "Mapping", "Optional",
                 "Any", "field", "dataclass", "logger"}
    real_missing = missing - tolerated
    assert not real_missing, (
        f"public symbols missing from __all__: {sorted(real_missing)}"
    )


def test_all_dashboard_rules_present_in_export_list() -> None:
    for rule_name in (
        "RULE_LOGIN_FAIL_BURST",
        "RULE_BOT_CHALLENGE_FAIL_SPIKE",
        "RULE_TOKEN_REFRESH_STORM",
        "RULE_HONEYPOT_TRIGGERED",
        "RULE_OAUTH_REVOKE_RELINK_LOOP",
        "RULE_DISTRIBUTED_LOGIN_FAIL",
    ):
        assert rule_name in d.__all__
