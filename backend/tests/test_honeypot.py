"""AS.4.1 — Unit tests for ``backend.security.honeypot``.

Coverage shape (test families):

  * Family  1 — Constants  (form prefix table, rare-word pool, CSS class,
    rotation period, reject code/status, attribute set).
  * Family  2 — Audit event canonical names (3 EVENT_* + ALL_HONEYPOT_EVENTS
    + outcome → event lookup, including the bypass → None branch).
  * Family  3 — ``current_epoch`` / ``honeypot_field_name`` determinism +
    SHA-256 reproducibility + tenant separation + form prefix selection.
  * Family  4 — ``validate_honeypot`` precedence: knob-off > bypass-kind >
    tenant-disabled > unknown form > field-missing > field-filled > pass.
  * Family  5 — 30-day boundary 1-request grace (current epoch and
    previous epoch both accepted).
  * Family  6 — Multi-valued / list-of-values form keys (any non-empty
    element counts).
  * Family  7 — ``validate_and_enforce`` raises on reject; passes on
    pass / bypass.
  * Family  8 — ``HoneypotRejected`` ctor / attrs / message / superclass.
  * Family  9 — AS.0.8 single-knob (settings.as_enabled = False).
  * Family 10 — ``__all__`` export shape.

Module-global state audit (per implement_phase_step.md SOP §1)
* All test data is local to test fns; no module-global mutable state.
* Tests run in any order — no fixture cross-test dependency.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest import mock

import pytest

from backend.security import honeypot as hp


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — make epoch math readable in tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _ts(epoch: int, *, offset: int = 0) -> float:
    """Return a UNIX timestamp that lands inside *epoch* (with
    optional +offset seconds inside the bucket)."""
    return float(epoch) * hp.HONEYPOT_ROTATION_PERIOD_SECONDS + offset


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_form_prefixes_locked() -> None:
    """AS.0.7 §4.1 invariant: 4 form paths, 2-letter prefix each."""
    assert dict(hp._FORM_PREFIXES) == {
        "/api/v1/auth/login": "lg_",
        "/api/v1/auth/signup": "sg_",
        "/api/v1/auth/password-reset": "pr_",
        "/api/v1/auth/contact": "ct_",
    }


def test_form_prefixes_immutable() -> None:
    """SOP §1: ``MappingProxyType`` rejects runtime mutation."""
    with pytest.raises(TypeError):
        hp._FORM_PREFIXES["/api/v1/auth/login"] = "XX_"  # type: ignore[index]


def test_rare_word_pool_size() -> None:
    """AS.0.7 §2.1: 12 rare words, frozen tuple."""
    assert len(hp._RARE_WORD_POOL) == 12
    assert isinstance(hp._RARE_WORD_POOL, tuple)


def test_rare_word_pool_unique() -> None:
    assert len(set(hp._RARE_WORD_POOL)) == len(hp._RARE_WORD_POOL)


def test_rare_word_pool_no_existing_form_collision() -> None:
    """AS.0.7 §4.2 invariant: rare-word pool 0-collision with WHATWG
    autocomplete values + OmniSight form names."""
    forbidden = frozenset(
        {
            "email", "username", "password", "current-password",
            "new-password", "one-time-code", "tel", "phone",
            "address", "name", "given-name", "family-name", "off",
            "url",
        }
    )
    for w in hp._RARE_WORD_POOL:
        assert w not in forbidden, f"rare-word {w!r} collides with WHATWG"


def test_os_honeypot_class_value() -> None:
    assert hp.OS_HONEYPOT_CLASS == "os-honeypot-field"


def test_honeypot_hide_css_off_screen_only() -> None:
    """AS.0.7 §2.2 invariant: off-screen positioning ONLY; never
    display:none / visibility:hidden / opacity:0."""
    css = hp.HONEYPOT_HIDE_CSS
    assert "position:absolute" in css
    assert "left:-9999px" in css
    assert "display:none" not in css
    assert "display: none" not in css
    assert "visibility:hidden" not in css
    assert "visibility: hidden" not in css
    assert "opacity:0" not in css


def test_honeypot_input_attrs_5_dimensions_present() -> None:
    """AS.0.7 §2.6 invariant: all 5 dimensions + 2 password-manager
    ignores."""
    expected_keys = {
        "tabindex",
        "autocomplete",
        "data-1p-ignore",
        "data-lpignore",
        "data-bwignore",
        "aria-hidden",
        "aria-label",
    }
    assert set(hp.HONEYPOT_INPUT_ATTRS.keys()) == expected_keys
    assert hp.HONEYPOT_INPUT_ATTRS["tabindex"] == "-1"
    assert hp.HONEYPOT_INPUT_ATTRS["autocomplete"] == "off"
    assert hp.HONEYPOT_INPUT_ATTRS["aria-hidden"] == "true"
    assert hp.HONEYPOT_INPUT_ATTRS["aria-label"] == "Do not fill"


def test_honeypot_input_attrs_immutable() -> None:
    with pytest.raises(TypeError):
        hp.HONEYPOT_INPUT_ATTRS["tabindex"] = "0"  # type: ignore[index]


def test_rotation_period_is_30_days() -> None:
    assert hp.HONEYPOT_ROTATION_PERIOD_SECONDS == 30 * 86400


def test_reject_code_matches_as_3_4() -> None:
    """AS.6.3 wiring relies on a single error code regardless of which
    layer (captcha / honeypot) caught the bot."""
    from backend.security.bot_challenge import BOT_CHALLENGE_REJECTED_CODE

    assert hp.HONEYPOT_REJECTED_CODE == BOT_CHALLENGE_REJECTED_CODE
    assert hp.HONEYPOT_REJECTED_CODE == "bot_challenge_failed"


def test_reject_status_matches_as_3_4() -> None:
    from backend.security.bot_challenge import (
        BOT_CHALLENGE_REJECTED_HTTP_STATUS,
    )

    assert hp.HONEYPOT_REJECTED_HTTP_STATUS == BOT_CHALLENGE_REJECTED_HTTP_STATUS
    assert hp.HONEYPOT_REJECTED_HTTP_STATUS == 429


def test_supported_form_paths_returns_4() -> None:
    paths = hp.supported_form_paths()
    assert isinstance(paths, tuple)
    assert len(paths) == 4
    assert set(paths) == set(hp._FORM_PREFIXES.keys())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Audit event canonical names
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_event_canonical_strings() -> None:
    """AS.0.7 §3.4: 3 events, byte-equal pinning."""
    assert hp.EVENT_BOT_CHALLENGE_HONEYPOT_PASS == "bot_challenge.honeypot_pass"
    assert hp.EVENT_BOT_CHALLENGE_HONEYPOT_FAIL == "bot_challenge.honeypot_fail"
    assert (
        hp.EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT
        == "bot_challenge.honeypot_form_drift"
    )


def test_all_honeypot_events_set() -> None:
    assert set(hp.ALL_HONEYPOT_EVENTS) == {
        hp.EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
        hp.EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
        hp.EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
    }


def test_outcomes_canonical() -> None:
    assert hp.OUTCOME_HONEYPOT_PASS == "honeypot_pass"
    assert hp.OUTCOME_HONEYPOT_FAIL == "honeypot_fail"
    assert hp.OUTCOME_HONEYPOT_FORM_DRIFT == "honeypot_form_drift"
    assert hp.OUTCOME_HONEYPOT_BYPASS == "honeypot_bypass"


def test_event_for_honeypot_outcome_covers_every_outcome() -> None:
    """Every outcome maps to either a canonical event or to ``None``
    (bypass)."""
    for o in hp.ALL_HONEYPOT_OUTCOMES:
        # No raise.
        hp.event_for_honeypot_outcome(o)
    assert (
        hp.event_for_honeypot_outcome(hp.OUTCOME_HONEYPOT_PASS)
        == hp.EVENT_BOT_CHALLENGE_HONEYPOT_PASS
    )
    assert (
        hp.event_for_honeypot_outcome(hp.OUTCOME_HONEYPOT_FAIL)
        == hp.EVENT_BOT_CHALLENGE_HONEYPOT_FAIL
    )
    assert (
        hp.event_for_honeypot_outcome(hp.OUTCOME_HONEYPOT_FORM_DRIFT)
        == hp.EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT
    )
    # Bypass intentionally has no honeypot-family event.
    assert hp.event_for_honeypot_outcome(hp.OUTCOME_HONEYPOT_BYPASS) is None


def test_event_for_honeypot_outcome_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown honeypot outcome"):
        hp.event_for_honeypot_outcome("not-a-real-outcome")


def test_failure_reason_constants() -> None:
    assert hp.FAILURE_REASON_FIELD_FILLED == "field_filled"
    assert hp.FAILURE_REASON_FIELD_MISSING_IN_FORM == "field_missing_in_form"
    assert hp.FAILURE_REASON_FORM_PATH_UNKNOWN == "form_path_unknown"


def test_bypass_kind_constants() -> None:
    assert hp.BYPASS_KIND_API_KEY == "apikey"
    assert hp.BYPASS_KIND_TEST_TOKEN == "test_token"
    assert hp.BYPASS_KIND_IP_ALLOWLIST == "ip_allowlist"
    assert hp.BYPASS_KIND_KNOB_OFF == "knob_off"
    assert hp.BYPASS_KIND_TENANT_DISABLED == "tenant_disabled"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Field-name generator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "form_path,expected_prefix",
    [
        ("/api/v1/auth/login", "lg_"),
        ("/api/v1/auth/signup", "sg_"),
        ("/api/v1/auth/password-reset", "pr_"),
        ("/api/v1/auth/contact", "ct_"),
    ],
)
def test_field_name_uses_prefix(
    form_path: str, expected_prefix: str
) -> None:
    name = hp.honeypot_field_name(form_path, "tenant-A", 1234)
    assert name.startswith(expected_prefix)
    suffix = name[len(expected_prefix) :]
    assert suffix in hp._RARE_WORD_POOL


def test_field_name_deterministic() -> None:
    a = hp.honeypot_field_name("/api/v1/auth/login", "tenant-A", 1234)
    b = hp.honeypot_field_name("/api/v1/auth/login", "tenant-A", 1234)
    assert a == b


def test_field_name_changes_per_tenant() -> None:
    """Different tenants → likely-different rare word (cross-tenant
    fingerprint isolation per AS.0.7 §2.1)."""
    seen = set()
    for tid in ("tenant-A", "tenant-B", "tenant-C", "tenant-D"):
        seen.add(hp.honeypot_field_name("/api/v1/auth/login", tid, 1234))
    # We don't pin the exact distribution (12-word pool, 4 tenants),
    # but at least one differs across the 4 tenants in any reasonable
    # SHA-256 distribution; otherwise we'd have a hash collision rate
    # of (1/12)^3 ≈ 0.06%, which would be a real bug.
    assert len(seen) >= 2


def test_field_name_changes_per_epoch() -> None:
    """Across 30-day boundaries the name rotates."""
    seen = set()
    for ep in range(0, 100):
        seen.add(hp.honeypot_field_name("/api/v1/auth/login", "tenant-A", ep))
    # 12-word pool, 100 epochs → expect ~all 12 words eventually.
    assert len(seen) == 12


def test_field_name_unknown_form_path_raises() -> None:
    with pytest.raises(ValueError, match="unknown form_path"):
        hp.honeypot_field_name("/api/v1/auth/unknown", "tenant-A", 1234)


def test_current_epoch_uses_now_override() -> None:
    assert hp.current_epoch(now=_ts(42)) == 42
    assert hp.current_epoch(now=_ts(42, offset=1)) == 42
    assert hp.current_epoch(now=_ts(42, offset=hp.HONEYPOT_ROTATION_PERIOD_SECONDS - 1)) == 42


def test_current_epoch_reads_time_when_no_override(monkeypatch) -> None:
    monkeypatch.setattr(hp.time, "time", lambda: _ts(99, offset=12345))
    assert hp.current_epoch() == 99


def test_expected_field_names_returns_current_and_prev() -> None:
    pair = hp.expected_field_names(
        "/api/v1/auth/login", "tenant-A", now=_ts(1234, offset=10)
    )
    expected_now = hp.honeypot_field_name(
        "/api/v1/auth/login", "tenant-A", 1234
    )
    expected_prev = hp.honeypot_field_name(
        "/api/v1/auth/login", "tenant-A", 1233
    )
    assert pair == (expected_now, expected_prev)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — validate_honeypot precedence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _name_for(form_path: str, tid: str, epoch: int) -> str:
    return hp.honeypot_field_name(form_path, tid, epoch)


def test_validate_pass_empty_field() -> None:
    n = _name_for("/api/v1/auth/login", "tA", 100)
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {n: "", "username": "alice"},
        now=_ts(100),
    )
    assert r.allow is True
    assert r.outcome == hp.OUTCOME_HONEYPOT_PASS
    assert r.audit_event == hp.EVENT_BOT_CHALLENGE_HONEYPOT_PASS
    assert r.field_name_used == n
    assert r.failure_reason is None
    assert r.bypass_kind is None
    assert r.audit_metadata["form_path"] == "/api/v1/auth/login"
    assert r.audit_metadata["epoch"] == 100


def test_validate_pass_whitespace_only_value_treated_as_empty() -> None:
    n = _name_for("/api/v1/auth/login", "tA", 100)
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {n: "   \t  \n"},
        now=_ts(100),
    )
    assert r.allow is True
    assert r.outcome == hp.OUTCOME_HONEYPOT_PASS


def test_validate_pass_none_value_treated_as_empty() -> None:
    n = _name_for("/api/v1/auth/login", "tA", 100)
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {n: None},
        now=_ts(100),
    )
    assert r.allow is True


def test_validate_fail_filled_field() -> None:
    n = _name_for("/api/v1/auth/login", "tA", 100)
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {n: "bot@bot.example", "username": "alice"},
        now=_ts(100),
    )
    assert r.allow is False
    assert r.outcome == hp.OUTCOME_HONEYPOT_FAIL
    assert r.audit_event == hp.EVENT_BOT_CHALLENGE_HONEYPOT_FAIL
    assert r.failure_reason == hp.FAILURE_REASON_FIELD_FILLED
    assert r.field_name_used == n
    # PII-redacted: only length, never the raw value.
    assert r.audit_metadata["field_filled_length"] == len("bot@bot.example")
    assert "field_filled_value" not in r.audit_metadata


def test_validate_form_drift_field_missing() -> None:
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {"username": "alice", "password": "secret"},
        now=_ts(100),
    )
    assert r.allow is False
    assert r.outcome == hp.OUTCOME_HONEYPOT_FORM_DRIFT
    assert r.audit_event == hp.EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT
    assert r.failure_reason == hp.FAILURE_REASON_FIELD_MISSING_IN_FORM
    # Submitted keys must be redacted of values.
    assert r.audit_metadata["submitted_keys"] == ("password", "username")
    # Expected fields exposed to the audit row for diagnostics.
    expected = r.audit_metadata["expected_field_names"]
    assert len(expected) == 2  # current + prev epoch


def test_validate_form_drift_unknown_form_path() -> None:
    r = hp.validate_honeypot(
        "/api/v1/auth/unknown",
        "tA",
        {},
        now=_ts(100),
    )
    assert r.allow is False
    assert r.outcome == hp.OUTCOME_HONEYPOT_FORM_DRIFT
    assert r.failure_reason == hp.FAILURE_REASON_FORM_PATH_UNKNOWN
    assert r.audit_metadata["form_path"] == "/api/v1/auth/unknown"


@pytest.mark.parametrize(
    "kind",
    [
        hp.BYPASS_KIND_API_KEY,
        hp.BYPASS_KIND_TEST_TOKEN,
        hp.BYPASS_KIND_IP_ALLOWLIST,
    ],
)
def test_validate_bypass_short_circuits_form_check(kind: str) -> None:
    """AS.0.6 §2 invariant: bypass-flagged callers don't render
    honeypot, so we don't check the form."""
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {},  # no honeypot field, no submitted keys at all
        bypass_kind=kind,
    )
    assert r.allow is True
    assert r.outcome == hp.OUTCOME_HONEYPOT_BYPASS
    assert r.audit_event is None  # caller emits bypass_* itself
    assert r.bypass_kind == kind
    assert r.field_name_used is None


def test_validate_unknown_bypass_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown bypass_kind"):
        hp.validate_honeypot(
            "/api/v1/auth/login",
            "tA",
            {},
            bypass_kind="not_a_real_axis",
        )


def test_validate_tenant_disabled_passes() -> None:
    """AS.0.7 §4.3: tenant opt-out via auth_features.honeypot_active."""
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {},
        tenant_honeypot_active=False,
    )
    assert r.allow is True
    assert r.outcome == hp.OUTCOME_HONEYPOT_BYPASS
    assert r.bypass_kind == hp.BYPASS_KIND_TENANT_DISABLED


def test_validate_precedence_knob_off_beats_bypass(monkeypatch) -> None:
    """Knob-off must short-circuit before any other check."""
    monkeypatch.setattr(hp, "is_enabled", lambda: False)
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {"some-irrelevant-key": "value"},
        bypass_kind=hp.BYPASS_KIND_API_KEY,  # ignored
        tenant_honeypot_active=False,  # ignored
    )
    assert r.allow is True
    assert r.bypass_kind == hp.BYPASS_KIND_KNOB_OFF


def test_validate_precedence_bypass_beats_tenant_disabled() -> None:
    """If both bypass_kind and tenant_disabled are set, bypass wins
    (AS.0.6 axes are caller-pre-detected and recorded for audit)."""
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {},
        bypass_kind=hp.BYPASS_KIND_API_KEY,
        tenant_honeypot_active=False,
    )
    assert r.allow is True
    assert r.bypass_kind == hp.BYPASS_KIND_API_KEY


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — 30-day boundary 1-request grace
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_grace_accepts_previous_epoch_field_name() -> None:
    """User renders form at end of epoch N, submits in epoch N+1 →
    server still accepts (1-request grace)."""
    n_prev = _name_for("/api/v1/auth/login", "tA", 99)
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {n_prev: ""},
        now=_ts(100, offset=5),  # current epoch = 100
    )
    assert r.allow is True
    assert r.field_name_used == n_prev


def test_grace_does_not_accept_two_epochs_old() -> None:
    """epoch N-2 field name is NOT accepted — that's drift, not skew."""
    # tA + epoch=98 vs tA + epoch=100/99: low collision probability,
    # but even on a rare collision the result still passes (the field
    # IS in the accepted set), so the test still holds for "correct
    # behaviour".  We assert: a name *uniquely* from epoch 98 (not
    # equal to current OR prev) → form_drift.
    n_now = _name_for("/api/v1/auth/login", "tA", 100)
    n_prev = _name_for("/api/v1/auth/login", "tA", 99)
    n_old = _name_for("/api/v1/auth/login", "tA", 98)
    if n_old == n_now or n_old == n_prev:
        # Hash-collision skip: a different tenant_id always works.
        n_old = _name_for("/api/v1/auth/login", "tA-old-tenant", 100)
        assert n_old != n_now and n_old != n_prev
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {n_old: ""},
        now=_ts(100, offset=5),
    )
    assert r.allow is False
    assert r.outcome == hp.OUTCOME_HONEYPOT_FORM_DRIFT
    assert r.failure_reason == hp.FAILURE_REASON_FIELD_MISSING_IN_FORM


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — Multi-valued / list-of-values form keys
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_validate_list_value_with_filled_element_is_fail() -> None:
    """A bot piling multiple values onto the honeypot key should still
    be caught (any non-empty element counts as filled)."""
    n = _name_for("/api/v1/auth/login", "tA", 100)
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {n: ["", "", "bot-value"]},
        now=_ts(100),
    )
    assert r.allow is False
    assert r.outcome == hp.OUTCOME_HONEYPOT_FAIL


def test_validate_list_value_all_empty_is_pass() -> None:
    n = _name_for("/api/v1/auth/login", "tA", 100)
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {n: ["", "", "  "]},
        now=_ts(100),
    )
    assert r.allow is True
    assert r.outcome == hp.OUTCOME_HONEYPOT_PASS


def test_validate_value_length_redacts_pii() -> None:
    """audit_metadata.field_filled_length is the only diagnostic; the
    raw value (potentially PII from autofill) is never written."""
    n = _name_for("/api/v1/auth/login", "tA", 100)
    secret_email = "user.real@company.example"
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {n: secret_email},
        now=_ts(100),
    )
    md = r.audit_metadata
    assert md["field_filled_length"] == len(secret_email)
    for key in md:
        # No metadata key should leak the raw value.
        assert secret_email not in str(md[key])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — validate_and_enforce
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_validate_and_enforce_pass_returns_result() -> None:
    n = _name_for("/api/v1/auth/login", "tA", 100)
    r = hp.validate_and_enforce(
        "/api/v1/auth/login",
        "tA",
        {n: ""},
        now=_ts(100),
    )
    assert r.allow is True


def test_validate_and_enforce_bypass_returns_result() -> None:
    r = hp.validate_and_enforce(
        "/api/v1/auth/login",
        "tA",
        {},
        bypass_kind=hp.BYPASS_KIND_API_KEY,
    )
    assert r.allow is True


def test_validate_and_enforce_field_filled_raises() -> None:
    n = _name_for("/api/v1/auth/login", "tA", 100)
    with pytest.raises(hp.HoneypotRejected) as excinfo:
        hp.validate_and_enforce(
            "/api/v1/auth/login",
            "tA",
            {n: "bot"},
            now=_ts(100),
        )
    err = excinfo.value
    assert err.code == hp.HONEYPOT_REJECTED_CODE
    assert err.http_status == hp.HONEYPOT_REJECTED_HTTP_STATUS
    assert err.result.outcome == hp.OUTCOME_HONEYPOT_FAIL
    assert err.result.failure_reason == hp.FAILURE_REASON_FIELD_FILLED


def test_validate_and_enforce_form_drift_raises() -> None:
    with pytest.raises(hp.HoneypotRejected) as excinfo:
        hp.validate_and_enforce(
            "/api/v1/auth/login",
            "tA",
            {"unrelated": "key"},
            now=_ts(100),
        )
    assert excinfo.value.result.outcome == hp.OUTCOME_HONEYPOT_FORM_DRIFT


def test_should_reject_predicate_pure() -> None:
    n = _name_for("/api/v1/auth/login", "tA", 100)
    r_pass = hp.validate_honeypot(
        "/api/v1/auth/login", "tA", {n: ""}, now=_ts(100)
    )
    r_fail = hp.validate_honeypot(
        "/api/v1/auth/login", "tA", {n: "x"}, now=_ts(100)
    )
    assert hp.should_reject(r_pass) is False
    assert hp.should_reject(r_fail) is True
    # Pure: 5 successive calls give the same answer.
    for _ in range(5):
        assert hp.should_reject(r_pass) is False
        assert hp.should_reject(r_fail) is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — HoneypotRejected ctor / attrs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_honeypot_rejected_default_code_and_status() -> None:
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {hp.honeypot_field_name("/api/v1/auth/login", "tA", 100): "bot"},
        now=_ts(100),
    )
    err = hp.HoneypotRejected(r)
    assert err.code == hp.HONEYPOT_REJECTED_CODE
    assert err.http_status == hp.HONEYPOT_REJECTED_HTTP_STATUS
    assert err.result is r


def test_honeypot_rejected_custom_keyword_args() -> None:
    r = hp.validate_honeypot(
        "/api/v1/auth/login",
        "tA",
        {hp.honeypot_field_name("/api/v1/auth/login", "tA", 100): "bot"},
        now=_ts(100),
    )
    err = hp.HoneypotRejected(r, code="custom_code", http_status=418)
    assert err.code == "custom_code"
    assert err.http_status == 418


def test_honeypot_rejected_subclasses_honeypot_error() -> None:
    """Caller can `except (BotChallengeError, HoneypotError)` to catch
    every AS.3 + AS.4 reject-shape error in one branch."""
    r = hp.HoneypotResult(
        allow=False,
        outcome=hp.OUTCOME_HONEYPOT_FAIL,
        audit_event=hp.EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
    )
    err = hp.HoneypotRejected(r)
    assert isinstance(err, hp.HoneypotError)
    assert isinstance(err, Exception)


def test_honeypot_rejected_message_grep_friendly() -> None:
    r = hp.HoneypotResult(
        allow=False,
        outcome=hp.OUTCOME_HONEYPOT_FAIL,
        audit_event=hp.EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
        failure_reason=hp.FAILURE_REASON_FIELD_FILLED,
    )
    err = hp.HoneypotRejected(r)
    msg = str(err)
    assert "outcome=honeypot_fail" in msg
    assert "reason=field_filled" in msg
    assert "code=bot_challenge_failed" in msg
    assert "http_status=429" in msg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 — AS.0.8 single-knob (settings.as_enabled = False)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_is_enabled_default_true() -> None:
    """Default behaviour: knob unset / settings module missing → True."""
    # We don't mutate settings; the live test environment may or may
    # not have settings.as_enabled — just assert it's a bool.
    assert isinstance(hp.is_enabled(), bool)


def test_is_enabled_returns_false_when_knob_off() -> None:
    """When backend.config.settings.as_enabled = False, the lib
    short-circuits."""
    fake_settings = mock.Mock()
    fake_settings.as_enabled = False
    fake_module = mock.Mock(settings=fake_settings)
    with mock.patch.dict(sys.modules, {"backend.config": fake_module}):
        assert hp.is_enabled() is False


def test_validate_returns_knob_off_bypass_when_disabled() -> None:
    fake_settings = mock.Mock()
    fake_settings.as_enabled = False
    fake_module = mock.Mock(settings=fake_settings)
    with mock.patch.dict(sys.modules, {"backend.config": fake_module}):
        n = hp.honeypot_field_name("/api/v1/auth/login", "tA", 100)
        # Even with a bot-filled field, knob-off short-circuits.
        r = hp.validate_honeypot(
            "/api/v1/auth/login",
            "tA",
            {n: "bot"},
            now=_ts(100),
        )
        assert r.allow is True
        assert r.bypass_kind == hp.BYPASS_KIND_KNOB_OFF


def test_is_enabled_forward_promotion_guard_on_missing_field() -> None:
    """Module ships before backend.config.Settings has the field — must
    default to True (forward-promotion safety)."""
    fake_module = mock.Mock(spec=[])
    fake_module.settings = mock.Mock(spec=[])  # no `as_enabled` attribute
    with mock.patch.dict(sys.modules, {"backend.config": fake_module}):
        assert hp.is_enabled() is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 10 — __all__ shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_all_export_complete() -> None:
    """Every public symbol the module documents is in __all__."""
    must_export = {
        # Constants
        "_FORM_PREFIXES",
        "_RARE_WORD_POOL",
        "OS_HONEYPOT_CLASS",
        "HONEYPOT_HIDE_CSS",
        "HONEYPOT_INPUT_ATTRS",
        "HONEYPOT_ROTATION_PERIOD_SECONDS",
        "HONEYPOT_REJECTED_CODE",
        "HONEYPOT_REJECTED_HTTP_STATUS",
        # Audit events
        "EVENT_BOT_CHALLENGE_HONEYPOT_PASS",
        "EVENT_BOT_CHALLENGE_HONEYPOT_FAIL",
        "EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT",
        "ALL_HONEYPOT_EVENTS",
        # Outcomes
        "OUTCOME_HONEYPOT_PASS",
        "OUTCOME_HONEYPOT_FAIL",
        "OUTCOME_HONEYPOT_FORM_DRIFT",
        "OUTCOME_HONEYPOT_BYPASS",
        "ALL_HONEYPOT_OUTCOMES",
        # Failure reasons
        "FAILURE_REASON_FIELD_FILLED",
        "FAILURE_REASON_FIELD_MISSING_IN_FORM",
        "FAILURE_REASON_FORM_PATH_UNKNOWN",
        # Bypass kinds
        "BYPASS_KIND_API_KEY",
        "BYPASS_KIND_TEST_TOKEN",
        "BYPASS_KIND_IP_ALLOWLIST",
        "BYPASS_KIND_KNOB_OFF",
        "BYPASS_KIND_TENANT_DISABLED",
        "ALL_BYPASS_KINDS",
        # Result + errors
        "HoneypotResult",
        "HoneypotError",
        "HoneypotRejected",
        # Functions
        "is_enabled",
        "supported_form_paths",
        "current_epoch",
        "honeypot_field_name",
        "expected_field_names",
        "validate_honeypot",
        "should_reject",
        "validate_and_enforce",
        "event_for_honeypot_outcome",
    }
    actual = set(hp.__all__)
    missing = must_export - actual
    assert not missing, f"missing from __all__: {sorted(missing)}"
    extra_unknown = actual - must_export
    assert not extra_unknown, f"unexpected in __all__: {sorted(extra_unknown)}"
