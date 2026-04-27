"""AS.3.2 — Bot-challenge contract drift guard (Python ↔ TS twin).

Behavioural drift guard between
:mod:`backend.security.bot_challenge` (Python) and
``templates/_shared/bot-challenge/index.ts`` (TS twin).

Why this test exists
────────────────────
The bot-challenge family is the **second** AS lib (after token-vault)
that has to ship the same audit-event vocabulary, the same outcome
literals, the same phase classifier behaviour, and the same bypass
axis precedence on both sides — the Python lib runs in OmniSight's
backend and the TS twin runs inside every generated app's server-side
or serverless handler. If the strings drift, the AS.5.2 dashboard's
event-counts split, the per-tenant audit chain breaks at the JSON
schema layer, and any cross-twin reuse (SC.13) silently misclassifies.

Coverage shape
──────────────
1. **Static parity** (no Node required) — regex-extract numeric
   constants, audit-event strings, outcome literals, provider enum
   values, bypass path-prefixes, bypass caller-kinds, score threshold,
   timeout, and test-token header from the TS source. ``==``-compare
   them to the Python side. Catches "someone bumps an audit event
   string on one side only" cleanly without spawning Node.

2. **Behavioural parity** (Node spawned once per session) — drive a
   matrix of fixtures through both sides and compare the **outcome**:

       * `evaluate_bypass` precedence A > C > B > D across every
         multi-axis combination
       * Path-prefix dispatch for every prefix in the bypass list
       * IP allowlist v4 + v6 + corrupt entry skip + wide-CIDR flag
       * Test-token header (≥32 chars, constant-time, mismatch)
       * `classify_outcome` phase 1/2/3 × pass/low-score/server-error
       * Score calibration per provider (Turnstile/v3 = float, v2/hCaptcha = binary)
       * `passthrough` shape on knob-off
       * `event_for_outcome` lookup table

   We deliberately do NOT compare the full siteverify HTTP path — both
   sides talk to the same vendor URL and the parse logic is verified by
   feeding canned vendor JSON through `_parse_response` (Python) /
   `_parseResponse` (TS).

3. **Aggregate SHA-256 oracle** — one hash over every fixture's
   normalised outcome. Catches "many-tiny-drifts" failure mode that
   per-fixture tests can't summarise.

4. **Coverage guard** — every provider in the enum + every outcome
   literal + every bypass-axis must be exercised by at least one
   fixture (drift guard's own drift guard).

How TS execution works
──────────────────────
Same harness as AS.1.5 / AS.2.3: spawn ``node --experimental-strip-types``
to import the TS twin directly — no transpile step, no node_modules
dep. A single subprocess runs every fixture and emits one JSON blob;
the session-scoped fixture caches that across the parametrised tests
so the spawn cost amortises to one invocation per pytest session.

The whole family ``pytest.skip``s if Node is unavailable (or below v22)
— this matches the AS.1.x / AS.2.3 cross-twin tests' "skip if TS twin
file is absent" gating.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* Fixture data lives in module-level dict literals containing only
  immutable scalars. Each pytest worker re-imports them with byte-
  identical content (answer #1 of SOP §1: deterministic across workers).
* The session-scoped Node-output cache lives on the pytest fixture, not
  module-level — pytest manages its lifecycle per worker.
* No DB, no network IO, no env reads at module import time.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import Any, Mapping

import pytest

from backend.security import bot_challenge as bc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Paths + Node gating
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TS_TWIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "templates"
    / "_shared"
    / "bot-challenge"
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
#  Family 1 — Static parity (no Node required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_ts_twin_file_exists() -> None:
    """AS.3.2 deliverable presence — the TS twin file must be on disk
    where the productizer's emit pipeline expects it."""
    assert _TS_TWIN_PATH.exists(), (
        f"AS.3.2 TS twin missing at {_TS_TWIN_PATH}; "
        "the bot-challenge row in the productizer scaffolds depends on this file."
    )


def test_ts_provider_enum_values_match_python() -> None:
    """The TS `Provider` const-object literal values must equal the
    Python `Provider` enum values byte-for-byte."""
    src = _ts_source()
    m = re.search(
        r"export\s+const\s+Provider\s*=\s*Object\.freeze\(\s*\{(.*?)\}\s*as\s+const",
        src,
        re.DOTALL,
    )
    assert m is not None, "TS twin missing `Provider` const object literal"
    body = m.group(1)
    pairs = re.findall(r'(\w+)\s*:\s*"([^"]+)"', body)
    ts_map = dict(pairs)
    expected = {p.name: p.value for p in bc.Provider}
    assert ts_map == expected, (
        f"Provider enum drift: Python={expected}, TS={ts_map}"
    )


def test_ts_score_threshold_matches_python() -> None:
    src = _ts_source()
    m = re.search(
        r"export\s+const\s+DEFAULT_SCORE_THRESHOLD\s*=\s*([\d.]+)", src
    )
    assert m is not None, "TS twin missing DEFAULT_SCORE_THRESHOLD export"
    assert float(m.group(1)) == bc.DEFAULT_SCORE_THRESHOLD, (
        f"DEFAULT_SCORE_THRESHOLD drift: Python={bc.DEFAULT_SCORE_THRESHOLD}, "
        f"TS={m.group(1)}"
    )


def test_ts_verify_timeout_matches_python() -> None:
    src = _ts_source()
    m = re.search(
        r"export\s+const\s+DEFAULT_VERIFY_TIMEOUT_SECONDS\s*=\s*([\d.]+)", src
    )
    assert m is not None, "TS twin missing DEFAULT_VERIFY_TIMEOUT_SECONDS export"
    assert float(m.group(1)) == bc.DEFAULT_VERIFY_TIMEOUT_SECONDS, (
        f"DEFAULT_VERIFY_TIMEOUT_SECONDS drift: "
        f"Python={bc.DEFAULT_VERIFY_TIMEOUT_SECONDS}, TS={m.group(1)}"
    )


def test_ts_test_token_header_matches_python() -> None:
    src = _ts_source()
    m = re.search(
        r'export\s+const\s+TEST_TOKEN_HEADER\s*=\s*"([^"]+)"', src
    )
    assert m is not None, "TS twin missing TEST_TOKEN_HEADER export"
    assert m.group(1) == bc.TEST_TOKEN_HEADER, (
        f"TEST_TOKEN_HEADER drift: Python={bc.TEST_TOKEN_HEADER!r}, "
        f"TS={m.group(1)!r}"
    )


def test_ts_event_strings_match_python() -> None:
    """All 19 `EVENT_BOT_CHALLENGE_*` strings must be byte-equal to
    the Python module's constants."""
    src = _ts_source()
    expected_constants = {
        name: getattr(bc, name)
        for name in dir(bc)
        if name.startswith("EVENT_BOT_CHALLENGE_") and name != "ALL_BOT_CHALLENGE_EVENTS"
        and isinstance(getattr(bc, name), str)
    }
    for name, value in expected_constants.items():
        m = re.search(
            rf'export\s+const\s+{name}\s*=\s*"([^"]+)"', src
        )
        assert m is not None, f"TS twin missing {name!r} export"
        assert m.group(1) == value, (
            f"{name} drift: Python={value!r}, TS={m.group(1)!r}"
        )


def test_ts_outcome_literals_match_python() -> None:
    """All 15 `OUTCOME_*` literals must be byte-equal to Python."""
    src = _ts_source()
    expected = {
        name: getattr(bc, name)
        for name in dir(bc)
        if name.startswith("OUTCOME_") and name != "ALL_OUTCOMES"
        and isinstance(getattr(bc, name), str)
    }
    for name, value in expected.items():
        m = re.search(rf'export\s+const\s+{name}\s*=\s*"([^"]+)"', src)
        assert m is not None, f"TS twin missing {name!r} export"
        assert m.group(1) == value, (
            f"{name} drift: Python={value!r}, TS={m.group(1)!r}"
        )


def test_ts_event_count_is_19() -> None:
    """8 verify outcomes + 7 bypass + 4 phase = 19 events on both sides."""
    assert len(bc.ALL_BOT_CHALLENGE_EVENTS) == 19


def test_ts_outcome_count_is_15() -> None:
    """4 verify (non-jsfail) + 7 bypass + 4 jsfail = 15 outcomes on both sides."""
    assert len(bc.ALL_OUTCOMES) == 15


def test_ts_siteverify_urls_match_python() -> None:
    """The four vendor /siteverify endpoints must agree across twins."""
    src = _ts_source()
    # Each entry of the form `[Provider.X]: "<url>",`
    pairs = re.findall(
        r"\[Provider\.(\w+)\]\s*:\s*\n?\s*\"([^\"]+)\"", src
    )
    assert pairs, "TS twin missing SITEVERIFY_URLS literal"
    ts_map = {f"Provider.{k}": v for k, v in pairs}
    expected = {f"Provider.{p.name}": bc.SITEVERIFY_URLS[p] for p in bc.Provider}
    assert ts_map == expected, (
        f"SITEVERIFY_URLS drift: Python={expected}, TS={ts_map}"
    )


def test_ts_bypass_path_prefixes_match_python() -> None:
    src = _ts_source()
    # The literal: `new Set<string>([ "p1", "p2", ... ])`
    m = re.search(
        r"BYPASS_PATH_PREFIXES[^=]*=\s*Object\.freeze\(\s*\n?\s*new\s+Set<string>\(\s*\[(.*?)\]",
        src,
        re.DOTALL,
    )
    assert m is not None, "TS twin missing BYPASS_PATH_PREFIXES literal"
    raw = re.findall(r'"([^"]+)"', m.group(1))
    ts_set = frozenset(raw)
    py_set = frozenset(bc._BYPASS_PATH_PREFIXES)
    assert ts_set == py_set, (
        f"BYPASS_PATH_PREFIXES drift: "
        f"Python={sorted(py_set)}, TS={sorted(ts_set)}"
    )


def test_ts_bypass_caller_kinds_match_python() -> None:
    src = _ts_source()
    m = re.search(
        r"BYPASS_CALLER_KINDS[^=]*=\s*Object\.freeze\(\s*\n?\s*new\s+Set<string>\(\s*\[(.*?)\]",
        src,
        re.DOTALL,
    )
    assert m is not None, "TS twin missing BYPASS_CALLER_KINDS literal"
    raw = re.findall(r'"([^"]+)"', m.group(1))
    ts_set = frozenset(raw)
    py_set = frozenset(bc._BYPASS_CALLER_KINDS)
    assert ts_set == py_set, (
        f"BYPASS_CALLER_KINDS drift: "
        f"Python={sorted(py_set)}, TS={sorted(ts_set)}"
    )


def test_ts_gdpr_strict_regions_match_python() -> None:
    """AS.3.3 — every ISO code in `GDPR_STRICT_REGIONS` must agree
    byte-for-byte across both twins. Drift here means a region's user
    silently gets the wrong vendor.
    """
    src = _ts_source()
    m = re.search(
        r"GDPR_STRICT_REGIONS[^=]*=\s*Object\.freeze\(\s*\n?\s*new\s+Set<string>\(\s*\[(.*?)\]",
        src,
        re.DOTALL,
    )
    assert m is not None, "TS twin missing GDPR_STRICT_REGIONS literal"
    raw = re.findall(r'"([^"]+)"', m.group(1))
    ts_set = frozenset(raw)
    py_set = frozenset(bc.GDPR_STRICT_REGIONS)
    assert ts_set == py_set, (
        f"GDPR_STRICT_REGIONS drift: "
        f"Python={sorted(py_set)}, TS={sorted(ts_set)}"
    )
    # Belt-and-braces: same count.
    assert len(ts_set) == 32, (
        f"GDPR_STRICT_REGIONS count drift: TS={len(ts_set)}, expected 32"
    )


def test_ts_ecosystem_hint_google_matches_python() -> None:
    """The single canonical ecosystem hint string must agree byte-for-
    byte; case-insensitive matching on both sides relies on this."""
    src = _ts_source()
    m = re.search(
        r'export\s+const\s+ECOSYSTEM_HINT_GOOGLE\s*=\s*"([^"]+)"', src
    )
    assert m is not None, "TS twin missing ECOSYSTEM_HINT_GOOGLE export"
    assert m.group(1) == bc.ECOSYSTEM_HINT_GOOGLE, (
        f"ECOSYSTEM_HINT_GOOGLE drift: "
        f"Python={bc.ECOSYSTEM_HINT_GOOGLE!r}, TS={m.group(1)!r}"
    )


def test_ts_declares_pick_provider_and_helpers() -> None:
    """AS.3.3 — TS twin must declare `pickProvider` + `isGdprStrictRegion`
    exports. Catches partial-port regressions where Python ships the
    heuristic but the TS twin still has the old placeholder."""
    src = _ts_source()
    assert re.search(
        r"export\s+function\s+pickProvider\s*\(", src
    ), "TS twin missing pickProvider export"
    assert re.search(
        r"export\s+function\s+isGdprStrictRegion\s*\(", src
    ), "TS twin missing isGdprStrictRegion export"


def test_ts_declares_three_typed_errors() -> None:
    """All three bot-challenge error classes must be declared on the TS
    side with the same names. Catches partial-port regressions."""
    src = _ts_source()
    expected = ["BotChallengeError", "ProviderConfigError", "InvalidProviderError"]
    for cls in expected:
        assert re.search(rf"export\s+class\s+{cls}\b", src), (
            f"TS twin missing typed error: {cls!r}"
        )


def test_ts_bot_challenge_rejected_code_matches_python() -> None:
    """AS.3.4 — the canonical front-end error code must agree byte-for-
    byte across the Python and TS twin. The browser UI keys on this
    string to render its retry CTA + 'contact admin' copy; drift would
    silently break the UX contract on one side. AS.0.5 §3 row 116."""
    src = _ts_source()
    m = re.search(
        r'export\s+const\s+BOT_CHALLENGE_REJECTED_CODE\s*=\s*"([^"]+)"', src
    )
    assert m is not None, "TS twin missing BOT_CHALLENGE_REJECTED_CODE export"
    assert m.group(1) == bc.BOT_CHALLENGE_REJECTED_CODE, (
        f"BOT_CHALLENGE_REJECTED_CODE drift: "
        f"Python={bc.BOT_CHALLENGE_REJECTED_CODE!r}, TS={m.group(1)!r}"
    )


def test_ts_bot_challenge_rejected_http_status_matches_python() -> None:
    """AS.3.4 — the canonical HTTP status code (429) must agree int-for-
    int across the Python and TS twin. AS.0.5 §3 row 116."""
    src = _ts_source()
    m = re.search(
        r"export\s+const\s+BOT_CHALLENGE_REJECTED_HTTP_STATUS\s*=\s*(\d+)", src
    )
    assert m is not None, "TS twin missing BOT_CHALLENGE_REJECTED_HTTP_STATUS export"
    assert int(m.group(1)) == bc.BOT_CHALLENGE_REJECTED_HTTP_STATUS, (
        f"BOT_CHALLENGE_REJECTED_HTTP_STATUS drift: "
        f"Python={bc.BOT_CHALLENGE_REJECTED_HTTP_STATUS}, TS={m.group(1)}"
    )


def test_ts_declares_bot_challenge_rejected_class() -> None:
    """AS.3.4 — TS twin must declare `BotChallengeRejected` as a class
    that extends `BotChallengeError` so callers can `catch (e instanceof
    BotChallengeError)` once and handle reject + transport / config
    errors in the same branch."""
    src = _ts_source()
    assert re.search(
        r"export\s+class\s+BotChallengeRejected\s+extends\s+BotChallengeError\b",
        src,
    ), "TS twin missing `BotChallengeRejected extends BotChallengeError`"


def test_ts_declares_should_reject_and_verify_and_enforce() -> None:
    """AS.3.4 — TS twin must export the two helper functions
    (`shouldReject`, `verifyAndEnforce`) so generated apps can wire
    their forms onto the same single-call enforce primitive without
    re-implementing the `!result.allow` semantics per route."""
    src = _ts_source()
    assert re.search(
        r"export\s+function\s+shouldReject\s*\(", src
    ), "TS twin missing shouldReject export"
    assert re.search(
        r"export\s+async\s+function\s+verifyAndEnforce\s*\(", src
    ), "TS twin missing verifyAndEnforce export"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Behavioural parity via Node subprocess
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Each fixture: { kind: <kind>, ...inputs, expect: ... }
#
# `kind` selects which surface to drive:
#   - "evaluate_bypass"    → both sides call evaluate_bypass(ctx)
#   - "classify_outcome"   → both sides call classify_outcome(parsed_resp, opts)
#   - "passthrough"        → both sides call passthrough(reason)
#   - "event_for_outcome"  → both sides call event_for_outcome(name)
BEHAVIOUR_FIXTURES: Mapping[str, dict[str, Any]] = {
    # ── Bypass precedence A > C > B > D ──
    "bypass_a_apikey_only": {
        "kind": "evaluate_bypass",
        "ctx": {"caller_kind": "apikey_omni", "api_key_id": "k1", "api_key_prefix": "ak_"},
        "expect_outcome": "bypass_apikey",
        "expect_also_matched": [],
    },
    "bypass_a_wins_over_all": {
        "kind": "evaluate_bypass",
        "ctx": {
            "caller_kind": "apikey_omni",
            "api_key_id": "k1",
            "test_token_header_value": "x" * 40,
            "test_token_expected": "x" * 40,
            "client_ip": "10.0.0.5",
            "tenant_ip_allowlist": ["10.0.0.0/24"],
            "path": "/api/v1/healthz",
        },
        "expect_outcome": "bypass_apikey",
        "expect_also_matched": ["test_token", "ip_allowlist", "path"],
    },
    "bypass_c_test_token": {
        "kind": "evaluate_bypass",
        "ctx": {
            "test_token_header_value": "y" * 40,
            "test_token_expected": "y" * 40,
            "tenant_id": "t-1",
        },
        "expect_outcome": "bypass_test_token",
        "expect_also_matched": [],
    },
    "bypass_c_wins_over_b_d": {
        "kind": "evaluate_bypass",
        "ctx": {
            "test_token_header_value": "y" * 40,
            "test_token_expected": "y" * 40,
            "client_ip": "10.0.0.5",
            "tenant_ip_allowlist": ["10.0.0.0/24"],
            "path": "/api/v1/healthz",
        },
        "expect_outcome": "bypass_test_token",
        "expect_also_matched": ["ip_allowlist", "path"],
    },
    "bypass_b_ipv4": {
        "kind": "evaluate_bypass",
        "ctx": {
            "client_ip": "192.168.1.42",
            "tenant_ip_allowlist": ["192.168.1.0/24"],
        },
        "expect_outcome": "bypass_ip_allowlist",
        "expect_also_matched": [],
    },
    "bypass_b_wins_over_d": {
        "kind": "evaluate_bypass",
        "ctx": {
            "client_ip": "10.0.0.5",
            "tenant_ip_allowlist": ["10.0.0.0/24"],
            "path": "/api/v1/healthz",
        },
        "expect_outcome": "bypass_ip_allowlist",
        "expect_also_matched": ["path"],
    },
    "bypass_b_ipv6": {
        "kind": "evaluate_bypass",
        "ctx": {
            "client_ip": "2001:db8::1",
            "tenant_ip_allowlist": ["2001:db8::/32"],
        },
        "expect_outcome": "bypass_ip_allowlist",
        "expect_also_matched": [],
    },
    "bypass_b_wide_cidr_flagged": {
        "kind": "evaluate_bypass",
        "ctx": {
            "client_ip": "10.0.0.5",
            "tenant_ip_allowlist": ["10.0.0.0/8"],
        },
        "expect_outcome": "bypass_ip_allowlist",
        "expect_also_matched": [],
    },
    "bypass_b_corrupt_entry_skipped": {
        "kind": "evaluate_bypass",
        "ctx": {
            "client_ip": "10.0.0.5",
            "tenant_ip_allowlist": ["not-a-cidr", "10.0.0.0/24"],
        },
        "expect_outcome": "bypass_ip_allowlist",
        "expect_also_matched": [],
    },
    "bypass_d_path_probe": {
        "kind": "evaluate_bypass",
        "ctx": {"path": "/api/v1/healthz"},
        "expect_outcome": "bypass_probe",
        "expect_also_matched": [],
    },
    "bypass_d_path_bootstrap": {
        "kind": "evaluate_bypass",
        "ctx": {"path": "/api/v1/bootstrap/init"},
        "expect_outcome": "bypass_bootstrap",
        "expect_also_matched": [],
    },
    "bypass_d_path_webhook": {
        "kind": "evaluate_bypass",
        "ctx": {"path": "/api/v1/webhooks/github"},
        "expect_outcome": "bypass_webhook",
        "expect_also_matched": [],
    },
    "bypass_d_path_chatops": {
        "kind": "evaluate_bypass",
        "ctx": {"path": "/api/v1/chatops/webhook/slack"},
        "expect_outcome": "bypass_chatops",
        "expect_also_matched": [],
    },
    "bypass_d_oidc_callback": {
        "kind": "evaluate_bypass",
        "ctx": {"path": "/api/v1/auth/oidc/callback"},
        "expect_outcome": "bypass_probe",
        "expect_also_matched": [],
    },
    "bypass_none": {
        "kind": "evaluate_bypass",
        "ctx": {"path": "/api/v1/login"},
        "expect_outcome": None,
        "expect_also_matched": [],
    },
    "bypass_token_too_short_rejected": {
        "kind": "evaluate_bypass",
        "ctx": {
            "test_token_header_value": "short",
            "test_token_expected": "short",
        },
        "expect_outcome": None,
        "expect_also_matched": [],
    },
    "bypass_token_mismatch_rejected": {
        "kind": "evaluate_bypass",
        "ctx": {
            "test_token_header_value": "x" * 40,
            "test_token_expected": "y" * 40,
        },
        "expect_outcome": None,
        "expect_also_matched": [],
    },
    "bypass_unknown_caller_kind_no_a": {
        "kind": "evaluate_bypass",
        "ctx": {"caller_kind": "anonymous"},
        "expect_outcome": None,
        "expect_also_matched": [],
    },
    # ── classify_outcome phase matrix ──
    "classify_pass_p1": {
        "kind": "classify_outcome",
        "response": {"success": True, "score": 0.9, "action": None, "hostname": None,
                     "errorCodes": []},
        "provider": "turnstile",
        "phase": 1,
        "expect_outcome": "pass",
        "expect_allow": True,
    },
    "classify_pass_p3": {
        "kind": "classify_outcome",
        "response": {"success": True, "score": 0.8, "action": None, "hostname": None,
                     "errorCodes": []},
        "provider": "recaptcha_v3",
        "phase": 3,
        "expect_outcome": "pass",
        "expect_allow": True,
    },
    "classify_low_p1_failopen": {
        "kind": "classify_outcome",
        "response": {"success": True, "score": 0.3, "action": None, "hostname": None,
                     "errorCodes": []},
        "provider": "turnstile",
        "phase": 1,
        "expect_outcome": "unverified_lowscore",
        "expect_allow": True,
    },
    "classify_low_p2_failopen": {
        "kind": "classify_outcome",
        "response": {"success": True, "score": 0.4, "action": None, "hostname": None,
                     "errorCodes": []},
        "provider": "recaptcha_v3",
        "phase": 2,
        "expect_outcome": "unverified_lowscore",
        "expect_allow": True,
    },
    "classify_low_p3_failclosed": {
        "kind": "classify_outcome",
        "response": {"success": True, "score": 0.2, "action": None, "hostname": None,
                     "errorCodes": []},
        "provider": "turnstile",
        "phase": 3,
        "expect_outcome": "blocked_lowscore",
        "expect_allow": False,
    },
    "classify_servererr_p1": {
        "kind": "classify_outcome",
        "response": {"success": False, "score": 0.0, "action": None, "hostname": None,
                     "errorCodes": ["timeout-or-duplicate"]},
        "provider": "turnstile",
        "phase": 1,
        "expect_outcome": "unverified_servererr",
        "expect_allow": True,
        "expect_error_kind": "timeout",
    },
    "classify_servererr_p3_still_failopen": {
        "kind": "classify_outcome",
        "response": {"success": False, "score": 0.0, "action": None, "hostname": None,
                     "errorCodes": ["http-503"]},
        "provider": "hcaptcha",
        "phase": 3,
        "expect_outcome": "unverified_servererr",
        "expect_allow": True,
        "expect_error_kind": "5xx",
    },
    "classify_servererr_invalid_token": {
        "kind": "classify_outcome",
        "response": {"success": False, "score": 0.0, "action": None, "hostname": None,
                     "errorCodes": ["invalid-input-response"]},
        "provider": "recaptcha_v2",
        "phase": 2,
        "expect_outcome": "unverified_servererr",
        "expect_allow": True,
        "expect_error_kind": "4xx_invalid_token",
    },
    "classify_threshold_boundary_at_0_5": {
        # score == threshold (0.5) → must pass per `>=` invariant.
        "kind": "classify_outcome",
        "response": {"success": True, "score": 0.5, "action": None, "hostname": None,
                     "errorCodes": []},
        "provider": "turnstile",
        "phase": 3,
        "expect_outcome": "pass",
        "expect_allow": True,
    },
    "classify_threshold_just_below_at_p3": {
        "kind": "classify_outcome",
        "response": {"success": True, "score": 0.499, "action": None, "hostname": None,
                     "errorCodes": []},
        "provider": "turnstile",
        "phase": 3,
        "expect_outcome": "blocked_lowscore",
        "expect_allow": False,
    },
    # ── passthrough ──
    "passthrough_default_reason": {
        "kind": "passthrough",
        "reason": "knob_off",
        "expect_outcome": "pass",
        "expect_allow": True,
    },
    "passthrough_custom_reason": {
        "kind": "passthrough",
        "reason": "dev_mode",
        "expect_outcome": "pass",
        "expect_allow": True,
    },
    # ── pick_provider AS.3.3 region + ecosystem heuristic ──
    # Heuristic precedence (highest first):
    #   1. override (caller pin)
    #   2. GDPR strict region → hCaptcha
    #   3. Google ecosystem hint → reCAPTCHA v3
    #   4. default (Turnstile by default)
    "pick_default_no_hints": {
        "kind": "pick_provider",
        "opts": {},
        "expect_provider": "turnstile",
    },
    "pick_default_caller_supplied": {
        "kind": "pick_provider",
        "opts": {"default": "hcaptcha"},
        "expect_provider": "hcaptcha",
    },
    "pick_override_wins_over_all": {
        "kind": "pick_provider",
        "opts": {
            "override": "turnstile",
            "region": "DE",
            "ecosystem_hints": ["google"],
        },
        "expect_provider": "turnstile",
    },
    "pick_gdpr_region_de": {
        "kind": "pick_provider",
        "opts": {"region": "DE"},
        "expect_provider": "hcaptcha",
    },
    "pick_gdpr_region_fr_lowercase": {
        "kind": "pick_provider",
        "opts": {"region": "fr"},
        "expect_provider": "hcaptcha",
    },
    "pick_gdpr_region_gb": {
        "kind": "pick_provider",
        "opts": {"region": "GB"},
        "expect_provider": "hcaptcha",
    },
    "pick_gdpr_region_ch": {
        "kind": "pick_provider",
        "opts": {"region": "CH"},
        "expect_provider": "hcaptcha",
    },
    "pick_non_gdpr_region_us": {
        "kind": "pick_provider",
        "opts": {"region": "US"},
        "expect_provider": "turnstile",
    },
    "pick_non_gdpr_region_jp": {
        "kind": "pick_provider",
        "opts": {"region": "JP"},
        "expect_provider": "turnstile",
    },
    "pick_empty_region": {
        "kind": "pick_provider",
        "opts": {"region": ""},
        "expect_provider": "turnstile",
    },
    "pick_google_ecosystem": {
        "kind": "pick_provider",
        "opts": {"ecosystem_hints": ["google"]},
        "expect_provider": "recaptcha_v3",
    },
    "pick_google_ecosystem_uppercase": {
        "kind": "pick_provider",
        "opts": {"ecosystem_hints": ["GOOGLE"]},
        "expect_provider": "recaptcha_v3",
    },
    "pick_google_ecosystem_among_others": {
        "kind": "pick_provider",
        "opts": {"ecosystem_hints": ["microsoft", "apple", "google"]},
        "expect_provider": "recaptcha_v3",
    },
    "pick_unknown_ecosystem_falls_through": {
        "kind": "pick_provider",
        "opts": {"ecosystem_hints": ["microsoft", "apple"]},
        "expect_provider": "turnstile",
    },
    "pick_gdpr_region_wins_over_google_ecosystem": {
        # Privacy > UX continuity — the EU-strict caller still gets
        # hCaptcha even with a Google ecosystem signal.
        "kind": "pick_provider",
        "opts": {"region": "FR", "ecosystem_hints": ["google"]},
        "expect_provider": "hcaptcha",
    },
    "pick_default_with_unknown_hints": {
        "kind": "pick_provider",
        "opts": {
            "default": "recaptcha_v2",
            "region": "US",
            "ecosystem_hints": ["microsoft"],
        },
        "expect_provider": "recaptcha_v2",
    },
    # ── AS.3.4 should_reject pure predicate ──
    # Cross-twin parity: both sides return the same boolean for every
    # outcome in `ALL_OUTCOMES`. The fixture passes a synthesised
    # `BotChallengeResult`-shaped dict (the predicate keys only on `allow`,
    # but we pass full result shape so the TS side can consume the same
    # dict literal as Python's frozen dataclass kwargs).
    "should_reject_blocked_lowscore": {
        "kind": "should_reject",
        "result": {"outcome": "blocked_lowscore", "allow": False, "score": 0.1,
                   "provider": "recaptcha_v3"},
        "expect_reject": True,
    },
    "should_reject_jsfail_honeypot_fail": {
        "kind": "should_reject",
        "result": {"outcome": "jsfail_honeypot_fail", "allow": False, "score": 0.0,
                   "provider": None},
        "expect_reject": True,
    },
    "should_reject_pass_no": {
        "kind": "should_reject",
        "result": {"outcome": "pass", "allow": True, "score": 0.9,
                   "provider": "turnstile"},
        "expect_reject": False,
    },
    "should_reject_unverified_lowscore_no": {
        "kind": "should_reject",
        "result": {"outcome": "unverified_lowscore", "allow": True, "score": 0.2,
                   "provider": "recaptcha_v3"},
        "expect_reject": False,
    },
    "should_reject_unverified_servererr_no": {
        "kind": "should_reject",
        "result": {"outcome": "unverified_servererr", "allow": True, "score": 0.0,
                   "provider": "turnstile"},
        "expect_reject": False,
    },
    "should_reject_bypass_apikey_no": {
        "kind": "should_reject",
        "result": {"outcome": "bypass_apikey", "allow": True, "score": 1.0,
                   "provider": None},
        "expect_reject": False,
    },
    "should_reject_bypass_ip_allowlist_no": {
        "kind": "should_reject",
        "result": {"outcome": "bypass_ip_allowlist", "allow": True, "score": 1.0,
                   "provider": None},
        "expect_reject": False,
    },
    "should_reject_jsfail_honeypot_pass_no": {
        "kind": "should_reject",
        "result": {"outcome": "jsfail_honeypot_pass", "allow": True, "score": 1.0,
                   "provider": None},
        "expect_reject": False,
    },
    "should_reject_jsfail_fallback_recaptcha_no": {
        "kind": "should_reject",
        "result": {"outcome": "jsfail_fallback_recaptcha", "allow": True, "score": 0.85,
                   "provider": "recaptcha_v3"},
        "expect_reject": False,
    },
    # ── event_for_outcome lookup table ──
    "lookup_pass": {
        "kind": "event_for_outcome",
        "outcome": "pass",
        "expect_event": "bot_challenge.pass",
    },
    "lookup_blocked_lowscore": {
        "kind": "event_for_outcome",
        "outcome": "blocked_lowscore",
        "expect_event": "bot_challenge.blocked_lowscore",
    },
    "lookup_bypass_apikey": {
        "kind": "event_for_outcome",
        "outcome": "bypass_apikey",
        "expect_event": "bot_challenge.bypass_apikey",
    },
    "lookup_bypass_test_token": {
        "kind": "event_for_outcome",
        "outcome": "bypass_test_token",
        "expect_event": "bot_challenge.bypass_test_token",
    },
    "lookup_bypass_ip_allowlist": {
        "kind": "event_for_outcome",
        "outcome": "bypass_ip_allowlist",
        "expect_event": "bot_challenge.bypass_ip_allowlist",
    },
    "lookup_jsfail_honeypot_pass": {
        "kind": "event_for_outcome",
        "outcome": "jsfail_honeypot_pass",
        "expect_event": "bot_challenge.jsfail_honeypot_pass",
    },
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Python-side driver — exercises every fixture against the Python lib
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _python_run_fixture(fx: dict[str, Any]) -> dict[str, Any]:
    kind = fx["kind"]
    if kind == "evaluate_bypass":
        ctx_raw = fx["ctx"]
        ctx = bc.BypassContext(
            path=ctx_raw.get("path"),
            caller_kind=ctx_raw.get("caller_kind"),
            api_key_id=ctx_raw.get("api_key_id"),
            api_key_prefix=ctx_raw.get("api_key_prefix"),
            client_ip=ctx_raw.get("client_ip"),
            tenant_ip_allowlist=tuple(ctx_raw.get("tenant_ip_allowlist") or ()),
            test_token_header_value=ctx_raw.get("test_token_header_value"),
            test_token_expected=ctx_raw.get("test_token_expected"),
            tenant_id=ctx_raw.get("tenant_id"),
            widget_action=ctx_raw.get("widget_action"),
        )
        result = bc.evaluate_bypass(ctx)
        if result is None:
            return {"outcome": None, "alsoMatched": []}
        return {
            "outcome": result.outcome,
            "alsoMatched": list(result.also_matched),
            "auditMetadataKeys": sorted(result.audit_metadata.keys()),
        }
    if kind == "classify_outcome":
        provider = bc.Provider(fx["provider"])
        resp = bc.ProviderResponse(
            success=fx["response"]["success"],
            score=fx["response"]["score"],
            action=fx["response"].get("action"),
            hostname=fx["response"].get("hostname"),
            raw={},
            error_codes=tuple(fx["response"].get("errorCodes") or ()),
        )
        result = bc.classify_outcome(resp, provider=provider, phase=fx["phase"])
        out = {
            "outcome": result.outcome,
            "allow": result.allow,
            "score": result.score,
            "auditEvent": result.audit_event,
            "provider": result.provider.value if result.provider else None,
        }
        if "error_kind" in result.audit_metadata:
            out["errorKind"] = result.audit_metadata["error_kind"]
        return out
    if kind == "passthrough":
        result = bc.passthrough(reason=fx["reason"])
        return {
            "outcome": result.outcome,
            "allow": result.allow,
            "score": result.score,
            "auditEvent": result.audit_event,
            "passthroughReason": result.audit_metadata.get("passthrough_reason"),
        }
    if kind == "event_for_outcome":
        return {"event": bc.event_for_outcome(fx["outcome"])}
    if kind == "should_reject":
        raw = fx["result"]
        provider = bc.Provider(raw["provider"]) if raw.get("provider") else None
        result = bc.BotChallengeResult(
            outcome=raw["outcome"],
            allow=raw["allow"],
            score=raw["score"],
            provider=provider,
            audit_event=bc.event_for_outcome(raw["outcome"]),
            audit_metadata={},
            error=None,
        )
        return {"reject": bool(bc.should_reject(result))}
    if kind == "pick_provider":
        opts_raw = fx["opts"]
        kwargs: dict[str, Any] = {}
        if "default" in opts_raw and opts_raw["default"] is not None:
            kwargs["default"] = bc.Provider(opts_raw["default"])
        if "override" in opts_raw and opts_raw["override"] is not None:
            kwargs["override"] = bc.Provider(opts_raw["override"])
        if "region" in opts_raw:
            kwargs["region"] = opts_raw["region"]
        if "ecosystem_hints" in opts_raw:
            kwargs["ecosystem_hints"] = tuple(opts_raw["ecosystem_hints"])
        return {"provider": bc.pick_provider(**kwargs).value}
    raise AssertionError(f"unknown fixture kind: {kind!r}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Node driver — exercises every fixture against the TS twin, returns JSON
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_NODE_DRIVER = """
import * as bc from __TWIN_PATH__
import { readFileSync } from "node:fs"

const fixtures = JSON.parse(readFileSync(0, "utf-8"))
const out = {}

// snake_case (Python) ↔ camelCase (TS) for BypassContext fields.
const SNAKE_TO_CAMEL = {
  caller_kind: "callerKind",
  api_key_id: "apiKeyId",
  api_key_prefix: "apiKeyPrefix",
  client_ip: "clientIp",
  tenant_ip_allowlist: "tenantIpAllowlist",
  test_token_header_value: "testTokenHeaderValue",
  test_token_expected: "testTokenExpected",
  tenant_id: "tenantId",
  widget_action: "widgetAction",
}
function ctxToCamel(snake) {
  const camel = {}
  for (const [k, v] of Object.entries(snake)) {
    const ck = SNAKE_TO_CAMEL[k] ?? k
    camel[ck] = v
  }
  return camel
}

for (const [key, fx] of Object.entries(fixtures)) {
  try {
    if (fx.kind === "evaluate_bypass") {
      const ctx = ctxToCamel(fx.ctx)
      const r = bc.evaluateBypass(ctx)
      if (r === null) {
        out[key] = { outcome: null, alsoMatched: [] }
      } else {
        out[key] = {
          outcome: r.outcome,
          alsoMatched: [...r.alsoMatched],
          auditMetadataKeys: Object.keys(r.auditMetadata).sort(),
        }
      }
    } else if (fx.kind === "classify_outcome") {
      const resp = {
        success: fx.response.success,
        score: fx.response.score,
        action: fx.response.action ?? null,
        hostname: fx.response.hostname ?? null,
        raw: {},
        errorCodes: fx.response.errorCodes ?? [],
      }
      const r = bc.classifyOutcome(resp, { provider: fx.provider, phase: fx.phase })
      const o = {
        outcome: r.outcome,
        allow: r.allow,
        score: r.score,
        auditEvent: r.auditEvent,
        provider: r.provider,
      }
      if ("error_kind" in r.auditMetadata) o.errorKind = r.auditMetadata.error_kind
      out[key] = o
    } else if (fx.kind === "passthrough") {
      const r = bc.passthrough(fx.reason)
      out[key] = {
        outcome: r.outcome,
        allow: r.allow,
        score: r.score,
        auditEvent: r.auditEvent,
        passthroughReason: r.auditMetadata.passthrough_reason,
      }
    } else if (fx.kind === "event_for_outcome") {
      out[key] = { event: bc.eventForOutcome(fx.outcome) }
    } else if (fx.kind === "pick_provider") {
      const o = fx.opts ?? {}
      const tsOpts = {}
      if (o.default !== undefined && o.default !== null) tsOpts.default = o.default
      if (o.override !== undefined && o.override !== null) tsOpts.override = o.override
      if ("region" in o) tsOpts.region = o.region
      if ("ecosystem_hints" in o) tsOpts.ecosystemHints = o.ecosystem_hints
      out[key] = { provider: bc.pickProvider(tsOpts) }
    } else if (fx.kind === "should_reject") {
      const r = fx.result
      const tsResult = {
        outcome: r.outcome,
        allow: r.allow,
        score: r.score,
        provider: r.provider ?? null,
        auditEvent: bc.eventForOutcome(r.outcome),
        auditMetadata: {},
        error: null,
      }
      out[key] = { reject: Boolean(bc.shouldReject(tsResult)) }
    } else {
      out[key] = { __error: `unknown kind ${fx.kind}` }
    }
  } catch (e) {
    out[key] = {
      __error: (e && e.constructor && e.constructor.name) || "Error",
      __message: String((e && e.message) || e),
    }
  }
}

process.stdout.write(JSON.stringify(out))
"""


def _run_ts_driver(fixtures: Mapping[str, Any]) -> dict[str, Any]:
    twin_path_literal = json.dumps(str(_TS_TWIN_PATH))
    driver_src = _NODE_DRIVER.replace("__TWIN_PATH__", twin_path_literal)

    cmd = [
        "node",
        "--no-warnings",
        "--experimental-strip-types",
        "--input-type=module",
        "--eval",
        driver_src,
    ]

    payload = json.dumps(dict(fixtures))
    env = dict(os.environ)
    env["TZ"] = "UTC"

    r = subprocess.run(
        cmd,
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Node TS driver exited {r.returncode}\n"
            f"stdout={r.stdout!r}\n"
            f"stderr={r.stderr!r}"
        )
    return json.loads(r.stdout)


@pytest.fixture(scope="session")
def ts_bot_challenge_results() -> dict[str, Any]:
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    if not _node_supports_strip_types():
        pytest.skip(
            "node ≥22 with --experimental-strip-types not available; "
            "TS twin behaviour cannot be exercised"
        )
    return _run_ts_driver(BEHAVIOUR_FIXTURES)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2a — Per-fixture behavioural parity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "name", sorted(BEHAVIOUR_FIXTURES.keys()), ids=lambda n: n
)
def test_behaviour_parity_python_ts(
    name: str, ts_bot_challenge_results: dict[str, Any]
) -> None:
    """For every fixture, both sides must produce the same outcome."""
    fx = BEHAVIOUR_FIXTURES[name]
    py = _python_run_fixture(fx)
    ts = ts_bot_challenge_results[name]
    assert "__error" not in ts, (
        f"TS driver raised on fixture {name!r}: {ts}"
    )

    if fx["kind"] == "evaluate_bypass":
        assert py["outcome"] == fx["expect_outcome"], (
            f"Python evaluate_bypass {name!r}: got {py['outcome']!r}, "
            f"expected {fx['expect_outcome']!r}"
        )
        assert ts["outcome"] == fx["expect_outcome"], (
            f"TS evaluate_bypass {name!r}: got {ts['outcome']!r}, "
            f"expected {fx['expect_outcome']!r}"
        )
        assert py["alsoMatched"] == fx["expect_also_matched"], (
            f"Python alsoMatched drift on {name!r}: {py['alsoMatched']}"
        )
        assert ts["alsoMatched"] == fx["expect_also_matched"], (
            f"TS alsoMatched drift on {name!r}: {ts['alsoMatched']}"
        )
        if py["outcome"] is not None:
            # Same metadata keys present on both sides.
            assert py["auditMetadataKeys"] == ts["auditMetadataKeys"], (
                f"audit metadata key drift on {name!r}: "
                f"Python={py['auditMetadataKeys']}, TS={ts['auditMetadataKeys']}"
            )
        return

    if fx["kind"] == "classify_outcome":
        for field in ("outcome", "allow", "auditEvent", "provider"):
            assert py[field] == ts[field] == _expected_field(fx, field), (
                f"{field} drift on {name!r}: "
                f"Python={py[field]!r}, TS={ts[field]!r}, "
                f"expected={_expected_field(fx, field)!r}"
            )
        # Score is float — exact equality (same source value).
        assert py["score"] == ts["score"] == fx["response"]["score"]
        if "expect_error_kind" in fx:
            assert py.get("errorKind") == ts.get("errorKind") == fx["expect_error_kind"]
        return

    if fx["kind"] == "passthrough":
        for field in ("outcome", "allow", "score", "auditEvent", "passthroughReason"):
            assert py[field] == ts[field], (
                f"{field} drift on {name!r}: Python={py[field]!r}, TS={ts[field]!r}"
            )
        assert py["outcome"] == fx["expect_outcome"]
        assert py["passthroughReason"] == fx["reason"]
        return

    if fx["kind"] == "event_for_outcome":
        assert py["event"] == ts["event"] == fx["expect_event"], (
            f"event_for_outcome {name!r}: "
            f"Python={py['event']!r}, TS={ts['event']!r}, "
            f"expected={fx['expect_event']!r}"
        )
        return

    if fx["kind"] == "pick_provider":
        assert py["provider"] == ts["provider"] == fx["expect_provider"], (
            f"pick_provider {name!r}: "
            f"Python={py['provider']!r}, TS={ts['provider']!r}, "
            f"expected={fx['expect_provider']!r}"
        )
        return

    if fx["kind"] == "should_reject":
        assert py["reject"] == ts["reject"] == fx["expect_reject"], (
            f"should_reject {name!r}: "
            f"Python={py['reject']!r}, TS={ts['reject']!r}, "
            f"expected={fx['expect_reject']!r}"
        )
        return

    raise AssertionError(f"unhandled fixture kind: {fx['kind']!r}")


def _expected_field(fx: dict[str, Any], field: str) -> Any:
    """Look up the expected value for a `classify_outcome` fixture by
    field name, derived from the fixture's `expect_*` keys + provider /
    audit-event lookup."""
    if field == "outcome":
        return fx["expect_outcome"]
    if field == "allow":
        return fx["expect_allow"]
    if field == "auditEvent":
        return bc.event_for_outcome(fx["expect_outcome"])
    if field == "provider":
        # `pass` etc. carry the provider attribution; no bypass branch.
        return fx["provider"]
    raise KeyError(field)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2b — Aggregate SHA-256 oracle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _normalise_outcome(d: Mapping[str, Any]) -> dict[str, Any]:
    """Project each fixture result to the comparable shape (drop fields
    that legitimately differ — e.g. cidr_match string only present on
    bypass_b paths). Both sides go through this projection before
    SHA-256 oracle comparison.

    JSON-encoding parity note: ``json.dumps(0.0)`` is ``"0.0"`` but
    ``json.loads("0")`` returns Python ``int`` 0 — meaning the TS side's
    integer-valued floats (``score: 0`` from `JSON.stringify`) round-trip
    as ``int`` on the Python side and then re-serialise to ``"0"``,
    whereas the Python lib's ``score=0.0`` serialises to ``"0.0"``.
    Force-coerce ``score`` to ``float`` so both sides produce the same
    decimal-formatted string under ``sort_keys=True`` JSON.
    """
    if "__error" in d:
        return {"__error": d["__error"]}
    out: dict[str, Any] = {}
    for key in ("outcome", "allow", "score", "auditEvent", "provider",
                "passthroughReason", "event", "errorKind", "reject"):
        if key in d:
            out[key] = d[key]
    # `pick_provider` returns {"provider": "<name>"} (no `outcome`),
    # so the loop above already covers it via the shared `provider` key.
    if "score" in out and out["score"] is not None:
        out["score"] = float(out["score"])
    if "alsoMatched" in d:
        out["alsoMatched"] = list(d["alsoMatched"])
    if "auditMetadataKeys" in d:
        out["auditMetadataKeys"] = list(d["auditMetadataKeys"])
    return out


def test_aggregate_sha256_parity_python_ts(
    ts_bot_challenge_results: dict[str, Any]
) -> None:
    """One hash over every fixture's normalised outcome.

    Catches the "many-tiny-drifts" failure mode that per-fixture tests
    can't summarise in a single error message.
    """
    py_blob: dict[str, Any] = {}
    ts_blob: dict[str, Any] = {}
    for key in sorted(BEHAVIOUR_FIXTURES.keys()):
        py_blob[key] = _normalise_outcome(_python_run_fixture(BEHAVIOUR_FIXTURES[key]))
        ts_blob[key] = _normalise_outcome(ts_bot_challenge_results[key])

    py_hash = hashlib.sha256(
        json.dumps(py_blob, sort_keys=True).encode("utf-8")
    ).hexdigest()
    ts_hash = hashlib.sha256(
        json.dumps(ts_blob, sort_keys=True).encode("utf-8")
    ).hexdigest()

    assert py_hash == ts_hash, (
        "aggregate bot-challenge behaviour drift between Python and TS twin\n"
        f"  Python SHA-256: {py_hash}\n"
        f"  TS     SHA-256: {ts_hash}\n"
        "  (run per-fixture tests for which scenario drifted)\n"
        f"  Python blob: {json.dumps(py_blob, sort_keys=True, indent=2)[:600]}...\n"
        f"  TS     blob: {json.dumps(ts_blob, sort_keys=True, indent=2)[:600]}..."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Coverage guard (each axis / provider / outcome exercised)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_every_provider_has_a_classify_fixture() -> None:
    """Every provider in the enum must drive at least one
    `classify_outcome` fixture."""
    covered = {
        fx["provider"]
        for fx in BEHAVIOUR_FIXTURES.values()
        if fx["kind"] == "classify_outcome"
    }
    expected = {p.value for p in bc.Provider}
    missing = expected - covered
    assert not missing, (
        f"AS.3.2 drift guard missing classify fixture for: {sorted(missing)}"
    )


def test_every_bypass_axis_exercised() -> None:
    """All four axes (A, C, B, D) and their precedence resolution must
    be exercised."""
    outcomes = {
        fx["expect_outcome"]
        for fx in BEHAVIOUR_FIXTURES.values()
        if fx["kind"] == "evaluate_bypass" and fx["expect_outcome"] is not None
    }
    required_axes = {
        "bypass_apikey",  # A
        "bypass_test_token",  # C
        "bypass_ip_allowlist",  # B
        "bypass_probe",  # D
    }
    missing = required_axes - outcomes
    assert not missing, (
        f"AS.3.2 drift guard missing bypass-axis fixture for: {sorted(missing)}"
    )


def test_every_pick_provider_axis_exercised() -> None:
    """AS.3.3 heuristic has 4 axes; every one must be exercised by at
    least one fixture or the cross-twin parity guard goes blind to that
    axis. Drift guard's own drift guard."""
    pick_fxs = [fx for fx in BEHAVIOUR_FIXTURES.values() if fx["kind"] == "pick_provider"]
    assert pick_fxs, "no pick_provider fixtures present — AS.3.3 parity blind"

    # Axis 1 — override.
    assert any("override" in fx["opts"] for fx in pick_fxs), (
        "no override-axis fixture for pick_provider"
    )
    # Axis 2 — GDPR strict region (positive case).
    assert any(
        fx["expect_provider"] == "hcaptcha"
        and fx["opts"].get("region")
        for fx in pick_fxs
    ), "no GDPR-region fixture for pick_provider"
    # Axis 3 — Google ecosystem (positive case).
    assert any(
        fx["expect_provider"] == "recaptcha_v3"
        and "google" in [h.lower() for h in fx["opts"].get("ecosystem_hints", [])]
        for fx in pick_fxs
    ), "no Google ecosystem fixture for pick_provider"
    # Axis 4 — default (no hints).
    assert any(
        not fx["opts"].get("region")
        and not fx["opts"].get("ecosystem_hints")
        and "override" not in fx["opts"]
        for fx in pick_fxs
    ), "no default-axis fixture for pick_provider"


def test_pick_provider_precedence_a_over_b_c_fixture_present() -> None:
    """Per AS.3.3 doc: override (axis 1) wins over EVERY other axis,
    and region (axis 2) wins over ecosystem (axis 3). The drift guard
    must include both these multi-axis cases or a future regression
    that swaps the precedence ordering goes undetected."""
    pick_fxs = [fx for fx in BEHAVIOUR_FIXTURES.values() if fx["kind"] == "pick_provider"]
    # Override beats region+ecosystem.
    assert any(
        "override" in fx["opts"]
        and fx["opts"].get("region")
        and fx["opts"].get("ecosystem_hints")
        for fx in pick_fxs
    ), "missing pick_provider precedence fixture: override beats region+ecosystem"
    # Region beats Google ecosystem.
    assert any(
        fx["expect_provider"] == "hcaptcha"
        and fx["opts"].get("region")
        and "google" in [h.lower() for h in fx["opts"].get("ecosystem_hints", [])]
        for fx in pick_fxs
    ), "missing pick_provider precedence fixture: region beats Google ecosystem"


def test_every_phase_exercised_in_classify() -> None:
    """All three live phases (1/2/3) must be exercised against
    classify_outcome — drift in the phase matrix is the AS.0.5 §2 tail
    risk and the most common Python-only edit miss."""
    phases = {
        fx["phase"]
        for fx in BEHAVIOUR_FIXTURES.values()
        if fx["kind"] == "classify_outcome"
    }
    assert phases == {1, 2, 3}, (
        f"phase coverage drift: got {sorted(phases)}, expected {{1, 2, 3}}"
    )


def test_as_3_4_should_reject_covers_both_branches() -> None:
    """AS.3.4 — both branches of the reject predicate must be
    exercised by at least one cross-twin fixture (True for confirmed
    reject outcomes, False for fail-open / bypass outcomes). Drift
    guard's own drift guard so a refactor can't accidentally drop
    coverage of either branch."""
    sr_fxs = [fx for fx in BEHAVIOUR_FIXTURES.values() if fx["kind"] == "should_reject"]
    assert sr_fxs, "no should_reject fixtures present — AS.3.4 parity blind"
    assert any(fx["expect_reject"] is True for fx in sr_fxs), (
        "no should_reject=True fixture (blocked_lowscore / honeypot_fail "
        "branch is unexercised in the cross-twin guard)"
    )
    assert any(fx["expect_reject"] is False for fx in sr_fxs), (
        "no should_reject=False fixture (allow=True branch is unexercised "
        "in the cross-twin guard)"
    )
    # Spot-check the two outcomes that should_reject MUST mark True.
    reject_outcomes = {
        fx["result"]["outcome"]
        for fx in sr_fxs
        if fx["expect_reject"] is True
    }
    assert "blocked_lowscore" in reject_outcomes, (
        "AS.3.4 cross-twin guard missing blocked_lowscore reject fixture"
    )
    assert "jsfail_honeypot_fail" in reject_outcomes, (
        "AS.3.4 cross-twin guard missing jsfail_honeypot_fail reject fixture"
    )
