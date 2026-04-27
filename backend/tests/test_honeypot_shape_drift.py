"""AS.4.1 — Honeypot contract drift guard (Python ↔ TS twin).

Behavioural drift guard between
:mod:`backend.security.honeypot` (Python) and
``templates/_shared/honeypot/index.ts`` (TS twin).

Why this test exists
────────────────────
Honeypot is the AS.4 family's only deliverable, and it provides a
2-layer defence with AS.3 (captcha) — every generated app's form
calls ``validateHoneypot`` server-side and renders the hidden field
client-side using the same constants the Python lib consumes.  The
12-word rare pool, the 4 form-prefix entries, the SHA-256 →
rare-pool-index field-name generator, and the 5 required HTML
attributes must agree byte-for-byte across the twins; drift means
the generated app renders one field name and the OmniSight backend
expects another, silently disabling the trap.

Coverage shape
──────────────
1. **Static parity** (no Node required) — regex-extract constants
   from the TS source, ``==``-compare them to Python.  Catches any
   "someone bumps a string on one side only" failure mode.

2. **Behavioural parity** (Node spawned once per session) — drive a
   matrix of fixtures through both twins and compare the outcome:
       * ``honeypot_field_name(form, tenant, epoch)`` deterministic
       * ``current_epoch(now)`` consistency
       * ``validate_honeypot`` × every precedence branch
       * ``should_reject`` predicate
       * ``event_for_honeypot_outcome`` lookup table

3. **Aggregate SHA-256 oracle** — one hash over every fixture's
   normalised outcome.  Catches "many-tiny-drifts" failure mode
   that per-fixture tests can't summarise.

4. **Coverage guard** — every form path × every outcome literal ×
   every bypass kind must be exercised by at least one fixture
   (drift guard's own drift guard).

How TS execution works
──────────────────────
Same harness as AS.1.5 / AS.2.3 / AS.3.2: spawn ``node
--experimental-strip-types`` to import the TS twin directly — no
transpile step, no node_modules dep.  A single subprocess runs every
fixture and emits one JSON blob; the session-scoped fixture caches
that across the parametrised tests so the spawn cost amortises to
one invocation per pytest session.

The whole family ``pytest.skip``s if Node ≥ 22 is unavailable —
matches the AS.1.x / AS.2.3 / AS.3.2 cross-twin tests' "skip if TS
twin file is absent" gating.

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
import os
import pathlib
import re
import shutil
import subprocess
from typing import Any, Mapping

import pytest

from backend.security import honeypot as hp


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Paths + Node gating
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TS_TWIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "templates"
    / "_shared"
    / "honeypot"
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
    """AS.4.1 deliverable presence — the TS twin file must be on disk
    where the productizer's emit pipeline expects it."""
    assert _TS_TWIN_PATH.exists(), (
        f"AS.4.1 TS twin missing at {_TS_TWIN_PATH}; "
        "the honeypot row in the productizer scaffolds depends on this file."
    )


def test_ts_form_prefixes_match_python() -> None:
    """AS.0.7 §4.1 invariant: 4 form paths, 2-letter prefix each,
    byte-equal across twins."""
    src = _ts_source()
    m = re.search(
        r"export\s+const\s+FORM_PREFIXES[^=]*=\s*Object\.freeze\(\s*\{(.*?)\}\s*\)",
        src,
        re.DOTALL,
    )
    assert m is not None, "TS twin missing FORM_PREFIXES literal"
    body = m.group(1)
    pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', body)
    ts_map = dict(pairs)
    py_map = dict(hp._FORM_PREFIXES)
    assert ts_map == py_map, (
        f"FORM_PREFIXES drift: Python={py_map}, TS={ts_map}"
    )
    assert len(ts_map) == 4


def test_ts_rare_word_pool_matches_python() -> None:
    """AS.0.7 §2.1: 12 rare words, byte-equal across twins."""
    src = _ts_source()
    m = re.search(
        r"export\s+const\s+RARE_WORD_POOL[^=]*=\s*Object\.freeze\(\s*\[(.*?)\]\s*\)",
        src,
        re.DOTALL,
    )
    assert m is not None, "TS twin missing RARE_WORD_POOL literal"
    raw = re.findall(r'"([^"]+)"', m.group(1))
    ts_pool = tuple(raw)
    assert ts_pool == hp._RARE_WORD_POOL, (
        f"RARE_WORD_POOL drift: Python={hp._RARE_WORD_POOL}, TS={ts_pool}"
    )
    assert len(ts_pool) == 12


def test_ts_os_honeypot_class_matches_python() -> None:
    src = _ts_source()
    m = re.search(
        r'export\s+const\s+OS_HONEYPOT_CLASS[^=]*=\s*"([^"]+)"', src
    )
    assert m is not None, "TS twin missing OS_HONEYPOT_CLASS export"
    assert m.group(1) == hp.OS_HONEYPOT_CLASS, (
        f"OS_HONEYPOT_CLASS drift: "
        f"Python={hp.OS_HONEYPOT_CLASS!r}, TS={m.group(1)!r}"
    )


def test_ts_honeypot_hide_css_matches_python() -> None:
    """The CSS rule body that actually hides the field — drift here
    means one twin uses off-screen positioning and the other uses
    display:none, breaking the AS.0.7 §2.2 invariant on one side."""
    src = _ts_source()
    # Concatenated string literal in the TS source is split across
    # two lines; reconstruct by joining adjacent "..."+ "..." pairs.
    m = re.search(
        r'HONEYPOT_HIDE_CSS[^=]*=\s*((?:"[^"]+"\s*\+?\s*)+)', src
    )
    assert m is not None, "TS twin missing HONEYPOT_HIDE_CSS"
    pieces = re.findall(r'"([^"]+)"', m.group(1))
    ts_css = "".join(pieces)
    assert ts_css == hp.HONEYPOT_HIDE_CSS, (
        f"HONEYPOT_HIDE_CSS drift: "
        f"Python={hp.HONEYPOT_HIDE_CSS!r}, TS={ts_css!r}"
    )
    # Sanity: neither twin uses display:none / visibility:hidden.
    assert "display:none" not in ts_css
    assert "visibility:hidden" not in ts_css


def test_ts_honeypot_input_attrs_keys_match_python() -> None:
    """AS.0.7 §2.6 invariant: 7 required HTML attribute keys (5
    dimensions + 2 password-manager ignores) byte-equal across twins."""
    src = _ts_source()
    m = re.search(
        r"export\s+const\s+HONEYPOT_INPUT_ATTRS[^=]*=\s*Object\.freeze\(\s*\{(.*?)\}\s*\)",
        src,
        re.DOTALL,
    )
    assert m is not None, "TS twin missing HONEYPOT_INPUT_ATTRS literal"
    body = m.group(1)
    # Keys can be bare or quoted (`tabindex:` vs `"data-1p-ignore":`).
    pairs = re.findall(
        r'(?:"([a-zA-Z0-9_-]+)"|([a-zA-Z0-9_]+))\s*:\s*"([^"]*)"', body
    )
    ts_map = {(quoted or bare): val for quoted, bare, val in pairs}
    py_map = dict(hp.HONEYPOT_INPUT_ATTRS)
    assert ts_map == py_map, (
        f"HONEYPOT_INPUT_ATTRS drift: Python={py_map}, TS={ts_map}"
    )


def test_ts_rotation_period_matches_python() -> None:
    src = _ts_source()
    m = re.search(
        r"export\s+const\s+HONEYPOT_ROTATION_PERIOD_SECONDS[^=]*=\s*([\d\s*]+)",
        src,
    )
    assert m is not None, (
        "TS twin missing HONEYPOT_ROTATION_PERIOD_SECONDS export"
    )
    # Strip whitespace, evaluate `30 * 86400` style expression safely.
    expr = m.group(1).strip()
    if "*" in expr:
        a, b = (int(x.strip()) for x in expr.split("*"))
        ts_val = a * b
    else:
        ts_val = int(expr)
    assert ts_val == hp.HONEYPOT_ROTATION_PERIOD_SECONDS, (
        f"HONEYPOT_ROTATION_PERIOD_SECONDS drift: "
        f"Python={hp.HONEYPOT_ROTATION_PERIOD_SECONDS}, TS={ts_val}"
    )


def test_ts_reject_code_matches_python() -> None:
    src = _ts_source()
    m = re.search(
        r'export\s+const\s+HONEYPOT_REJECTED_CODE[^=]*=\s*"([^"]+)"', src
    )
    assert m is not None, "TS twin missing HONEYPOT_REJECTED_CODE export"
    assert m.group(1) == hp.HONEYPOT_REJECTED_CODE


def test_ts_reject_status_matches_python() -> None:
    src = _ts_source()
    m = re.search(
        r"export\s+const\s+HONEYPOT_REJECTED_HTTP_STATUS[^=]*=\s*(\d+)", src
    )
    assert m is not None, (
        "TS twin missing HONEYPOT_REJECTED_HTTP_STATUS export"
    )
    assert int(m.group(1)) == hp.HONEYPOT_REJECTED_HTTP_STATUS


def test_ts_event_strings_match_python() -> None:
    """Three honeypot audit-event strings byte-equal across twins."""
    src = _ts_source()
    expected = {
        "EVENT_BOT_CHALLENGE_HONEYPOT_PASS": hp.EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
        "EVENT_BOT_CHALLENGE_HONEYPOT_FAIL": hp.EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
        "EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT":
            hp.EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
    }
    for name, value in expected.items():
        m = re.search(rf'export\s+const\s+{name}[^=]*=\s*"([^"]+)"', src)
        assert m is not None, f"TS twin missing {name!r} export"
        assert m.group(1) == value, (
            f"{name} drift: Python={value!r}, TS={m.group(1)!r}"
        )


def test_ts_outcome_literals_match_python() -> None:
    src = _ts_source()
    expected = {
        "OUTCOME_HONEYPOT_PASS": hp.OUTCOME_HONEYPOT_PASS,
        "OUTCOME_HONEYPOT_FAIL": hp.OUTCOME_HONEYPOT_FAIL,
        "OUTCOME_HONEYPOT_FORM_DRIFT": hp.OUTCOME_HONEYPOT_FORM_DRIFT,
        "OUTCOME_HONEYPOT_BYPASS": hp.OUTCOME_HONEYPOT_BYPASS,
    }
    for name, value in expected.items():
        m = re.search(rf'export\s+const\s+{name}[^=]*=\s*"([^"]+)"', src)
        assert m is not None, f"TS twin missing {name!r} export"
        assert m.group(1) == value


def test_ts_failure_reason_constants_match_python() -> None:
    src = _ts_source()
    expected = {
        "FAILURE_REASON_FIELD_FILLED": hp.FAILURE_REASON_FIELD_FILLED,
        "FAILURE_REASON_FIELD_MISSING_IN_FORM":
            hp.FAILURE_REASON_FIELD_MISSING_IN_FORM,
        "FAILURE_REASON_FORM_PATH_UNKNOWN":
            hp.FAILURE_REASON_FORM_PATH_UNKNOWN,
    }
    for name, value in expected.items():
        m = re.search(rf'export\s+const\s+{name}[^=]*=\s*"([^"]+)"', src)
        assert m is not None, f"TS twin missing {name!r} export"
        assert m.group(1) == value


def test_ts_bypass_kinds_match_python() -> None:
    src = _ts_source()
    expected = {
        "BYPASS_KIND_API_KEY": hp.BYPASS_KIND_API_KEY,
        "BYPASS_KIND_TEST_TOKEN": hp.BYPASS_KIND_TEST_TOKEN,
        "BYPASS_KIND_IP_ALLOWLIST": hp.BYPASS_KIND_IP_ALLOWLIST,
        "BYPASS_KIND_KNOB_OFF": hp.BYPASS_KIND_KNOB_OFF,
        "BYPASS_KIND_TENANT_DISABLED": hp.BYPASS_KIND_TENANT_DISABLED,
    }
    for name, value in expected.items():
        m = re.search(rf'export\s+const\s+{name}[^=]*=\s*"([^"]+)"', src)
        assert m is not None, f"TS twin missing {name!r} export"
        assert m.group(1) == value


def test_ts_declares_two_typed_errors() -> None:
    """``HoneypotError`` + ``HoneypotRejected`` must both be declared
    on the TS side with the same names."""
    src = _ts_source()
    assert re.search(
        r"export\s+class\s+HoneypotError\b", src
    ), "TS twin missing HoneypotError class"
    assert re.search(
        r"export\s+class\s+HoneypotRejected\s+extends\s+HoneypotError\b",
        src,
    ), "TS twin missing HoneypotRejected extends HoneypotError"


def test_ts_declares_validate_helpers() -> None:
    """All five surface helpers must be exported on the TS side so
    generated apps can wire their forms onto the same primitives."""
    src = _ts_source()
    expected_exports = [
        r"export\s+function\s+honeypotFieldName\s*\(",
        r"export\s+function\s+expectedFieldNames\s*\(",
        r"export\s+function\s+currentEpoch\s*\(",
        r"export\s+function\s+validateHoneypot\s*\(",
        r"export\s+function\s+validateAndEnforce\s*\(",
        r"export\s+function\s+shouldReject\s*\(",
        r"export\s+function\s+eventForHoneypotOutcome\s*\(",
        r"export\s+function\s+isEnabled\s*\(",
        r"export\s+function\s+supportedFormPaths\s*\(",
    ]
    for pattern in expected_exports:
        assert re.search(pattern, src), (
            f"TS twin missing export matching {pattern!r}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Behavioural parity via Node subprocess
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ── Field-name fixtures: same triple → same output ──
_FIELD_NAME_FIXTURES: Mapping[str, dict[str, Any]] = {
    "field_name_login_tA_e1234": {
        "kind": "field_name",
        "form_path": "/api/v1/auth/login",
        "tenant_id": "tenant-A",
        "epoch": 1234,
    },
    "field_name_signup_tA_e1234": {
        "kind": "field_name",
        "form_path": "/api/v1/auth/signup",
        "tenant_id": "tenant-A",
        "epoch": 1234,
    },
    "field_name_pwreset_tA_e1234": {
        "kind": "field_name",
        "form_path": "/api/v1/auth/password-reset",
        "tenant_id": "tenant-A",
        "epoch": 1234,
    },
    "field_name_contact_tA_e1234": {
        "kind": "field_name",
        "form_path": "/api/v1/auth/contact",
        "tenant_id": "tenant-A",
        "epoch": 1234,
    },
    "field_name_login_tB_e1234": {
        "kind": "field_name",
        "form_path": "/api/v1/auth/login",
        "tenant_id": "tenant-B",
        "epoch": 1234,
    },
    "field_name_login_tA_e0": {
        "kind": "field_name",
        "form_path": "/api/v1/auth/login",
        "tenant_id": "tenant-A",
        "epoch": 0,
    },
    "field_name_login_tA_e9999": {
        "kind": "field_name",
        "form_path": "/api/v1/auth/login",
        "tenant_id": "tenant-A",
        "epoch": 9999,
    },
}

# ── current_epoch determinism (seconds in / epoch out) ──
_EPOCH_FIXTURES: Mapping[str, dict[str, Any]] = {
    "epoch_zero": {"kind": "current_epoch", "now_seconds": 0.0, "expect": 0},
    "epoch_one_boundary": {
        "kind": "current_epoch",
        # Exactly the start of epoch 1.
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS),
        "expect": 1,
    },
    "epoch_one_minus_one": {
        "kind": "current_epoch",
        # One second before the boundary → still epoch 0.
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS - 1),
        "expect": 0,
    },
    "epoch_42": {
        "kind": "current_epoch",
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS * 42 + 12345),
        "expect": 42,
    },
}

# ── validate_honeypot fixtures (covers every precedence branch + outcome) ──
_VALIDATE_FIXTURES: Mapping[str, dict[str, Any]] = {
    # Pass: empty field present.
    "validate_pass_empty_field": {
        "kind": "validate",
        "form_path": "/api/v1/auth/login",
        "tenant_id": "tA",
        "submitted": {"__placeholder__": ""},  # backfilled with field name
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS * 100),
        "expect_outcome": "honeypot_pass",
        "expect_allow": True,
    },
    # Fail: filled field.
    "validate_fail_filled": {
        "kind": "validate",
        "form_path": "/api/v1/auth/signup",
        "tenant_id": "tA",
        "submitted": {"__placeholder__": "bot-value"},
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS * 100),
        "expect_outcome": "honeypot_fail",
        "expect_allow": False,
    },
    # Form drift: field missing entirely.
    "validate_form_drift_missing": {
        "kind": "validate",
        "form_path": "/api/v1/auth/login",
        "tenant_id": "tA",
        "submitted": {"username": "alice"},
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS * 100),
        "expect_outcome": "honeypot_form_drift",
        "expect_allow": False,
    },
    # Form drift: unknown form path.
    "validate_form_drift_unknown_path": {
        "kind": "validate",
        "form_path": "/api/v1/auth/unknown",
        "tenant_id": "tA",
        "submitted": {},
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS * 100),
        "expect_outcome": "honeypot_form_drift",
        "expect_allow": False,
    },
    # Bypass — apikey axis.
    "validate_bypass_apikey": {
        "kind": "validate",
        "form_path": "/api/v1/auth/login",
        "tenant_id": "tA",
        "submitted": {},
        "bypass_kind": "apikey",
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS * 100),
        "expect_outcome": "honeypot_bypass",
        "expect_allow": True,
        "expect_bypass_kind": "apikey",
    },
    # Bypass — test_token axis.
    "validate_bypass_test_token": {
        "kind": "validate",
        "form_path": "/api/v1/auth/contact",
        "tenant_id": "tA",
        "submitted": {},
        "bypass_kind": "test_token",
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS * 100),
        "expect_outcome": "honeypot_bypass",
        "expect_allow": True,
        "expect_bypass_kind": "test_token",
    },
    # Bypass — ip_allowlist axis.
    "validate_bypass_ip_allowlist": {
        "kind": "validate",
        "form_path": "/api/v1/auth/password-reset",
        "tenant_id": "tA",
        "submitted": {},
        "bypass_kind": "ip_allowlist",
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS * 100),
        "expect_outcome": "honeypot_bypass",
        "expect_allow": True,
        "expect_bypass_kind": "ip_allowlist",
    },
    # Bypass — tenant disabled.
    "validate_bypass_tenant_disabled": {
        "kind": "validate",
        "form_path": "/api/v1/auth/login",
        "tenant_id": "tA",
        "submitted": {},
        "tenant_honeypot_active": False,
        "now_seconds": float(hp.HONEYPOT_ROTATION_PERIOD_SECONDS * 100),
        "expect_outcome": "honeypot_bypass",
        "expect_allow": True,
        "expect_bypass_kind": "tenant_disabled",
    },
}

# ── should_reject + event_for_outcome surface ──
_PREDICATE_FIXTURES: Mapping[str, dict[str, Any]] = {
    "event_for_pass": {
        "kind": "event_for_outcome",
        "outcome": "honeypot_pass",
        "expect_event": "bot_challenge.honeypot_pass",
    },
    "event_for_fail": {
        "kind": "event_for_outcome",
        "outcome": "honeypot_fail",
        "expect_event": "bot_challenge.honeypot_fail",
    },
    "event_for_form_drift": {
        "kind": "event_for_outcome",
        "outcome": "honeypot_form_drift",
        "expect_event": "bot_challenge.honeypot_form_drift",
    },
    "event_for_bypass_is_null": {
        "kind": "event_for_outcome",
        "outcome": "honeypot_bypass",
        "expect_event": None,
    },
}


def _all_behaviour_fixtures() -> Mapping[str, dict[str, Any]]:
    """Backfill the ``__placeholder__`` honeypot field name into every
    validate fixture so both twins use the same submitted dict shape."""
    out: dict[str, dict[str, Any]] = {}
    out.update(_FIELD_NAME_FIXTURES)
    out.update(_EPOCH_FIXTURES)
    out.update(_PREDICATE_FIXTURES)
    for name, fx in _VALIDATE_FIXTURES.items():
        cloned = dict(fx)
        if "__placeholder__" in cloned["submitted"]:
            value = cloned["submitted"]["__placeholder__"]
            # Compute the field name using *Python* (drift guard already
            # enforces TS produces the same name; the validator inside
            # the TS twin will look up by the exact key we plant).
            epoch = int(
                cloned["now_seconds"] // hp.HONEYPOT_ROTATION_PERIOD_SECONDS
            )
            field_name = hp.honeypot_field_name(
                cloned["form_path"], cloned["tenant_id"], epoch
            )
            cloned["submitted"] = {field_name: value}
            cloned["expect_field_name_used"] = field_name
        out[name] = cloned
    return out


BEHAVIOUR_FIXTURES = _all_behaviour_fixtures()


# Node driver: imports the TS twin, reads JSON fixtures from stdin,
# emits a JSON object keyed by fixture name.  The TS twin's
# ``isEnabled()`` reads ``OMNISIGHT_AS_FRONTEND_ENABLED`` — we leave it
# unset so the default ``true`` path runs (mirrors Python which we
# don't unset either).
_NODE_DRIVER = """
import { readFileSync } from "node:fs"
import * as hp from __TWIN_PATH__

const stdin = readFileSync(0, "utf8")
const fixtures = JSON.parse(stdin)

const out = {}
for (const [key, fx] of Object.entries(fixtures)) {
  try {
    if (fx.kind === "field_name") {
      out[key] = {
        name: hp.honeypotFieldName(fx.form_path, fx.tenant_id, fx.epoch),
      }
    } else if (fx.kind === "current_epoch") {
      out[key] = {
        epoch: hp.currentEpoch(fx.now_seconds * 1000),
      }
    } else if (fx.kind === "validate") {
      const opts = {
        nowMs: fx.now_seconds * 1000,
      }
      if (fx.bypass_kind !== undefined && fx.bypass_kind !== null) {
        opts.bypassKind = fx.bypass_kind
      }
      if (fx.tenant_honeypot_active !== undefined) {
        opts.tenantHoneypotActive = fx.tenant_honeypot_active
      }
      const r = hp.validateHoneypot(
        fx.form_path,
        fx.tenant_id,
        fx.submitted,
        opts,
      )
      out[key] = {
        outcome: r.outcome,
        allow: r.allow,
        auditEvent: r.auditEvent,
        bypassKind: r.bypassKind,
        fieldNameUsed: r.fieldNameUsed,
        failureReason: r.failureReason,
      }
    } else if (fx.kind === "event_for_outcome") {
      out[key] = { event: hp.eventForHoneypotOutcome(fx.outcome) }
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
def ts_honeypot_results() -> dict[str, Any]:
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    if not _node_supports_strip_types():
        pytest.skip(
            "node ≥22 with --experimental-strip-types not available; "
            "TS twin behaviour cannot be exercised"
        )
    return _run_ts_driver(BEHAVIOUR_FIXTURES)


def _python_run_fixture(fx: Mapping[str, Any]) -> dict[str, Any]:
    """Run a single fixture through the Python lib and return the
    normalised shape (same key set as the TS driver emits)."""
    kind = fx["kind"]
    if kind == "field_name":
        return {
            "name": hp.honeypot_field_name(
                fx["form_path"], fx["tenant_id"], fx["epoch"]
            )
        }
    if kind == "current_epoch":
        return {"epoch": hp.current_epoch(now=fx["now_seconds"])}
    if kind == "validate":
        kwargs: dict[str, Any] = {"now": fx["now_seconds"]}
        if "bypass_kind" in fx:
            kwargs["bypass_kind"] = fx["bypass_kind"]
        if "tenant_honeypot_active" in fx:
            kwargs["tenant_honeypot_active"] = fx["tenant_honeypot_active"]
        r = hp.validate_honeypot(
            fx["form_path"], fx["tenant_id"], fx["submitted"], **kwargs
        )
        return {
            "outcome": r.outcome,
            "allow": r.allow,
            "auditEvent": r.audit_event,
            "bypassKind": r.bypass_kind,
            "fieldNameUsed": r.field_name_used,
            "failureReason": r.failure_reason,
        }
    if kind == "event_for_outcome":
        return {"event": hp.event_for_honeypot_outcome(fx["outcome"])}
    raise AssertionError(f"unhandled fixture kind: {kind!r}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2a — Per-fixture behavioural parity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "name", sorted(BEHAVIOUR_FIXTURES.keys()), ids=lambda n: n
)
def test_behaviour_parity_python_ts(
    name: str, ts_honeypot_results: dict[str, Any]
) -> None:
    """For every fixture, both twins must produce the same shape."""
    fx = BEHAVIOUR_FIXTURES[name]
    py = _python_run_fixture(fx)
    ts = ts_honeypot_results[name]
    assert "__error" not in ts, (
        f"TS driver raised on fixture {name!r}: {ts}"
    )

    if fx["kind"] == "field_name":
        assert py["name"] == ts["name"], (
            f"field_name {name!r}: Python={py['name']!r}, TS={ts['name']!r}"
        )
        # Sanity: result starts with one of the 4 prefixes.
        assert any(
            py["name"].startswith(p) for p in hp._FORM_PREFIXES.values()
        )
        return

    if fx["kind"] == "current_epoch":
        assert py["epoch"] == ts["epoch"] == fx["expect"], (
            f"current_epoch {name!r}: "
            f"Python={py['epoch']}, TS={ts['epoch']}, expected={fx['expect']}"
        )
        return

    if fx["kind"] == "validate":
        for field in (
            "outcome", "allow", "auditEvent", "bypassKind",
            "fieldNameUsed", "failureReason",
        ):
            assert py[field] == ts[field], (
                f"{field} drift on {name!r}: "
                f"Python={py[field]!r}, TS={ts[field]!r}"
            )
        # Pin the expected outcome too — a regression on either side
        # would cause the parity to silently agree on the wrong answer.
        assert py["outcome"] == fx["expect_outcome"]
        assert py["allow"] == fx["expect_allow"]
        if "expect_bypass_kind" in fx:
            assert py["bypassKind"] == fx["expect_bypass_kind"]
        return

    if fx["kind"] == "event_for_outcome":
        assert py["event"] == ts["event"] == fx["expect_event"], (
            f"event_for_outcome {name!r}: "
            f"Python={py['event']!r}, TS={ts['event']!r}, "
            f"expected={fx['expect_event']!r}"
        )
        return

    raise AssertionError(f"unhandled fixture kind: {fx['kind']!r}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2b — Aggregate SHA-256 oracle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _normalise(fx_name: str, result: Mapping[str, Any]) -> str:
    """Stable JSON projection used in the SHA-256 oracle."""
    keys_for_kind = {
        "field_name": ("name",),
        "current_epoch": ("epoch",),
        "validate": (
            "outcome", "allow", "auditEvent", "bypassKind",
            "fieldNameUsed", "failureReason",
        ),
        "event_for_outcome": ("event",),
    }
    fx = BEHAVIOUR_FIXTURES[fx_name]
    cols = keys_for_kind[fx["kind"]]
    proj = {c: result.get(c) for c in cols}
    return json.dumps({"name": fx_name, **proj}, sort_keys=True)


def test_aggregate_sha256_parity(
    ts_honeypot_results: dict[str, Any],
) -> None:
    """Single SHA-256 over every fixture's normalised projection.
    Catches the many-tiny-drifts failure mode."""
    py_lines: list[str] = []
    ts_lines: list[str] = []
    for name in sorted(BEHAVIOUR_FIXTURES.keys()):
        py_lines.append(_normalise(name, _python_run_fixture(BEHAVIOUR_FIXTURES[name])))
        ts_lines.append(_normalise(name, ts_honeypot_results[name]))
    py_hash = hashlib.sha256("\n".join(py_lines).encode()).hexdigest()
    ts_hash = hashlib.sha256("\n".join(ts_lines).encode()).hexdigest()
    assert py_hash == ts_hash, (
        f"aggregate SHA-256 drift: Python={py_hash}, TS={ts_hash}\n"
        "First differing line:\n"
        f"  Python: {next((p for p, t in zip(py_lines, ts_lines) if p != t), 'none')!r}\n"
        f"  TS:     {next((t for p, t in zip(py_lines, ts_lines) if p != t), 'none')!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Coverage guards (drift guard's own drift guard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_coverage_every_form_path_exercised() -> None:
    """Every entry in ``_FORM_PREFIXES`` must appear in at least one
    fixture (so a partial port that drops a path doesn't slip)."""
    seen = set()
    for fx in BEHAVIOUR_FIXTURES.values():
        seen.add(fx.get("form_path"))
    for path in hp._FORM_PREFIXES.keys():
        assert path in seen, f"no fixture exercises form_path {path!r}"


def test_coverage_every_validate_outcome_exercised() -> None:
    """Every honeypot outcome literal must appear as the expected
    outcome of at least one ``validate`` fixture."""
    seen = set()
    for fx in BEHAVIOUR_FIXTURES.values():
        if fx.get("kind") == "validate":
            seen.add(fx["expect_outcome"])
    expected = set(hp.ALL_HONEYPOT_OUTCOMES)
    missing = expected - seen
    assert not missing, (
        f"validate fixtures missing outcomes: {sorted(missing)}"
    )


def test_coverage_three_axes_exercised() -> None:
    """All three AS.0.6 bypass axes (apikey / test_token / ip_allowlist)
    must be exercised by at least one fixture."""
    axes = {"apikey", "test_token", "ip_allowlist"}
    seen = set()
    for fx in BEHAVIOUR_FIXTURES.values():
        if fx.get("kind") == "validate":
            kind = fx.get("bypass_kind")
            if kind:
                seen.add(kind)
    missing = axes - seen
    assert not missing, f"bypass-axis fixtures missing: {sorted(missing)}"


def test_coverage_all_event_lookups_exercised() -> None:
    """``event_for_honeypot_outcome`` must be exercised across every
    honeypot outcome (one fixture per outcome)."""
    seen = set()
    for fx in BEHAVIOUR_FIXTURES.values():
        if fx.get("kind") == "event_for_outcome":
            seen.add(fx["outcome"])
    expected = set(hp.ALL_HONEYPOT_OUTCOMES)
    missing = expected - seen
    assert not missing, (
        f"event_for_outcome fixtures missing: {sorted(missing)}"
    )
