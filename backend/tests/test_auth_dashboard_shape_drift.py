"""AS.5.2 — Auth dashboard drift guard (Python ↔ TS twin).

Behavioural drift guard between
:mod:`backend.security.auth_dashboard` (Python) and
``templates/_shared/auth-dashboard/index.ts`` (TS twin).

Why this test exists
────────────────────
The six dashboard rule names + per-rule default thresholds + per-rule
default severities + per-event counter mapping + per-rule alert
evidence keys MUST be byte-identical between the OmniSight backend
(which feeds the admin pane) and any generated app's TS-side dashboard
widget (which reduces an offline-cached audit stream into the same
shape).  Drift means the OmniSight admin pane shows
``login_success_rate=0.42`` while the same generated-app dashboard
shows ``loginSuccessRate=0.51`` for the same rows.

Coverage shape
──────────────
1. **Static parity** (no Node required) — regex-extract constants from
   the TS source, ``==``-compare them to Python.
2. **Behavioural parity** (Node spawned once per session) — drive a
   fixture matrix of audit-row sequences through both twins and
   compare normalised summaries + alert lists.
3. **Aggregate SHA-256 oracle** — one hash over the full normalised
   fixture matrix.  Catches the "many-tiny-drifts" failure mode that
   per-fixture tests can't summarise.
4. **Coverage guard** — every rule × at least one trigger fixture +
   every counter field × at least one fixture (drift guard's own
   drift guard).

How TS execution works
──────────────────────
Same harness as AS.5.1 / AS.4.1 / AS.3.x: spawn ``node
--experimental-strip-types`` to import the TS twin directly.  A single
subprocess runs every fixture and emits one JSON blob; the
session-scoped fixture caches that across the parametrised tests so
spawn cost amortises to one invocation per pytest session.

The behavioural family ``pytest.skip``s if Node ≥ 22 is unavailable.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* Fixture data lives in module-level dict literals containing only
  immutable scalars.  Each pytest worker re-imports them with byte-
  identical content (answer #1 of SOP §1).
* The session-scoped Node-output cache lives on the pytest fixture,
  not module-level — pytest manages its lifecycle per worker.
* No DB, no network IO, no env reads at module import time.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess
from typing import Any

import pytest

from backend.security import auth_dashboard as ad


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Paths + Node gating
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TS_TWIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "templates"
    / "_shared"
    / "auth-dashboard"
    / "index.ts"
)


def _node_supports_strip_types() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    try:
        r = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    raw = r.stdout.strip().lstrip("v")
    try:
        major = int(raw.split(".", 1)[0])
    except (ValueError, IndexError):
        return False
    return major >= 22


def _ts_source() -> str:
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    return _TS_TWIN_PATH.read_text(encoding="utf-8")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TS extractor helpers (regex over the source)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _ts_string_const(src: str, name: str) -> str:
    m = re.search(
        rf'export\s+const\s+{name}\s*=\s*"((?:[^"\\]|\\.)*)"', src
    )
    assert m, f"could not find `export const {name} = \"...\"` in TS twin"
    return m.group(1)


def _ts_int_const(src: str, name: str) -> int:
    m = re.search(rf"export\s+const\s+{name}\s*=\s*(-?\d+)\b", src)
    assert m, f"could not find `export const {name} = <int>` in TS twin"
    return int(m.group(1))


def _ts_array_members(src: str, name: str) -> list[str]:
    pattern = re.compile(
        rf"export\s+const\s+{name}\s*:\s*ReadonlyArray<string>\s*=\s*"
        rf"Object\.freeze\(\[([\s\S]*?)\]\)",
        re.MULTILINE,
    )
    m = pattern.search(src)
    assert m, f"could not find `export const {name} = ...Array...`"
    body = m.group(1)
    members: list[str] = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        members.append(_ts_string_const(src, tok))
    return members


def _ts_set_members(src: str, name: str) -> list[str]:
    pattern = re.compile(
        rf"export\s+const\s+{name}\s*:\s*[^=]+=\s*Object\.freeze\(\s*"
        rf"new\s+Set<string>\(\s*\[([\s\S]*?)\]\s*\)",
        re.MULTILINE,
    )
    m = pattern.search(src)
    assert m, f"could not find `export const {name} = ...Set...`"
    body = m.group(1)
    members: list[str] = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        members.append(_ts_string_const(src, tok))
    return members


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Static parity (no Node required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_ts_twin_present_and_python_empty_shape_real_call() -> None:
    src = _ts_source()
    assert "export function summarise" in src

    summary = ad.summarise(
        [],
        tenant_id="t-empty-real-call",
        since=10.0,
        until=20.0,
    )
    empty = ad.empty_summary(
        "t-empty-real-call",
        since=10.0,
        until=20.0,
    )
    alerts = ad.detect_suspicious_patterns(
        [],
        tenant_id="t-empty-real-call",
    )

    assert _normalise_summary_python(summary) == _normalise_summary_python(empty)
    assert summary.since == 10.0
    assert summary.until == 20.0
    assert summary.total_events == 0
    assert summary.login_success_rate is None
    assert dict(summary.auth_method_distribution) == {}
    assert alerts == ()


def test_rule_strings_byte_equal() -> None:
    src = _ts_source()
    pairs = [
        ("RULE_LOGIN_FAIL_BURST", ad.RULE_LOGIN_FAIL_BURST),
        ("RULE_BOT_CHALLENGE_FAIL_SPIKE", ad.RULE_BOT_CHALLENGE_FAIL_SPIKE),
        ("RULE_TOKEN_REFRESH_STORM", ad.RULE_TOKEN_REFRESH_STORM),
        ("RULE_HONEYPOT_TRIGGERED", ad.RULE_HONEYPOT_TRIGGERED),
        ("RULE_OAUTH_REVOKE_RELINK_LOOP", ad.RULE_OAUTH_REVOKE_RELINK_LOOP),
        ("RULE_DISTRIBUTED_LOGIN_FAIL", ad.RULE_DISTRIBUTED_LOGIN_FAIL),
    ]
    for name, py_value in pairs:
        assert _ts_string_const(src, name) == py_value, f"drift on {name}"


def test_all_dashboard_rules_array_byte_equal() -> None:
    src = _ts_source()
    ts = _ts_array_members(src, "ALL_DASHBOARD_RULES")
    assert ts == list(ad.ALL_DASHBOARD_RULES)


def test_severity_strings_byte_equal() -> None:
    src = _ts_source()
    assert _ts_string_const(src, "SEVERITY_INFO") == ad.SEVERITY_INFO
    assert _ts_string_const(src, "SEVERITY_WARN") == ad.SEVERITY_WARN
    assert _ts_string_const(src, "SEVERITY_CRITICAL") == ad.SEVERITY_CRITICAL


def test_severities_set_byte_equal() -> None:
    src = _ts_source()
    ts = set(_ts_set_members(src, "SEVERITIES"))
    assert ts == ad.SEVERITIES


def test_limit_rows_default_byte_equal() -> None:
    src = _ts_source()
    assert _ts_int_const(src, "LIMIT_ROWS_DEFAULT") == ad.LIMIT_ROWS_DEFAULT


def test_default_thresholds_byte_equal() -> None:
    """Per-rule (count, window_s) MUST equal (count, windowS) on TS side."""
    src = _ts_source()
    # Extract the DEFAULT_THRESHOLDS object literal.  Each entry is
    #   [RULE_X]: Object.freeze({ count: N, windowS: M }),
    # We pull each rule's count + windowS by anchored regex.
    for rule_var, rule_value in (
        ("RULE_LOGIN_FAIL_BURST", ad.RULE_LOGIN_FAIL_BURST),
        ("RULE_BOT_CHALLENGE_FAIL_SPIKE", ad.RULE_BOT_CHALLENGE_FAIL_SPIKE),
        ("RULE_TOKEN_REFRESH_STORM", ad.RULE_TOKEN_REFRESH_STORM),
        ("RULE_HONEYPOT_TRIGGERED", ad.RULE_HONEYPOT_TRIGGERED),
        ("RULE_OAUTH_REVOKE_RELINK_LOOP", ad.RULE_OAUTH_REVOKE_RELINK_LOOP),
        ("RULE_DISTRIBUTED_LOGIN_FAIL", ad.RULE_DISTRIBUTED_LOGIN_FAIL),
    ):
        pattern = re.compile(
            rf"\[{rule_var}\]\s*:\s*Object\.freeze\(\s*\{{\s*"
            rf"count\s*:\s*(\d+)\s*,\s*windowS\s*:\s*(\d+)\s*\}}",
            re.MULTILINE,
        )
        m = pattern.search(src)
        assert m, f"could not find DEFAULT_THRESHOLDS entry for {rule_var}"
        ts_count = int(m.group(1))
        ts_window = int(m.group(2))
        py = ad.DEFAULT_THRESHOLDS[rule_value]
        assert ts_count == py["count"], f"count drift on {rule_var}"
        assert ts_window == py["window_s"], f"window_s drift on {rule_var}"


def test_default_rule_severities_byte_equal() -> None:
    src = _ts_source()
    for rule_var, rule_value in (
        ("RULE_LOGIN_FAIL_BURST", ad.RULE_LOGIN_FAIL_BURST),
        ("RULE_BOT_CHALLENGE_FAIL_SPIKE", ad.RULE_BOT_CHALLENGE_FAIL_SPIKE),
        ("RULE_TOKEN_REFRESH_STORM", ad.RULE_TOKEN_REFRESH_STORM),
        ("RULE_HONEYPOT_TRIGGERED", ad.RULE_HONEYPOT_TRIGGERED),
        ("RULE_OAUTH_REVOKE_RELINK_LOOP", ad.RULE_OAUTH_REVOKE_RELINK_LOOP),
        ("RULE_DISTRIBUTED_LOGIN_FAIL", ad.RULE_DISTRIBUTED_LOGIN_FAIL),
    ):
        pattern = re.compile(
            rf"\[{rule_var}\]\s*:\s*(SEVERITY_\w+)",
            re.MULTILINE,
        )
        # Find the entry inside DEFAULT_RULE_SEVERITIES (not
        # DEFAULT_THRESHOLDS) — the SEVERITY_* form distinguishes them.
        m = pattern.search(src)
        assert m, f"could not find DEFAULT_RULE_SEVERITIES entry for {rule_var}"
        ts_sev = _ts_string_const(src, m.group(1))
        py_sev = ad.DEFAULT_RULE_SEVERITIES[rule_value]
        assert ts_sev == py_sev, f"severity drift on {rule_var}"


def test_ts_exports_execute_empty_fixture() -> None:
    """Import the TS twin and execute the four public functions."""
    if not _node_supports_strip_types():
        pytest.skip("Node ≥ 22 (--experimental-strip-types) not available")

    runner = """
import {
  summarise,
  detectSuspiciousPatterns,
  emptySummary,
  isEnabled,
} from %(twin_path)s;

const summary = summarise([], { tenantId: "t-empty-real-call" });
const empty = emptySummary("t-empty-real-call");
const alerts = detectSuspiciousPatterns([], { tenantId: "t-empty-real-call" });
process.stdout.write(JSON.stringify({
  summary,
  empty,
  alerts,
  enabled: isEnabled(),
}));
""" % {"twin_path": json.dumps(str(_TS_TWIN_PATH))}

    proc = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-"],
        input=runner,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"TS twin export real-call failed:\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    out = json.loads(proc.stdout)
    assert out["summary"]["tenantId"] == "t-empty-real-call"
    assert out["summary"]["totalEvents"] == 0
    assert out["empty"] == out["summary"]
    assert out["alerts"] == []
    assert out["enabled"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Behavioural fixture matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Each fixture is a list of audit rows + a "kind" tag (which detector
# it primarily exercises).  Both twins reduce the same input and we
# compare the normalised outputs.

FIXTURES: list[dict[str, Any]] = [
    {
        "id": "f-empty",
        "tenant_id": "t-empty",
        "rows": [],
    },
    {
        "id": "f-mixed-rates",
        "tenant_id": "t-mixed",
        "rows": [
            {"action": "auth.login_success", "ts": 1, "entity_id": "u-1",
             "after": {"auth_method": "password"}},
            {"action": "auth.login_success", "ts": 2, "entity_id": "u-2",
             "after": {"auth_method": "oauth", "provider": "github"}},
            {"action": "auth.login_fail", "ts": 3, "entity_id": "fp-x",
             "after": {"attempted_user_fp": "fp-x", "fail_reason": "bad_password",
                       "auth_method": "password", "ip_fp": "ip-1"}},
            {"action": "auth.bot_challenge_pass", "ts": 4, "entity_id": "/login",
             "after": {"kind": "verified", "form_path": "/login"}},
            {"action": "auth.bot_challenge_fail", "ts": 5, "entity_id": "/signup",
             "after": {"reason": "lowscore", "form_path": "/signup"}},
            {"action": "auth.oauth_connect", "ts": 6, "entity_id": "github:u-1",
             "after": {"provider": "github", "outcome": "connected"}},
            {"action": "auth.oauth_revoke", "ts": 7, "entity_id": "github:u-2",
             "after": {"provider": "github", "initiator": "user"}},
            {"action": "auth.token_refresh", "ts": 8, "entity_id": "github:u-1",
             "after": {"provider": "github", "outcome": "success"}},
            {"action": "auth.token_rotated", "ts": 9, "entity_id": "github:u-1",
             "after": {"provider": "github", "triggered_by": "auto_refresh"}},
        ],
    },
    {
        "id": "f-login-fail-burst",
        "tenant_id": "t-burst",
        "rows": [
            {"action": "auth.login_fail", "ts": float(i), "entity_id": "fp-target",
             "after": {"attempted_user_fp": "fp-target",
                       "fail_reason": "bad_password",
                       "auth_method": "password", "ip_fp": "ip-1"}}
            for i in range(10)
        ],
    },
    {
        "id": "f-bot-challenge-fail-spike",
        "tenant_id": "t-spike",
        "rows": [
            {"action": "auth.bot_challenge_fail", "ts": float(i),
             "entity_id": "/signup",
             "after": {"reason": "lowscore", "form_path": "/signup"}}
            for i in range(20)
        ],
    },
    {
        "id": "f-token-refresh-storm",
        "tenant_id": "t-storm",
        "rows": [
            {"action": "auth.token_refresh", "ts": float(i),
             "entity_id": "github:u-1",
             "after": {"provider": "github", "outcome": "success"}}
            for i in range(10)
        ],
    },
    {
        "id": "f-honeypot-triggered",
        "tenant_id": "t-honey",
        "rows": [
            {"action": "auth.bot_challenge_fail", "ts": 100,
             "entity_id": "/signup",
             "after": {"reason": "honeypot", "form_path": "/signup"}},
        ],
    },
    {
        "id": "f-revoke-relink-loop",
        "tenant_id": "t-loop",
        "rows": [
            {"action": "auth.oauth_revoke", "ts": 0, "entity_id": "github:u-1",
             "after": {"provider": "github", "initiator": "user"}},
            {"action": "auth.oauth_connect", "ts": 10, "entity_id": "github:u-1",
             "after": {"provider": "github", "outcome": "relinked"}},
            {"action": "auth.oauth_revoke", "ts": 20, "entity_id": "github:u-1",
             "after": {"provider": "github", "initiator": "user"}},
            {"action": "auth.oauth_connect", "ts": 30, "entity_id": "github:u-1",
             "after": {"provider": "github", "outcome": "relinked"}},
            {"action": "auth.oauth_revoke", "ts": 40, "entity_id": "github:u-1",
             "after": {"provider": "github", "initiator": "user"}},
            {"action": "auth.oauth_connect", "ts": 50, "entity_id": "github:u-1",
             "after": {"provider": "github", "outcome": "relinked"}},
        ],
    },
    {
        "id": "f-distributed-login-fail",
        "tenant_id": "t-distributed",
        "rows": [
            {"action": "auth.login_fail", "ts": float(i * 10),
             "entity_id": "fp-victim",
             "after": {"attempted_user_fp": "fp-victim",
                       "fail_reason": "bad_password",
                       "auth_method": "password", "ip_fp": f"ip-{i}"}}
            for i in range(5)
        ],
    },
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Normalisation: Python and TS twins emit slightly different shapes
#  (Python snake_case dataclass vs TS camelCase Object.freeze).  We
#  project both onto a common normalised form for comparison.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _normalise_summary_python(s: ad.DashboardSummary) -> dict[str, Any]:
    return {
        "tenant_id": s.tenant_id,
        "total_events": s.total_events,
        "login_success_count": s.login_success_count,
        "login_fail_count": s.login_fail_count,
        "login_success_rate": s.login_success_rate,
        "login_fail_reasons": dict(s.login_fail_reasons),
        "auth_method_distribution": dict(s.auth_method_distribution),
        "bot_challenge_pass_count": s.bot_challenge_pass_count,
        "bot_challenge_fail_count": s.bot_challenge_fail_count,
        "bot_challenge_pass_rate": s.bot_challenge_pass_rate,
        "bot_challenge_pass_kinds": dict(s.bot_challenge_pass_kinds),
        "bot_challenge_fail_reasons": dict(s.bot_challenge_fail_reasons),
        "oauth_connect_count": s.oauth_connect_count,
        "oauth_revoke_count": s.oauth_revoke_count,
        "oauth_revoke_initiators": dict(s.oauth_revoke_initiators),
        "token_refresh_count": s.token_refresh_count,
        "token_refresh_outcomes": dict(s.token_refresh_outcomes),
        "token_rotated_count": s.token_rotated_count,
    }


def _normalise_summary_ts(s: dict[str, Any]) -> dict[str, Any]:
    """TS twin emits camelCase; map each key to the snake_case Python
    counterpart so the two shapes align for comparison."""
    return {
        "tenant_id": s["tenantId"],
        "total_events": s["totalEvents"],
        "login_success_count": s["loginSuccessCount"],
        "login_fail_count": s["loginFailCount"],
        "login_success_rate": s["loginSuccessRate"],
        "login_fail_reasons": dict(s["loginFailReasons"]),
        "auth_method_distribution": dict(s["authMethodDistribution"]),
        "bot_challenge_pass_count": s["botChallengePassCount"],
        "bot_challenge_fail_count": s["botChallengeFailCount"],
        "bot_challenge_pass_rate": s["botChallengePassRate"],
        "bot_challenge_pass_kinds": dict(s["botChallengePassKinds"]),
        "bot_challenge_fail_reasons": dict(s["botChallengeFailReasons"]),
        "oauth_connect_count": s["oauthConnectCount"],
        "oauth_revoke_count": s["oauthRevokeCount"],
        "oauth_revoke_initiators": dict(s["oauthRevokeInitiators"]),
        "token_refresh_count": s["tokenRefreshCount"],
        "token_refresh_outcomes": dict(s["tokenRefreshOutcomes"]),
        "token_rotated_count": s["tokenRotatedCount"],
    }


def _normalise_alert_python(a: ad.SuspiciousPatternAlert) -> dict[str, Any]:
    return {
        "rule": a.rule,
        "severity": a.severity,
        "tenant_id": a.tenant_id,
        "evidence": _normalise_evidence(dict(a.evidence)),
    }


def _normalise_alert_ts(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule": a["rule"],
        "severity": a["severity"],
        "tenant_id": a["tenantId"],
        "evidence": _normalise_evidence(dict(a["evidence"])),
    }


def _normalise_evidence(ev: dict[str, Any]) -> dict[str, Any]:
    """Sort `ip_fps` / cast tuples to lists to give the same JSON
    shape across twins."""
    out: dict[str, Any] = {}
    for k, v in ev.items():
        if isinstance(v, tuple):
            out[k] = list(v)
        else:
            out[k] = v
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Python-side compute (pure, no spawn)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _python_compute_one(fx: dict[str, Any]) -> dict[str, Any]:
    rows = fx["rows"]
    tenant_id = fx["tenant_id"]
    summary = ad.summarise(rows, tenant_id=tenant_id)
    alerts = ad.detect_suspicious_patterns(rows, tenant_id=tenant_id)
    return {
        "id": fx["id"],
        "summary": _normalise_summary_python(summary),
        "alerts": [_normalise_alert_python(a) for a in alerts],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TS-side compute (Node spawn, session-scoped cache)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_TS_RUNNER_TEMPLATE = r"""
import {
  summarise,
  detectSuspiciousPatterns,
} from %(twin_path)s;

const FIXTURES = %(fixtures)s;

const out = [];
for (const fx of FIXTURES) {
  const summary = summarise(fx.rows, { tenantId: fx.tenant_id });
  const alerts = detectSuspiciousPatterns(fx.rows, { tenantId: fx.tenant_id });
  out.push({
    id: fx.id,
    summary,
    alerts,
  });
}

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="session")
def ts_compute_results() -> list[dict[str, Any]]:
    """Session-scoped Node spawn — runs every fixture in one
    invocation and caches the JSON output for the parametrised tests
    below.  Skips if Node ≥ 22 is not available."""
    if not _node_supports_strip_types():
        pytest.skip("Node ≥ 22 (--experimental-strip-types) not available")

    twin_path_lit = json.dumps(str(_TS_TWIN_PATH))
    fixtures_lit = json.dumps(FIXTURES)
    runner = _TS_RUNNER_TEMPLATE % {
        "twin_path": twin_path_lit,
        "fixtures": fixtures_lit,
    }

    proc = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-"],
        input=runner,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        pytest.fail(f"TS twin runner failed:\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}")
    return json.loads(proc.stdout)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Behavioural parity (per fixture)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_summary_parity_per_fixture(
    fx: dict[str, Any], ts_compute_results: list[dict[str, Any]],
) -> None:
    """Per-fixture summary parity — Python summarise vs TS summarise."""
    py = _python_compute_one(fx)
    ts = next(x for x in ts_compute_results if x["id"] == fx["id"])
    py_norm = py["summary"]
    ts_norm = _normalise_summary_ts(ts["summary"])
    assert py_norm == ts_norm, (
        f"summary drift on fixture {fx['id']}:\n"
        f"  python={json.dumps(py_norm, sort_keys=True)}\n"
        f"  ts={json.dumps(ts_norm, sort_keys=True)}"
    )


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_alerts_parity_per_fixture(
    fx: dict[str, Any], ts_compute_results: list[dict[str, Any]],
) -> None:
    """Per-fixture alert parity — Python detect vs TS detect."""
    py = _python_compute_one(fx)
    ts = next(x for x in ts_compute_results if x["id"] == fx["id"])
    py_alerts = py["alerts"]
    ts_alerts = [_normalise_alert_ts(a) for a in ts["alerts"]]
    assert py_alerts == ts_alerts, (
        f"alerts drift on fixture {fx['id']}:\n"
        f"  python={json.dumps(py_alerts, sort_keys=True)}\n"
        f"  ts={json.dumps(ts_alerts, sort_keys=True)}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Aggregate SHA-256 oracle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _coerce_numerics(obj: Any) -> Any:
    """Recursively coerce every int/float/bool to a string-form float
    so Python's `0.0` and JS's `0` (string-different but numerically
    identical after JSON round-trip) hash to the same value.

    Booleans are coerced to lowercase strings ("true" / "false") so
    they don't conflate with numeric 0/1.
    """
    if isinstance(obj, bool):
        return f"bool:{str(obj).lower()}"
    if isinstance(obj, (int, float)):
        return f"num:{float(obj):.10g}"
    if isinstance(obj, str):
        return obj
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_coerce_numerics(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _coerce_numerics(v) for k, v in obj.items()}
    return obj


def _sha256_of(obj: Any) -> str:
    raw = json.dumps(
        _coerce_numerics(obj), sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def test_aggregate_oracle_python_ts_match(
    ts_compute_results: list[dict[str, Any]],
) -> None:
    """One hash over the full normalised matrix.  Catches
    'many-tiny-drifts' that per-fixture asserts can't summarise."""
    py_normalised = []
    ts_normalised = []
    for fx in FIXTURES:
        py = _python_compute_one(fx)
        ts = next(x for x in ts_compute_results if x["id"] == fx["id"])
        py_normalised.append({
            "id": fx["id"],
            "summary": py["summary"],
            "alerts": py["alerts"],
        })
        ts_normalised.append({
            "id": fx["id"],
            "summary": _normalise_summary_ts(ts["summary"]),
            "alerts": [_normalise_alert_ts(a) for a in ts["alerts"]],
        })
    assert _sha256_of(py_normalised) == _sha256_of(ts_normalised), (
        "aggregate hash mismatch — drift somewhere in the matrix"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — Coverage guard (drift guard's own drift guard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_every_rule_has_a_trigger_fixture() -> None:
    """For every rule in ALL_DASHBOARD_RULES, at least one fixture
    must produce an alert with that rule.  Otherwise the drift guard
    silently never exercises that rule."""
    rules_seen: set[str] = set()
    for fx in FIXTURES:
        py = _python_compute_one(fx)
        for a in py["alerts"]:
            rules_seen.add(a["rule"])
    missing = set(ad.ALL_DASHBOARD_RULES) - rules_seen
    assert not missing, (
        f"drift guard missing trigger fixture for rules: {sorted(missing)}"
    )


def test_every_summary_field_has_nonzero_fixture() -> None:
    """For every counter / distribution field on DashboardSummary that
    has a vocabulary, at least one fixture must produce a non-empty
    value.  Otherwise the drift guard never confirms the field
    actually populates."""
    bumped: dict[str, bool] = {
        "login_success_count": False,
        "login_fail_count": False,
        "auth_method_distribution": False,
        "login_fail_reasons": False,
        "bot_challenge_pass_count": False,
        "bot_challenge_fail_count": False,
        "bot_challenge_pass_kinds": False,
        "bot_challenge_fail_reasons": False,
        "oauth_connect_count": False,
        "oauth_revoke_count": False,
        "oauth_revoke_initiators": False,
        "token_refresh_count": False,
        "token_refresh_outcomes": False,
        "token_rotated_count": False,
    }
    for fx in FIXTURES:
        py = _python_compute_one(fx)["summary"]
        for k in list(bumped.keys()):
            v = py[k]
            if isinstance(v, int) and v > 0:
                bumped[k] = True
            elif isinstance(v, dict) and len(v) > 0:
                bumped[k] = True
    not_exercised = [k for k, ok in bumped.items() if not ok]
    assert not not_exercised, (
        f"drift guard missing fixture exercising fields: {not_exercised}"
    )
